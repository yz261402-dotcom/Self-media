"""Explicit single-shot retries retain the unknown charge and never resend on replay."""
import copy
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from app import jobs, storage as st
from app.avatar import person_image
from app.main import app
from tests.short_video.test_storyboard import prepared, approve_images, image_transport, ready_images, attach_storyboard_identity


def stalled(prepared):
    service, graph, root = prepared
    approve_images(service, graph)
    graph = service.generate_images(graph["id"], transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    return service, graph, root


def approval(graph, *, request_id="retry-one", **changes):
    return {"actor_id": "user_test", "paid_ack": True, "duplicate_charge_ack": True,
            "plan_digest": graph["plan_digest"], "expected_image_job_id": graph["shots"][0]["image_job_id"],
            "request_id": request_id, "ceiling_fen": graph["image_ceiling_fen_per_shot"], **changes}


@pytest.mark.parametrize("changes", [{"paid_ack": False}, {"duplicate_charge_ack": False},
    {"actor_id": "agent"}, {"plan_digest": "old-plan"}, {"request_id": ""}, {"ceiling_fen": 0}])
def test_regeneration_requires_bound_explicit_approval(prepared, changes):
    service, graph, _ = stalled(prepared)
    calls = []
    before = st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]])
    with pytest.raises(service.StoryboardError):
        service.regenerate_image(graph["id"], 1, **approval(graph, **changes), transport=image_transport(calls))
    assert calls == []
    assert st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]]) == before


def test_unknown_image_regeneration_creates_only_one_new_call_and_retains_original(prepared):
    service, graph, root = stalled(prepared)
    old = jobs.get_job(graph["shots"][0]["image_job_id"])
    assert graph["shots"][0]["image_regeneration_available"] is True
    calls = []
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    shot = result["shots"][0]
    assert len(calls) == 1
    assert shot["image_state"] == "succeeded" and shot["image_attempt"] == 1
    assert shot["image_job_id"] != old["id"]
    assert result["shots"][1]["image_state"] == "pending"
    assert shot["image_history"][0]["image_job_id"] == old["id"]
    assert jobs.get_job(old["id"]) == old and old["state"] == "unknown" and old["settlement_required"]
    new = jobs.get_job(shot["image_job_id"])
    assert new["state"] == "succeeded" and new["settlement_required"]
    assert new["image_regeneration"]["previous_job_id"] == old["id"]
    assert (root / shot["image"]["storage_uri"]).is_file()
    assert new["image_receipt"]["storage_uri"].endswith("shot_001_attempt_001.receipt.json")


def test_replaying_regeneration_request_does_not_generate_or_change_history(prepared):
    service, graph, _ = stalled(prepared)
    calls = []
    first = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    second = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert second == first
    assert len(calls) == 1
    assert len(second["shots"][0]["image_history"]) == 1


def test_new_request_using_stale_original_job_is_rejected(prepared):
    service, graph, _ = stalled(prepared)
    calls = []
    service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_image(graph["id"], 1, **approval(graph, request_id="another-request"), transport=image_transport(calls))
    assert error.value.code == "STORYBOARD_IMAGE_ATTEMPT_CHANGED"
    assert len(calls) == 1


def test_retry_request_id_cannot_be_reused_with_changed_approval(prepared):
    service, graph, _ = stalled(prepared)
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport([]))
    calls = []
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_image(graph["id"], 1, **approval(result), transport=image_transport(calls))
    assert error.value.code == "STORYBOARD_IMAGE_RETRY_CONFLICT"
    assert calls == []


def test_successful_image_regeneration_preserves_original_file_and_clears_video_approval(prepared):
    service, graph, root = prepared
    graph = ready_images(service, graph)
    original = copy.deepcopy(graph["shots"][0])
    original_file = root / original["image"]["storage_uri"]
    original_bytes = original_file.read_bytes()
    service.approve_videos(graph["id"], actor_id="user_test", paid_ack=True, image_review_ack=True,
        task_graph_digest=graph["task_graph_digest"], total_ceiling_fen=graph["estimate"]["video_total_fen"])
    calls = []
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert len(calls) == 1
    assert result["video_approval"] is None and result["clip_approval"] is None
    assert result["shots"][0]["image"]["storage_uri"] != original["image"]["storage_uri"]
    assert original_file.read_bytes() == original_bytes
    assert result["shots"][0]["image_history"][0]["image"]["storage_uri"] == original["image"]["storage_uri"]
    assert result["shots"][1] == graph["shots"][1]


def test_unknown_retry_stays_unknown_until_another_explicit_approval(prepared):
    service, graph, _ = stalled(prepared)
    calls = []

    def disconnected(*a, **kw):
        calls.append(1)
        raise TimeoutError()

    first = service.regenerate_image(graph["id"], 1, **approval(graph), transport=disconnected)
    assert first["shots"][0]["image_state"] == "unknown"
    assert service.regenerate_image(graph["id"], 1, **approval(graph), transport=disconnected) == first
    assert len(calls) == 1
    second = service.regenerate_image(graph["id"], 1, **approval(first, request_id="retry-two"), transport=image_transport([]))
    assert second["shots"][0]["image_state"] == "succeeded"
    assert second["shots"][0]["image_attempt"] == 2
    assert len(second["shots"][0]["image_history"]) == 2


def test_retry_budget_block_does_not_call_provider(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "0")
    calls = []
    with pytest.raises(service.StoryboardError):
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert calls == []


def test_regeneration_checks_only_single_shot_budget_not_full_batch(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "100")
    calls = []
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert result["shots"][1]["image_state"] == "pending"
    assert len(calls) == 1


def test_blocked_retry_preserves_current_successful_image(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = ready_images(service, graph)
    before = service.get_graph(graph["id"])
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "0")
    calls = []
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert error.value.code == "BUDGET_NOT_AUTHORIZED"
    assert service.get_graph(graph["id"]) == before
    assert calls == []


def test_completed_retry_replay_restores_graph_after_update_interruption(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    original = service._update_shot
    interrupted = False

    def stop(graph_id, index, **fields):
        nonlocal interrupted
        if fields.get("image_state") == "succeeded" and not interrupted:
            interrupted = True
            raise OSError("interrupted graph update")
        return original(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", stop)
    calls = []
    with pytest.raises(OSError):
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    waiting = service.get_graph(graph["id"])["shots"][0]
    assert waiting["image_state"] == "sending" and jobs.get_job(waiting["image_job_id"])["state"] == "succeeded"
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert result["shots"][1]["image_state"] == "pending"
    assert len(calls) == 1


def test_concurrent_regeneration_cannot_submit_two_paid_calls(prepared):
    service, graph, _ = stalled(prepared)
    entered, finish = threading.Event(), threading.Event()
    calls = []
    delegate = image_transport(calls)

    def waiting(request, **kwargs):
        entered.set()
        assert finish.wait(5)
        return delegate(request, **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.regenerate_image, graph["id"], 1, **approval(graph), transport=waiting)
        try:
            assert entered.wait(5)
            with pytest.raises(service.StoryboardError) as error:
                service.regenerate_image(graph["id"], 1, **approval(graph, request_id="parallel"), transport=image_transport(calls))
            assert error.value.code == "STORYBOARD_SUBMISSION_BUSY"
        finally:
            finish.set()
        assert running.result(timeout=5)["shots"][0]["image_state"] == "succeeded"
    assert len(calls) == 1
    assert len(st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]])) == 2


def test_retry_can_resume_queued_intent_after_interruption_before_graph_binding(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    original = st.insert_row
    interrupted = False

    def stop(conn, table, key, data, *args):
        nonlocal interrupted
        if table == "media_jobs" and key == graph["id"] and data["shots"][0].get("image_attempt") == 1 and not interrupted:
            interrupted = True
            raise OSError("interrupted binding")
        return original(conn, table, key, data, *args)

    monkeypatch.setattr(st, "insert_row", stop)
    calls = []
    with pytest.raises(OSError):
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert calls == []
    assert service.get_graph(graph["id"])["shots"][0]["image_job_id"] == graph["shots"][0]["image_job_id"]
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "2")
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "succeeded" and len(calls) == 1
    assert len(st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]])) == 2


def test_retry_replay_can_finish_bound_job_that_has_never_been_leased(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    original = service._generate_image_job
    monkeypatch.setattr(service, "_generate_image_job", lambda *a, **kw: (_ for _ in ()).throw(SystemExit()))
    calls = []
    with pytest.raises(SystemExit):
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    current = service.get_graph(graph["id"])["shots"][0]
    assert jobs.get_job(current["image_job_id"])["state"] == "queued"
    monkeypatch.setattr(service, "_generate_image_job", original)
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert len(calls) == 1


def test_retry_uses_approved_identity_reference(prepared):
    service, original, root = prepared
    _, hero, data, _ = attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    graph = service.generate_images(graph["id"], transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    calls = []
    result = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert len(calls) == 1
    assert calls[0]["image_bytes"] == data and calls[0]["reference_sha256"] == hero["file_hash"]
    assert "服装约束" in calls[0]["prompt"] and "不换装" in calls[0]["prompt"]
    assert result["shots"][0]["image"]["identity_reference"]["asset_hash"] == hero["file_hash"]


def test_regeneration_after_crashed_sender_requires_expired_lease_and_explicit_ack(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    with pytest.raises(SystemExit):
        service.generate_images(graph["id"], transport=lambda *a, **kw: (_ for _ in ()).throw(SystemExit()))
    sending = service.get_graph(graph["id"])
    assert sending["shots"][0]["image_state"] == "sending"
    assert service.project(sending)["shots"][0]["image_regeneration_available"] is False
    clock[0] += service.IMAGE_LEASE_SECONDS + 1
    before = jobs.get_job(sending["shots"][0]["image_job_id"])
    view = service.project(sending)
    assert view["shots"][0]["image_state"] == "unknown"
    assert view["state"] == "unknown"
    assert view["shots"][0]["image_regeneration_available"] is True
    assert service.get_graph(graph["id"]) == sending
    assert jobs.get_job(before["id"]) == before
    calls = []
    with pytest.raises(service.StoryboardError):
        service.regenerate_image(graph["id"], 1, **approval(view, duplicate_charge_ack=False), transport=image_transport(calls))
    result = service.regenerate_image(graph["id"], 1, **approval(view), transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert len(calls) == 1
    assert jobs.get_job(before["id"]) == before


def test_video_job_created_before_graph_binding_still_prevents_image_regeneration(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user_test", paid_ack=True, image_review_ack=True,
        task_graph_digest=graph["task_graph_digest"], total_ceiling_fen=graph["estimate"]["video_total_fen"])
    original = service._update_shot

    def stop(graph_id, index, **fields):
        if fields.get("video_state") == "sending":
            raise SystemExit()
        return original(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", stop)
    with pytest.raises(SystemExit):
        service.submit_videos(graph["id"], transport=lambda *a, **kw: pytest.fail("Interrupted before remote video call"))
    assert service.get_graph(graph["id"])["shots"][0]["video_state"] == "pending"
    calls = []
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert error.value.code == "STORYBOARD_IMAGE_RETRY_VIDEO_STARTED"
    assert calls == []
    assert service.project(service.get_graph(graph["id"]))["shots"][0]["image_regeneration_available"] is False


def test_late_original_request_cannot_mark_replacement_image_unknown(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    entered, finish = threading.Event(), threading.Event()

    def delayed_failure(*a, **kw):
        entered.set()
        assert finish.wait(5)
        raise TimeoutError("original request disconnected")

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.generate_images, graph["id"], transport=delayed_failure)
        try:
            assert entered.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            current = service.project(service.get_graph(graph["id"]))
            replacement = service.regenerate_image(graph["id"], 1, **approval(current), transport=image_transport([]))
            new_job_id = replacement["shots"][0]["image_job_id"]
        finally:
            finish.set()
        first.result(timeout=5)
    result = service.project(service.get_graph(graph["id"]))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert result["shots"][0]["image_job_id"] == new_job_id
    assert result["shots"][0]["image"] == replacement["shots"][0]["image"]
    assert result["shots"][0]["error"] is None


def test_late_receipt_recovery_cannot_replace_newer_image(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    original_store = service._store
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    graph = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport([]))
    monkeypatch.setattr(service, "_store", original_store)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    extract = person_image._extract_bytes
    entered, finish = threading.Event(), threading.Event()

    def delayed_extract(*a, **kw):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert finish.wait(5)
        return extract(*a, **kw)

    monkeypatch.setattr(person_image, "_extract_bytes", delayed_extract)
    with ThreadPoolExecutor(max_workers=1) as pool:
        recovering = pool.submit(service.recover_image, graph["id"], 1)
        try:
            assert entered.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            replacement = service.regenerate_image(graph["id"], 1,
                **approval(graph, request_id="replacement-after-recovery"), transport=image_transport([]))
        finally:
            finish.set()
        recovering.result(timeout=5)
    result = service.project(service.get_graph(graph["id"]))
    assert result["shots"][0]["image_state"] == "succeeded"
    assert result["shots"][0]["image_job_id"] == replacement["shots"][0]["image_job_id"]
    assert result["shots"][0]["image"] == replacement["shots"][0]["image"]


def test_queued_retry_has_one_sender_when_old_request_resumes_after_replay(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    first_paused, first_resume, sending, finish = [threading.Event() for _ in range(4)]
    helper = service._generate_image_job
    paused = False

    def stop_before_lease(*a, **kw):
        nonlocal paused
        if not paused:
            paused = True
            first_paused.set()
            assert first_resume.wait(5)
        return helper(*a, **kw)

    calls = []
    delegate = image_transport([])

    def provider(request, **kwargs):
        calls.append(request)
        sending.set()
        assert finish.wait(5)
        return delegate(request, **kwargs)

    monkeypatch.setattr(service, "_generate_image_job", stop_before_lease)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.regenerate_image, graph["id"], 1, **approval(graph), transport=provider)
        try:
            assert first_paused.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            replay = pool.submit(service.regenerate_image, graph["id"], 1, **approval(graph), transport=provider)
            assert sending.wait(5)
            first_resume.set()
            # A resumed caller must return promptly while the unique sender waits.
            first.result(timeout=3)
            assert len(calls) == 1
        finally:
            first_resume.set()
            finish.set()
        assert replay.result(timeout=5)["shots"][0]["image_state"] == "succeeded"


def test_old_sender_cannot_rebind_replacement_before_its_network_call(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    entered, finish = threading.Event(), threading.Event()
    update = service._update_shot
    stopped = False

    def pause_sending(graph_id, index, **fields):
        nonlocal stopped
        if fields.get("image_state") == "sending" and not stopped:
            stopped = True
            entered.set()
            assert finish.wait(5)
        return update(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", pause_sending)
    calls = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        original = pool.submit(service.regenerate_image, graph["id"], 1, **approval(graph), transport=image_transport(calls))
        try:
            assert entered.wait(5)
            clock[0] += service.IMAGE_LEASE_SECONDS + 1
            current = service.project(service.get_graph(graph["id"]))
            replacement = service.regenerate_image(graph["id"], 1,
                **approval(current, request_id="replace-paused-sender"), transport=image_transport(calls))
        finally:
            finish.set()
        original.result(timeout=5)
    assert len(calls) == 1
    result = service.project(service.get_graph(graph["id"]))
    assert result["shots"][0]["image_job_id"] == replacement["shots"][0]["image_job_id"]
    assert result["shots"][0]["image_state"] == "succeeded"
    assert result["shots"][0]["image"] == replacement["shots"][0]["image"]


@pytest.mark.parametrize("field,value", [("video_state", "sending"), ("remote_task_id", "remote"),
    ("video_job_id", "video-job"), ("clip", {"storage_uri": "old.mp4"})])
def test_started_video_prevents_regeneration(prepared, field, value):
    service, graph, _ = stalled(prepared)
    service._update_shot(graph["id"], 2, **{field: value})
    calls = []
    with pytest.raises(service.StoryboardError) as error:
        service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport(calls))
    assert error.value.code == "STORYBOARD_IMAGE_RETRY_VIDEO_STARTED"
    assert calls == []
    assert service.project(service.get_graph(graph["id"]))["shots"][0]["image_regeneration_available"] is False


def test_regenerated_image_receipt_recovers_latest_attempt_without_new_call(prepared, monkeypatch):
    service, graph, root = stalled(prepared)
    original_store = service._store
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    first = service.regenerate_image(graph["id"], 1, **approval(graph), transport=image_transport([]))
    assert first["shots"][0]["image_recovery_available"]
    monkeypatch.setattr(service, "_store", original_store)
    monkeypatch.setattr(person_image, "httpx_transport", lambda *a, **kw: pytest.fail("Recovery must not generate"))
    recovered = service.recover_image(graph["id"], 1)
    assert recovered["shots"][0]["image_state"] == "succeeded"
    assert recovered["shots"][0]["image_attempt"] == 1
    assert recovered["shots"][0]["image"]["storage_uri"].endswith("shot_001_attempt_001.png")
    assert recovered["shots"][1]["image_state"] == "pending"


def test_regeneration_http_endpoint_requires_confirmation_and_work_binding(prepared, monkeypatch):
    service, graph, _ = stalled(prepared)
    monkeypatch.setattr(person_image, "httpx_transport", image_transport([]))
    client = TestClient(app)
    path = f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}/shots/1/image/regenerate"
    assert client.post(path, json={}).status_code == 409
    assert client.post(path.replace(graph["work_id"], "wrong-work"), json=approval(graph)).status_code == 404
    result = client.post(path, json=approval(graph))
    assert result.status_code == 200
    assert result.json()["shots"][0]["image_state"] == "succeeded"
    assert client.get(result.json()["shots"][0]["image"]["preview_url"]).status_code == 200
    before = st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]])
    assert client.post(path, json=approval(graph)).status_code == 200
    assert st.all_rows("provider_jobs", "work_id=?", [graph["work_id"]]) == before
