"""纯离线：排队解析必须绑定用户确认的模型和分析版本，不使用其他调用的确认。"""
import pytest

from app.content import contracts as cc, media, tasks, visual
from app.content.errors import ContentRejected
from app.content.providers import qwen_vision as qv


def asset():
    return cc.MediaAsset(id="media", work_id="work", kind="reference_video",
                         storage_uri="content-media/work/a.mp4", sha256="sha256:" + "a" * 64,
                         mime_type="video/mp4", byte_size=100, duration_ms=3000, width=320, height=568,
                         source_kind="user_upload", created_at="t")


@pytest.mark.parametrize("changed", [
    {"model": "qwen3-vl-flash"}, {"analysis_version": "visual-v1"}, {"request_digest": "old-confirmation"},
])
def test_stale_visual_job_cannot_borrow_a_new_profile_confirmation(monkeypatch, changed):
    from app.content import visual_pipeline as vp
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", "qwen3-vl-plus")
    monkeypatch.setattr(visual, "_bound", lambda *a: ({"id": "reference"}, asset()))
    monkeypatch.setattr(visual, "quote", lambda *a: {"request_digest": "current-confirmation",
        "call_list": {"model": qv.model(), "analysis_version": qv.ANALYSIS_VERSION}})
    monkeypatch.setattr(visual, "has_user_confirmation", lambda *a: True)
    monkeypatch.setattr(tasks, "checkpoint", lambda *a: None)
    monkeypatch.setattr(tasks, "set_progress", lambda *a, **kw: None)
    monkeypatch.setattr(media, "resolve_path", lambda *a: "fixture.mp4")
    monkeypatch.setattr(vp, "sample_frames", lambda *a: [{"timestamp_ms": 0, "sha256": "sha256:a"}])
    monkeypatch.setattr(vp, "analyze_audio", lambda *a: {})
    def cannot_send(**kw):
        pytest.fail("变更过的解析请求不应进入 provider 调用")
    monkeypatch.setattr(tasks, "run_call", cannot_send)
    payload = {"reference_snapshot_id": "reference", "media_asset_id": "media", "total_ceiling_fen": 80,
               "model": qv.model(), "analysis_version": qv.ANALYSIS_VERSION,
               "request_digest": "current-confirmation", **changed}
    with pytest.raises(ContentRejected) as error:
        visual.handle_visual_analysis({"id": "job", "work_id": "work", "payload": payload})
    assert error.value.code == "COST_LIST_CHANGED"


def test_submitted_visual_payload_freezes_confirmation_profile(monkeypatch):
    confirmation = {"ceiling_fen": 80, "request_digest": "confirmed-digest",
                    "call_list": {"model": qv.model(), "analysis_version": qv.ANALYSIS_VERSION}}
    monkeypatch.setattr(visual, "verify_confirmation", lambda *a: confirmation)
    monkeypatch.setattr(media, "asset_for_work", lambda *a: asset())
    monkeypatch.setattr(tasks, "submit_job", lambda work, kind, rid, payload, **kw: payload)
    payload = visual.submit_visual_analysis("work", "reference", "media", confirmation_id="confirmed")
    assert payload["analysis_version"] == confirmation["call_list"]["analysis_version"]
    assert payload["request_digest"] == confirmation["request_digest"]


def test_explicit_new_job_can_retry_a_confirmed_failure(monkeypatch):
    monkeypatch.setattr(visual.st, "all_rows", lambda *a: [
        {"id": "old-call", "parent_content_job_id": "old-job", "state": "failed", "idempotency_key": "visual-base"}])
    assert visual._call_key({"id": "new-job", "work_id": "work"}, "visual-base", 3000) == "visual-base:attempt:new-job"


def test_unknown_retry_attempt_prevents_another_paid_call(monkeypatch):
    monkeypatch.setattr(visual.st, "all_rows", lambda *a: [
        {"id": "first-call", "parent_content_job_id": "first-job", "state": "failed", "idempotency_key": "visual-base"},
        {"id": "unknown-call", "parent_content_job_id": "retry-job", "state": "unknown",
         "idempotency_key": "visual-base:attempt:retry-job"}])
    with pytest.raises(tasks.CallPending) as error:
        visual._call_key({"id": "new-job", "work_id": "work"}, "visual-base", 3000)
    assert error.value.detail == "unknown-call"


def test_resuming_same_job_preserves_attempt_idempotency(monkeypatch):
    monkeypatch.setattr(visual.st, "all_rows", lambda *a: [
        {"id": "own-call", "parent_content_job_id": "job", "state": "failed", "idempotency_key": "visual-base:attempt:job"}])
    assert visual._call_key({"id": "job", "work_id": "work"}, "visual-base", 3000) == "visual-base:attempt:job"


@pytest.mark.parametrize("state", ["queued", "failed", "blocked", "unknown"])
def test_same_job_base_call_is_never_submitted_as_a_new_attempt(monkeypatch, state):
    monkeypatch.setattr(visual.st, "all_rows", lambda *a: [
        {"id": "own-call", "parent_content_job_id": "job", "state": state, "idempotency_key": "visual-base"}])
    assert visual._call_key({"id": "job", "work_id": "work"}, "visual-base", 3000) == "visual-base"
