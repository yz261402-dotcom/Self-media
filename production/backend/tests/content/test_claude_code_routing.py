"""Claude Code 只替代写作运输层，计费与审核仍走既有内容服务。"""
import json

import pytest

from app import storage as st
from app.content import tasks
from app.content.errors import ContentBlocked, ContentRejected
from app.content.providers import text, claude_code
from tests.content.test_text_providers import env, parent  # noqa: F401


@pytest.fixture(autouse=True)
def cli_settings(monkeypatch, env):
    for role in text.ROLES:
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{role.upper()}_EXECUTION_MODE", raising=False)
        monkeypatch.delenv(f"AVATAR_STUDIO_CONTENT_{role.upper()}_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_EXECUTION_MODE", "claude_code")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "anthropic")
    monkeypatch.setattr(text, "_default_transport", lambda *a: pytest.fail("HTTP调用不得离开测试"))


def native_result(**changed):
    return {"text": '{"ok":true}', "model": "claude-sonnet-5-5", "finish_reason": "stop",
            "usage": {"input_tokens": 120, "output_tokens": 6}, "provider_request_id": "msg_native", **changed}


def test_writer_and_repair_follow_cli_mode_but_planner_reviewer_keep_http():
    for role in ("writer", "repair"):
        p = text.profile(role)
        assert p["execution_mode"] == "claude_code" and p["timeout_seconds"] == 600
    for role in ("planner", "reviewer"):
        assert text.profile(role)["execution_mode"] == "http_api"


def test_cli_calls_once_and_settles_existing_ledger(monkeypatch):
    sent, checked = [], []
    monkeypatch.setattr(claude_code, "preflight", lambda **kw: checked.append(kw))
    monkeypatch.setattr(claude_code, "call", lambda **kw: sent.append(kw) or native_result())
    monkeypatch.setattr(text, "_default_transport", lambda *a: pytest.fail("CLI路线不走HTTP运输层"))
    p = parent()
    result = text.call_text("writer", parent=p, step="write", system="sys", user="usr")
    assert len(sent) == 1 and checked
    assert sent[0]["model"] == "claude-sonnet-5-5" and sent[0]["timeout"] == 600
    assert sent[0]["max_tokens"] == 6144
    assert result["json"] == {"ok": True} and result["provider_request_id"] == "msg_native"
    call = tasks.jobs_calls(p["id"])[0]
    assert call["state"] == "succeeded" and call["settlement_state"] == "settled" and call["attempts"] == 1
    again = text.call_text("writer", parent=p, step="write", system="sys", user="usr")
    assert again["job_id"] == result["job_id"] and len(sent) == 1


def test_missing_cli_is_blocked_before_any_fee_reservation(monkeypatch):
    def missing(**kw):
        raise tasks.NotSent("缺少Claude Code", "CLAUDE_CODE_NOT_INSTALLED")
    monkeypatch.setattr(claude_code, "preflight", missing)
    p = parent()
    with pytest.raises(ContentBlocked) as exc:
        text.call_text("writer", parent=p, step="write", system="s", user="u")
    assert exc.value.code == "CLAUDE_CODE_NOT_INSTALLED"
    assert tasks.jobs_calls(p["id"]) == []


def test_cli_timeout_keeps_unknown_once_and_lease_covers_wait(monkeypatch):
    monkeypatch.setattr(claude_code, "preflight", lambda **kw: {})
    p, sent = parent(), []
    def timeout(**kw):
        call = tasks.jobs_calls(p["id"])[0]
        assert call["lease"]["expires_at"] - call["lease"]["acquired_at"] >= 660
        sent.append(1)
        raise TimeoutError()
    monkeypatch.setattr(claude_code, "call", timeout)
    for _ in range(2):
        with pytest.raises(tasks.CallPending):
            text.call_text("writer", parent=p, step="write", system="s", user="u")
    call = tasks.jobs_calls(p["id"])[0]
    assert sent == [1] and call["state"] == "unknown" and call["settlement_state"] == "held"
    assert call["actual_cost_fen"] is None


def test_cli_model_mismatch_still_preserves_received_usage(monkeypatch):
    monkeypatch.setattr(claude_code, "preflight", lambda **kw: {})
    monkeypatch.setattr(claude_code, "call", lambda **kw: native_result(model="claude-sonnet-5-5-other"))
    p = parent()
    with pytest.raises(ContentRejected) as exc:
        text.call_text("writer", parent=p, step="write", system="s", user="u")
    assert exc.value.code == "MODEL_MISMATCH"
    assert tasks.jobs_calls(p["id"])[0]["usage"] == {"input_tokens": 120, "output_tokens": 6}


def test_invalid_cli_profile_is_rejected_before_sending(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "qwen")
    with pytest.raises(ContentBlocked):
        text.profile("writer")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "anthropic")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_REVIEWER_EXECUTION_MODE", "claude_code")
    with pytest.raises(ContentBlocked):
        text.profile("reviewer")


def test_runtime_identity_changes_with_key_without_exposing_it(monkeypatch):
    first = text.runtime_profile("writer")
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_API_KEY", "new-private-writer")
    second = text.runtime_profile("writer")
    assert first["profile_id"] != second["profile_id"]
    assert second["execution_mode"] == "claude_code" and second["timeout_seconds"] == 600
    assert "new-private-writer" not in json.dumps(second)


def test_cli_mode_cannot_silently_use_injected_http_transport(monkeypatch):
    p = parent()
    with pytest.raises(ContentRejected) as exc:
        text.call_text("writer", parent=p, step="write", system="s", user="u", transport=lambda *a: (200,b'{}'))
    assert exc.value.code == "TRANSPORT_MODE_CONFLICT" and tasks.jobs_calls(p["id"]) == []


def test_task_profile_freeze_survives_replay_and_blocks_changed_execution_before_paid_call(monkeypatch):
    from app.content import service
    p = parent()
    assert p["writer_runtime_profiles"]["writer"] == text.runtime_profile("writer")["profile_id"]
    monkeypatch.setenv("AVATAR_STUDIO_ANTHROPIC_BASE_URL", "https://another.example")
    replay = tasks.submit_job(p["work_id"], p["kind"], p["request_id"], p["payload"])
    assert replay["id"] == p["id"] and replay["writer_runtime_profiles"] == p["writer_runtime_profiles"]
    with pytest.raises(ContentBlocked) as exc:
        service._assert_writer_runtime(p)
    assert exc.value.code == "WRITER_PROFILE_CHANGED" and tasks.jobs_calls(p["id"]) == []


def test_legacy_paid_task_cannot_resume_as_new_cli_request(monkeypatch):
    from app.content import service
    p = parent()
    legacy = dict(p)
    legacy.pop("writer_runtime_profiles", None)
    monkeypatch.setattr(tasks, "jobs_calls", lambda _: [{"provider":"anthropic","step":"write","attempts":1}])
    with pytest.raises(ContentBlocked) as exc:
        service._assert_writer_runtime(legacy)
    assert exc.value.code == "WRITER_PROFILE_CHANGED"
