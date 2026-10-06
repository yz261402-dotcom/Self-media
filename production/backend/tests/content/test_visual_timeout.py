"""视觉响应等待与超时账本：使用离线 transport 和隔离测试库。"""
import json
from urllib.error import URLError
from uuid import uuid4

import pytest

from app import jobs, storage as st
from app.content import tasks
from app.content.errors import ContentRejected
from app.content.providers import qwen_vision as qv


def response():
    return json.dumps({"id": "response-local", "model": "qwen3-vl-plus", "choices": [
        {"message": {"content": json.dumps({"scenes": [{"start_ms": 0, "end_ms": 1000,
                                                      "description": "可见动作"}]})},
         "finish_reason": "stop"}]}).encode()


@pytest.mark.parametrize("method", [qv.call_frames, qv.analyze_frames])
@pytest.mark.parametrize("configured,explicit,expected", [
    (None, None, 600), ("900", None, 900), ("900", 320, 320),
])
def test_visual_transport_waits_for_quality_model_once(monkeypatch, method, configured, explicit, expected):
    monkeypatch.delenv("AVATAR_STUDIO_CONTENT_VISUAL_TIMEOUT_SECONDS", raising=False)
    if configured is not None:
        monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_TIMEOUT_SECONDS", configured)
    seen = []

    def transport(url, headers, body, timeout):
        seen.append(timeout)
        return 200, response()

    args = dict(model="qwen3-vl-plus", frames=[{"timestamp_ms": 0, "jpeg_bytes": b"jpeg"}],
                audio_summary={}, duration_ms=1000, api_key="offline", transport=transport)
    if explicit is not None:
        args["timeout"] = explicit
    method(**args)
    assert seen == [expected]


@pytest.mark.parametrize("value", ["0", "-1", "bad", "nan", "1801"])
def test_bad_visual_wait_setting_cannot_send_a_paid_request(monkeypatch, value):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_TIMEOUT_SECONDS", value)
    seen = []

    def transport(*args):
        seen.append(args)
        return 200, response()

    with pytest.raises(ContentRejected) as exc:
        qv.call_frames(model="qwen3-vl-plus", frames=[{"timestamp_ms": 0, "jpeg_bytes": b"jpeg"}],
                       audio_summary={}, duration_ms=1000, api_key="offline",
                       transport=transport)
    assert exc.value.code == "VISUAL_TIMEOUT_INVALID"
    assert not seen


def parent():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "timeout", "version": 1, "content_revision": 0})
    return tasks.submit_job(wid, "visual_analysis", "visual", {
        "reference_snapshot_id": "reference-test", "media_asset_id": "media-test"})


def run(job, send, **kwargs):
    return tasks.run_call(parent=job, provider="qwen", model="qwen3-vl-plus",
                          capability="visual_analysis", step="visual_analysis", round_no=0,
                          request={"media": "local"}, send=send, reserve_fen=80, **kwargs)


def test_visual_lease_covers_response_wait_during_recovery(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(jobs, "_now", lambda: clock["now"])
    monkeypatch.setenv("AVATAR_STUDIO_LEASE_TTL", "30")
    job = parent()

    def send():
        clock["now"] = 300.0
        assert jobs.recover_content_calls(job["id"]) == []
        return tasks.CallOutcome(response={"ok": True}, cost_fen=None)

    assert run(job, send, lease_seconds=660)["state"] == "succeeded"


@pytest.mark.parametrize("error", [TimeoutError("private transport detail"),
                                   URLError(TimeoutError("private transport detail"))])
def test_timeout_reports_cause_without_repeating_or_discarding_possible_cost(error):
    job = parent()
    sent = []

    def send():
        sent.append(1)
        raise error

    with pytest.raises(tasks.CallPending) as exc:
        run(job, send)
    assert exc.value.code == "PROVIDER_RESPONSE_TIMEOUT"
    assert "超时" in exc.value.message and "private transport detail" not in exc.value.message
    tasks.run_job_error(job, exc.value)
    assert tasks.get_job(job["id"])["state"] == "unknown"
    with pytest.raises(tasks.CallPending):
        run(job, send)
    call = jobs.content_calls(job["id"])[0]
    assert sent == [1] and call["attempts"] == 1
    assert call["state"] == "unknown" and call["settlement_state"] == "held"
    assert call.get("actual_cost_fen") is None


def test_old_timeout_receipt_displays_known_cause_and_reference_without_mutating_ledger():
    from app.content_api import receipt
    job = parent()
    with pytest.raises(tasks.CallPending):
        run(job, lambda: (_ for _ in ()).throw(TimeoutError()))
    call = jobs.content_calls(job["id"])[0]
    old = dict(job, state="unknown", error={"code": "CONTENT_CALL_PENDING",
                                           "message": "旧版未确认提示", "detail": call["id"]})
    before = st.get("provider_jobs", call["id"])
    out = receipt(old, job["work_id"])
    assert out["error"]["code"] == "PROVIDER_RESPONSE_TIMEOUT"
    assert "超时" in out["error"]["message"]
    assert out["reference_snapshot_id"] == "reference-test"
    assert out["media_asset_id"] == "media-test"
    assert st.get("provider_jobs", call["id"]) == before
