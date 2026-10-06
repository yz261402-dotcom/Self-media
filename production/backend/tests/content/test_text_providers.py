"""角色文本客户端：协议请求、错误映射、台账一致。全部使用 transport stub，不发任何网络请求。"""
import json
from uuid import uuid4

import pytest

from app import storage as st
from app.content import tasks
from app.content.errors import ContentBlocked, ContentRejected
from app.content.providers import text


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "10000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "10000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_API_KEY", "sk-test-anthropic")
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "sk-test-qwen")
    monkeypatch.setenv("AVATAR_STUDIO_FX_USD_CNY", "7.2")
    for k in ("PLANNER", "WRITER", "REPAIR", "REVIEWER"):
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{k}_PROVIDER", raising=False)
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{k}_MODEL", raising=False)


def parent():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {}, "version": 1, "content_revision": 0})
    return tasks.submit_job(wid, "generation", "g" + uuid4().hex[:6], {})


class Stub:
    def __init__(self, status=200, body=None):
        self.calls, self.status, self.body = [], status, body

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        if isinstance(self.body, Exception):
            raise self.body
        return self.status, self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


def claude_ok(text_='{"a": 1}', model="claude-sonnet-5-5", stop="end_turn", usage=True):
    d = {"id": "msg_1", "model": model, "content": [{"type": "text", "text": text_}], "stop_reason": stop}
    if usage:
        d["usage"] = {"input_tokens": 8000, "output_tokens": 2500}
    return d


def qwen_ok(text_='{"a": 1}', model="qwen-plus", finish="stop", usage=True):
    d = {"id": "chat_1", "model": model, "choices": [{"message": {"content": text_}, "finish_reason": finish}]}
    if usage:
        d["usage"] = {"prompt_tokens": 16000, "completion_tokens": 2800}
    return d


def test_default_role_routing():
    assert (text.profile("planner")["provider"], text.profile("planner")["model"]) == ("qwen", "qwen-plus")
    assert (text.profile("reviewer")["provider"], text.profile("reviewer")["model"]) == ("qwen", "qwen-plus")
    assert text.profile("writer")["model"] == "claude-sonnet-5-5"
    assert text.profile("repair")["model"] == "claude-sonnet-5-5"


def test_repair_follows_writer_override(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "qwen")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_MODEL", "qwen3.7-plus")
    assert text.profile("repair")["provider"] == "qwen"


def test_sonnet_messages_protocol_and_ledger():
    stub, p = Stub(body=claude_ok()), parent()
    r = text.call_text("writer", parent=p, step="write", system="sys", user="usr", transport=stub)
    url, headers, body = stub.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-test-anthropic" and headers["anthropic-version"]
    assert body["model"] == "claude-sonnet-5-5" and body["messages"] == [{"role": "user", "content": "usr"}]
    assert "temperature" not in body and "response_format" not in body
    assert r["json"] == {"a": 1} and r["usage"] == {"input_tokens": 8000, "output_tokens": 2500}
    assert r["cost_fen"] == 30
    assert len(tasks.jobs_calls(p["id"])) == len(stub.calls) == 1


def test_qwen_protocol():
    stub = Stub(body=qwen_ok())
    r = text.call_text("planner", parent=parent(), step="plan", system="sys", user="usr", transport=stub)
    url, headers, body = stub.calls[0]
    assert url.endswith("/chat/completions") and headers["authorization"] == "Bearer sk-test-qwen"
    assert body["model"] == "qwen-plus" and body["response_format"] == {"type": "json_object"}
    assert r["cost_fen"] == 6


def test_unsupported_param_rejected_before_any_call():
    stub = Stub(body=claude_ok())
    with pytest.raises(ContentRejected) as e:
        text.call_text("writer", parent=parent(), step="w", system="s", user="u", transport=stub,
                       extra={"temperature": 0.9})
    assert e.value.code == "PARAM_UNSUPPORTED" and not stub.calls


def test_missing_credential_blocks_without_call(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_ANTHROPIC_API_KEY")
    stub = Stub(body=claude_ok())
    with pytest.raises(ContentBlocked) as e:
        text.call_text("writer", parent=parent(), step="w", system="s", user="u", transport=stub)
    assert e.value.code == "PROVIDER_NOT_CONFIGURED" and not stub.calls


@pytest.mark.parametrize("status,code", [(401, "AUTH_FAILED"), (403, "AUTH_FAILED"), (429, "RATE_LIMITED")])
def test_http_errors_map_and_release_reservation(status, code):
    from app.content import costs
    stub, p = Stub(status=status, body={"error": {"type": "boom", "message": "SECRET-ECHO sk-test-anthropic"}}), parent()
    with pytest.raises(ContentRejected) as e:
        text.call_text("writer", parent=p, step="w", system="s", user="u", transport=stub)
    assert e.value.code == code
    assert "SECRET-ECHO" not in e.value.message and "sk-test" not in e.value.message
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 0


def test_timeout_is_unknown_and_not_resent():
    stub, p = Stub(body=TimeoutError("t")), parent()
    with pytest.raises(tasks.CallPending):
        text.call_text("writer", parent=p, step="w", system="s", user="u", transport=stub)
    with pytest.raises(tasks.CallPending):
        text.call_text("writer", parent=p, step="w", system="s", user="u", transport=stub)
    assert len(stub.calls) == 1


def test_truncated_output_fails_without_repair_call():
    stub, p = Stub(body=claude_ok('{"a": ', stop="max_tokens")), parent()
    with pytest.raises(ContentRejected) as e:
        text.call_text("writer", parent=p, step="w", system="s", user="u", transport=stub)
    assert e.value.code == "OUTPUT_TRUNCATED" and len(stub.calls) == 1


def test_invalid_json_and_non_object_rejected():
    for body in ("not json", "[1,2]"):
        with pytest.raises(ContentRejected) as e:
            text.call_text("writer", parent=parent(), step="w", system="s", user="u",
                           transport=Stub(body=claude_ok(body)))
        assert e.value.code == "MODEL_OUTPUT_INVALID"


def test_missing_usage_keeps_reservation_held():
    p = parent()
    r = text.call_text("planner", parent=p, step="plan", system="s", user="u", transport=Stub(body=qwen_ok(usage=False)))
    assert r["usage"] is None and r["cost_fen"] is None
    assert tasks.jobs_calls(p["id"])[0]["settlement_state"] == "held"


def test_model_mismatch_not_accepted_as_same_name():
    with pytest.raises(ContentRejected) as e:
        text.call_text("writer", parent=parent(), step="w", system="s", user="u",
                       transport=Stub(body=claude_ok(model="claude-haiku-4-5")))
    assert e.value.code == "MODEL_MISMATCH"


def test_fenced_json_is_accepted():
    r = text.call_text("planner", parent=parent(), step="plan", system="s", user="u",
                       transport=Stub(body=qwen_ok('```json\n{"a": 2}\n```')))
    assert r["json"] == {"a": 2}


def test_usd_without_fx_blocks_before_call(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_FX_USD_CNY")
    stub = Stub(body=claude_ok())
    with pytest.raises(ContentBlocked):
        text.call_text("writer", parent=parent(), step="w", system="s", user="u", transport=stub)
    assert not stub.calls


def test_oversized_input_rejected_not_truncated():
    stub = Stub(body=claude_ok())
    with pytest.raises(ContentRejected) as e:
        text.call_text("writer", parent=parent(), step="w", system="s", user="字" * 30000, transport=stub)
    assert e.value.code == "INPUT_TOO_LARGE" and not stub.calls


def test_capabilities_page_has_no_secret():
    dump = json.dumps(text.capabilities(), ensure_ascii=False)
    assert "sk-test" not in dump and "claude-sonnet-5" in dump


def test_relay_upstream_access_denied_is_distinct_and_keeps_unknown_fees():
    p = parent()
    stub = Stub(status=502, body={"error": {"type": "upstream_error",
        "message": "Upstream access forbidden, please contact administrator sk-test-anthropic"}})
    with pytest.raises(ContentRejected) as exc:
        text.call_text("writer", parent=p, step="write", system="s", user="u", transport=stub)
    assert exc.value.code == "UPSTREAM_ACCESS_FORBIDDEN"
    assert "上游访问被拒绝" in exc.value.message and "中转管理员" in exc.value.message
    assert "claude-sonnet-5-5" in exc.value.message and "sk-test" not in exc.value.message
    call = tasks.jobs_calls(p["id"])[0]
    assert call["state"] == "failed" and call["settlement_state"] == "held"
    assert call["actual_cost_fen"] is None and call["attempts"] == 1
    with pytest.raises(ContentRejected):
        text.call_text("writer", parent=p, step="write", system="s", user="u", transport=stub)
    assert len(stub.calls) == 1


def test_other_upstream_errors_are_not_invented_as_access_denial():
    code, tag = text._error_kind(502, b'{"error":{"type":"upstream_error","message":"temporarily unavailable"}}')
    assert code == "PROVIDER_ERROR" and tag == "upstream_error"
    code, tag = text._error_kind(400, b'{"error":{"type":"invalid_request","message":"Upstream access forbidden"}}')
    assert code == "PROVIDER_ERROR" and tag == "invalid_request"


def test_default_transport_identifies_real_application_to_relay(monkeypatch):
    requests = []
    class Response:
        status = 200
        def read(self): return b'{}'
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def open_request(req, timeout):
        requests.append(req)
        return Response()
    monkeypatch.setattr(text.urllib.request, "urlopen", open_request)
    assert text._default_transport("https://relay.example/v1/messages", {"x-api-key": "test-private"}, b'{}', 30) == (200, b'{}')
    assert requests[0].get_header("User-agent") == "AvatarStudio/0.1"
    assert requests[0].get_header("X-api-key") == "test-private"


def test_claude_code_only_group_is_explained_without_retry_or_releasing_fees():
    p = parent()
    stub = Stub(status=503, body={"error": {"type": "api_error", "message":
        "No available accounts: this group only allows Claude Code clients"}})
    with pytest.raises(ContentRejected) as exc:
        text.call_text("writer", parent=p, step="write", system="s", user="u", transport=stub)
    assert exc.value.code == "PROVIDER_CLIENT_RESTRICTED"
    assert "分组" in exc.value.message and "Claude Code" in exc.value.message and "通用 API" in exc.value.message
    call = tasks.jobs_calls(p["id"])[0]
    assert call["state"] == "failed" and call["settlement_state"] == "held"
    assert call["actual_cost_fen"] is None and len(stub.calls) == 1
