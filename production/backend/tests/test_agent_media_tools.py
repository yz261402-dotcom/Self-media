"""Supervisor media tools use business services after user confirmation."""
from __future__ import annotations

import pytest

from app import mcp, storage as st
from app.agents import tools
from app.agents.roles import ROLES
from tests.tts.tts_helpers import new_work, approved_alt_voice, allow_billable, make_transport, ok_body


def test_media_compilation_is_supervisor_only():
    assert "compile_performance_plan" in ROLES["supervisor"].tools
    assert "compile_performance_plan" not in ROLES["content"].tools


def test_supervisor_tts_calls_full_synthesis_after_confirmation(monkeypatch, tmp_path):
    from app.tts import audio

    voice = approved_alt_voice(st.new_id("voice_agent"))
    work = new_work(voice_id=voice["id"])
    monkeypatch.setattr(audio, "runtime_root", lambda: str(tmp_path))
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=1500))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_FEN", "10000")
    from app.agents import media
    quote = media.tts_quote(work["id"])
    confirmation = media.confirm_tts(work["id"], request_digest=quote["request_digest"], ceiling_fen=50,
                                     actor_id="user_test")
    out = tools.invoke("supervisor", "submit_tts",
                       {"work_id": work["id"], "confirmation_id": confirmation["id"],
                        "request_digest": confirmation["request_digest"]},
                       {"work_id": work["id"], "work_item_id": st.new_id("wi"), "tts_transport": transport})
    assert len(calls) == 1
    assert out["result"]["audio_id"]
    assert out["result"]["state"] == "succeeded"
    assert not out["result"].get("simulated")
    assert st.get("audios", out["result"]["audio_id"])["duration_ms"] == 1500


def test_supervisor_video_tool_calls_business_submission(monkeypatch):
    from app.digital_human import submission

    work_id = st.new_id("w_video_tool")
    st.put("works", work_id, {"id": work_id, "route": "OMNIHUMAN_1_5"})
    confirmation = {"id": st.new_id("cost"), "work_id": work_id, "route": "OMNIHUMAN_1_5",
                    "actor_id": "user_test", "request_digest": "approved-input", "ceiling_fen": 100}
    st.put("cost_confirmations", confirmation["id"], confirmation, {"work_id": work_id})
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_FEN", "10000")
    calls = []

    def submit(wid, **kwargs):
        calls.append((wid, kwargs))
        return {"submitted": True, "is_mock": False,
                "runs": [{"id": "real_provider_run", "state": "waiting_provider"}]}

    monkeypatch.setattr(submission, "submit_omnihuman", submit)
    monkeypatch.setattr(submission, "current_call_list", lambda *a, **k: {"digest": "approved-input"})
    from app.digital_human import cost
    monkeypatch.setattr(cost, "assert_submission_allowed", lambda *a, **k: confirmation)
    out = tools.invoke("supervisor", "submit_avatar_video",
                       {"work_id": work_id, "route": "OMNIHUMAN_1_5",
                        "confirmation_id": confirmation["id"], "request_digest": "approved-input"},
                       {"work_id": work_id, "work_item_id": st.new_id("wi")})
    assert len(calls) == 1
    assert calls[0][1]["dry_run"] is False
    assert out["result"]["run_ids"] == ["real_provider_run"]
    assert out["result"]["state"] == "waiting_provider"
    assert not st.all_rows("media_jobs", "work_id=?", [work_id])


def test_mcp_media_submission_uses_business_tool_instead_of_second_job(monkeypatch):
    work_id = st.new_id("w_mcp_media")
    confirmation = {"id": st.new_id("cost"), "work_id": work_id, "route": "TTS_MINIMAX",
                    "actor_id": "user_test", "request_digest": "current-input"}
    st.put("cost_confirmations", confirmation["id"], confirmation, {"work_id": work_id})
    calls = []
    monkeypatch.setitem(tools.TOOLS, "submit_tts", lambda args, ctx: (
        calls.append((args, ctx)) or {"audio_id": "real_audio", "job_id": "real_tts_job", "state": "succeeded"}))
    result = mcp.submit(role="supervisor", capability="tts",
                        args={"work_id": work_id, "confirmation_id": confirmation["id"],
                              "request_digest": "current-input"},
                        ctx={"work_id": work_id, "work_item_id": st.new_id("wi")}, idempotency_key="same-request")
    assert len(calls) == 1
    assert result["id"] == "real_tts_job"
    assert result["result"]["audio_id"] == "real_audio"
    assert st.all_rows("provider_jobs", "work_id=?", [work_id]) == []


def test_media_query_returns_actual_job_and_enforces_work_scope():
    work_id = st.new_id("w_query_media")
    job_id = st.new_id("actual_tts_job")
    st.put("provider_jobs", job_id, {"id": job_id, "work_id": work_id, "capability": "tts", "state": "succeeded"},
           {"work_id": work_id})
    out = tools.invoke("supervisor", "get_media_job", {"job_id": job_id},
                       {"work_id": work_id, "work_item_id": st.new_id("wi")})
    assert out["result"]["state"] == "succeeded"
    with pytest.raises(tools.ToolDenied) as denied:
        tools.invoke("supervisor", "get_media_job", {"job_id": job_id},
                     {"work_id": "another_work", "work_item_id": st.new_id("wi")})
    assert denied.value.code == "WORK_SCOPE_DENIED"


def test_tts_confirmation_is_invalidated_by_control_changes(monkeypatch):
    from app.agents import media
    from app.tts import gate, voices

    voice = approved_alt_voice(st.new_id("voice_confirm"))
    work = new_work(voice_id=voice["id"])
    allow_billable(monkeypatch)
    quote = media.tts_quote(work["id"])
    confirmation = media.confirm_tts(work["id"], request_digest=quote["request_digest"], ceiling_fen=50, actor_id="user")
    controls = {key: value for key, value in voices.effective_profile(st.get("works", work["id"]), voice).items()
                if key in voices.WORK_CONTROL_KEYS}
    gate.set_work_controls(work["id"], dict(controls, speed=1.15))
    with pytest.raises(tools.ToolDenied) as denied:
        tools.invoke("supervisor", "submit_tts",
                     {"work_id": work["id"], "confirmation_id": confirmation["id"],
                      "request_digest": quote["request_digest"]},
                     {"work_id": work["id"], "work_item_id": st.new_id("wi"),
                      "tts_transport": lambda r: pytest.fail("stale input submitted")})
    assert denied.value.code == "COST_LIST_CHANGED"


def test_tts_tool_rejects_cancelled_current_task_after_confirmation(monkeypatch):
    from app.agents import media
    voice = approved_alt_voice(st.new_id("voice_cancel"))
    work = new_work(voice_id=voice["id"])
    allow_billable(monkeypatch)
    quote = media.tts_quote(work["id"])
    confirmation = media.confirm_tts(work["id"], request_digest=quote["request_digest"], ceiling_fen=50, actor_id="user")
    work = st.get("works", work["id"])
    task_id = st.new_id("task_cancel")
    work["current_task_id"] = task_id
    st.put("works", work["id"], work)
    st.put("tasks", task_id, {"id": task_id, "work_id": work["id"], "state": "cancelled"}, {"work_id": work["id"]})
    with pytest.raises(tools.ToolDenied) as denied:
        tools.invoke("supervisor", "submit_tts",
                     {"work_id": work["id"], "confirmation_id": confirmation["id"],
                      "request_digest": quote["request_digest"]},
                     {"work_id": work["id"], "work_item_id": st.new_id("wi"),
                      "tts_transport": lambda r: pytest.fail("cancelled task submitted")})
    assert denied.value.code == "TASK_CANCELLED"
