#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-securitycheck —— 本地 DeepSeek 中转（Claude Code / DeepSeek flash + pro）

作用：
  1. 把 Claude Code 的全部请求转发到 DeepSeek 的 Anthropic 兼容端点；
  2. 安全分类器请求（auto 模式审批前的 severity 判定调用）强制 thinking disabled，
     避免 DeepSeek 推理模型把 64/256 token 预算全烧在 thinking 上、吐不出 <severity> 结论，
     导致分类器 fail-closed 拦截所有有副作用的工具；
  3. 其余请求原样透传，思考强度完全跟随 Claude Code 内部的 effort 配置。

依赖：仅 Python 标准库，无任何第三方包。
配置：全部走环境变量（均有默认值），也可写在同目录 .env 里（真实环境变量优先）。
      见 README.md、start.bat 与 .env.example。
"""

import http.client
import json
import os
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

# 分类器判定阈值：Claude Code auto 模式分类器请求的 max_tokens 为 64/256。
CLASSIFIER_MAX_TOKENS = 256
BUFFER = 8192


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


def is_classifier(body: dict) -> bool:
    """判定是否为 auto 模式安全分类器调用。

    依据（2026-08-03 实验证实）：
      - 分类器请求 body.max_tokens 为 64/256（远小于主模型调用，主模型用 4096+）；
      - 或 system 提示里要求输出 <severity>N</severity>。
    """
    if body.get("max_tokens") is not None:
        try:
            if int(body["max_tokens"]) <= CLASSIFIER_MAX_TOKENS:
                return True
        except (TypeError, ValueError):
            pass
    return "severity" in _system_text(body.get("system", "")).lower()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 通用转发（所有方法） ----
    def _forward(self):
        # 1. 读请求体
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        # 2. 改写（仅 /v1/messages 的 JSON 请求）
        is_cls = False
        modified = False
        path_no_query = self.path.split("?", 1)[0]
        if path_no_query.rstrip("/").endswith("/v1/messages"):
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    is_cls = is_classifier(data)
                    if is_cls:
                        # 分类器不推理：DeepSeek 兼容层默认开 thinking 且吃 max_tokens 预算，
                        # 64/256 token 全烧在 thinking 上就永远吐不出 verdict。
                        data["thinking"] = {"type": "disabled"}
                        modified = True
                    if MODEL_OVERRIDE:
                        data["model"] = MODEL_OVERRIDE
                        modified = True
                    if modified:
                        body = json.dumps(data).encode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass  # 非 JSON 或解码失败 → 原样透传

        # 3. 构造上游请求
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

        # 4. 回传响应头
        status = resp.status
        out_headers = {k: v for k, v in resp.getheaders()
                       if k.lower() not in ("transfer-encoding", "connection")}
        content_length = resp.getheader("Content-Length")

        tag = "CLASSIFIER->NO_THINK" if is_cls else "PASSTHROUGH"
        extra = " model=%s" % MODEL_OVERRIDE if MODEL_OVERRIDE else ""
        print("[%s] %s %s -> %s%s" % (tag, self.command, path_no_query, status, extra), flush=True)

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

        # 5. 逐块流式回传
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
    print("  分类器   : 强制 thinking disabled（不推理，直接出 verdict）", flush=True)
    print("  其他请求 : 原样透传（思考强度跟随 CC 内部 effort 配置）", flush=True)
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
