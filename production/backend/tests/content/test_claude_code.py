"""Claude Code transport contract: loopback-only fake upstream, never paid calls."""
from __future__ import annotations

import json
import io
import os
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.message import Message
from pathlib import Path

import pytest

from app.content import tasks
from app.content.providers import claude_code


MODEL = "claude-sonnet-5-5"
KEY = "sk-fake-cli-test-secret"


def message(text='{"scripts": []}', *, stop="end_turn"):
    return {"id": "msg_local_1", "type": "message", "role": "assistant", "model": MODEL,
            "content": [{"type": "text", "text": text}], "stop_reason": stop,
            "usage": {"input_tokens": 12, "cache_creation_input_tokens": 5,
                      "cache_read_input_tokens": 8, "output_tokens": 17}}


@pytest.fixture
def upstream():
    state = {"calls": [], "status": 200, "body": json.dumps(message()).encode(),
             "headers": {"Content-Type": "application/json", "request-id": "req_fake_1"}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["calls"].append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
            self.send_response(state["status"])
            for key, val in state["headers"].items():
                self.send_header(key, val)
            if not state.get("omit_length"):
                self.send_header("Content-Length", str(state.get("declared_length", len(state["body"]))))
            self.end_headers()
            if state.get("body_wait"):
                state["body_wait"].wait(timeout=2)
            try:
                self.wfile.write(state["body"])
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    if state.get("body_wait"):
        state["body_wait"].set()
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    executable = tmp_path / "claude.exe"
    executable.write_bytes(b"fake-process-test-placeholder")
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_EXECUTABLE", str(executable))
    state = {"requests": 1, "returncode": 0, "popen": [], "payload_overrides": {}, "no_request": False}

    class FakeProcess:
        def __init__(self, args, **kwargs):
            self.args, self.kwargs, self.returncode = args, kwargs, None
            state["popen"].append(self)
            self.killed = False

        def communicate(self, input=None, timeout=None):
            state["stdin"] = input
            if state.get("timeout"):
                if not self.killed:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                self.returncode = -1
                return b"", b""
            env = self.kwargs["env"]
            if not state["no_request"]:
                for _ in range(state["requests"]):
                    cli_model = self.args[self.args.index("--model") + 1]
                    payload = {"model": cli_model.removesuffix("[1m]"),
                               "max_tokens": int(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"]),
                               "tools": [], "messages": [{"role": "user", "content": input.decode()}],
                               "system": self.args[self.args.index("--append-system-prompt") + 1], "stream": False}
                    payload.update(state["payload_overrides"])
                    request = urllib.request.Request(env["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true",
                        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                        "Authorization": "Bearer " + env["ANTHROPIC_AUTH_TOKEN"],
                        "User-Agent": "claude-cli/2.1.185 (external, cli)", "x-app": "cli"})
                    try:
                        with urllib.request.urlopen(request, timeout=3) as response:
                            state.setdefault("responses", []).append((response.status, response.read()))
                    except urllib.error.HTTPError as exc:
                        state.setdefault("responses", []).append((exc.code, exc.read()))
            self.returncode = state["returncode"]
            return state.get("stdout", b"{}"), state.get("stderr", b"")

        def kill(self):
            self.killed = True

    monkeypatch.setattr(claude_code.subprocess, "Popen", FakeProcess)
    return state


def call(upstream, **kwargs):
    return claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL, system="只输出 JSON",
                           user="用户参考", max_tokens=128, timeout=10, **kwargs)


def test_real_client_headers_and_response_are_preserved(upstream, cli):
    result = call(upstream)
    assert result == {"text": '{"scripts": []}', "model": MODEL, "finish_reason": "stop",
                      "usage": {"input_tokens": 25, "output_tokens": 17},
                      "provider_request_id": "req_fake_1"}
    sent = upstream["calls"][0]
    assert sent["path"] == "/v1/messages?beta=true"
    assert sent["headers"]["User-Agent"] == "claude-cli/2.1.185 (external, cli)"
    assert sent["headers"]["Authorization"] == "Bearer " + KEY
    assert {k.lower(): v for k, v in sent["headers"].items()}["x-app"] == "cli"
    assert sent["body"]["model"] == MODEL


def test_cli_internal_retries_cannot_send_another_paid_request(upstream, cli):
    cli["requests"] = 3
    result = call(upstream)
    assert len(upstream["calls"]) == 1 and result["usage"]["output_tokens"] == 17
    assert [r[0] for r in cli["responses"]] == [200, 400, 400]


def test_cli_has_isolated_configuration_no_tools_and_stdin_prompt(upstream, cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-global-key-do-not-leak")
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "sk-other-provider-do-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    call(upstream)
    proc = cli["popen"][0]
    args, env = proc.args, proc.kwargs["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == KEY and "ANTHROPIC_API_KEY" not in env
    assert "AVATAR_STUDIO_QWEN_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    for flag in ("--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == MODEL
    assert "--system-prompt" not in args
    assert args[args.index("--append-system-prompt") + 1] == "只输出 JSON"
    assert cli["stdin"] == "用户参考".encode()
    assert KEY not in str(args)
    assert not Path(proc.kwargs["cwd"]).exists()
    assert not Path(env["CLAUDE_CONFIG_DIR"]).exists()


def test_context_suffix_changes_only_native_cli_configuration(upstream, cli, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "true")
    result = call(upstream)
    args = cli["popen"][0].args
    assert args[args.index("--model") + 1] == MODEL + "[1m]"
    assert upstream["calls"][0]["body"]["model"] == MODEL and result["model"] == MODEL


def test_context_configuration_can_be_frozen_before_environment_changes(upstream, cli, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "false")
    config = claude_code.preflight(base_url=upstream["url"], api_key=KEY, model=MODEL,
        max_tokens=128, timeout=10)
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "true")
    call(upstream, context_1m=config["context_1m"])
    args = cli["popen"][0].args
    assert args[args.index("--model") + 1] == MODEL


def test_invalid_context_setting_rejected_before_send(upstream, cli, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "sometimes")
    with pytest.raises(tasks.NotSent):
        call(upstream)
    assert not upstream["calls"] and not cli["popen"]


@pytest.mark.parametrize("overrides", [{"model": "claude-haiku-4-5"}, {"max_tokens": 129},
                                      {"tools": [{"name": "Read"}]}, {"max_tokens": 0}])
def test_gate_blocks_wrong_model_budget_or_tools_before_upstream(upstream, cli, overrides):
    cli["payload_overrides"] = overrides
    with pytest.raises(tasks.NotSent):
        call(upstream)
    assert not upstream["calls"]


@pytest.mark.parametrize("status,charged,code", [(401, False, "AUTH_FAILED"),
    (429, False, "RATE_LIMITED"), (502, None, "UPSTREAM_ACCESS_FORBIDDEN")])
def test_definite_provider_rejection_is_kept_even_when_cli_exits_nonzero(upstream, cli, status, charged, code):
    upstream["status"] = status
    upstream["body"] = json.dumps({"error": {"type": "upstream_error", "message":
        "Upstream access forbidden, please contact administrator " + KEY}}).encode()
    cli["returncode"] = 1
    cli["stderr"] = ("sensitive " + KEY).encode()
    with pytest.raises(tasks.ProviderRefused) as caught:
        call(upstream)
    assert caught.value.status == status and caught.value.charged is charged and caught.value.code == code
    assert KEY not in str(caught.value)


def test_received_http_refusal_survives_incomplete_error_body(upstream, cli):
    upstream["status"] = 401
    upstream["body"] = b'{"error":'
    upstream["declared_length"] = 200
    with pytest.raises(tasks.ProviderRefused) as caught:
        claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL,
            system="JSON", user="ref", max_tokens=128, timeout=0.2)
    assert caught.value.status == 401 and caught.value.charged is False
    assert len(upstream["calls"]) == 1


def test_full_received_message_settles_despite_cli_error(upstream, cli):
    cli["returncode"] = 1
    assert call(upstream)["usage"] == {"input_tokens": 25, "output_tokens": 17}


def test_base_url_with_v1_is_not_doubled(upstream, cli):
    result = claude_code.call(base_url=upstream["url"] + "/v1", api_key=KEY, model=MODEL,
        system="JSON", user="ref", max_tokens=128, timeout=10)
    assert result["model"] == MODEL
    assert upstream["calls"][0]["path"] == "/v1/messages?beta=true"


def test_truncated_output_flag_is_preserved(upstream, cli):
    upstream["body"] = json.dumps(message("cut", stop="max_tokens")).encode()
    assert call(upstream)["finish_reason"] == "length"


def test_sse_merges_text_stop_and_cached_usage(upstream, cli):
    parts = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_sse", "model": MODEL,
            "content": [], "usage": {"input_tokens": 7, "output_tokens": 1,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 9}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": '{"ok":true}'}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 13}}), ("message_stop", {"type": "message_stop"})]
    upstream["body"] = "".join(f"event: {name}\ndata: {json.dumps(obj)}\n\n" for name, obj in parts).encode()
    upstream["headers"]["Content-Type"] = "text/event-stream"
    result = call(upstream)
    assert result["text"] == '{"ok":true}' and result["finish_reason"] == "stop"
    assert result["usage"] == {"input_tokens": 19, "output_tokens": 13}


def test_incomplete_sse_is_unknown_and_is_not_fabricated_success(upstream, cli):
    upstream["body"] = b'event: message_start\ndata: {"type":"message_start","message":{"id":"x"}}\n\n'
    upstream["headers"]["Content-Type"] = "text/event-stream"
    with pytest.raises(urllib.error.URLError):
        call(upstream)
    assert len(upstream["calls"]) == 1


def test_sse_message_stop_is_complete_before_upstream_socket_closes(upstream, cli):
    start = {"type": "message_start", "message": message()}
    end = {"type": "message_stop"}
    upstream["body"] = (f"event: message_start\ndata: {json.dumps(start)}\n\n"
                        f"event: message_stop\ndata: {json.dumps(end)}\n\n").encode()
    upstream["headers"]["Content-Type"] = "text/event-stream"
    upstream["omit_length"] = True
    result = claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL,
        system="JSON", user="ref", max_tokens=128, timeout=0.5)
    assert result["model"] == MODEL and result["usage"]["output_tokens"] == 17


def test_upstream_redirect_is_not_followed(upstream, cli):
    upstream["status"] = 307
    upstream["headers"]["Location"] = "https://example.invalid/paid-messages"
    with pytest.raises(tasks.ProviderRefused) as caught:
        call(upstream)
    assert caught.value.status == 307 and caught.value.charged is None
    assert len(upstream["calls"]) == 1


def test_no_remote_request_and_cli_failure_releases_unsent_reservation(upstream, cli):
    cli["no_request"], cli["returncode"] = True, 1
    cli["stderr"] = ("oops " + KEY).encode()
    with pytest.raises(tasks.NotSent) as caught:
        call(upstream)
    assert KEY not in str(caught.value)
    assert not upstream["calls"]


def test_timeout_before_remote_request_is_not_sent(upstream, cli):
    cli["timeout"] = True
    with pytest.raises(tasks.NotSent):
        call(upstream)
    assert cli["popen"][0].killed


def test_timeout_after_remote_request_is_unknown(upstream, cli):
    upstream["body_wait"] = threading.Event()
    with pytest.raises(TimeoutError):
        claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL,
            system="JSON", user="ref", max_tokens=128, timeout=0.2)
    assert len(upstream["calls"]) == 1


def test_header_delivery_failure_does_not_lose_received_usage(upstream):
    payload = json.dumps({"model": MODEL, "max_tokens": 128, "tools": [],
                          "messages": [{"role": "user", "content": "ref"}]}).encode()

    class DisconnectedCli:
        path = "/v1/messages"
        rfile = io.BytesIO(payload)
        wfile = io.BytesIO()
        headers = Message()
        headers["Content-Length"] = str(len(payload))
        headers["Authorization"] = "Bearer " + KEY

        def send_response(self, _status):
            raise BrokenPipeError("CLI disconnected")

    gate = claude_code._OnceGate({"endpoint": upstream["url"] + "/v1/messages",
        "model": MODEL, "max_tokens": 128, "timeout": 2}, KEY)
    try:
        gate.forward(DisconnectedCli())
        assert gate.result["usage"] == {"input_tokens": 25, "output_tokens": 17}
        assert gate.error is None and len(upstream["calls"]) == 1
    finally:
        gate.close()


def test_missing_executable_and_invalid_base_url_rejected_preflight(tmp_path):
    with pytest.raises(tasks.NotSent):
        claude_code.preflight(base_url="https://relay.invalid", api_key=KEY, model=MODEL,
                              max_tokens=128, timeout=10, executable=str(tmp_path / "no-such-claude.exe"))
    with pytest.raises(tasks.NotSent):
        claude_code.preflight(base_url="https://secret:pwd@relay.invalid", api_key=KEY, model=MODEL,
                              max_tokens=128, timeout=10, executable=str(Path(__file__)))


@pytest.mark.skipif(not Path("D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe").is_file(),
                    reason="Native Claude Code not installed on this test host")
def test_native_cli_reaches_only_fake_upstream_with_exact_model_and_budget(upstream):
    """Real installed process, no mocked SDK or fabricated client identity."""
    result = claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL, system="只输出 JSON",
        user="返回一个对象", max_tokens=128, timeout=20,
        executable="D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    assert result["model"] == MODEL and result["usage"] == {"input_tokens": 25, "output_tokens": 17}
    assert len(upstream["calls"]) == 1
    request = upstream["calls"][0]
    assert request["body"]["model"] == MODEL and request["body"]["max_tokens"] <= 128
    assert not request["body"].get("tools")
    system = "\n".join(b.get("text", "") for b in request["body"]["system"])
    assert "Claude Code" in system and "software engineering tasks" in system
    assert "只输出 JSON" in system
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["user-agent"].startswith("claude-cli/") and headers["x-app"] == "cli"
    assert headers["authorization"] == "Bearer " + KEY


@pytest.mark.skipif(not Path("D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe").is_file(),
                    reason="Native Claude Code not installed on this test host")
def test_native_cli_cannot_retry_paid_request_after_503(upstream):
    upstream["status"] = 503
    upstream["body"] = b'{"type":"error","error":{"type":"overloaded_error","message":"local fixture"}}'
    with pytest.raises(tasks.ProviderRefused) as caught:
        claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL, system="JSON", user="ref",
            max_tokens=128, timeout=10,
            executable="D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    assert caught.value.status == 503 and caught.value.charged is None
    assert len(upstream["calls"]) == 1


@pytest.mark.skipif(not Path("D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe").is_file(),
                    reason="Native Claude Code not installed on this test host")
def test_native_1m_option_keeps_base_model_and_emits_real_context_beta(upstream, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CLAUDE_CODE_CONTEXT_1M", "true")
    result = claude_code.call(base_url=upstream["url"], api_key=KEY, model=MODEL,
        system="JSON", user="ref", max_tokens=128, timeout=20,
        executable="D:/NodeJS/node_global/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    assert result["model"] == MODEL and len(upstream["calls"]) == 1
    request = upstream["calls"][0]
    assert request["body"]["model"] == MODEL and request["body"]["max_tokens"] <= 128
    assert not request["body"].get("tools")
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert "context-1m-2025-08-07" in headers["anthropic-beta"]
    assert headers["user-agent"].startswith("claude-cli/")
