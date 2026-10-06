"""自定义动作参考上传：先验证文件，再由人命名确认。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.avatar import motion, registry


@pytest.fixture()
def upload_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "studio.db"))
    from app import storage
    monkeypatch.setattr(storage, "_local", threading.local())
    storage.init_db()
    identity = registry.create_identity_candidate("我的主播", "专业温柔")
    from app.main import app
    return TestClient(app), identity, tmp_path


def _upload(client: TestClient, identity_id: str, body: bytes = b"fake-video", name: str = "action.mp4"):
    return client.post(
        f"/api/v1/identities/{identity_id}/motion-uploads",
        content=body,
        headers={"content-type": "video/mp4", "x-file-name": quote(name)},
    )


def test_upload_rejects_unreadable_video_without_creating_template(upload_app, monkeypatch):
    client, identity, tmp_path = upload_app
    res = _upload(client, identity["id"])
    assert res.status_code == 422
    assert res.json()["error"]["code"] in {"MOTION_UPLOAD_INVALID", "MOTION_UPLOAD_PROBE_FAILED"}
    assert st.all_rows("motion_uploads") == []
    assert motion.list_references(identity["id"]) == []


def test_upload_metadata_and_confirm_create_personal_motion_reference(upload_app, monkeypatch):
    client, identity, tmp_path = upload_app
    normalized = tmp_path / "normalized.mp4"
    normalized.write_bytes(b"normalized-motion")
    monkeypatch.setattr("app.motion_upload_api._validate_and_normalize", lambda *args, **kwargs: {
        "path": str(normalized), "duration_seconds": 4.2, "width": 720, "height": 1280,
        "file_hash": "sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
    })
    res = _upload(client, identity["id"])
    assert res.status_code == 200, res.text
    upload = res.json()
    assert upload["state"] == "pending" and upload["reference_id"] is None
    assert upload["preview_url"].endswith(f"/motion-uploads/{upload['id']}/preview")

    confirmed = client.post(f"/api/v1/motion-uploads/{upload['id']}/confirm", json={
        "label": "我自己的自然讲述", "motion_key": "smile", "description": "轻微微笑和自然点头",
        "source_basis": "本人拍摄，允许本项目使用", "actor_id": "user_local", "confirmed": True,
    })
    assert confirmed.status_code == 200, confirmed.text
    ref = confirmed.json()
    assert ref["identity_id"] == identity["id"] and ref["approval_state"] == "approved"
    assert ref["asset_id"]
    assert st.get("motion_uploads", upload["id"])["reference_id"] == ref["id"]
    assert client.get(f"/api/v1/identities/{identity['id']}/motions").json()["approved"][0]["id"] == ref["id"]


@pytest.mark.parametrize("payload", [
    {"confirmed": False}, {"actor_id": "agent"}, {"label": ""}, {"source_basis": ""},
])
def test_confirm_requires_explicit_user_and_source(payload, upload_app, monkeypatch):
    client, identity, tmp_path = upload_app
    normalized = tmp_path / "normalized.mp4"
    normalized.write_bytes(b"normalized")
    monkeypatch.setattr("app.motion_upload_api._validate_and_normalize", lambda *args, **kwargs: {
        "path": str(normalized), "duration_seconds": 4.2, "width": 720, "height": 1280,
        "file_hash": "sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
    })
    upload = _upload(client, identity["id"]).json()
    body = {"label": "我的动作", "motion_key": "smile", "description": "动作", "source_basis": "本人拍摄",
            "actor_id": "user_local", "confirmed": True}
    body.update(payload)
    res = client.post(f"/api/v1/motion-uploads/{upload['id']}/confirm", json=body)
    assert res.status_code == 422
    assert st.get("motion_uploads", upload["id"])["reference_id"] is None


def test_preview_and_confirm_are_idempotent(upload_app, monkeypatch):
    client, identity, tmp_path = upload_app
    normalized = tmp_path / "normalized.mp4"
    normalized.write_bytes(b"normalized")
    monkeypatch.setattr("app.motion_upload_api._validate_and_normalize", lambda *args, **kwargs: {
        "path": str(normalized), "duration_seconds": 4.2, "width": 720, "height": 1280,
        "file_hash": "sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
    })
    upload = _upload(client, identity["id"]).json()
    preview = client.get(f"/api/v1/motion-uploads/{upload['id']}/preview")
    assert preview.status_code == 200 and preview.content == b"normalized"
    body = {"label": "我的动作", "motion_key": "smile", "description": "动作", "source_basis": "本人拍摄",
            "actor_id": "user_local", "confirmed": True}
    first = client.post(f"/api/v1/motion-uploads/{upload['id']}/confirm", json=body)
    second = client.post(f"/api/v1/motion-uploads/{upload['id']}/confirm", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(motion.list_references(identity["id"])) == 1


def test_upload_isolated_to_identity_and_size_limit(upload_app, monkeypatch):
    client, identity, _ = upload_app
    res = client.post(f"/api/v1/identities/missing/motion-uploads", content=b"x", headers={"x-file-name": "x.mp4"})
    assert res.status_code == 404
    monkeypatch.setattr("app.motion_upload_api.MAX_UPLOAD_BYTES", 3)
    assert _upload(client, identity["id"], b"1234").status_code == 413


@pytest.fixture(scope="module")
def real_video(tmp_path_factory):
    from app.video import ffmpeg
    target = tmp_path_factory.mktemp("motion-media") / "real.mp4"
    result = ffmpeg.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
        "-t", "3.2", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(target),
    ], timeout=30)
    assert result.returncode == 0
    return target.read_bytes()


def _confirm(client, upload_id, **changes):
    return client.post(f"/api/v1/motion-uploads/{upload_id}/confirm", json={
        "label": "自己拍摄的点头", "motion_key": "nod", "description": "轻轻点头",
        "source_basis": "本人拍摄", "actor_id": "user_local", "confirmed": True, **changes,
    })


def test_real_upload_preserves_source_and_normalizes_playable_silent_video(upload_app, real_video):
    from app.video import ffmpeg
    client, identity, root = upload_app
    response = _upload(client, identity["id"], real_video, name="动作参考.mp4")
    assert response.status_code == 200, response.text
    upload = response.json()
    row = st.get("motion_uploads", upload["id"])
    assert upload["original_filename"] == "动作参考.mp4"
    assert row["source_uri"] != row["storage_uri"]
    assert (root / row["source_uri"]).read_bytes() == real_video
    media = ffmpeg.probe(str(root / row["storage_uri"]))
    assert [stream["codec_type"] for stream in media["streams"]] == ["video"]
    video = media["streams"][0]
    assert video["codec_name"] == "h264" and video["r_frame_rate"] == "25/1"
    assert video["width"] / video["height"] == 4 / 3
    assert (upload["width"], upload["height"]) == (video["width"], video["height"])
    assert max(video["width"], video["height"]) <= 1280
    ffmpeg.full_decode_check(str(root / row["storage_uri"]))
    preview = client.get(upload["preview_url"], headers={"Range": "bytes=0-15"})
    assert preview.status_code == 206
    confirmed = _confirm(client, upload["id"])
    assert confirmed.status_code == 200, confirmed.text
    from app.contracts import MotionReference
    MotionReference.model_validate(confirmed.json())
    assert registry.get_identity(identity["id"]) == identity
    assert client.get(f"/api/v1/identities/{identity['id']}/motion-uploads").json()["uploads"][0]["state"] == "confirmed"


def test_file_changes_block_preview_and_confirmation(upload_app, real_video):
    client, identity, root = upload_app
    upload = _upload(client, identity["id"], real_video).json()
    row = st.get("motion_uploads", upload["id"])
    (root / row["storage_uri"]).write_bytes(b"changed")
    assert client.get(upload["preview_url"]).status_code == 409
    assert _confirm(client, upload["id"]).status_code == 409
    assert motion.list_references(identity["id"]) == []


def test_concurrent_confirmation_only_creates_one_template(upload_app, real_video):
    client, identity, _ = upload_app
    upload = _upload(client, identity["id"], real_video).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _confirm(client, upload["id"]), range(2)))
    assert all(res.status_code == 200 for res in responses), [r.text for r in responses]
    assert len({res.json()["id"] for res in responses}) == 1
    assert len(motion.list_references(identity["id"])) == 1


@pytest.mark.parametrize("bad_duration", [float("nan"), float("inf"), 0, 61])
def test_invalid_duration_is_rejected(upload_app, monkeypatch, bad_duration):
    from app import motion_upload_api as uploads
    probe = {
        "format": {"duration": bad_duration}, "streams": [{"codec_type": "video", "width": 320, "height": 240}],
    }
    monkeypatch.setattr(uploads.ffmpeg, "_run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(probe).encode(), b""))
    with pytest.raises(Exception) as error:
        uploads._video_meta("file")
    assert getattr(error.value, "status_code", None) == 422


def test_gesture_catalog_does_not_claim_body_motion_support(upload_app, real_video):
    client, identity, _ = upload_app
    upload = _upload(client, identity["id"], real_video).json()
    assert _confirm(client, upload["id"], motion_key="gesture_open").status_code == 422
