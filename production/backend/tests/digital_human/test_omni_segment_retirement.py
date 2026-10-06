"""OmniHuman 分段功能退休后的提交、历史回收与 HTTP 语义。"""
from __future__ import annotations

import pytest

from app import storage as st
from app.digital_human import omni_driver, routes, segments, submission
from app.digital_human.errors import TalkingHeadRejected
from dh_helpers import allow_billable, authorize_cost, prepare_work, volc_query, volc_submit_ok


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _task(work: dict) -> dict:
    return st.get("tasks", work["current_task_id"])


def _historical_run(work: dict, *, segment_count: int = 2,
                    state: str = "waiting_provider") -> dict:
    """写入一条已经付费/受理过的历史 run，不走新的供应商提交。"""
    run = submission.start_run(
        work["id"], route=routes.ROUTE_OMNIHUMAN, provider="volcengine",
        model="omnihuman-1.5", capability=routes.CAP_AVATAR_VIDEO_CLOUD,
        request_digest="sha256:" + "a" * 64,
        idempotency_key=f"legacy:{work['id']}:oh_seg_1",
        provider_params={"segment_index": 0, "segment_count": segment_count,
                         "retry_suffix": ""},
    )
    submission._update_run(run["id"], state=state, remote_job_id="legacy-remote-1")
    return submission.get_run(run["id"])


def test_new_short_submit_uses_one_whole_track_request_without_segment_slice(
        monkeypatch):
    work = prepare_work(audio_ms=9000)
    allow_billable(monkeypatch)
    authorize_cost(work["id"])
    monkeypatch.setenv("AVATAR_STUDIO_VOLC_ACCESS_KEY", "fixture-ak")
    monkeypatch.setenv("AVATAR_STUDIO_VOLC_SECRET_KEY", "fixture-sk")

    from app.digital_human import omni_media

    monkeypatch.setattr(omni_media, "prepare_media", lambda source, audio, *, ak, sk: {
        "image_url": "https://cdn.example.com/hero.png",
        "audio_url": "https://cdn.example.com/full.wav",
        "audio_path": None,
    })
    monkeypatch.setattr(
        omni_media, "prepare_segment_audio",
        lambda *args, **kwargs: pytest.fail("新提交不得切分音频"),
    )
    calls = []

    def transport(request):
        calls.append(request)
        return volc_submit_ok("whole-track-remote")

    out = submission.submit_omnihuman(
        work["id"], task=_task(work), transport=transport, dry_run=False)

    assert out["submitted"] is True
    assert len(calls) == 1
    assert calls[0]["json"]["audio_url"] == "https://cdn.example.com/full.wav"
    assert len(out["segments"]) == 1
    assert out["segments"][0]["segment_id"] == "oh_seg_1"
    assert out["pending_segments"] == 0
    assert out["driver_started"] is False
    assert out["runs"][0]["idempotency_key"].endswith(":oh_seg_1")


def test_historical_long_run_is_polled_but_pending_segments_are_never_submitted(
        monkeypatch):
    work = prepare_work(audio_ms=75000)
    run = _historical_run(work, segment_count=2)
    monkeypatch.setenv("AVATAR_STUDIO_VOLC_ACCESS_KEY", "fixture-ak")
    monkeypatch.setenv("AVATAR_STUDIO_VOLC_SECRET_KEY", "fixture-sk")
    requests = []

    def transport(request):
        requests.append(request)
        return volc_query("running")

    out = omni_driver.advance_once(work["id"], transport=transport)

    assert requests, "历史已受理任务必须先允许轮询"
    assert requests[0]["query"]["Action"] == "CVGetResult"
    assert out["state"] == "STOPPED"
    assert out["stop_reason"] == "SEGMENT_SUBMISSION_RETIRED"
    assert not out.get("submitted")
    assert submission.get_run(run["id"])["state"] == "waiting_provider"


def test_historical_progress_never_reports_resumable(monkeypatch):
    work = prepare_work(audio_ms=75000)
    _historical_run(work, segment_count=2)

    snapshot = omni_driver.progress(work["id"])

    assert snapshot["resumable"] is False
    assert snapshot["stop_reason"] == "SEGMENT_SUBMISSION_RETIRED"
    assert "仅查询" in snapshot["note"]


def test_driver_resume_is_explicitly_retired_for_historical_runs():
    work = prepare_work(audio_ms=75000)
    _historical_run(work, segment_count=2)

    with pytest.raises(TalkingHeadRejected) as raised:
        omni_driver.resume(work["id"])

    assert raised.value.code == "OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED"


def test_segment_post_endpoints_are_gone_but_get_history_remains(http):
    work = prepare_work(audio_ms=9000)
    segments.create_plan(work["id"])

    post_segments = http.post(
        f"/api/v1/works/{work['id']}/talking-head/segments", json={})
    post_stitch = http.post(
        f"/api/v1/works/{work['id']}/talking-head/stitch", json={})
    post_resume = http.post(
        f"/api/v1/works/{work['id']}/talking-head/driver/resume", json={})

    assert post_segments.status_code == 410
    assert post_stitch.status_code == 410
    assert post_resume.status_code == 410
    for response in (post_segments, post_stitch, post_resume):
        assert response.json()["error"]["code"] == "OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED"

    get_segments = http.get(f"/api/v1/works/{work['id']}/talking-head/segments")
    get_driver = http.get(f"/api/v1/works/{work['id']}/talking-head/driver")
    assert get_segments.status_code == 200
    assert get_driver.status_code == 200
