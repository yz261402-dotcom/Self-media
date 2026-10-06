"""远端结果回收不得重复提交，也不得借当前作品绑定给旧结果换人物/音轨。"""
import hashlib

import pytest

from app import jobs, storage as st
from app.digital_human import plates, submission as sub, videos
from app.digital_human.errors import TalkingHeadBlocked
from app.tts import audio as tts_audio
from dh_helpers import allow_billable, authorize_cost, make_mp4, make_transport


def _downloaded():
    raw = make_mp4(9000, audio=True)
    return {"data": raw, "probe": {"duration_ms": 9000,
            "file_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "has_video_track": True, "has_audio_track": True, "width": 720, "height": 1280}}


def _legacy_run(work, *, remote_id="remote_1"):
    listing = sub.current_call_list(work["id"], route="M2")
    run = sub.start_run(work["id"], route="M2", provider="m2_worker", model="MuseTalk-1.5",
                        capability="lipsync", request_digest=listing["digest"], idempotency_key=st.new_id("idem"),
                        provider_params={"plate_id": listing["plate_id"]})
    return sub._update_run(run["id"], state="waiting_provider", remote_job_id=remote_id)


def test_same_m2_pending_click_reuses_run_without_resubmit(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    authorize_cost(m2_work["id"], route="M2")
    transport, calls = make_transport(default={"status_code": 200,
                                               "json": {"state": "running", "job_id": "remote_1"}})
    task = st.get("tasks", m2_work["current_task_id"])
    first = sub.submit_m2(m2_work["id"], task=task, transport=transport, dry_run=False)
    second = sub.submit_m2(m2_work["id"], task=task, transport=transport, dry_run=False)
    assert len(calls) == 1
    assert len(sub.runs_for_work(m2_work["id"])) == 1
    assert first["runs"][0]["id"] == second["runs"][0]["id"]
    assert first["runs"][0]["provider_params"]["input_snapshot"]["audio_asset_id"] == tts_audio.final_audio(m2_work["id"])["id"]


def test_two_legacy_runs_for_same_remote_result_share_original(m2_work, monkeypatch):
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    one, two = _legacy_run(m2_work), _legacy_run(m2_work)
    first = sub._register_original(one["id"], _downloaded(), note="test")
    second = sub._register_original(two["id"], _downloaded(), note="test")
    assert first["video"]["id"] == second["video"]["id"]
    assert len(videos.list_videos(m2_work["id"], "original")) == 1
    assert second["video"]["approval_state"] == "pending"


def test_reused_legacy_original_repairs_only_verified_missing_provenance(m2_work, monkeypatch):
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    one, two = _legacy_run(m2_work), _legacy_run(m2_work)
    first = sub._register_original(one["id"], _downloaded(), note="test")
    original = first["video"]
    for field in ("motion_ref_id", "source_asset_id", "source_asset_hash"):
        original[field] = None
    st.put("videos", original["id"], original, {"work_id": m2_work["id"]})
    before = dict(original)

    repaired = sub._register_original(two["id"], _downloaded(), note="test")["video"]
    plate = plates.approved_plate_for(m2_work["id"])
    assert repaired["id"] == original["id"]
    for field in ("motion_ref_id", "source_asset_id", "source_asset_hash"):
        assert repaired[field] == plate[field]
    for field, value in before.items():
        if field not in ("motion_ref_id", "source_asset_id", "source_asset_hash"):
            assert repaired[field] == value, field
    assert len(videos.list_videos(m2_work["id"], "original")) == 1


def test_equal_file_hash_from_different_remote_job_does_not_merge(m2_work, monkeypatch):
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    one, two = _legacy_run(m2_work, remote_id="remote_1"), _legacy_run(m2_work, remote_id="remote_2")
    first = sub._register_original(one["id"], _downloaded(), note="test")
    second = sub._register_original(two["id"], _downloaded(), note="test")
    assert first["video"]["id"] != second["video"]["id"]


def test_late_legacy_result_retains_original_identity_and_is_not_current(m2_work, monkeypatch):
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    run = _legacy_run(m2_work)
    current = st.get("works", m2_work["id"])
    current["identity_id"] = "av_user_selected_another_person"
    st.put("works", current["id"], current)
    out = sub._register_original(run["id"], _downloaded(), note="test")
    assert out["video"]["identity_id"] == m2_work["identity_id"]
    assert out["video"]["approval_state"] == "revoked"
    assert out["video"]["confirmed"] is False


def test_legacy_run_with_invalid_digest_cannot_bind_current_inputs(m2_work, monkeypatch):
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    run = _legacy_run(m2_work)
    sub._update_run(run["id"], request_digest="sha256:wrong")
    with pytest.raises(TalkingHeadBlocked) as error:
        sub._register_original(run["id"], _downloaded(), note="test")
    assert error.value.code == "RUN_INPUT_SNAPSHOT_UNVERIFIABLE"
    assert videos.list_videos(m2_work["id"], "original") == []


def test_completed_m2_submit_reuses_original_and_completes_job(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    authorize_cost(m2_work["id"], route="M2")
    monkeypatch.setattr(videos, "store_video_bytes", lambda *args: "test-only/original.mp4")
    transport, calls = make_transport(default={"status_code": 200,
                                               "json": {"state": "running", "job_id": "remote_1"}})
    task = st.get("tasks", m2_work["current_task_id"])
    submitted = sub.submit_m2(m2_work["id"], task=task, transport=transport, dry_run=False)
    run = submitted["runs"][0]
    output = sub._register_original(run["id"], _downloaded(), note="test")
    job = jobs.get_job(run["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["video_id"] == output["video"]["id"]
    second = sub.submit_m2(m2_work["id"], task=task, transport=transport, dry_run=False)
    assert second["runs"][0]["id"] == run["id"]
    assert second["runs"][0]["result_asset_ids"] == [output["video"]["id"]]
    assert len(calls) == 1


def test_changed_audio_bytes_rejects_saved_snapshot(m2_work, monkeypatch):
    allow_billable(monkeypatch)
    authorize_cost(m2_work["id"], route="M2")
    transport, _ = make_transport(default={"status_code": 200,
                                           "json": {"state": "running", "job_id": "remote_1"}})
    out = sub.submit_m2(m2_work["id"], task=st.get("tasks", m2_work["current_task_id"]),
                        transport=transport, dry_run=False)
    audio = tts_audio.final_audio(m2_work["id"])
    audio["file_hash"] = "sha256:" + "f" * 64
    st.put("audios", audio["id"], audio, {"work_id": m2_work["id"]})
    with pytest.raises(TalkingHeadBlocked, match="输入|摘要|音频"):
        sub._register_original(out["runs"][0]["id"], _downloaded(), note="test")
    assert videos.list_videos(m2_work["id"], "original") == []


def test_concurrent_submit_claims_single_run_and_external_request(m2_work, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    allow_billable(monkeypatch)
    authorize_cost(m2_work["id"], route="M2")
    entered, release = Event(), Event()
    calls = []
    task = st.get("tasks", m2_work["current_task_id"])

    def transport(request):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        return {"status_code": 200, "json": {"state": "running", "job_id": "remote_concurrent"}}

    def submit():
        return sub.submit_m2(m2_work["id"], task=task, transport=transport, dry_run=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        assert entered.wait(5)
        second = pool.submit(submit)
        try:
            reused = second.result(timeout=5)
        finally:
            release.set()
        submitted = first.result(timeout=5)
    assert submitted["runs"][0]["id"] == reused["runs"][0]["id"]
    assert len(calls) == 1
    assert len(sub.runs_for_work(m2_work["id"])) == 1
