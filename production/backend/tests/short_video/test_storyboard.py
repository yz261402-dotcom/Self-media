"""Wan storyboard graphs use isolated storage and injected provider transports."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import hashlib
import json
import threading

import pytest
from PIL import Image

from app import jobs, storage as st
from app.avatar import person_image
from app.content import contracts as cc, selection


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    from app.short_video import service
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "10000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "10000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "10000")
    monkeypatch.setenv(person_image.KEY_ENV, "test-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "test-key")
    monkeypatch.setenv("AVATAR_STUDIO_WAN_WORKSPACE_ID", "ws-test")
    monkeypatch.setattr(selection, "current_selection_valid", lambda wid: True)
    monkeypatch.setattr(selection, "project_batch", lambda wid: dict(st.get("script_batches", wid), ready_to_present=True))
    wid, sid = st.new_id("work"), st.new_id("script")
    shots = [{"shot_index": index, "visual": f"Scene {index}", "dialogue": f"Line {index}",
              "sound_effects": "Birds", "duration_seconds": index + 1, "prompt": f"Camera {index}"}
             for index in (1, 2)]
    version = {"id": sid, "work_id": wid, "slot": 1, "title": "Approved storyboard", "description": "Short story",
               "tags": ["story"], "text": "Approved lines", "content_route": "short_video_storyboard",
               "storyboard": shots}
    st.put("works", wid, {"id": wid, "name": "Storyboard test", "script_version_id": sid, "content_revision": 7,
                          "brief": {"content_route": "short_video_storyboard"}})
    st.put("script_versions", sid, version, {"work_id": wid})
    st.put("script_batches", wid, {"source_kind": "llm", "candidates": [version,
        dict(version, id=st.new_id("script"), slot=2), dict(version, id=st.new_id("script"), slot=3)]})
    graph = service.create_graph(wid, image_ceiling_fen_per_shot=100)
    return service, graph, tmp_path


def image_transport(calls):
    picture = io.BytesIO()
    Image.new("RGB", (480, 720), "green").save(picture, "PNG")
    def invoke(request, **kwargs):
        calls.append(request)
        return {"http_status": 200, "body": {"id": "img-return", "data": [
            {"b64_json": base64.b64encode(picture.getvalue()).decode("ascii")} ]}}
    return invoke


def approve_images(service, graph):
    return service.approve_images(graph["id"], actor_id="user", plan_digest=graph["plan_digest"],
                                  paid_ack=True, total_ceiling_fen=200)


def ready_images(service, graph):
    approve_images(service, graph)
    return service.generate_images(graph["id"], transport=image_transport([]))


def test_graph_has_ordered_shot_prompts_and_aggregate_quote(prepared):
    service, graph, _ = prepared
    assert [s["shot_index"] for s in graph["shots"]] == [1, 2]
    assert graph["estimate"]["video_total_fen"] == 300
    assert graph["estimate"]["image_ceiling_total_fen"] == 200
    assert graph["estimate"]["total_ceiling_fen"] == 500
    assert graph["estimate"]["image_price_known"] is False
    assert all("Line" in s["wan_prompt"] and "Birds" in s["wan_prompt"] for s in graph["shots"])
    assert service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=100)["id"] == graph["id"]


def test_image_calls_require_bound_confirmation(prepared):
    service, graph, _ = prepared
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_IMAGE_APPROVAL_REQUIRED"
    assert calls == []


def test_image_stage_persists_each_asset_and_does_not_repeat(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    calls = []
    first = service.generate_images(graph["id"], transport=image_transport(calls))
    second = service.generate_images(graph["id"], transport=image_transport(calls))
    assert first["state"] == second["state"] == "images_ready"
    assert len(calls) == 2
    for shot in first["shots"]:
        assert shot["image"]["asset_hash"].startswith("sha256:")
        assert jobs.get_job(shot["image_job_id"])["state"] == "succeeded"


@pytest.mark.parametrize("interruption", [SystemExit, OSError])
def test_completed_image_job_recovers_after_graph_update_crash(prepared, monkeypatch, interruption):
    service, graph, _ = prepared
    approve_images(service, graph)
    calls = []
    original_update = service._update_shot
    crashed = False

    def interrupt_after_job_completion(graph_id, index, **fields):
        nonlocal crashed
        if fields.get("image_state") == "succeeded" and not crashed:
            crashed = True
            raise interruption("process stopped after image job completion")
        return original_update(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", interrupt_after_job_completion)
    with pytest.raises(interruption):
        service.generate_images(graph["id"], transport=image_transport(calls))
    stalled = service.get_graph(graph["id"])["shots"][0]
    assert stalled["image_state"] == "sending"
    assert jobs.get_job(stalled["image_job_id"])["state"] == "succeeded"

    result = service.generate_images(graph["id"], transport=image_transport(calls))
    assert result["state"] == "images_ready"
    assert result["shots"][0]["image"]["asset_hash"] == jobs.get_job(stalled["image_job_id"])["result"]["image"]["asset_hash"]
    assert len(calls) == 2


def test_completed_image_job_with_missing_file_stays_unknown_without_reposting(prepared, monkeypatch):
    service, graph, root = prepared
    approve_images(service, graph)
    calls = []
    original_update = service._update_shot
    crashed = False

    def interrupt_after_job_completion(graph_id, index, **fields):
        nonlocal crashed
        if fields.get("image_state") == "succeeded" and not crashed:
            crashed = True
            raise SystemExit("process stopped after image job completion")
        return original_update(graph_id, index, **fields)

    monkeypatch.setattr(service, "_update_shot", interrupt_after_job_completion)
    with pytest.raises(SystemExit):
        service.generate_images(graph["id"], transport=image_transport(calls))
    image = jobs.get_job(service.get_graph(graph["id"])["shots"][0]["image_job_id"])["result"]["image"]
    (root / image["storage_uri"]).unlink()

    result = service.generate_images(graph["id"], transport=image_transport(calls))
    assert result["shots"][0]["image_state"] == "unknown"
    assert result["shots"][0]["image"] is None
    assert len(calls) == 1


def test_image_timeout_is_unknown_and_never_reposted(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    calls = []
    def timeout(request, **kwargs):
        calls.append(request)
        raise TimeoutError()
    first = service.generate_images(graph["id"], transport=timeout)
    second = service.generate_images(graph["id"], transport=timeout)
    assert len(calls) == 1
    assert first["state"] == second["state"] == "unknown"
    assert jobs.get_job(first["shots"][0]["image_job_id"])["state"] == "unknown"


def test_image_network_error_keeps_specific_reason_and_job_number(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)

    def disconnected(*args, **kwargs):
        raise person_image.PersonImageBlocked("IMAGE_PROVIDER_UNREACHABLE",
            "调用图像生成服务失败：ReadTimeout（未取得结论，未重试）",
            detail={"stage": "generation", "exception_type": "ReadTimeout"})

    result = service.generate_images(graph["id"], transport=disconnected)
    shot = result["shots"][0]
    assert "ReadTimeout" in shot["error"]["message"]
    assert shot["image_job_id"] in shot["error"]["message"]
    job = jobs.get_job(shot["image_job_id"])
    assert job["error"]["code"] == "IMAGE_PROVIDER_UNREACHABLE"
    assert "ReadTimeout" in job["unknown_reason"]
    assert shot["image_recovery_available"] is False


def test_image_download_recovers_original_receipt_without_new_generation(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    calls = []
    delegate = image_transport(calls)
    data = person_image._extract_bytes(delegate({}, timeout=0)["body"]["data"][0])
    calls.clear()

    def response(request, **kwargs):
        calls.append(request)
        return {"http_status": 200, "body": {"id": "original-image", "data": [{"url": "https://image.example/original.png"}]}}

    def unavailable_download(*args, **kwargs):
        raise person_image.PersonImageBlocked("IMAGE_PROVIDER_UNREACHABLE", "下载生成结果失败：ReadTimeout",
            detail={"stage": "download", "exception_type": "ReadTimeout"})

    monkeypatch.setattr(person_image, "_extract_bytes", unavailable_download)
    stalled = service.generate_images(graph["id"], transport=response)
    assert stalled["shots"][0]["image_recovery_available"] is True
    receipt = jobs.get_job(stalled["shots"][0]["image_job_id"])["image_receipt"]
    assert receipt["provider_response_id"] == "original-image"
    monkeypatch.setattr(person_image, "httpx_transport", lambda *a, **kw: pytest.fail("Recovery must not generate images"))
    monkeypatch.setattr(person_image, "_extract_bytes", lambda *a, **kw: data)
    recovered = service.recover_image(graph["id"], 1)
    assert recovered["shots"][0]["image_state"] == "succeeded"
    assert recovered["shots"][0]["image"]["provider_response_id"] == "original-image"
    assert recovered["shots"][0]["error"] is None
    assert recovered["shots"][1]["image_state"] == "pending"
    completed = jobs.get_job(stalled["shots"][0]["image_job_id"])
    assert completed["state"] == "succeeded" and completed["result_digest"]
    assert completed["settlement_required"] is True
    assert service.recover_image(graph["id"], 1)["shots"][0]["image"] == recovered["shots"][0]["image"]
    assert len(calls) == 1


def test_image_recovery_rejects_changed_receipt_before_download(prepared, monkeypatch):
    service, graph, root = prepared
    approve_images(service, graph)
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    # Receipt storage is separate from writing the final image.
    stalled = service.generate_images(graph["id"], transport=image_transport([]))
    receipt = jobs.get_job(stalled["shots"][0]["image_job_id"])["image_receipt"]
    (root / receipt["storage_uri"]).write_text('{"body": "changed"}', encoding="utf-8")
    monkeypatch.setattr(person_image, "_extract_bytes", lambda *a, **kw: pytest.fail("Changed receipt must not be downloaded"))
    with pytest.raises(service.StoryboardError) as exc:
        service.recover_image(graph["id"], 1)
    assert exc.value.code == "STORYBOARD_IMAGE_RECEIPT_INVALID"
    assert jobs.get_job(stalled["shots"][0]["image_job_id"])["state"] == "unknown"


def test_image_recovery_without_receipt_never_reposts(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    stalled = service.generate_images(graph["id"], transport=lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(person_image, "httpx_transport", lambda *a, **kw: pytest.fail("Unknown job must not be resent"))
    with pytest.raises(service.StoryboardError) as exc:
        service.recover_image(graph["id"], 1)
    assert exc.value.code == "STORYBOARD_IMAGE_RECEIPT_MISSING"
    assert jobs.get_job(stalled["shots"][0]["image_job_id"])["state"] == "unknown"


def test_image_error_does_not_expose_exception_secrets(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    secret = "sk-private-test-credential"

    def failure(*args, **kwargs):
        raise person_image.PersonImageBlocked("IMAGE_PROVIDER_UNREACHABLE", "Bearer " + secret,
            detail={"stage": "generation", "exception_type": "ReadError", "url": "https://example/?key=" + secret})

    result = service.generate_images(graph["id"], transport=failure)
    job = jobs.get_job(result["shots"][0]["image_job_id"])
    assert "ReadError" in result["shots"][0]["error"]["message"]
    assert secret not in json.dumps(result) + json.dumps(job)


def test_image_recovery_does_not_overwrite_conflicting_local_asset(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    original = service._store
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    stalled = service.generate_images(graph["id"], transport=image_transport([]))
    monkeypatch.setattr(service, "_store", original)
    path = service._image_file(service.get_graph(graph["id"]), stalled["shots"][0])
    path.write_bytes(b"existing unrelated data")
    recovered = service.recover_image(graph["id"], 1)
    assert recovered["shots"][0]["image_state"] == "unknown"
    assert recovered["shots"][0]["error"]["code"] == "STORYBOARD_ARTIFACT_CONFLICT"
    assert path.read_bytes() == b"existing unrelated data"


def test_succeeded_image_can_be_restored_from_receipt_without_changing_asset(prepared, monkeypatch):
    service, graph, _ = prepared
    result = ready_images(service, graph)
    original = result["shots"][0]["image"]
    path = service._image_file(service.get_graph(graph["id"]), result["shots"][0])
    path.unlink()
    service._update_shot(graph["id"], 1, image_state="unknown", image=None)
    monkeypatch.setattr(person_image, "httpx_transport", lambda *a, **kw: pytest.fail("Restoring must not generate"))
    recovered = service.recover_image(graph["id"], 1)
    assert recovered["shots"][0]["image_state"] == "succeeded"
    assert recovered["shots"][0]["image"] == original
    assert recovered["state"] == "images_ready"


def test_image_recovery_keeps_identity_reference_and_restores_after_write_error(prepared, monkeypatch):
    service, original_graph, root = prepared
    _, hero, _, _ = attach_storyboard_identity(original_graph, root)
    graph = service.create_graph(original_graph["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    original = service._store
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    stalled = service.generate_images(graph["id"], transport=image_transport([]))
    monkeypatch.setattr(service, "_store", original)
    monkeypatch.setattr(person_image, "httpx_transport", lambda *a, **kw: pytest.fail("Recovery must not submit"))
    recovered = service.recover_image(graph["id"], 1)
    assert recovered["shots"][0]["image_state"] == "succeeded"
    assert recovered["shots"][0]["image"]["identity_reference"]["asset_hash"] == hero["file_hash"]
    assert recovered["shots"][1]["image_state"] == "pending"


def test_recovery_refuses_receipt_from_other_shot(prepared, monkeypatch):
    service, graph, root = prepared
    approve_images(service, graph)
    monkeypatch.setattr(service, "_store", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk failed")))
    stalled = service.generate_images(graph["id"], transport=image_transport([]))
    job = jobs.get_job(stalled["shots"][0]["image_job_id"])
    path = root / job["image_receipt"]["storage_uri"]
    doc = json.loads(path.read_bytes())
    doc["shot_index"] = 2
    data = json.dumps(doc).encode()
    path.write_bytes(data)
    job["image_receipt"]["file_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
    st.put("provider_jobs", job["id"], job, {"work_id": graph["work_id"]})
    monkeypatch.setattr(person_image, "_extract_bytes", lambda *a, **kw: pytest.fail("Foreign receipt must not be parsed"))
    with pytest.raises(service.StoryboardError) as exc:
        service.recover_image(graph["id"], 1)
    assert exc.value.code == "STORYBOARD_IMAGE_RECEIPT_INVALID"


def test_legacy_image_unknown_explains_missing_evidence_on_read_without_mutation(prepared):
    service, graph, _ = prepared
    service._update_shot(graph["id"], 1, image_state="unknown", image_job_id="legacy-job",
        error={"code": "IMAGE_PROVIDER_UNREACHABLE", "message": "图片生成结果未知，保留作业记录，请先核对"})
    before = service.get_graph(graph["id"])
    view = service.project(before)
    assert "连接" in view["shots"][0]["error"]["message"]
    assert "供应商后台" in view["shots"][0]["error"]["message"]
    assert view["shots"][0]["image_job_id"] in view["shots"][0]["error"]["message"]
    assert service.get_graph(graph["id"]) == before


def test_image_lease_covers_generation_and_result_download(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    clock = [1000]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    download = person_image._extract_bytes
    delegate = image_transport([])
    calls = []

    def transport(request, **kwargs):
        calls.append(request)
        clock[0] += kwargs["timeout"]
        response = delegate(request, **kwargs)
        response["body"]["data"][0] = {"url": "https://image.example/result.png"}
        return response

    def slow_download(item, timeout=person_image.DEFAULT_TIMEOUT_S):
        clock[0] += timeout
        if len(calls) == 1:
            with pytest.raises(service.StoryboardError) as exc:
                service.generate_images(graph["id"], transport=image_transport([]))
            assert exc.value.code == "STORYBOARD_SUBMISSION_BUSY"
        return download({"b64_json": image_transport_bytes}, timeout=timeout)

    image_transport_bytes = delegate({}, timeout=person_image.DEFAULT_TIMEOUT_S)["body"]["data"][0]["b64_json"]
    monkeypatch.setattr(person_image, "_extract_bytes", slow_download)
    result = service.generate_images(graph["id"], transport=transport)

    assert result["state"] == "images_ready"
    assert len(calls) == 2
    assert all(jobs.get_job(shot["image_job_id"])["state"] == "succeeded" for shot in result["shots"])


def test_content_revision_change_blocks_paid_call(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    work = st.get("works", graph["work_id"])
    work["content_revision"] += 1
    st.put("works", work["id"], work)
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_INPUT_CHANGED"


def test_wan_requires_image_review_and_total_quote_then_is_idempotent(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    calls = []
    def transport(request):
        calls.append(request)
        return {"status_code": 200, "json": {"output": {"task_id": "wan-" + str(len(calls))}}}
    with pytest.raises(service.StoryboardError) as exc:
        service.submit_videos(graph["id"], transport=transport)
    assert exc.value.code == "STORYBOARD_VIDEO_APPROVAL_REQUIRED"
    assert calls == []
    service.approve_videos(graph["id"], actor_id="user", task_graph_digest=graph["task_graph_digest"],
                           paid_ack=True, image_review_ack=True, total_ceiling_fen=300)
    first = service.submit_videos(graph["id"], transport=transport)
    second = service.submit_videos(graph["id"], transport=transport)
    assert first["state"] == second["state"] == "waiting_provider"
    assert len(calls) == 1
    assert [shot["remote_task_id"] for shot in second["shots"]] == ["wan-1", None]
    service._update_shot(graph["id"], 1, video_state="succeeded")
    third = service.submit_videos(graph["id"], transport=transport)
    assert len(calls) == 2
    assert [shot["remote_task_id"] for shot in third["shots"]] == ["wan-1", "wan-2"]
    assert all(len(call["json"]["input"]["media"]) == 1 and call["json"]["parameters"]["audio"] for call in calls)


def test_wan_unknown_submission_stops_batch_and_never_reposts(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user", task_graph_digest=graph["task_graph_digest"],
                           paid_ack=True, image_review_ack=True, total_ceiling_fen=300)
    calls = []
    def transport(request):
        calls.append(request)
        return {"status_code": 503}
    service.submit_videos(graph["id"], transport=transport)
    graph = service.submit_videos(graph["id"], transport=transport)
    assert graph["state"] == "unknown" and len(calls) == 1
    assert graph["shots"][1]["video_state"] == "pending"


def test_wan_replayed_intent_without_shot_binding_is_visible_unknown(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user", task_graph_digest=graph["task_graph_digest"],
                           paid_ack=True, image_review_ack=True, total_ceiling_fen=300)
    original = jobs.submit(work_id=graph["work_id"], capability="storyboard_wan", provider="dashscope_wan",
                           model="wan3.0-video", args={"interrupted": True},
                           idempotency_key=f"{graph['id']}:wan:1", billable=True, amount_fen=120,
                           namespace="wan-storyboard")
    calls = []
    result = service.submit_videos(graph["id"], transport=lambda request: calls.append(request))
    assert calls == []
    assert result["shots"][0]["video_state"] == "unknown"
    assert result["shots"][0]["video_job_id"] == original["id"]


def test_invalid_duration_is_rejected_before_graph_creation(prepared):
    service, graph, _ = prepared
    version = st.get("script_versions", graph["script_version_id"])
    version["storyboard"][0]["duration_seconds"] = 31
    st.put("script_versions", version["id"], version, {"work_id": graph["work_id"]})
    batch = st.get("script_batches", graph["work_id"])
    batch["candidates"][0] = version
    st.put("script_batches", graph["work_id"], batch)
    with pytest.raises(service.StoryboardError) as exc:
        service.create_graph(graph["work_id"])
    assert exc.value.code == "STORYBOARD_SHOT_INVALID"


def _submitted(service, graph, monkeypatch):
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user", task_graph_digest=graph["task_graph_digest"],
                           paid_ack=True, image_review_ack=True, total_ceiling_fen=300)
    serial = iter(("original-1", "original-2"))
    return service.submit_videos(graph["id"], transport=lambda request: {
        "status_code": 200, "json": {"output": {"task_id": next(serial)}}})


def test_recovery_queries_original_task_and_endpoint(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = _submitted(service, graph, monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WAN_WORKSPACE_ID", "ws-new-space")
    calls = []
    def transport(request):
        calls.append(request)
        return {"status_code": 200, "json": {"output": {"task_id": "original-1", "task_status": "RUNNING"}}}
    result = service.poll_shot(graph["id"], 1, transport=transport)
    assert calls[0]["method"] == "GET"
    assert "ws-test." in calls[0]["url"] and calls[0]["url"].endswith("/original-1")
    assert result["shots"][0]["remote_task_id"] == "original-1"


def test_query_mismatched_task_cannot_register_clip(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = _submitted(service, graph, monkeypatch)
    calls = []
    def transport(request):
        calls.append(request)
        return {"status_code": 200, "json": {"output": {"task_id": "unrelated-task", "task_status": "SUCCEEDED",
                                                         "video_url": "https://media.example/clip.mp4"}}}
    result = service.poll_shot(graph["id"], 1, transport=transport)
    assert len(calls) == 1 and result["shots"][0]["clip"] is None


def test_wan_download_is_verified_before_clip_registration(prepared, monkeypatch):
    service, graph, _ = prepared
    graph = _submitted(service, graph, monkeypatch)
    def transport(request):
        if request["url"].endswith("/original-1"):
            return {"status_code": 200, "json": {"output": {"task_id": "original-1", "task_status": "SUCCEEDED",
                                                             "video_url": "https://media.example/clip.mp4"}}}
        return {"status_code": 200, "body": b"<html>expired URL</html>"}
    with pytest.raises(Exception):
        service.poll_shot(graph["id"], 1, transport=transport)
    assert service.get_graph(graph["id"])["shots"][0]["clip"] is None


def test_stitch_requires_all_native_clips_and_user_review(prepared):
    service, graph, _ = prepared
    with pytest.raises(service.StoryboardError) as exc:
        service.approve_clips(graph["id"], actor_id="user", clip_digest=graph["clip_digest"], review_ack=True)
    assert exc.value.code == "STORYBOARD_CLIP_REVIEW_INVALID"


def test_failed_or_unknown_image_never_falls_back_to_placeholder(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    result = service.generate_images(graph["id"], transport=lambda *args, **kwargs: {"http_status": 200, "body": {"data": []}})
    assert result["state"] == "unknown"
    assert all(s["image"] is None for s in result["shots"])


def test_agent_actor_cannot_approve_paid_calls(prepared):
    service, graph, _ = prepared
    with pytest.raises(service.StoryboardError) as exc:
        service.approve_images(graph["id"], actor_id="agent", plan_digest=graph["plan_digest"],
                               paid_ack=True, total_ceiling_fen=200)
    assert exc.value.code == "STORYBOARD_IMAGE_APPROVAL_INVALID"


def test_tampered_current_batch_is_not_allowed_to_supply_unreviewed_version(prepared):
    service, graph, _ = prepared
    batch = st.get("script_batches", graph["work_id"])
    batch["candidates"][0]["storyboard"] = [{**batch["candidates"][0]["storyboard"][0], "visual": "Unreviewed replacement"}]
    st.put("script_batches", graph["work_id"], batch)
    with pytest.raises(service.StoryboardError):
        service.create_graph(graph["work_id"])


def test_all_three_current_candidates_required_even_with_selected_record(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    batch = st.get("script_batches", graph["work_id"])
    batch["candidates"] = batch["candidates"][:1]
    st.put("script_batches", graph["work_id"], batch)
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_INPUT_CHANGED"


def test_aggregate_image_budget_blocks_entire_batch_before_first_call(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "150")
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_BATCH_BUDGET_BLOCKED"
    assert calls == []


def test_storyboard_unlimited_mode_ignores_global_budget_caps(prepared, monkeypatch):
    service, graph, _ = prepared
    approve_images(service, graph)
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "1")
    calls = []
    result = service.generate_images(graph["id"], transport=image_transport(calls))
    assert result["state"] == "images_ready"
    assert len(calls) == len(graph["shots"])
    assert all(jobs.get_job(shot["image_job_id"])["state"] == "succeeded" for shot in result["shots"])


def test_storyboard_unlimited_mode_allows_zero_image_ceiling(prepared, monkeypatch):
    service, original, _ = prepared
    monkeypatch.setenv("AVATAR_STUDIO_WAN_STORYBOARD_BUDGET_UNLIMITED", "1")
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=0)
    assert graph["estimate"]["image_ceiling_total_fen"] == 0
    approved = service.approve_images(graph["id"], actor_id="user", plan_digest=graph["plan_digest"],
                                      paid_ack=True, total_ceiling_fen=0)
    assert approved["image_approval"]["total_ceiling_fen"] == 0


def test_unknown_graph_cannot_be_bypassed_by_changing_image_budget(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    service.generate_images(graph["id"], transport=lambda *a, **kw: {"http_status": 503})
    replacement = service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=200)
    service.approve_images(replacement["id"], actor_id="user", plan_digest=replacement["plan_digest"],
                           paid_ack=True, total_ceiling_fen=400)
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(replacement["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_RECONCILIATION_REQUIRED"
    assert calls == []


def _zombie_shot(service, graph, shot_index, monkeypatch):
    """留下一个"发送方已死且无回执"的僵尸镜头：作业仍 leased，但租约已过期。

    复刻进程在 acquire_lease 之后、回写之前崩溃的场景：镜头持久化为 sending，
    作业停在 leased 且没有 image_receipt，因此 recover() 永远取不回、也永远清不掉。
    """
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    from app.avatar import person_image

    def crash_before_response(request, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(person_image, "generate", crash_before_response)
    with pytest.raises(KeyboardInterrupt):
        service.generate_images(graph["id"], transport=lambda *a, **kw: {})
    monkeypatch.undo()
    shot = next(s for s in st.get("media_jobs", graph["id"])["shots"] if s["shot_index"] == shot_index)
    job = jobs.get_job(shot["image_job_id"])
    assert shot["image_state"] == "sending" and job["state"] == "leased" and not job.get("image_receipt")
    clock[0] += service.IMAGE_LEASE_SECONDS + 1
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    return job


def test_dead_lease_zombie_does_not_wedge_the_work(prepared, monkeypatch):
    """租约已过期且无回执的僵尸镜头不可回收、也不可重新生成，不能永久堵死该作品。"""
    service, graph, _ = prepared
    approve_images(service, graph)
    _zombie_shot(service, graph, 1, monkeypatch)

    replacement = service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=200)
    service.approve_images(replacement["id"], actor_id="user", plan_digest=replacement["plan_digest"],
                           paid_ack=True, total_ceiling_fen=400)
    calls = []
    result = service.generate_images(replacement["id"], transport=image_transport(calls))
    assert len(calls) == len(replacement["shots"])
    assert all(s["image_state"] == "succeeded" for s in result["shots"])


def test_dead_lease_zombie_still_blocks_while_sender_is_alive(prepared, monkeypatch):
    """发送方仍在飞（租约未过期）时必须继续拦，避免同一批镜头重复计费。"""
    service, graph, _ = prepared
    approve_images(service, graph)
    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    from app.avatar import person_image

    monkeypatch.setattr(person_image, "generate", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        service.generate_images(graph["id"], transport=lambda *a, **kw: {})
    monkeypatch.undo()

    replacement = service.create_graph(graph["work_id"], image_ceiling_fen_per_shot=200)
    service.approve_images(replacement["id"], actor_id="user", plan_digest=replacement["plan_digest"],
                           paid_ack=True, total_ceiling_fen=400)
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(replacement["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_RECONCILIATION_REQUIRED"
    assert calls == []


@pytest.mark.parametrize("field,value", [("wan_prompt", "Unapproved prompt"), ("duration_seconds", 30)])
def test_graph_shot_tampering_cannot_inherit_approval(prepared, field, value):
    service, graph, _ = prepared
    approve_images(service, graph)
    service._update_shot(graph["id"], 1, **{field: value})
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_GRAPH_CHANGED"


def test_graph_cost_tampering_cannot_reduce_budget_reservation(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    service._mutate(graph["id"], lambda g: g["estimate"].update(video_total_fen=1))
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_GRAPH_CHANGED"


def test_cancelled_graph_task_blocks_any_new_paid_submission(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    task = st.get("tasks", graph["task_id"])
    task["state"] = "cancelled"
    st.put("tasks", task["id"], task, {"work_id": graph["work_id"]})
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_INPUT_CHANGED"


def test_concurrent_image_submit_is_blocked_without_mutating_active_shot(prepared):
    service, graph, _ = prepared
    approve_images(service, graph)
    entered, release = threading.Event(), threading.Event()
    delegate = image_transport([])
    def transport(request, **kwargs):
        entered.set()
        assert release.wait(10)
        return delegate(request, **kwargs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.generate_images, graph["id"], transport=transport)
        assert entered.wait(10)
        try:
            with pytest.raises(service.StoryboardError) as exc:
                service.generate_images(graph["id"], transport=image_transport([]))
            assert exc.value.code == "STORYBOARD_SUBMISSION_BUSY"
            assert service.get_graph(graph["id"])["shots"][0]["image_state"] == "sending"
        finally:
            release.set()
        assert first.result()["state"] == "images_ready"


def attach_storyboard_identity(graph, root):
    from app.avatar import licensing, registry
    from app.contracts import HERO_ONLY_LIMITATIONS
    ident = registry.create_identity_candidate("分镜人物", "当前主角", origin="generated")
    output = io.BytesIO()
    Image.new("RGB", (480, 720), "blue").save(output, "PNG")
    data = output.getvalue()
    path = root / "assets" / ident["id"] / "hero.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    lic = licensing.register(subject=ident["id"], kind="image_source", scope="commercial_ok",
                             evidence="隔离测试人物", verified_by="user_test")
    hero = registry.register_asset(ident["id"], "hero", storage_uri=path.relative_to(root).as_posix(),
        file_hash="sha256:" + hashlib.sha256(data).hexdigest(), source_evidence="隔离测试图片", license_id=lic["id"])
    registry.record_review(hero["id"], reviewer="user_test", outcome="pass", identity_digest=hero["file_hash"], method="manual")
    registry.set_reference_policy(ident["id"], "hero_only", "user_test", reason="分镜测试",
                                  acknowledged=HERO_ONLY_LIMITATIONS)
    registry.freeze_identity(ident["id"], hero["id"], hero["file_hash"], reviewer="user_test", license_id=lic["id"])
    work = st.get("works", graph["work_id"])
    work.update(identity_id=ident["id"], variant_id=None)
    st.put("works", work["id"], work)
    return ident, hero, data, path


def test_selected_identity_is_sent_as_reference_bytes_to_every_shot(prepared):
    service, original, root = prepared
    ident, hero, data, _ = attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    calls = []
    result = service.generate_images(graph["id"], transport=image_transport(calls))
    assert result["state"] == "images_ready"
    assert graph["identity_reference"]["identity_id"] == ident["id"]
    assert graph["identity_reference"]["asset_id"] == hero["id"]
    assert graph["identity_reference"]["preview_url"].startswith("/api/v1/media?uri=")
    assert len(calls) == 2
    for request, shot in zip(calls, result["shots"]):
        assert request["endpoint"] == person_image.EDITS_PATH
        assert request["image_bytes"] == data
        assert request["reference_sha256"] == hero["file_hash"]
        assert "参考图中的同一人物" in request["prompt"]
        assert "不额外加入人脸" in request["prompt"]
        reference = service.get_graph(graph["id"])["identity_reference"]
        expected_args = {"graph_id": graph["id"], "shot_index": shot["shot_index"], "plan_digest": graph["plan_digest"],
                         "prompt": shot["image_prompt"], "size": graph["image_size"], "model": graph["image_model"],
                         "identity_reference": reference}
        assert jobs.get_job(shot["image_job_id"])["args_digest"] == jobs.args_digest(expected_args)
        assert shot["image"]["identity_reference"]["asset_hash"] == hero["file_hash"]
    service.generate_images(graph["id"], transport=image_transport(calls))
    assert len(calls) == 2


def test_every_image_and_video_prompt_locks_reference_outfit_despite_scene_conflicts(prepared):
    service, original, root = prepared
    _, hero, data, _ = attach_storyboard_identity(original, root)
    version = st.get("script_versions", original["script_version_id"])
    for shot, clothes in zip(version["storyboard"], ("主角穿深蓝夹克", "主角换成白色T恤")):
        shot["visual"] = clothes + "，她拿起手机。"
        shot["prompt"] = shot["visual"]
    st.put("script_versions", version["id"], version, {"work_id": version["work_id"]})
    batch = st.get("script_batches", original["work_id"])
    batch["candidates"][0] = version
    st.put("script_batches", original["work_id"], batch)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    assert graph.get("outfit_policy") == "reference_image"
    approve_images(service, graph)
    calls = []
    result = service.generate_images(graph["id"], transport=image_transport(calls))
    assert len(calls) == 2
    for request, shot in zip(calls, result["shots"]):
        assert request["image_bytes"] == data and request["reference_sha256"] == hero["file_hash"]
        for prompt in (request["prompt"], shot["wan_prompt"]):
            assert "服装约束" in prompt
            assert "颜色" in prompt and "款式" in prompt and "配饰" in prompt
            assert "优先于" in prompt and "不换装" in prompt
        # Raw approved scenes remain available for review, without rewriting the script.
        assert shot["visual"] in request["prompt"]


def test_reference_outfit_policy_is_bound_to_approval_digest(prepared):
    service, original, root = prepared
    attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    service._mutate(graph["id"], lambda g: g.update(outfit_policy=None))
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_GRAPH_CHANGED"
    assert calls == []


def test_reprepare_outfit_preserves_historical_graph_and_requires_new_fee_confirmation(prepared):
    service, original, root = prepared
    attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    legacy = service.get_graph(graph["id"])
    legacy.pop("outfit_policy", None)
    for shot in legacy["shots"]:
        shot["image_prompt"] = "历史人物图片提示"
        shot["wan_prompt"] = "历史视频提示"
    legacy["plan_digest"] = service._plan_digest(legacy)
    legacy["image_approval"] = {"plan_digest": legacy["plan_digest"]}
    st.put("media_jobs", legacy["id"], legacy, {"work_id": legacy["work_id"]})
    snapshot = service.get_graph(legacy["id"])
    replacement = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    assert replacement["id"] != legacy["id"]
    assert replacement.get("outfit_policy") == "reference_image"
    assert replacement["image_approval"] is None
    assert service.get_graph(legacy["id"]) == snapshot
    service.assert_current(snapshot)
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(replacement["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_IMAGE_APPROVAL_REQUIRED"
    assert calls == []


@pytest.mark.parametrize("change", ["identity", "version", "file", "missing_file"])
def test_reference_changes_block_before_any_paid_image_call(prepared, change):
    service, original, root = prepared
    ident, hero, _, path = attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    if change == "identity":
        work = st.get("works", graph["work_id"])
        work["identity_id"] = None
        st.put("works", work["id"], work)
    elif change == "version":
        current = st.get("identities", ident["id"])
        current["identity_version"] += 1
        st.put("identities", ident["id"], current)
    elif change == "file":
        path.write_bytes(b"changed reference")
    else:
        path.unlink()
    calls = []
    with pytest.raises(service.StoryboardError):
        service.generate_images(graph["id"], transport=image_transport(calls))
    assert calls == []


def test_reference_snapshot_is_bound_to_image_approval(prepared):
    service, original, root = prepared
    attach_storyboard_identity(original, root)
    graph = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    approve_images(service, graph)
    service._mutate(graph["id"], lambda g: g["identity_reference"].update(asset_hash="sha256:" + "f" * 64))
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(graph["id"], transport=image_transport([]))
    assert exc.value.code == "STORYBOARD_GRAPH_CHANGED"


def test_legacy_graph_without_reference_stays_readable_but_cannot_submit_for_selected_identity(prepared):
    service, original, root = prepared
    legacy = service.get_graph(original["id"])
    legacy.pop("identity_reference", None)
    legacy["schema_version"] = 2
    legacy["plan_digest"] = service._plan_digest(legacy)
    legacy["image_approval"] = {"plan_digest": legacy["plan_digest"]}
    st.put("media_jobs", legacy["id"], legacy, {"work_id": legacy["work_id"]})
    attach_storyboard_identity(original, root)
    assert service.project(service.get_graph(legacy["id"]))["id"] == legacy["id"]
    calls = []
    with pytest.raises(service.StoryboardError) as exc:
        service.generate_images(legacy["id"], transport=image_transport(calls))
    assert exc.value.code == "STORYBOARD_IDENTITY_REFERENCE_REQUIRED"
    replacement = service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
    assert replacement["id"] != original["id"]
    assert replacement["image_approval"] is None
    assert replacement["identity_reference"]
    assert calls == []


def test_no_selected_identity_keeps_scene_only_generation(prepared):
    service, graph, _ = prepared
    assert graph["identity_reference"] is None
    calls = []
    approve_images(service, graph)
    service.generate_images(graph["id"], transport=image_transport(calls))
    assert all(request["endpoint"] == person_image.GENERATIONS_PATH and "image_bytes" not in request for request in calls)


def test_unapproved_selected_variant_does_not_silently_drop_person_reference(prepared):
    service, original, root = prepared
    ident, _, _, _ = attach_storyboard_identity(original, root)
    variant_id = st.new_id("variant")
    st.put("variants", variant_id, {"id": variant_id, "identity_id": ident["id"], "approval_state": "pending"})
    work = st.get("works", original["work_id"])
    work["variant_id"] = variant_id
    st.put("works", work["id"], work)
    with pytest.raises(service.StoryboardError):
        service.create_graph(original["work_id"], image_ceiling_fen_per_shot=100)
