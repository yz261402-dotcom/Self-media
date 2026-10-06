"""真实 Claude Code 文本客户端；一次本地 gate 最多放行一次远端 Messages。

CLI 内部的重试、工具、其他模型均不能再次出站。凭据仅在隔离的子进程
环境和内存中使用；原始 CLI 输出、请求头、提示词不写日志或文件。
本模块没有业务任务创建、费用确认或自动重试，调用者仍使用 tasks.run_call。
"""
from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import tasks
from . import anthropic

PROTOCOL = "claude-code-cli"
_WINDOWS_NATIVE = "D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


def preflight(*, base_url: str, api_key: str, model: str, max_tokens: int,
              timeout: float, executable: str | None = None,
              context_1m: bool | None = None) -> dict[str, Any]:
    """无网络预检；确定尚未发送的问题统一 NotSent，绝不回显凭据。"""
    try:
        if context_1m is None:
            value = os.environ.get("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "false").strip().lower()
            if value not in ("true", "false", "1", "0"):
                raise ValueError
            context_1m = value in ("true", "1")
        elif not isinstance(context_1m, bool):
            raise ValueError
        parsed = urllib.parse.urlsplit(base_url)
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "\r" in base_url or "\n" in base_url
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))):
            raise ValueError
        # Resolve the port now: malformed URLs must fail before entering sending.
        _ = parsed.port
        if (not api_key or any(c in api_key for c in "\r\n\0") or not model
                or any(c in model for c in "\r\n\0[]") or isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int) or max_tokens < 1
                or not math.isfinite(float(timeout)) or float(timeout) <= 0):
            raise ValueError
    except (ValueError, TypeError):
        raise tasks.NotSent("Claude Code 写稿配置无效，尚未发送请求", "PROVIDER_CONFIG_INVALID") from None
    location = (executable or os.environ.get("AVATAR_STUDIO_CLAUDE_CODE_EXECUTABLE") or
                shutil.which("claude.exe" if os.name == "nt" else "claude") or
                (_WINDOWS_NATIVE if os.name == "nt" else ""))
    path = Path(location) if location else None
    if not path or not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        raise tasks.NotSent("本机未找到真实 Claude Code 可执行文件，请配置其路径", "CLAUDE_CODE_UNAVAILABLE")
    if os.name == "nt" and path.suffix.lower() != ".exe":
        raise tasks.NotSent("Claude Code 必须配置原生可执行文件，不能通过 shell 启动", "PROVIDER_CONFIG_INVALID")
    return {"executable": str(path.resolve()), "endpoint": anthropic.endpoint(base_url),
            "model": model, "max_tokens": max_tokens, "timeout": float(timeout), "context_1m": context_1m}


def _environment(*, config_dir: str, local_url: str, api_key: str, max_tokens: int) -> dict[str, str]:
    # Whitelist OS runtime variables; never copy other provider keys or global CLI routing.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
               "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
               "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL"}
    env = {key: val for key, val in os.environ.items() if key.upper() in allowed}
    env.update({"ANTHROPIC_AUTH_TOKEN": api_key, "ANTHROPIC_BASE_URL": local_url,
                "CLAUDE_CONFIG_DIR": config_dir, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1"})
    return env


def _parse_complete(raw: bytes, *, content_type: str, request_id: str) -> dict[str, Any]:
    """解析完整 Messages / SSE；没有 message_stop 的流绝不当成完成。"""
    if "text/event-stream" not in content_type.lower():
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ValueError
            result = anthropic.parse_response(raw)
        except Exception:
            raise urllib.error.URLError("Claude Code 上游响应结构未确认") from None
    else:
        data, blocks, completed = {}, {}, False
        text_data: list[str] = []

        def consume(lines: list[str]) -> None:
            nonlocal data, completed
            if not lines:
                return
            event = json.loads("\n".join(lines))
            kind = event.get("type")
            if kind == "message_start":
                data = dict(event["message"])
            elif kind == "content_block_start":
                blocks[event["index"]] = dict(event["content_block"])
            elif kind == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    block = blocks.setdefault(event["index"], {"type": "text", "text": ""})
                    block["text"] = block.get("text", "") + delta["text"]
            elif kind == "message_delta":
                data.update(event.get("delta") or {})
                data.setdefault("usage", {}).update(event.get("usage") or {})
            elif kind == "message_stop":
                completed = True
            elif kind == "error":
                raise ValueError

        try:
            for line in raw.decode("utf-8").splitlines():
                if not line:
                    consume(text_data)
                    text_data = []
                elif line.startswith("data:"):
                    text_data.append(line[5:].lstrip())
            consume(text_data)
            if not completed or not data:
                raise ValueError
            data["content"] = [blocks[key] for key in sorted(blocks)]
            result = anthropic.parse_response(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except Exception:
            raise urllib.error.URLError("Claude Code 上游流式响应未完整结束") from None
    result["provider_request_id"] = request_id or result.get("provider_request_id", "")
    return result


def _refusal(status: int, raw: bytes) -> tasks.ProviderRefused:
    # Import at call time to avoid the text adapter import cycle.
    from .text import _error_kind
    code, _tag = _error_kind(status, raw)
    if 300 <= status < 400:
        return tasks.ProviderRefused("Claude Code 上游返回重定向，未继续转发；费用尚待核对",
                                     "PROVIDER_REDIRECT_REFUSED", status, charged=None)
    message = f"Claude Code 写稿服务返回 HTTP {status}"
    if code == "UPSTREAM_ACCESS_FORBIDDEN":
        message += "，上游访问被拒绝，请联系中转管理员恢复线路；费用尚待核对"
    elif code == "PROVIDER_CLIENT_RESTRICTED":
        message += "，中转分组拒绝了本机真实 Claude Code 客户端"
    return tasks.ProviderRefused(message, code, status, charged=False if status < 500 else None)


class _OnceGate:
    """Loopback bridge preserving native client headers, with a single paid-request permit."""

    def __init__(self, config: dict[str, Any], api_key: str):
        self.config, self.api_key = config, api_key
        self.deadline = time.monotonic() + config["timeout"]
        self.lock = threading.Lock()
        self.sent, self.closed = False, False
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.connection: http.client.HTTPConnection | None = None
        self.finished = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def local_error(self, status=400):
                raw = b'{"type":"error","error":{"type":"invalid_request_error","message":"Local request gate refused this request"}}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_GET(self):
                self.local_error(404)

            def do_POST(self):
                owner.forward(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.local_url = f"http://127.0.0.1:{self.server.server_port}"

    def forward(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            target = urllib.parse.urlsplit(handler.path)
            size = int(handler.headers.get("Content-Length", "0"))
            if target.path != "/v1/messages" or target.scheme or target.netloc or not (0 < size <= _MAX_REQUEST_BYTES):
                raise ValueError
            raw = handler.rfile.read(size)
            payload = json.loads(raw.decode("utf-8"))
            budget = payload.get("max_tokens")
            if (payload.get("model") != self.config["model"] or isinstance(budget, bool)
                    or not isinstance(budget, int) or not (0 < budget <= self.config["max_tokens"])
                    or payload.get("tools") or payload.get("tool_choice") not in (None, {"type": "none"})
                    or not isinstance(payload.get("messages"), list)):
                raise ValueError
            if any(not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
                   for item in payload["messages"]):
                raise ValueError
            auth = handler.headers.get("Authorization")
            x_key = handler.headers.get("x-api-key")
            if not (auth == "Bearer " + self.api_key or x_key == self.api_key):
                raise ValueError
            if ((auth is not None and auth != "Bearer " + self.api_key)
                    or (x_key is not None and x_key != self.api_key)):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError):
            handler.local_error()
            return
        with self.lock:
            if self.sent or self.closed or time.monotonic() >= self.deadline:
                handler.local_error()
                return
            # Consumed before connection: failed/uncertain network sends are never retried.
            self.sent = True
        response_status, chunks = None, []
        try:
            destination = urllib.parse.urlsplit(self.config["endpoint"])
            connection_class = http.client.HTTPSConnection if destination.scheme == "https" else http.client.HTTPConnection
            connection = connection_class(destination.hostname, destination.port,
                timeout=max(0.01, self.deadline - time.monotonic()))
            with self.lock:
                self.connection = connection
                if self.closed:
                    raise TimeoutError("Claude Code 请求等待已结束")
            headers = {key: val for key, val in handler.headers.items() if key.lower() not in _HOP_HEADERS}
            # Keep real Claude Code identification; only normalize the known credential.
            headers["Authorization"] = "Bearer " + self.api_key
            headers["Accept-Encoding"] = "identity"
            path = destination.path + ("?" + target.query if target.query else "")
            connection.request("POST", path, raw, headers)
            response = connection.getresponse()
            response_status = response.status
            request_id = response.getheader("request-id") or response.getheader("x-request-id") or ""
            content_type = response.getheader("Content-Type") or ""
            cli_open = True
            try:
                handler.send_response(response.status)
                for key, val in response.getheaders():
                    if key.lower() not in _HOP_HEADERS:
                        handler.send_header(key, val)
                handler.send_header("Connection", "close")
                handler.end_headers()
            except OSError:
                # The provider response can still settle when the CLI wrapper disconnects.
                cli_open = False
            total, terminal_tail = 0, b""
            streaming = "text/event-stream" in content_type.lower()
            while True:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Claude Code 上游响应超时")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise urllib.error.URLError("Claude Code 上游响应超过接收范围，结果未确认")
                chunks.append(chunk)
                if cli_open:
                    try:
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
                    except OSError:
                        cli_open = False
                if streaming and response.status < 300:
                    # message_stop settles a Messages stream even if the HTTP connection
                    # remains open. Account from the actual stream, not the CLI envelope.
                    terminal_window = terminal_tail + chunk
                    if b"message_stop" in terminal_window:
                        try:
                            self.result = _parse_complete(b"".join(chunks), content_type=content_type,
                                                          request_id=request_id)
                        except urllib.error.URLError:
                            pass  # The marker may be inside text or split before complete JSON.
                        if self.result is not None:
                            break
                    terminal_tail = terminal_window[-128:]
            # read1 alone does not enforce Content-Length, so verify completeness explicitly.
            content_length = response.getheader("Content-Length")
            if self.result is None and content_length is not None and total != int(content_length):
                raise urllib.error.URLError("Claude Code 上游响应未完整接收")
            response_raw = b"".join(chunks)
            if response.status >= 300:
                self.error = _refusal(response.status, response_raw)
            elif self.result is None:
                self.result = _parse_complete(response_raw, content_type=content_type, request_id=request_id)
        except TimeoutError:
            self.error = (_refusal(response_status, b"".join(chunks)) if response_status and response_status >= 300
                          else TimeoutError("Claude Code 上游响应超时，原调用结果尚未确认"))
        except Exception:
            # Exceptions may contain URL, headers or credentials; do not retain their text.
            self.error = (_refusal(response_status, b"".join(chunks)) if response_status and response_status >= 300
                          else urllib.error.URLError("Claude Code 上游通信结果尚未确认"))
        finally:
            if self.connection:
                self.connection.close()
            self.finished.set()
            handler.close_connection = True

    def close(self) -> None:
        with self.lock:
            self.closed = True
            connection = self.connection
        if connection:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        if self.sent:
            self.finished.wait(timeout=1)


def call(*, base_url: str, api_key: str, model: str, system: str, user: str,
         max_tokens: int, timeout: float, executable: str | None = None,
         context_1m: bool | None = None) -> dict[str, Any]:
    """一次真实 CLI 调用，确定已收响应优先于 CLI envelope/退出码。"""
    config = preflight(base_url=base_url, api_key=api_key, model=model, max_tokens=max_tokens,
                       timeout=timeout, executable=executable, context_1m=context_1m)
    process, timed_out = None, False
    # Native Windows subprocess helpers may briefly retain cwd after CLI exit.
    # Cleanup must never discard an already received response/usage or refusal.
    with tempfile.TemporaryDirectory(prefix="avatar-content-claude-", ignore_cleanup_errors=True) as directory:
        cwd, config_dir = Path(directory) / "work", Path(directory) / "config"
        cwd.mkdir()
        config_dir.mkdir()
        gate = _OnceGate(config, api_key)
        # This is the native client's context option; the gate still requires the exact
        # base model in the actual HTTP body and never manufactures beta headers.
        cli_model = model + "[1m]" if config["context_1m"] else model
        args = [config["executable"], "--print", "--output-format", "json", "--model", cli_model,
                # Retain the real client's built-in system and append writing instructions.
                "--append-system-prompt", system, "--setting-sources", "", "--safe-mode", "--settings",
                '{"disableAllHooks":true}',
                "--tools", "", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--no-session-persistence", "--no-chrome", "--permission-mode", "dontAsk"]
        env = _environment(config_dir=str(config_dir), local_url=gate.local_url,
                           api_key=api_key, max_tokens=max_tokens)
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(cwd), env=env, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            process.communicate(input=user.encode("utf-8"), timeout=max(0.01, gate.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            if process:
                process.kill()
                try:
                    process.communicate(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        except OSError:
            # Classify only after gate closure; no handler can start a remote send afterward.
            pass
        finally:
            if gate.sent and not timed_out and not gate.finished.is_set():
                # The CLI may exit just before the HTTP handler publishes its completed
                # response. Do not cut that response off while time is still available.
                gate.finished.wait(timeout=max(0, gate.deadline - time.monotonic()))
            gate.close()
        if gate.result is not None:
            return gate.result
        if isinstance(gate.error, tasks.ProviderRefused):
            raise gate.error
        if not gate.sent:
            raise tasks.NotSent("Claude Code 未发出模型请求，请检查本机客户端配置", "CLAUDE_CODE_NOT_SENT")
        if timed_out or isinstance(gate.error, TimeoutError):
            raise TimeoutError("Claude Code 写稿响应超时，结果和费用尚未确认")
        raise urllib.error.URLError("Claude Code 写稿请求已发送，结果和费用尚未确认")
