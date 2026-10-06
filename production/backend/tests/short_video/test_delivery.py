"""A genuine local stitched file becomes publishable only after explicit review."""
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import delivery, storage as st
from app.digital_human import gates
from app.main import app
from app.short_video import worker
from app.video import asr
from tests.short_video.test_stitch import _clip
from tests.short_video.test_storyboard import prepared, ready_images


@pytest.fixture
def stitched(prepared, monkeypatch):
    service, graph, root = prepared
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", root / "production/backend/runtime")
    monkeypatch.setattr(asr, "require_adapter", lambda: pytest.fail("Native storyboard must not invoke ASR"))
    graph = ready_images(service, graph)
    monkeypatch.setattr(service, "prepare_native_media", lambda source: {"image_url": "https://media.example/shot.png"})
    service.approve_videos(graph["id"], actor_id="user_local", task_graph_digest=graph["task_graph_digest"],
                           paid_ack=True, image_review_ack=True, total_ceiling_fen=300)
    for index, (color, frequency, seconds) in enumerate((("red", 440, 2), ("blue", 880, 3)), 1):
        path = root / f"provider_clip_{index}.mp4"
        _clip(path, color, frequency, seconds)
        data = path.read_bytes()
        def transport(request):
            if request["method"] == "POST":
                return {"status_code": 200, "json": {"output": {"task_id": f"original-{index}"}}}
            if "/tasks/" in request["url"]:
                return {"status_code": 200, "json": {"output": {"task_id": f"original-{index}",
                    "task_status": "SUCCEEDED", "video_url": f"https://media.example/clip-{index}.mp4"}}}
            return {"status_code": 200, "body": data}
        service.submit_videos(graph["id"], transport=transport)
        graph = service.poll_shot(graph["id"], index, transport=transport)
    service.approve_clips(graph["id"], actor_id="user_local", clip_digest=graph["clip_digest"], review_ack=True)
    service.queue_stitch(graph["id"])
    result = worker.run_stitch(graph["id"])
    return service, result, root


def _confirm(service, graph, **kwargs):
    return service.confirm_and_deliver(graph["id"], actor_id="user_local", review_ack=True,
        video_id=graph["video_id"], output_digest=graph["output"]["file_hash"], **kwargs)


def test_storyboard_review_produces_existing_publish_package_without_tts_or_asr(stitched):
    service, graph, root = stitched
    video = st.get("videos", graph["video_id"])
    assert video["approval_state"] == "pending" and not video.get("confirmed")
    before = st.all_rows("audios", "work_id=?", [graph["work_id"]])
    result = _confirm(service, graph)
    assert result["ok"] and result["video"]["confirmed"] is True
    assert result["package"]["state"] == "ready"
    assert gates.video_gate(video["id"])["ok"] is True
    assert delivery.status(graph["work_id"])["ready_for_review"] is True
    from app.publishing.service import resolve_publish_package
    package, master, cover = resolve_publish_package(graph["work_id"])
    assert package["id"] == result["package"]["id"]
    assert master.read_bytes() == (root / graph["output"]["storage_uri"]).read_bytes()
    assert cover.stat().st_size > 0
    with zipfile.ZipFile(delivery.download_path(package["id"])) as bundle:
        assert "字幕.ass" not in bundle.namelist() and "字幕.srt" not in bundle.namelist()
        info = json.loads(bundle.read("作品信息.json"))
        assert info["generation_mode"] == "storyboard_native_audio"
        assert info["title"] == "Approved storyboard" and info["subtitles"] == "not_generated"
        assert info["sound_mode"] == "native_audio" and len(info["storyboard"]) == 2
    assert st.all_rows("audios", "work_id=?", [graph["work_id"]]) == before
    again = _confirm(service, graph)
    assert again["package"]["id"] == package["id"]
    review = st.all_rows("video_reviews", "video_id=?", [video["id"]])[-1]
    assert review["rule_version"] == "wan-storyboard-1"
    assert "lip_sync" not in review["dimensions"]


def test_confirmation_requires_current_video_digest_and_real_user(stitched):
    service, graph, _ = stitched
    for actor, ack, digest in (("agent", True, graph["output"]["file_hash"]),
                               ("user_local", False, graph["output"]["file_hash"]),
                               ("user_local", True, "sha256:old")):
        with pytest.raises(service.StoryboardError):
            service.confirm_and_deliver(graph["id"], actor_id=actor, review_ack=ack,
                video_id=graph["video_id"], output_digest=digest)
    assert not st.all_rows("packages", "work_id=?", [graph["work_id"]])


def test_script_change_invalidates_download_and_existing_publishing_input(stitched):
    service, graph, _ = stitched
    result = _confirm(service, graph)
    work = st.get("works", graph["work_id"])
    work["content_revision"] += 1
    st.put("works", work["id"], work)
    assert not gates.video_gate(graph["video_id"])["ok"]
    with pytest.raises(delivery.DeliveryBlocked):
        delivery.download_path(result["package"]["id"])
    from app.publishing.service import PublishBlocked, resolve_publish_package
    with pytest.raises(PublishBlocked):
        resolve_publish_package(graph["work_id"])


def test_final_confirm_http_shape(stitched):
    _, graph, _ = stitched
    response = TestClient(app).post(f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}/confirm", json={
        "actor_id": "user_local", "review_ack": True, "video_id": graph["video_id"], "output_digest": graph["output"]["file_hash"]})
    assert response.status_code == 200
    assert response.json()["graph"]["id"] == graph["id"]
    assert response.json()["package"]["state"] == "ready"


def test_confirmed_graph_readback_contains_current_publish_package(stitched):
    service, graph, _ = stitched
    package_id = _confirm(service, graph)["package"]["id"]
    response = TestClient(app).get(
        f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}"
    )
    assert response.status_code == 200
    assert response.json()["package_id"] == package_id


def test_storyboard_readback_survives_package_filesystem_error(stitched, monkeypatch):
    service, graph, _ = stitched
    _confirm(service, graph)
    def inaccessible(_work_id):
        raise OSError("archive temporarily inaccessible")
    monkeypatch.setattr(delivery, "current_package", inaccessible)
    response = TestClient(app).get(
        f"/api/v1/works/{graph['work_id']}/wan/storyboards/{graph['id']}"
    )
    assert response.status_code == 200
    assert response.json()["state"] == "succeeded"
    assert response.json().get("package_id") is None
    assert response.json()["package_error"]["code"] == "STORYBOARD_PACKAGE_UNAVAILABLE"


def test_unknown_provider_job_cannot_reuse_clip_approval_to_publish(stitched):
    service, graph, _ = stitched
    from app import jobs
    shot = graph["shots"][0]
    jobs.mark_unknown(shot["video_job_id"], reason="Lost confirmation evidence")
    with pytest.raises(service.StoryboardError):
        _confirm(service, graph)
    assert not st.all_rows("packages", "work_id=?", [graph["work_id"]])


def test_image_job_result_rebinding_invalidates_final_review(stitched):
    service, graph, _ = stitched
    from app import jobs
    job = jobs.get_job(graph["shots"][0]["image_job_id"])
    job["result"]["image"]["asset_hash"] = "sha256:unrelated"
    st.put("provider_jobs", job["id"], job, {"work_id": graph["work_id"]})
    with pytest.raises(service.StoryboardError):
        _confirm(service, graph)


def test_duplicate_confirmation_does_not_rewrite_existing_review_or_execution(stitched):
    service, graph, _ = stitched
    result = _confirm(service, graph)
    execution = delivery.latest_execution(st.get("works", graph["work_id"]))
    reviews = st.all_rows("video_reviews", "video_id=?", [graph["video_id"]])
    final_reviews = [r for r in st.all_rows("quality_decisions", "work_id=?", [graph["work_id"]])
                     if r.get("kind") == "final_delivery_review"]
    repeated = _confirm(service, graph)
    assert repeated["package"]["id"] == result["package"]["id"]
    assert delivery.latest_execution(st.get("works", graph["work_id"])) == execution
    assert st.all_rows("video_reviews", "video_id=?", [graph["video_id"]]) == reviews
    assert [r for r in st.all_rows("quality_decisions", "work_id=?", [graph["work_id"]])
            if r.get("kind") == "final_delivery_review"] == final_reviews
