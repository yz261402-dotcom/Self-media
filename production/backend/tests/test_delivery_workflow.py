"""真实文件交付、版本隔离与失败恢复，不调用收费模型。"""
import hashlib
import json
import zipfile
import asyncio
import threading

import pytest

from app import storage as st


def seeded(tmp_path, monkeypatch):
    from app import delivery
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", tmp_path)
    from app.digital_human import gates
    monkeypatch.setattr(gates, "video_gate", lambda _: {"ok": True})
    wid, tid = st.new_id("delivery"), st.new_id("task")
    work = {"id": wid, "name": "RAG", "route": "M2", "identity_id": "person",
            "variant_id": "variant", "voice_asset_id": "voice", "script_version_id": "script",
            "final_audio_id": "audio", "video_asset_id": "video", "current_task_id": tid}
    task = {"id": tid, "work_id": wid, "mode": "production", "state": "waiting_selection",
            "stage": "video_review", "route": "M2"}
    st.put("works", wid, work)
    st.put("tasks", tid, task, {"work_id": wid})
    st.put("audios", "audio", {"id": "audio", "work_id": wid, "confirmed": True, "approved": True, "file_hash": "audio-hash"}, {"work_id": wid})
    st.put("videos", "video", {"id": "video", "work_id": wid, "confirmed": True, "approval_state": "approved", "file_hash": "video-hash"}, {"work_id": wid})
    st.put("script_batches", wid, {"candidates": [{"id": "script", "text": "RAG 先查资料，再组织答案。",
            "title": "RAG 是什么", "description": "一分钟认识 RAG", "tags": ["AI", "RAG"]}]})
    files = []
    for kind, name in [("master", "source.mp4"), ("subtitles_ass", "source.ass"),
                       ("subtitles_srt", "source.srt"), ("cover", "source.jpg")]:
        path = tmp_path / name
        path.write_bytes((kind + " fixture bytes").encode())
        files.append({"kind": kind, "storage_uri": name,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    execution = {"id": st.new_id("worker"), "task_id": tid, "work_id": wid, "stage": "postprocess",
                 "executor_kind": "worker", "state": "succeeded", "simulated": False,
                 "input_assets": {"audio_id": "audio", "video_id": "video", "script_version_id": "script",
                                  "audio_hash": "audio-hash", "video_hash": "video-hash",
                                  "script_hash": hashlib.sha256("RAG 先查资料，再组织答案。".encode()).hexdigest()},
                 "output_files": files, "verify_log": {"probe": {"width": 1080, "height": 1920}},
                 "input_digest": "digest", "created_at": "2026-09-25T00:00:00Z"}
    st.put("media_jobs", execution["id"], execution, {"work_id": wid})
    return delivery, work, task, execution


def test_delivery_requires_explicit_final_review(tmp_path, monkeypatch):
    d, w, t, _ = seeded(tmp_path, monkeypatch)
    with pytest.raises(d.DeliveryBlocked, match="确认"):
        d.build_delivery(t, w)


def test_zip_has_verified_media_metadata_and_manifest_and_is_idempotent(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    result = d.build_delivery(t, w)
    assert result["state"] == "succeeded"
    pkg = st.get("packages", result["package_id"])
    path = d.download_path(pkg["id"])
    with zipfile.ZipFile(path) as z:
        assert {"成片.mp4", "字幕.srt", "字幕.ass", "封面.jpg", "作品信息.json", "质量报告.json", "manifest.json"} <= set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["input_assets"]["audio_id"] == "audio"
        for item in manifest["files"]:
            assert hashlib.sha256(z.read(item["path"])).hexdigest() == item["sha256"]
    assert d.build_delivery(t, w)["package_id"] == pkg["id"]


def test_changed_audio_hides_previous_preview_and_rejects_download(tmp_path, monkeypatch):
    d, w, t, _ = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    pkg_id = d.build_delivery(t, w)["package_id"]
    w["final_audio_id"] = "new_audio"
    st.put("works", w["id"], w)
    assert d.status(w["id"])["ready_for_review"] is False
    assert d.current_package(w["id"]) is None
    with pytest.raises(d.DeliveryBlocked):
        d.download_path(pkg_id)


def test_tampered_output_and_traversal_are_rejected(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    (tmp_path / "source.mp4").write_bytes(b"changed")
    with pytest.raises(d.DeliveryBlocked):
        d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    record["output_files"][0]["storage_uri"] = "../outside.mp4"
    st.put("media_jobs", record["id"], record, {"work_id": w["id"]})
    with pytest.raises(d.DeliveryBlocked):
        d.confirm_review(w["id"], actor_id="user_local", review_ack=True)


def test_old_browser_cannot_confirm_a_new_render(tmp_path, monkeypatch):
    d, w, _, record = seeded(tmp_path, monkeypatch)
    with pytest.raises(d.DeliveryBlocked):
        d.confirm_review(w["id"], actor_id="user_local", review_ack=True, execution_id="old_render")


def test_mock_record_is_never_downloadable(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    record["simulated"] = True
    st.put("media_jobs", record["id"], record, {"work_id": w["id"]})
    assert not d.status(w["id"])["ready_for_review"]
    with pytest.raises(d.DeliveryBlocked):
        d.confirm_review(w["id"], actor_id="user_local", review_ack=True)


def test_http_can_confirm_preview_and_download_real_archive(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    d, w, _, record = seeded(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get(f"/api/v1/works/{w['id']}/postprocess").status_code == 200
    response = client.post(f"/api/v1/works/{w['id']}/postprocess-confirmation", json={
        "review_ack": True, "actor_id": "user_local", "execution_id": record["id"]})
    assert response.status_code == 200, response.text
    downloaded = client.get(response.json()["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content[:2] == b"PK"


def test_postprocess_does_not_block_event_loop_and_stops_for_review(tmp_path, monkeypatch):
    from app import engine, postprocess_worker
    d, w, t, record = seeded(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def slow_worker(*args):
        entered.set()
        assert release.wait(timeout=3)
        return record
    monkeypatch.setattr(postprocess_worker, "run_stage", slow_worker)
    t.update(state="queued", stage="postprocess")
    st.put("tasks", t["id"], t, {"work_id": w["id"]})
    async def run():
        job = asyncio.create_task(engine.render_postprocess(t["id"]))
        assert await asyncio.to_thread(entered.wait, 1)
        release.set()
        await job
    asyncio.run(run())
    final = st.get("tasks", t["id"])
    assert final["state"] == "waiting_selection"
    assert final["stage"] == "video_review"
    assert not st.all_rows("packages", "work_id=?", [w["id"]])


def test_postprocess_failure_is_persisted_instead_of_hanging(tmp_path, monkeypatch):
    from app import engine, postprocess_worker
    _, w, t, _ = seeded(tmp_path, monkeypatch)
    def broken(*args):
        raise RuntimeError("upstream secret should not leak")
    monkeypatch.setattr(postprocess_worker, "run_stage", broken)
    t.update(state="queued", stage="postprocess")
    st.put("tasks", t["id"], t, {"work_id": w["id"]})
    asyncio.run(engine.render_postprocess(t["id"]))
    final = st.get("tasks", t["id"])
    assert final["state"] == "failed"
    assert "secret" not in str(final["last_error"])


def test_parallel_confirmation_reuses_one_valid_archive(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    d, w, t, _ = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: d.build_delivery(t, w), range(8)))
    assert len({r["package_id"] for r in results}) == 1
    with zipfile.ZipFile(d.download_path(results[0]["package_id"])) as z:
        assert z.testzip() is None


def test_http_confirmation_requires_the_preview_version(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _, w, _, _ = seeded(tmp_path, monkeypatch)
    response = TestClient(app).post(f"/api/v1/works/{w['id']}/postprocess-confirmation", json={
        "review_ack": True, "actor_id": "user_local"})
    assert response.status_code == 409


def test_source_hash_change_invalidates_existing_delivery(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    record["input_assets"]["audio_hash"] = "old"
    st.put("audios", "audio", {"id": "audio", "work_id": w["id"], "file_hash": "old", "approved": True, "confirmed": True}, {"work_id": w["id"]})
    st.put("media_jobs", record["id"], record, {"work_id": w["id"]})
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    pid = d.build_delivery(t, w)["package_id"]
    st.put("audios", "audio", {"id": "audio", "work_id": w["id"], "file_hash": "changed"}, {"work_id": w["id"]})
    with pytest.raises(d.DeliveryBlocked):
        d.download_path(pid)


def test_revoked_original_review_invalidates_package_without_changing_hash(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    pid = d.build_delivery(t, w)["package_id"]
    video = st.get("videos", "video")
    video["approval_state"] = "rejected"
    st.put("videos", "video", video, {"work_id": w["id"]})
    with pytest.raises(d.DeliveryBlocked):
        d.download_path(pid)


def test_input_edit_during_packaging_never_publishes_old_package(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    original_write = zipfile.ZipFile.write
    def edit_during_write(archive, *args, **kwargs):
        current = st.get("works", w["id"])
        current["final_audio_id"] = "new_audio"
        st.put("works", w["id"], current)
        return original_write(archive, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "write", edit_during_write)
    with pytest.raises(d.DeliveryBlocked):
        d.build_delivery(t, w)
    assert not st.all_rows("packages", "work_id=?", [w["id"]])


def test_cancelled_render_cannot_be_confirmed_or_shown_ready(tmp_path, monkeypatch):
    d, w, t, record = seeded(tmp_path, monkeypatch)
    t["state"] = "cancelled"
    st.put("tasks", t["id"], t, {"work_id": w["id"]})
    assert not d.status(w["id"])["ready_for_review"]
    with pytest.raises(d.DeliveryBlocked):
        d.confirm_review(w["id"], actor_id="user_local", review_ack=True, execution_id=record["id"])
    assert not st.all_rows("quality_decisions", "work_id=?", [w["id"]])


def test_cancelled_package_is_not_downloadable(tmp_path, monkeypatch):
    d, w, t, _ = seeded(tmp_path, monkeypatch)
    d.confirm_review(w["id"], actor_id="user_local", review_ack=True)
    pid = d.build_delivery(t, w)["package_id"]
    t["state"] = "cancelled"
    st.put("tasks", t["id"], t, {"work_id": w["id"]})
    with pytest.raises(d.DeliveryBlocked):
        d.download_path(pid)


@pytest.mark.parametrize("change", ["missing", "tampered"])
def test_status_hides_unplayable_media_and_explains_recovery(tmp_path, monkeypatch, change):
    d, w, _, _ = seeded(tmp_path, monkeypatch)
    path = tmp_path / "source.mp4"
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed")
    view = d.status(w["id"])
    assert not view["ready_for_review"]
    assert view["preview_url"] is None
    assert view["error"]["code"] in ("MEDIA_FILE_MISSING", "DELIVERY_HASH_MISMATCH")


@pytest.mark.parametrize("change", ["cancel", "replace"])
def test_http_never_completes_a_cancelled_or_replaced_task_after_packaging(tmp_path, monkeypatch, change):
    from fastapi.testclient import TestClient
    from app.main import app
    d, w, t, record = seeded(tmp_path, monkeypatch)
    original_build = d.build_delivery
    def build_then_change(*args):
        result = original_build(*args)
        if change == "cancel":
            t["state"] = "cancelled"
            st.put("tasks", t["id"], t, {"work_id": w["id"]})
        else:
            w["current_task_id"] = "replacement_task"
            st.put("works", w["id"], w)
        return result
    monkeypatch.setattr(d, "build_delivery", build_then_change)
    response = TestClient(app).post(f"/api/v1/works/{w['id']}/postprocess-confirmation", json={
        "review_ack": True, "actor_id": "user_local", "execution_id": record["id"]})
    assert response.status_code == 409
    assert st.get("tasks", t["id"])["state"] != "succeeded"
