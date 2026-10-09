#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-securitycheck —— 本地中转，让 Claude Code 在非原生模型上也能用好 auto mode

作用：
  1. 分类器请求（auto 模式审批副作用工具前的那次独立调用）：去掉 max_tokens
     （不限制输出长度、消除截断）、补 thinking:{"type":"disabled"}（能识别的上游省掉推理）；
  2. 分类器响应：截取 <block> 及之后的内容，丢掉模型写在 verdict 前的推理，
     让 CC 拿到"以 <block> 开头"的干净文本；
  3. 其余请求原样透传，思考强度完全跟随 Claude Code 的 effort 配置。

三步全部作用在 Anthropic /v1/messages 上 —— 不换端点、不做协议转换，
因此不依赖上游对 OpenAI 协议的实现，也不随上游模型映射变化而失效。
分类器 system 要求 "Your ENTIRE response MUST begin with <block>"，
非原生推理模型会先写推理，输出再被截断就永远吐不出 verdict，
CC 解析不到即 fail-closed，拦下所有有副作用的工具。

依赖：仅 Python 标准库，无任何第三方包。
配置：全部走环境变量（均有默认值），也可写在同目录 .env 里（真实环境变量优先）。
      见 README.md、start.bat 与 .env.example。
"""

import http.client
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def _load_dotenv(filename=".env") -> None:
    """零依赖 .env 加载器（无第三方包）。

    - 只认 KEY=VALUE，跳过空行与 # 注释；值两侧的引号会被剥掉。
    - 已存在的真实环境变量优先（os.environ.setdefault），便于临时覆盖调试。
    - 路径取自脚本所在目录，不依赖 cwd —— start.bat 用 %~dp0 启动，cwd 不定。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                os.environ.setdefault(key, val)
    except OSError as e:
        print("[warn] 读取 %s 失败，忽略：%s" % (path, e), flush=True)


_load_dotenv()  # 必须在下面读配置之前调用

# ---------------------------------------------------------------------------
# 配置（环境变量，均可缺省）
# ---------------------------------------------------------------------------
HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROXY_PORT", "8008"))
UPSTREAM = os.environ.get("UPSTREAM", "https://api.deepseek.com/anthropic")
# 可选：仅分类器改写 model 字段（如 "deepseek-chat"、"glm-4-flash" 等轻量模型）；
# 留空则透传 CC 的 model。主请求永远透传，不受此项影响。
MODEL_OVERRIDE = os.environ.get("MODEL_OVERRIDE", "").strip()

# 可选：仅分类器覆盖思考强度（thinking 字段）。取值（大小写不敏感）：
#   disabled / off / none —— {"type":"disabled"}，完全关闭思考；
#   low / medium / high  —— {"type":"enabled","budget_tokens":...}，降档但不关闭；
#   空                    —— 不覆盖（交由 CLASSIFIER_NO_THINK 决定是否补 disabled）。
# 有些模型不认 disabled、无法关闭思考，可用这个降档减少推理。
THINKING_OVERRIDE = os.environ.get("THINKING_OVERRIDE", "").strip()
_THINK_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}

# 分类器改写（均可在 .env 里置 0/false/off 关闭）：
#   CLASSIFIER_CAP_MAX_TOKENS —— 去掉 max_tokens，让模型推完再出结论，不再被截断；
#   CLASSIFIER_NO_THINK       —— 补 thinking:{"type":"disabled"}，能识别该字段的上游可省推理。
# 注意：THINKING_OVERRIDE 非空时优先，CLASSIFIER_NO_THINK 不再生效。
CLASSIFIER_CAP_MAX_TOKENS = os.environ.get("CLASSIFIER_CAP_MAX_TOKENS", "1").strip().lower() \
    not in ("0", "false", "no", "off")
CLASSIFIER_NO_THINK = os.environ.get("CLASSIFIER_NO_THINK", "1").strip().lower() \
    not in ("0", "false", "no", "off")
# 排障用（两个独立开关，日常都关）：
#   CLASSIFIER_DUMP_REQ —— 打印 CC 分类器请求全文；
#   CLASSIFIER_DUMP_RAW —— 打印上游分类器响应全文（改写前、原样）。
def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off")


CLASSIFIER_DUMP_REQ = _env_bool("CLASSIFIER_DUMP_REQ")
CLASSIFIER_DUMP_RAW = _env_bool("CLASSIFIER_DUMP_RAW")


def build_thinking_override():
    """把 THINKING_OVERRIDE 解析成 thinking 对象；未配置返回 None。"""
    v = THINKING_OVERRIDE.lower()
    if not v:
        return None
    if v in ("disabled", "off", "none"):
        return {"type": "disabled"}
    if v in _THINK_BUDGETS:
        return {"type": "enabled", "budget_tokens": _THINK_BUDGETS[v]}
    # 也接受裸数字，直接当 budget_tokens（上限给足，避免被上游截断）
    if v.isdigit():
        return {"type": "enabled", "budget_tokens": int(v)}
    print("[warn] THINKING_OVERRIDE=%r 无法识别（可用 disabled/low/medium/high 或数字），忽略"
          % THINKING_OVERRIDE, flush=True)
    return None


THINKING_OVERRIDE_VALUE = build_thinking_override()

# ---------------------------------------------------------------------------
# 分类器判定
#
# 依据 2026-10-08 实测的真实请求：
#   - system 首句： "You are a security monitor for autonomous AI coding agents."
#   - 输出契约：   "Your ENTIRE response MUST begin with <block>."
#   - 请求体： max_tokens=2112，stream=false，thinking 未设置。
#
# 只看 system 里这两条独有标记 —— 不用 max_tokens 阈值（真实分类器为 2112，
# 阈值判定不会触发却会误伤正常的小请求），也不用 "severity" 之类泛词
# （项目自身的 CLAUDE.md 里就可能出现，会把正常请求误判成分类器）。
#
# 取舍：宁可漏判，不可误判 —— 漏判只让分类器回到 fail-closed（报错显眼、已知）；
# 误判则会把"改写"施加到正常请求上（静默改坏数据）。故取精不取全。
# ---------------------------------------------------------------------------
CLASSIFIER_MARKERS = (
    "you are a security monitor",   # system 首句
    "must begin with <block>",      # 输出契约
)

BUFFER = 8192

# 分类器响应里 verdict 的起点：system 要求输出 <block>…</block>，
# 取 <block> 及之后的内容回给 CC，丢掉模型在它前面写的一切（推理/解释）。
_VERDICT_RE = re.compile(r"<block>.*", re.I | re.S)


def _ts() -> str:
    """日志时间戳（本地时区，秒级 + 毫秒）。"""
    t = time.time()
    return "%s.%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                        int((t % 1) * 1000))


def _system_text(system) -> str:
    """把 messages 请求的 system 字段（str 或 content-block 数组）拼成文本。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for item in system:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, list):
                    for c in t:
                        if isinstance(c, dict) and isinstance(c.get("text"), str):
                            parts.append(c["text"])
        return "\n".join(parts)
    return ""


def classifier_hits(body: dict) -> list:
    """返回命中的分类器标记列表（空列表 = 非分类器）。供判定与日志共用。"""
    sys_text = _system_text(body.get("system", "")).lower()
    return [m for m in CLASSIFIER_MARKERS if m in sys_text]


def is_classifier(body: dict) -> bool:
    """判定是否为 auto 模式安全分类器调用 —— 见上方 CLASSIFIER_MARKERS 的实测依据。"""
    return bool(classifier_hits(body))


def _to_text(content) -> str:
    """Anthropic content（str 或 content-block 数组）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(item.get("content"), str):  # tool_result 之类
                    parts.append(item["content"])
        return "\n".join(parts)
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    LOG_PREVIEW = 240   # 日志里 verdict 文本预览的最大字符数（分类器输出通常远小于此）

    # ---- 把响应文本压成单行预览，超长截断（tail=True 取尾部）----
    def _preview(self, text: str, tail: bool = False) -> str:
        """仅用于日志：去掉首尾空白、换行转义，太长则截断，避免刷屏。"""
        s = (text or "").strip().replace("\r", "").replace("\n", "\\n")
        limit = self.LOG_PREVIEW
        if len(s) <= limit:
            return s
        return ("..." + s[-limit:]) if tail else (s[:limit] + "...")

    def _log_classifier_req(self, data: dict):
        """CLASSIFIER_DUMP_REQ=1 时：把分类器请求体**全文**原样打到日志，供排障。"""
        try:
            shown = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            shown = repr(data)
        print("[%s] [CLASSIFIER-REQ] %dB\n%s" % (_ts(), len(shown), shown), flush=True)

    def _log_classifier_raw(self, raw: bytes):
        """CLASSIFIER_DUMP_RAW=1 时：把分类器响应**改写前**的全文原样打到日志，供排障。"""
        try:
            shown = raw.decode("utf-8")
        except UnicodeDecodeError:
            shown = repr(raw)
        print("[%s] [CLASSIFIER-RAW] %dB\n%s" % (_ts(), len(raw), shown), flush=True)

    def _verdict_repr(self, text: str) -> str:
        """日志用的 verdict 文本表示：完整打印，仅在超过 LOG_PREVIEW 时截断（带数量提示）。

        不 strip —— verdict 前后的空白/换行往往是 CC 解析失败的主因，必须让它现形。
        """
        s = text.replace("\r", "").replace("\n", "\\n")
        limit = self.LOG_PREVIEW
        if len(s) <= limit:
            return repr(s)
        return "%r...（等共%d字，仅示前%d字）" % (s[:limit], len(s), limit)

    # ---- 通用转发（所有方法） ----
    def _forward(self):
        # 1. 读请求体
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        # 2. 解析 + 判定是否分类器（仅 /v1/messages 的 JSON 请求）
        data = None
        is_cls = False
        path_no_query = self.path.split("?", 1)[0]
        is_messages = path_no_query.rstrip("/").endswith("/v1/messages")
        if is_messages:
            try:
                parsed_req = json.loads(body.decode("utf-8"))
                if isinstance(parsed_req, dict):
                    data = parsed_req
                    is_cls = is_classifier(data)
            except (ValueError, UnicodeDecodeError):
                pass  # 非 JSON 或解码失败 → 原样透传

        # 3. 分类器改写：去掉 max_tokens（不截断）+ 补 thinking disabled（能认的上游省推理）+ 可选覆盖分类器模型。
        #    仅作用于分类器请求，主模型请求完全原样透传。
        modified = False
        if is_cls:
            self._log_classifier_in(data)
            if CLASSIFIER_DUMP_REQ:
                self._log_classifier_req(data)
            if CLASSIFIER_CAP_MAX_TOKENS and data.pop("max_tokens", None) is not None:
                modified = True
            if THINKING_OVERRIDE_VALUE is not None:
                data["thinking"] = THINKING_OVERRIDE_VALUE
                modified = True
            elif CLASSIFIER_NO_THINK:
                data["thinking"] = {"type": "disabled"}
                modified = True
            if MODEL_OVERRIDE:
                data["model"] = MODEL_OVERRIDE
                modified = True
        if modified:
            body = json.dumps(data).encode("utf-8")

        # 4. 构造上游请求
        up = urlparse(UPSTREAM)
        is_https = up.scheme == "https"
        upstream_host = up.hostname
        upstream_port = up.port or (443 if is_https else 80)
        upstream_path = up.path.rstrip("/") + self.path

        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection",
                                     "transfer-encoding", "accept-encoding")}
        hdrs["Host"] = upstream_host

        Conn = http.client.HTTPSConnection if is_https else http.client.HTTPConnection
        conn = Conn(upstream_host, upstream_port, timeout=300)
        try:
            conn.request(self.command, upstream_path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except Exception as e:  # 上游不可达 → 可读的 502
            msg = json.dumps({"type": "proxy_error",
                              "message": "cc-securitycheck 无法连接上游 %s: %s" % (UPSTREAM, e)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        # 5. 回传响应头。分类器且非流式 → 先整包读下、剥掉 verdict 前的推理再回。
        status = resp.status
        content_length = resp.getheader("Content-Length")

        if is_cls and content_length is not None:
            try:
                raw = resp.read(int(content_length))
            except http.client.IncompleteRead as e:   # 上游中途断流
                raw = e.partial
            conn.close()
            payload = self._rewrite_classifier_response(raw)
            note = payload.pop("_note", "")   # _note 只进日志，不回给 CC
            print("[%s] [CLASSIFIER-OUT] -> %s %s" % (_ts(), status, note), flush=True)
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.close_connection = False
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        out_headers = {k: v for k, v in resp.getheaders()
                       if k.lower() not in ("transfer-encoding", "connection")}

        self.send_response(status)
        if content_length is not None:
            out_headers["Content-Length"] = content_length
            self.close_connection = False  # 长度已知，可保活
        else:
            # 流式/未知长度 → 关连接表示正文结束（CC 的客户端按 EOF 读 SSE）
            self.close_connection = True
        for k, v in out_headers.items():
            self.send_header(k, v)
        self.end_headers()

        # 6. 逐块流式回传
        try:
            remaining = int(content_length) if content_length is not None else -1
            while True:
                if remaining >= 0:
                    if remaining <= 0:
                        break
                    n = min(BUFFER, remaining)
                else:
                    n = BUFFER
                try:
                    chunk = resp.read(n)
                except http.client.IncompleteRead as e:   # 上游中途断流
                    chunk = e.partial                # 已读到的部分先发给 CC
                except (ConnectionResetError, OSError):
                    break                            # 读取出错 → 停止，交给 finally 关连接
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break                            # 客户端提前断开 → 停止
                if remaining >= 0:
                    remaining -= len(chunk)
        finally:
            conn.close()

    # ---- 分类器专用：记录请求关键字段（改写与截取在 _forward 里完成）----
    def _log_classifier_in(self, data: dict):
        """记录 CC 分类器请求的关键字段 —— 一行，便于确认判定与 CC 原始的思考强度。"""
        hits = classifier_hits(data)
        th = data.get("thinking")
        # 显示 CC 原始请求的思考强度：未带 thinking 时回落到 effort（CC 的思考档位，透传不改）。
        if th is not None:
            shown = json.dumps(th, ensure_ascii=False)
        elif data.get("effort") is not None:
            shown = "effort=%s（无 thinking）" % data.get("effort")
        else:
            shown = "<未设置>"
        print("[%s] [CLASSIFIER-IN] model=%s max_tokens=%s stream=%s 命中=%s thinking=%s"
              % (_ts(), data.get("model", "?"), data.get("max_tokens", "?"),
                 data.get("stream", False), "+".join(hits) or "?", shown), flush=True)

    # ---- 分类器响应改写：截取 <block> 及之后，丢弃其前面的推理 ----
    def _rewrite_classifier_response(self, raw: bytes) -> dict:
        """对 Anthropic /v1/messages 的非流式响应做"截取 verdict"改写。

        - 在 content 里找 <block>，命中则只保留"从 <block> 开始"的那一个 text block，
          其前的所有内容（推理、解释）与其后的块全部丢弃；
        - 找不到 <block> → 原样返回，让 CC 自己按 fail-closed 处理；
        - 解析失败/非 JSON 也原样返回 —— 这条路径永不比透传更糟。
        """
        fail = {"_note": "pass-through(非JSON)"}
        try:
            resp = json.loads(raw.decode("utf-8"))
            if not isinstance(resp, dict):
                return fail
        except (ValueError, UnicodeDecodeError):
            return fail

        if CLASSIFIER_DUMP_RAW:
            self._log_classifier_raw(raw)

        content = resp.get("content")
        if not isinstance(content, list):
            resp["_note"] = "pass-through(无content)"
            return resp

        # 统计改写前的内容概况：块类型 + 各类内容长度，便于核对 usage 里 token 的去向。
        #   text     —— verdict 所在（会截取）
        #   thinking —— 模型没听话、输出了推理（会丢弃）
        #   other    —— 其它未知块，取 JSON 序列化长度；out 远大于 text+think 时看这里
        kinds = []
        raw_text = ""
        think_len = 0
        other_len = 0
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type", "?")
            kinds.append(t)
            if t == "text":
                raw_text += b.get("text") or ""
            elif t == "thinking":
                think_len += len(b.get("thinking") or "")
            else:
                other_len += len(json.dumps(b, ensure_ascii=False).encode("utf-8"))
        u = resp.get("usage") or {}
        base = "块[%s] 正文%dB think%dB other%dB out=%s stop=%s" % (
            ",".join(kinds), len(raw_text.encode("utf-8")), think_len, other_len,
            u.get("output_tokens"), resp.get("stop_reason"))

        cut_at = None          # (block 序号, 匹配起点)
        for i, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            m = _VERDICT_RE.search(block.get("text") or "")
            if m:
                cut_at = (i, m.start())
                break
        if cut_at is None:
            # 找不到 verdict：原样放行，fail-closed 与否交给 CC 自己判。
            # 此时回给 CC 的就是全文，打印摘要 + 尾部预览（尾部即截断发生处，最有用）。
            resp["_note"] = "verdict=MISSING %s output=%r" % (
                base, self._preview(raw_text, tail=True))
            return resp

        i, start = cut_at
        # 只保留截断后的这一个 text block —— verdict 之前（含其它块）与之后的内容全丢。
        # 分类器响应就一个 verdict， CC 拿到"以 <block> 开头"的干净文本。
        trimmed_text = content[i]["text"][start:]
        resp["content"] = [dict(content[i], text=trimmed_text)]
        # 丢失的“字符”里既有丢失的块（thinking / other），也含被丢的前缀；只丢块也要报，
        # 否则 verdict 不完整时（如少了 </block>）日志会静默看不出任何异常。
        # other 块无法直接按字符算，用序列化字节数近似（仅用于日志提示，不参与改写）。
        before_chars = len(raw_text) + think_len + other_len
        dropped_chars = before_chars - len(trimmed_text)
        resp["_note"] = "trimmed %s verdict=%s (块%d丢%d字)" % (
            base, self._verdict_repr(trimmed_text), i, dropped_chars)
        return resp

    def log_message(self, *args):
        pass  # 关闭 http.server 自带访问日志，避免刷屏

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _forward


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 58, flush=True)
    print("cc-securitycheck 本地 DeepSeek 中转已启动", flush=True)
    print("  监听     : http://%s:%s" % (HOST, PORT), flush=True)
    print("  上游     : %s" % UPSTREAM, flush=True)
    steps = []
    if CLASSIFIER_CAP_MAX_TOKENS:
        steps.append("去掉 max_tokens")
    if THINKING_OVERRIDE_VALUE is not None:
        steps.append("思考改写->%s" % json.dumps(THINKING_OVERRIDE_VALUE, ensure_ascii=False))
    elif CLASSIFIER_NO_THINK:
        steps.append("think:disabled")
    if MODEL_OVERRIDE:
        steps.append("模型改写->%s" % MODEL_OVERRIDE)
    print("  分类器   : %s（仅作用于分类器）" % (" + ".join(steps) or "原样透传"), flush=True)
    if CLASSIFIER_DUMP_REQ or CLASSIFIER_DUMP_RAW:
        print("  排障打印 : %s" % " + ".join(
            n for n, on in (("请求全文", CLASSIFIER_DUMP_REQ),
                            ("响应全文", CLASSIFIER_DUMP_RAW)) if on), flush=True)
    print("  其他请求 : 原样透传 %s（思考强度与模型跟随 CC 配置）" % UPSTREAM, flush=True)
    print("  CC 配置  : ANTHROPIC_BASE_URL=http://%s:%s" % (HOST, PORT), flush=True)
    print("=" * 58, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
