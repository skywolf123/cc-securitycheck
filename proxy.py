#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-securitycheck —— 本地 DeepSeek 中转（Claude Code / DeepSeek flash + pro）

作用：
  1. 安全分类器请求（auto 模式审批前的 severity 判定调用）改走上游的 OpenAI 兼容端点
     /v1/chat/completions，并带 thinking:{"type":"disabled"} —— 让推理模型不推理、
     直接出 <severity> 结论。分类器只给 64/256 token，若放任推理会烧光预算、
     吐不出结论，导致分类器 fail-closed 拦截所有有副作用的工具；
  2. 其余请求原样透传到 Anthropic 兼容端点，思考强度完全跟随 Claude Code 的 effort 配置。

为什么分类器必须换端点（2026-10-08 实测，见 README「上游协议差异」）：
  上游只在 OpenAI 协议路径上实现了 thinking:{"type":"disabled"}；
  Anthropic 端点 /v1/messages 收到该字段会静默忽略，照常推理。
  实测 reasoning_tokens：OpenAI 端点 [0, 0] vs Anthropic 端点 409~914（与 baseline 无异）。

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
# 可选：统一改写 model 字段（如 "deepseek-v4-flash[1M]"）；留空则透传 CC 的 model。
MODEL_OVERRIDE = os.environ.get("MODEL_OVERRIDE", "").strip()

# 分类器改写（均可在 .env 里置 0/false/off 关闭）：
#   CLASSIFIER_CAP_MAX_TOKENS —— 去掉 max_tokens，让模型推完再出结论，不再被截断；
#   CLASSIFIER_NO_THINK       —— 补 thinking:{"type":"disabled"}，能识别该字段的上游可省推理。
CLASSIFIER_CAP_MAX_TOKENS = os.environ.get("CLASSIFIER_CAP_MAX_TOKENS", "1").strip().lower() \
    not in ("0", "false", "no", "off")
CLASSIFIER_NO_THINK = os.environ.get("CLASSIFIER_NO_THINK", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# ---------------------------------------------------------------------------
# 分类器判定
#
# 依据 2026-10-08 实测的真实请求（见 README「分类器请求的真实形态」）：
#   - system 首句固定： "You are a security monitor for autonomous AI coding agents."
#   - 输出契约固定：   "Your ENTIRE response MUST begin with <block>."
#   - max_tokens 实为 2112（并非早期记载的 64/256），stream 为 false。
#
# 判定只看 system 里这两条独有标记：比 "severity" 之类泛词精确得多
# （本仓库自己的 README 就满是 severity，用泛词会被自身内容误伤）。
#
# 取舍：宁可漏判，不可误判 —— 漏判只让分类器回到 fail-closed（报错显眼、已知）；
# 误判则会把"改写"施加到正常请求上（静默改坏数据）。故取精不取全。
# ---------------------------------------------------------------------------
CLASSIFIER_MARKERS = (
    "you are a security monitor",   # system 首句
    "must begin with <block>",      # 输出契约
)

BUFFER = 8192

# 分类器响应里 verdict 的起点：<block>yes|no</block>。取其及之后的内容回给 CC，
# 丢掉模型在 verdict 前面写的一切（推理/解释）—— 分类器 system 明确要求
# "Your ENTIRE response MUST begin with <block>"，前置内容全是模型没听话的部分。
_VERDICT_RE = re.compile(r"<(severity|block)>.*", re.I | re.S)


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


# ---------------------------------------------------------------------------
# 分类器请求的协议转换：Anthropic /v1/messages ↔ OpenAI /v1/chat/completions
#
# 只服务分类器这一类请求 —— 它是纯文本判定调用，不含工具调用与多模态，
# 因此转换只需搬运 system / messages / max_tokens / temperature。
# ---------------------------------------------------------------------------

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

        # 3. 分类器改写：去掉 max_tokens（不截断）+ 补 thinking disabled（能认的上游省推理）。
        #    仍在 /v1/messages 上发，不换端点、不做协议转换。
        modified = False
        if is_cls:
            self._log_classifier_in(data)
            if CLASSIFIER_CAP_MAX_TOKENS and data.pop("max_tokens", None) is not None:
                modified = True
            if CLASSIFIER_NO_THINK:
                data["thinking"] = {"type": "disabled"}
                modified = True
        if MODEL_OVERRIDE and isinstance(data, dict):
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
            raw = resp.read(int(content_length))
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
                    chunk = resp.read(min(BUFFER, remaining))
                else:
                    chunk = resp.read(BUFFER)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if remaining >= 0:
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端提前断开
        finally:
            conn.close()

    # ---- 分类器专用：Anthropic 请求 → OpenAI 端点 → Anthropic 响应 ----
    def _log_classifier_in(self, data: dict):
        """记录 CC 分类器请求的关键字段 —— 一行，便于确认判定与 CC 是否自带 thinking。"""
        hits = classifier_hits(data)
        th = data.get("thinking")
        th_repr = json.dumps(th, ensure_ascii=False) if th is not None else "<未设置>"
        print("[%s] [CLASSIFIER-IN] model=%s max_tokens=%s stream=%s 命中=%s thinking=%s"
              % (_ts(), data.get("model", "?"), data.get("max_tokens", "?"),
                 data.get("stream", False), "+".join(hits) or "?", th_repr), flush=True)

    # ---- 分类器响应改写：截取 <block> 及之后，丢弃其前面的推理 ----
    def _rewrite_classifier_response(self, raw: bytes) -> dict:
        """对 Anthropic /v1/messages 的非流式响应做"截取 verdict"改写。

        - 在 text block 里找 verdict（<block>… 或旧 <severity>…），
          命中则把该 block 截成"从 verdict 开始"，并丢弃其后的 text block；
        - 找不到 verdict → 原样返回（fail-closed 交给 CC 自己处理）。
        解析失败/非 JSON 也原样返回，保证这条路径永不比透传更糟。
        """
        fail = {"_note": "pass-through(非JSON)"}
        try:
            resp = json.loads(raw.decode("utf-8"))
            if not isinstance(resp, dict):
                return fail
        except (ValueError, UnicodeDecodeError):
            return fail

        content = resp.get("content")
        if not isinstance(content, list):
            resp["_note"] = "pass-through(无content)"
            return resp

        # 统计改写前的内容概况：块类型（thinking 字段即模型没听话、输出了推理）+ 正文字节数。
        kinds = ",".join(b.get("type", "?") for b in content if isinstance(b, dict))
        raw_text = "".join(b.get("text") or "" for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        think_len = sum(len(b.get("thinking") or "") for b in content
                        if isinstance(b, dict) and b.get("type") == "thinking")
        u = resp.get("usage") or {}
        base = "块[%s] 正文%dB think%dB out=%s stop=%s" % (
            kinds, len(raw_text.encode("utf-8")), think_len,
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
            # 找不到 verdict：原样放行，fail-closed 与否交给 CC 自己判
            resp["_note"] = "verdict=MISSING " + base
            return resp

        i, start = cut_at
        # 只保留截断后的这一个 text block —— verdict 之前（含其它块）与之后的内容全丢。
        # 分类器响应就一个 verdict， CC 拿到"以 <block> 开头"的干净文本。
        resp["content"] = [dict(content[i], text=content[i]["text"][start:])]
        kept = len(resp["content"][0]["text"].encode("utf-8"))
        print("[%s] [CLASSIFIER-TRIM] verdict 位于第 %d 块，丢 %d 字前缀，保留 %dB：%s"
              % (_ts(), i, len(raw_text) - len(resp["content"][0]["text"]), kept, base),
              flush=True)
        resp["_note"] = "trimmed " + base
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
    print("  模型覆盖 : %s" % (MODEL_OVERRIDE or "(透传，跟随 Claude Code 配置)"), flush=True)
    steps = []
    if CLASSIFIER_CAP_MAX_TOKENS:
        steps.append("去掉 max_tokens")
    if CLASSIFIER_NO_THINK:
        steps.append("think:disabled")
    print("  分类器   : %s（仍在 Anthropic 端点）" % (" + ".join(steps) or "原样透传"), flush=True)
    print("  其他请求 : 原样透传 %s（思考强度跟随 CC 内部 effort 配置）" % UPSTREAM, flush=True)
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
