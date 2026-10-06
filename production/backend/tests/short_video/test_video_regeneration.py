"""Explicit Wan replacement attempts preserve earlier native video and charges."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import jobs, storage as st
from app.avatar import person_image
from app.main import app
from tests.short_video.test_storyboard import prepared, ready_images


def video_ready(prepared, monkeypatch):
    service, graph, root = prepared
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user", task_graph_digest=graph["task_graph_digest"],
        paid_ack=True, image_review_ack=True, total_ceiling_fen=graph["estimate"]["video_total_fen"])
    return service, graph, root


def failed_video(prepared, monkeypatch):
    service, graph, root = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=lambda request: {"status_code": 429, "json": {"code": "Arrearage"}})
    return service, graph, root


def approval(graph, **changes):
    return {"actor_id": "user_test", "paid_ack": True, "duplicate_charge_ack": True,
        "task_graph_digest": graph["task_graph_digest"], "expected_video_job_id": graph["shots"][0]["video_job_id"],
        "request_id": "video-retry-one", "ceiling_fen": graph["estimate"]["shot_prices"][0]["ceiling_fen"], **changes}


def accepted(calls, task="new-task"):
    def transport(request):
        calls.append(request)
        return {"status_code": 200, "json": {"output": {"task_id": task}}}
    return transport


@pytest.mark.parametrize("changes", [{"paid_ack": False}, {"duplicate_charge_ack": False},
    {"actor_id": "agent"}, {"task_graph_digest": "old"}, {"request_id": ""}, {"ceiling_fen": 0}])
def test_video_retry_requires_explicit_current_approval(prepared, monkeypatch, changes):
    service, graph, _ = failed_video(prepared, monkeypatch)
    calls = []
    before = st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]])
    with pytest.raises(service.StoryboardError):
        service.regenerate_video(graph["id"], 1, **approval(graph, **changes), transport=accepted(calls))
    assert calls == [] and st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]]) == before


def test_quota_failed_video_can_be_retried_without_changing_other_shots_or_ledger(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    original = jobs.get_job(graph["shots"][0]["video_job_id"])
    assert graph["shots"][0]["video_regeneration_available"]
    calls = []
    result = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    shot = result["shots"][0]
    assert len(calls) == 1 and shot["video_state"] == "waiting_provider"
    assert shot["remote_task_id"] == "new-task" and shot["video_job_id"] != original["id"]
    assert shot["video_attempt"] == 1 and shot["error"] is None
    assert shot["video_history"][0]["video_job_id"] == original["id"]
    assert jobs.get_job(original["id"]) == original
    assert result["shots"][1] == graph["shots"][1]
    assert shot["image"] == graph["shots"][0]["image"]
    job = jobs.get_job(shot["video_job_id"])
    assert job["wan_submission"]["remote_task_id"] == "new-task"
    assert job["settlement_required"] is True
    assert result["video_approval"] == graph["video_approval"] and result["clip_approval"] is None
    assert service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls)) == result
    assert len(calls) == 1


def test_unknown_video_can_be_replaced_after_duplicate_charge_ack(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=lambda *a: {"status_code": None})
    old = jobs.get_job(graph["shots"][0]["video_job_id"])
    result = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted([]))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert jobs.get_job(old["id"]) == old and old["state"] == "unknown"


def test_video_retry_with_stale_original_job_or_reused_request_id_is_rejected(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    calls = []
    current = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_video(graph["id"], 1, **approval(current), transport=accepted(calls))
    assert error.value.code == "STORYBOARD_VIDEO_RETRY_CONFLICT"
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_video(graph["id"], 1, **approval(graph, request_id="different"), transport=accepted(calls))
    assert error.value.code == "STORYBOARD_VIDEO_ATTEMPT_CHANGED"
    assert len(calls) == 1


def test_running_video_is_queried_and_cannot_be_replaced(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    assert graph["shots"][0]["video_regeneration_available"] is False
    calls = []
    with pytest.raises(service.StoryboardError):
        service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    assert calls == []


@pytest.mark.parametrize("state", ["stitch_queued", "stitching", "succeeded", "stitch_failed"])
def test_stitching_or_delivered_graph_locks_video_regeneration(prepared, monkeypatch, state):
    service, graph, _ = failed_video(prepared, monkeypatch)
    service._mutate(graph["id"], lambda g: g.update(state=state))
    calls = []
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    assert error.value.code == "STORYBOARD_VIDEO_RETRY_FROZEN"
    assert calls == []


def test_video_retry_budget_checks_only_single_shot_and_preserves_current_on_block(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", str(graph["estimate"]["shot_prices"][0]["ceiling_fen"]))
    current = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted([]))
    service._update_shot(graph["id"], 1, video_state="failed")
    before = service.get_graph(graph["id"])
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "0")
    calls = []
    with pytest.raises(service.StoryboardError):
        service.regenerate_video(graph["id"], 1, **approval(current, request_id="another"), transport=accepted(calls))
    assert service.get_graph(graph["id"]) == before and calls == []


def test_remote_receipt_is_durable_before_graph_update_and_replay_never_resubmits(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    update = service._update_shot
    interrupted = False

    def stop(graph_id, index, **fields):
        nonlocal interrupted
        if fields.get("video_state") == "waiting_provider" and not interrupted:
            interrupted = True
            raise SystemExit()
        return update(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", stop)
    calls = []
    with pytest.raises(SystemExit):
        service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    current = service.get_graph(graph["id"])["shots"][0]
    assert current["video_state"] == "sending"
    assert jobs.get_job(current["video_job_id"])["wan_submission"]["remote_task_id"] == "new-task"
    result = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted(calls))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert result["shots"][0]["remote_task_id"] == "new-task" and len(calls) == 1


def test_video_attempt_downloads_to_new_file_and_retains_old_clip(prepared, monkeypatch):
    from app.short_video import media
    from tests.short_video.test_stitch import _clip
    service, graph, root = failed_video(prepared, monkeypatch)
    clip_path = root / 'test-original.mp4'
    _clip(clip_path, "red", 440, 2)
    data = clip_path.read_bytes()
    old_shot = dict(graph["shots"][0], remote_task_id="old-task")
    old_clip = media.save_clip(graph, old_shot, data)
    service._update_shot(graph["id"], 1, video_state="succeeded", clip=old_clip, remote_task_id="old-task")
    graph = service.project(service.get_graph(graph["id"]))
    result = service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted([]))

    def query(request):
        if request["method"] == "GET" and request["url"].endswith('/new-task'):
            return {"status_code": 200, "json": {"output": {"task_id": "new-task", "task_status": "SUCCEEDED", "video_url": "https://media.example/clip.mp4"}}}
        return {"status_code": 200, "body": data}

    result = service.poll_shot(graph["id"], 1, transport=query)
    assert result["shots"][0]["video_state"] == "succeeded"
    assert result["shots"][0]["clip"]["storage_uri"].endswith('shot_001_attempt_001_raw.mp4')
    assert (root / old_clip["storage_uri"]).read_bytes() == data
    assert result["shots"][0]["video_history"][0]["clip"] == old_clip
    job = jobs.get_job(result["shots"][0]["video_job_id"])
    assert job["state"] == "succeeded" and job["result_digest"]


def test_video_retry_http_confirmation_and_work_binding(prepared, monkeypatch):
    from app.digital_human import wan_provider
    service, graph, _ = failed_video(prepared, monkeypatch)
    monkeypatch.setattr(wan_provider, "urllib_transport", accepted([]))
    client = TestClient(app)
    path = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}/shots/1/video/regenerate"
    assert client.post(path, json={}).status_code == 409
    assert client.post(path.replace(graph["work_id"], 'wrong-work'), json=approval(graph)).status_code == 404
    response = client.post(path, json=approval(graph))
    assert response.status_code == 200 and response.json()["shots"][0]["remote_task_id"] == "new-task"


def test_retry_quota_failure_is_terminal_and_requires_a_new_explicit_request(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    calls = []

    def no_balance(request):
        calls.append(request)
        return {"status_code": 403, "json": {"code": "Arrearage", "message": "private original body"}}

    first = service.regenerate_video(graph["id"], 1, **approval(graph), transport=no_balance)
    assert first["shots"][0]["video_state"] == "failed"
    assert first["shots"][0]["error"]["code"] == "WAN_QUOTA_EXHAUSTED"
    assert service.regenerate_video(graph["id"], 1, **approval(graph), transport=no_balance) == first
    assert len(calls) == 1
    second = service.regenerate_video(graph["id"], 1,
        **approval(first, request_id="confirmed-after-top-up"), transport=accepted(calls))
    assert second["shots"][0]["video_state"] == "waiting_provider" and len(calls) == 2


def test_expired_video_sender_is_projected_unknown_and_can_be_replaced(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    with pytest.raises(SystemExit):
        service.submit_videos(graph["id"], transport=lambda request: (_ for _ in ()).throw(SystemExit()))
    original = service.get_graph(graph["id"])
    assert service.project(original)["shots"][0]["video_regeneration_available"] is False
    clock[0] += service.IMAGE_LEASE_SECONDS + 1
    old = jobs.get_job(original["shots"][0]["video_job_id"])
    view = service.project(original)
    assert view["shots"][0]["video_state"] == "unknown" and view["shots"][0]["video_regeneration_available"]
    result = service.regenerate_video(graph["id"], 1, **approval(view), transport=accepted([]))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert jobs.get_job(old["id"]) == old


def test_late_original_send_failure_cannot_change_new_video_state(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    entered, finish = threading.Event(), threading.Event()

    def late_failure(request):
        entered.set()
        assert finish.wait(5)
        return {"status_code": None}

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.regenerate_video, graph["id"], 1, **approval(graph), transport=late_failure)
        try:
            assert entered.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            current = service.project(service.get_graph(graph["id"]))
            new = service.regenerate_video(graph["id"], 1, **approval(current, request_id="new-after-timeout"), transport=accepted([]))
        finally:
            finish.set()
        first.result(timeout=5)
    result = service.project(service.get_graph(graph["id"]))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert result["shots"][0]["video_job_id"] == new["shots"][0]["video_job_id"]
    assert result["shots"][0]["remote_task_id"] == "new-task" and result["shots"][0]["error"] is None


def test_old_video_poll_failure_cannot_overwrite_retry_task(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([], "old-task"))
    entered, finish = threading.Event(), threading.Event()

    def late_query(request):
        entered.set()
        assert finish.wait(5)
        return {"status_code": 200, "json": {"output": {"task_id": "old-task", "task_status": "FAILED", "code": "Arrearage"}}}

    with ThreadPoolExecutor(max_workers=1) as pool:
        polling = pool.submit(service.poll_shot, graph["id"], 1, transport=late_query)
        try:
            assert entered.wait(5)
            old = jobs.get_job(graph["shots"][0]["video_job_id"])
            service._settle(old, success=False, result={"remote_task_id": "old-task"})
            service._update_shot(graph["id"], 1, video_state="failed")
            current = service.project(service.get_graph(graph["id"]))
            new = service.regenerate_video(graph["id"], 1, **approval(current), transport=accepted([]))
        finally:
            finish.set()
        polling.result(timeout=5)
    result = service.project(service.get_graph(graph["id"]))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert result["shots"][0]["video_job_id"] == new["shots"][0]["video_job_id"]
    assert result["shots"][0]["remote_task_id"] == "new-task"


def test_queued_video_retry_has_one_sender_after_expired_claim_replay(prepared, monkeypatch):
    from app.short_video import video_regeneration as vr
    service, graph, _ = failed_video(prepared, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    paused, resume, sending, finish = [threading.Event() for _ in range(4)]
    send = vr.send
    stopped = False

    def stop(*a, **kw):
        nonlocal stopped
        if not stopped:
            stopped = True
            paused.set()
            assert resume.wait(5)
        return send(*a, **kw)

    calls = []

    def provider(request):
        calls.append(request)
        sending.set()
        assert finish.wait(5)
        return {"status_code": 200, "json": {"output": {"task_id": "new-task"}}}

    monkeypatch.setattr(vr, "send", stop)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.regenerate_video, graph["id"], 1, **approval(graph), transport=provider)
        try:
            assert paused.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            replay = pool.submit(service.regenerate_video, graph["id"], 1, **approval(graph), transport=provider)
            assert sending.wait(5)
            resume.set()
            first.result(timeout=3)
            assert len(calls) == 1
        finally:
            resume.set()
            finish.set()
        assert replay.result(timeout=5)["shots"][0]["video_state"] == "waiting_provider"


def test_known_accepted_receipt_restores_unknown_projection_and_prevents_new_charge(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    service._update_shot(graph["id"], 1, video_state="unknown", remote_task_id=None)
    raw = service.get_graph(graph["id"])
    view = service.project(raw)
    assert view["shots"][0]["video_state"] == "waiting_provider"
    assert view["shots"][0]["remote_task_id"] == "new-task"
    assert view["shots"][0]["video_regeneration_available"] is False
    calls = []
    with pytest.raises(service.StoryboardError):
        service.regenerate_video(graph["id"], 1, **approval(view), transport=accepted(calls))
    assert calls == [] and service.get_graph(graph["id"]) == raw


def test_poll_running_restores_unknown_job_without_regeneration(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    old_id = graph["shots"][0]["video_job_id"]
    jobs.mark_unknown(old_id, reason="prior query uncertain")
    service._update_shot(graph["id"], 1, video_state="unknown")
    result = service.poll_shot(graph["id"], 1, transport=lambda request: {"status_code": 200,
        "json": {"output": {"task_id": "new-task", "task_status": "RUNNING"}}})
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert result["shots"][0]["video_regeneration_available"] is False
    assert jobs.get_job(old_id)["state"] == "waiting_provider"


def test_stitch_queue_and_video_regeneration_share_submission_lock(prepared, monkeypatch):
    from app.short_video import media
    service, graph, _ = failed_video(prepared, monkeypatch)
    for shot in graph["shots"]:
        service._update_shot(graph["id"], shot["shot_index"], video_state="succeeded", clip={"storage_uri": "fixture.mp4"})
    graph = service.project(service.get_graph(graph["id"]))
    service._mutate(graph["id"], lambda g: g.update(clip_approval={"clip_digest": graph["clip_digest"]}))
    entered, finish = threading.Event(), threading.Event()

    def waiting(graph):
        entered.set()
        assert finish.wait(5)

    monkeypatch.setattr(media, "verify_clips", waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stitching = pool.submit(service.queue_stitch, graph["id"])
        try:
            assert entered.wait(5)
            with pytest.raises(service.StoryboardError) as error:
                service.regenerate_video(graph["id"], 1, **approval(graph), transport=accepted([]))
            assert error.value.code == "STORYBOARD_SUBMISSION_BUSY"
        finally:
            finish.set()
        assert stitching.result(timeout=5)["state"] == "stitch_queued"


def test_normal_video_queued_replay_does_not_double_reserve_budget(prepared, monkeypatch):
    from app.short_video import video_regeneration as vr
    service, graph, _ = video_ready(prepared, monkeypatch)
    send = vr.send
    monkeypatch.setattr(vr, "send", lambda *a, **kw: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        service.submit_videos(graph["id"], transport=accepted([]))
    monkeypatch.setattr(vr, "send", send)
    # Other tests share this isolated schema; reserve room for exactly the one
    # remaining unsent shot, counting all outstanding attempts in this test run.
    day = service.now_iso()[:10]
    reserved = [j for j in st.all_rows("provider_jobs") if j.get("billable")
        and str(j.get("created_at", ""))[:10] == day and j.get("state") != "blocked"
        and not (j.get("state") == "cancelled" and not j.get("settlement_required"))]
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", str(len(reserved) + 1))
    calls = []
    result = service.submit_videos(graph["id"], transport=accepted(calls))
    assert result["shots"][0]["video_state"] == "waiting_provider"
    assert len(calls) == 1


def test_remote_receipt_restore_does_not_mix_new_task_with_old_provider_ledger(prepared, monkeypatch):
    from app.short_video import video_regeneration as vr
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([], "old-task"))
    old_id = graph["shots"][0]["video_job_id"]
    service._update_shot(graph["id"], 1, video_state="sending", remote_task_id=None)
    entered, finish = threading.Event(), threading.Event()
    restore = vr.restore

    def waiting(*a, **kw):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert finish.wait(5)
        return restore(*a, **kw)

    monkeypatch.setattr(vr, "restore", waiting)
    calls = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        polling = pool.submit(service.poll_shot, graph["id"], 1, transport=accepted(calls))
        try:
            assert entered.wait(5)
            # The original task was positively rejected while the restore caller paused.
            service._settle(jobs.get_job(old_id), success=False, result={"remote_task_id": "old-task"})
            service._update_shot(graph["id"], 1, video_state="failed")
            current = service.project(service.get_graph(graph["id"]))
            new = service.regenerate_video(graph["id"], 1, **approval(current), transport=accepted([]))
        finally:
            finish.set()
        result = polling.result(timeout=5)
    assert calls == []
    assert result["shots"][0]["video_job_id"] == new["shots"][0]["video_job_id"]


def test_late_running_poll_returns_latest_attempt_without_overwriting_browser(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([], "old-task"))
    entered, finish = threading.Event(), threading.Event()

    def waiting(request):
        entered.set()
        assert finish.wait(5)
        return {"status_code": 200, "json": {"output": {"task_id": "old-task", "task_status": "RUNNING"}}}

    with ThreadPoolExecutor(max_workers=1) as pool:
        poll = pool.submit(service.poll_shot, graph["id"], 1, transport=waiting)
        try:
            assert entered.wait(5)
            service._settle(jobs.get_job(graph["shots"][0]["video_job_id"]), success=False, result={"remote_task_id": "old-task"})
            service._update_shot(graph["id"], 1, video_state="failed")
            current = service.project(service.get_graph(graph["id"]))
            new = service.regenerate_video(graph["id"], 1, **approval(current), transport=accepted([]))
        finally:
            finish.set()
        returned = poll.result(timeout=5)
    assert returned["shots"][0]["video_job_id"] == new["shots"][0]["video_job_id"]
    assert returned["shots"][0]["remote_task_id"] == "new-task"


def test_old_sender_cannot_rebind_new_video_before_call(prepared, monkeypatch):
    service, graph, _ = failed_video(prepared, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    update = service._update_shot
    entered, finish = threading.Event(), threading.Event()
    stopped = False

    def waiting(graph_id, index, **fields):
        nonlocal stopped
        if fields.get("video_state") == "sending" and not stopped:
            stopped = True
            entered.set()
            assert finish.wait(5)
        return update(graph_id, index, **fields)

    calls = []
    monkeypatch.setattr(service, "_update_shot", waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        sending = pool.submit(service.regenerate_video, graph["id"], 1, **approval(graph), transport=accepted(calls))
        try:
            assert entered.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            current = service.project(service.get_graph(graph["id"]))
            new = service.regenerate_video(graph["id"], 1, **approval(current, request_id="replace-paused"), transport=accepted(calls))
        finally:
            finish.set()
        sending.result(timeout=5)
    assert len(calls) == 1
    assert service.get_graph(graph["id"])["shots"][0]["video_job_id"] == new["shots"][0]["video_job_id"]


def test_remote_quota_failure_is_persisted_and_allows_explicit_regeneration(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    result = service.poll_shot(graph["id"], 1, transport=lambda request: {"status_code": 200,
        "json": {"output": {"task_id": "new-task", "task_status": "FAILED", "code": "Arrearage", "message": "private upstream"}}})
    shot = result["shots"][0]
    assert shot["video_state"] == "failed" and shot["video_regeneration_available"]
    assert shot["error"]["code"] == "WAN_QUOTA_EXHAUSTED"
    assert jobs.get_job(shot["video_job_id"])["error"]["code"] == "WAN_QUOTA_EXHAUSTED"
    assert 'private upstream' not in shot["error"]["message"]


def test_video_restore_does_not_reopen_already_failed_job_from_stale_receipt(prepared, monkeypatch):
    from app.short_video import video_regeneration as vr
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    cached = jobs.get_job(graph["shots"][0]["video_job_id"])
    service._settle(cached, success=False, result={"remote_task_id": "new-task"})
    service._update_shot(graph["id"], 1, video_state="failed")
    before = service.get_graph(graph["id"])
    assert vr.restore(graph, graph["shots"][0], cached) is False
    assert service.get_graph(graph["id"]) == before


def test_late_same_job_unknown_query_cannot_demote_successful_video(prepared, monkeypatch):
    from tests.short_video.test_stitch import _clip
    service, graph, root = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    path = root / 'terminal.mp4'
    _clip(path, "blue", 440, 2)
    data = path.read_bytes()
    entered, finish = threading.Event(), threading.Event()

    def late_unknown(request):
        entered.set()
        assert finish.wait(5)
        return {"status_code": 200, "json": {"output": {"task_id": "new-task", "task_status": "OTHER"}}}

    def success(request):
        if request['url'].endswith('/new-task'):
            return {"status_code": 200, "json": {"output": {"task_id": "new-task", "task_status": "SUCCEEDED", "video_url": "https://media.example/final.mp4"}}}
        return {"status_code": 200, "body": data}

    with ThreadPoolExecutor(max_workers=1) as pool:
        old_poll = pool.submit(service.poll_shot, graph["id"], 1, transport=late_unknown)
        try:
            assert entered.wait(5)
            successful = service.poll_shot(graph["id"], 1, transport=success)
        finally:
            finish.set()
        returned = old_poll.result(timeout=5)
    job = jobs.get_job(graph["shots"][0]["video_job_id"])
    assert job["state"] == "succeeded"
    assert returned["shots"][0]["video_state"] == "succeeded"
    assert returned["shots"][0]["clip"] == successful["shots"][0]["clip"]


def test_unknown_video_settlement_never_writes_success_without_result_digest(prepared, monkeypatch):
    service, graph, _ = video_ready(prepared, monkeypatch)
    graph = service.submit_videos(graph["id"], transport=accepted([]))
    job_id = graph["shots"][0]["video_job_id"]
    jobs.mark_unknown(job_id, reason="query interrupted")
    insert = st.insert_row

    def verify_atomic_result(conn, table, key, data, *a):
        if table == 'provider_jobs' and key == job_id and data.get('state') == 'succeeded':
            assert data.get('result') and data.get('result_digest'), 'Success must contain recoverable evidence at every write'
        return insert(conn, table, key, data, *a)

    monkeypatch.setattr(st, 'insert_row', verify_atomic_result)
    service._settle(jobs.get_job(job_id), success=True, result={"remote_task_id": "new-task", "clip": {"file_hash": "sha256:fixture"}})
    current = jobs.get_job(job_id)
    assert current['state'] == 'succeeded' and current['result_digest'] == jobs.args_digest(current['result'])
    assert current['settlement_required'] is True
