"""动作上传运输契约；只连本机测试 Worker，绝不运行 GPU 或发送用户素材。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.digital_human import worker_client as wc
from app.digital_human import providers as pv
from app.digital_human.errors import TalkingHeadBlocked
from test_m2_worker_transport import worker  # noqa: F401 - 本机真实 HTTP fixture
from test_motion_uploads import _upload, real_video  # noqa: F401 - 真 FFmpeg 合成技术测试片


def _motion(uri: str, digest: str) -> dict:
    return pv.build_liveportrait_request(
        source_image_url="https://storage.example/avatar.png", motion_ref_url=uri,
        output_seconds=4.2, source_hash="sha256:" + "b" * 64, motion_hash=digest)


@pytest.fixture()
def local_motion(tmp_path, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    uri = "assets/动作参考/用户上传/mup_" + "a" * 12 + "/动作参考.mp4"
    path = Path(tmp_path) / uri
    path.parent.mkdir(parents=True)
    data = b"synthetic-motion-for-transport-test"
    path.write_bytes(data)
    return uri, path, data, "sha256:" + hashlib.sha256(data).hexdigest()


def test_motion_upload_replaces_only_motion_path_and_keeps_hash(local_motion):
    uri, _path, data, digest = local_motion
    seen = []
    reference = "worker-asset://" + digest[7:] + ".mp4"

    def send(req):
        seen.append(req)
        if req["method"] == "GET":
            return {"status_code": 200, "json": {"version": "2", "capabilities": {"asset_upload": "sha256-v1"}}}
        if req["method"] == "PUT":
            assert req["body"] == data
            assert req["headers"]["Authorization"] == "Bearer local-token"
            return {"status_code": 201, "json": {"asset_uri": reference, "file_hash": digest, "bytes": len(data)}}
        body = json.loads(req["body"])
        assert body["args"]["motion_ref_url"] == reference
        assert body["args"]["motion_hash"] == digest
        assert body["args"]["source_image_url"] == "https://storage.example/avatar.png"
        assert body["args"]["output_seconds"] == 4.2
        assert uri not in req["body"].decode()
        return {"status_code": 200, "json": {"job_id": "accepted", "state": "succeeded"}}

    original = _motion(uri, digest)
    wc.transport(original, base_url="http://worker.test", token="local-token", inner=send)
    assert [r["method"] for r in seen] == ["GET", "PUT", "POST"]
    assert original["args"]["motion_ref_url"] == uri


def test_motion_upload_accepts_actual_mup_asset_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    uri = "assets/动作参考/用户上传/mup_0123456789ab/动作参考.mp4"
    path = Path(tmp_path) / uri
    path.parent.mkdir(parents=True)
    data = b"mup-motion"
    path.write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    seen = []
    def send(req):
        seen.append(req)
        if req["method"] == "GET":
            return {"status_code": 200, "json": {"version": "2", "capabilities": {"asset_upload": "sha256-v1"}}}
        if req["method"] == "PUT":
            return {"status_code": 201, "json": {"asset_uri": "worker-asset://" + digest[7:] + ".mp4", "file_hash": digest, "bytes": len(data)}}
        return {"status_code": 200, "json": {"job_id": "mup", "state": "succeeded"}}
    wc.transport(_motion(uri, digest), base_url="http://worker.test", token="t", inner=send)
    assert [r["method"] for r in seen] == ["GET", "PUT", "POST"]


def test_http_upload_confirm_storage_uri_reaches_worker(worker, tmp_path, real_video, monkeypatch):
    """真实上传 HTTP → 用户确认 → storage_uri → worker 资产运输；只用本机假 runner。"""
    import threading
    from fastapi.testclient import TestClient
    from app import storage as st
    from app.avatar import registry
    from app.main import app
    root = Path(tmp_path)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(root))
    monkeypatch.setattr(st, "DB_PATH", str(root / "http-upload.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()
    identity = registry.create_identity_candidate("动作上传联测", "测试隔离人物")
    client = TestClient(app)
    uploaded = _upload(client, identity["id"], real_video, name="我的动作.mp4")
    assert uploaded.status_code == 200, uploaded.text
    upload = uploaded.json()
    confirmed = client.post(f"/api/v1/motion-uploads/{upload['id']}/confirm", json={
        "label": "HTTP 上传动作", "motion_key": "nod", "description": "自然点头",
        "source_basis": "用户本人拍摄", "actor_id": "user_local", "confirmed": True,
    })
    assert confirmed.status_code == 200, confirmed.text
    row = st.get("motion_uploads", upload["id"])
    assert row["state"] == "confirmed" and row["storage_uri"].endswith("/动作参考.mp4")
    asset = registry.get_asset(confirmed.json()["asset_id"])
    assert asset["storage_uri"] == row["storage_uri"]
    req = _motion(asset["storage_uri"], asset["file_hash"])
    base, token, _payload = worker
    response = wc.transport(req, base_url=base, token=token, poll_interval=0.02, max_wait=10)
    parsed = pv.parse_m2_response(response)
    assert parsed["state"] == "succeeded"
    job_path = _payload.parent / "jobs" / parsed["remote_job_id"] / "job.json"
    staged_uri = json.loads(job_path.read_text(encoding="utf-8"))["args"]["motion_ref_url"]
    import sys
    from test_m2_worker_transport import WORKERS_SRC
    sys.path.insert(0, str(WORKERS_SRC))
    try:
        import runner_common
    finally:
        sys.path.pop(0)
    downloaded = root / "真实runner读取动作.mp4"
    runner_common.fetch_and_verify(staged_uri, str(downloaded), asset["file_hash"], "动作参考")
    assert downloaded.read_bytes() == (root / asset["storage_uri"]).read_bytes()


def test_remote_url_never_uses_local_fallback(local_motion):
    _uri, _path, _data, digest = local_motion
    seen = []

    def send(req):
        seen.append(req)
        return {"status_code": 200, "json": {"job_id": "remote", "state": "succeeded"}}

    url = "https://storage.example/original.mp4?signature=unchanged"
    wc.transport(_motion(url, digest), base_url="http://worker.test", token="t", inner=send)
    assert len(seen) == 1
    assert json.loads(seen[0]["body"])["args"]["motion_ref_url"] == url


@pytest.mark.parametrize("uri", ["../secret.mp4", "assets/other.mp4", "C:/private/video.mp4", "file:///etc/passwd", "worker-asset://" + "a" * 64 + ".mp4"])
def test_local_motion_outside_upload_scope_is_blocked(uri):
    seen = []
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, "sha256:" + "a" * 64), base_url="http://worker.test", token="t", inner=lambda req: seen.append(req))
    assert exc.value.code == "M2_LOCAL_MOTION_PATH_INVALID"
    assert seen == []


def test_changed_file_hash_stops_before_any_network(local_motion):
    uri, path, _data, digest = local_motion
    path.write_bytes(b"changed after approval")
    seen = []
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, digest), base_url="http://worker.test", token="t", inner=lambda req: seen.append(req))
    assert exc.value.code == "M2_LOCAL_MOTION_HASH_MISMATCH"
    assert seen == []


@pytest.mark.parametrize("reply,expected", [
    ({"status_code": 404}, "ACTION_UPGRADE_REQUIRED"),
    ({"status_code": 200, "json": {"version": "1"}}, "ACTION_UPGRADE_REQUIRED"),
    ({"status_code": None, "error": "network_error"}, "M2_ASSET_TRANSFER_FAILED"),
])
def test_old_or_unreachable_worker_never_receives_video_or_job(local_motion, reply, expected):
    uri, _path, _data, digest = local_motion
    seen = []

    def send(req):
        seen.append(req)
        return reply

    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, digest), base_url="http://worker.test", token="t", inner=send)
    assert exc.value.code == expected
    assert [r["method"] for r in seen] == ["GET"]


@pytest.mark.parametrize("upload_reply,expected", [
    ({"status_code": 401}, "M2_ASSET_TRANSFER_FAILED"),
    ({"status_code": 404}, "ACTION_UPGRADE_REQUIRED"),
    ({"status_code": None, "error": "network_error"}, "M2_ASSET_TRANSFER_FAILED"),
    ({"status_code": 201, "json": {"asset_uri": "file:///etc/passwd"}}, "M2_ASSET_RECEIPT_INVALID"),
])
def test_upload_failure_does_not_submit_job(local_motion, upload_reply, expected):
    uri, _path, _data, digest = local_motion
    seen = []

    def send(req):
        seen.append(req)
        if req["method"] == "GET":
            return {"status_code": 200, "json": {"version": "2", "capabilities": {"asset_upload": "sha256-v1"}}}
        return upload_reply

    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, digest), base_url="http://worker.test", token="t", inner=send)
    assert exc.value.code == expected
    assert [r["method"] for r in seen] == ["GET", "PUT"]


def test_worker_stages_uploaded_video_as_file_uri_for_runner(worker, local_motion):
    base, token, _payload = worker
    uri, _path, data, digest = local_motion
    response = wc.transport(_motion(uri, digest), base_url=base, token=token, poll_interval=0.02, max_wait=10)
    parsed = pv.parse_m2_response(response)
    assert parsed["state"] == "succeeded", parsed
    job_path = _payload.parent / "jobs" / parsed["remote_job_id"] / "job.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    staged_uri = job["args"]["motion_ref_url"]
    assert staged_uri.startswith("file:")
    import urllib.request
    with urllib.request.urlopen(staged_uri) as uploaded:
        assert uploaded.read() == data
    assert job["args"]["motion_hash"] == digest


def test_worker_upload_rejects_bad_hash_and_missing_auth(worker):
    base, token, _payload = worker
    target = base + "/v1/assets/" + "a" * 64 + ".mp4"
    auth = {"Authorization": "Bearer " + token, "Content-Type": "video/mp4"}
    bad = pv.urllib_transport({"method": "PUT", "url": target, "headers": auth, "body": b"wrong"})
    assert bad["status_code"] == 422
    assert json.loads(bad["body"])["code"] == "ASSET_HASH_MISMATCH"
    anonymous = pv.urllib_transport({"method": "PUT", "url": target, "body": b"wrong"})
    assert anonymous["status_code"] == 401
    assert not list((_payload.parent / "jobs" / "_assets").glob("*.mp4"))


@pytest.mark.parametrize("motion_uri", ["file:///etc/passwd", "worker-asset://../secret.mp4", "worker-asset://" + "a" * 64 + ".mp4", "ftp://x/a.mp4"])
def test_worker_rejects_unowned_or_unsupported_input_uri(worker, motion_uri):
    base, token, _payload = worker
    response = pv.urllib_transport(wc.build_job_request(_motion(motion_uri, "sha256:" + "a" * 64), base_url=base, token=token))
    assert response["status_code"] == 422
    assert not list((_payload.parent / "jobs").glob("*/job.json"))


def test_exact_size_limit_is_rejected_before_network(local_motion, monkeypatch):
    uri, _path, data, digest = local_motion
    monkeypatch.setattr(wc, "_MAX_LOCAL_MOTION_BYTES", len(data))
    seen = []
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, digest), base_url="http://worker.test", token="t", inner=lambda req: seen.append(req))
    assert exc.value.code == "M2_LOCAL_MOTION_TOO_LARGE"
    assert seen == []


def test_missing_token_does_not_upload_private_motion(local_motion):
    uri, _path, _data, digest = local_motion
    seen = []
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(_motion(uri, digest), base_url="http://worker.test", token="", inner=lambda req: seen.append(req))
    assert exc.value.code == "M2_ASSET_AUTH_REQUIRED"
    assert seen == []


def test_worker_rejects_declared_hash_different_from_owned_asset(worker, local_motion):
    base, token, payload = worker
    _uri, _path, data, digest = local_motion
    upload = pv.urllib_transport({"method": "PUT", "url": base + "/v1/assets/" + digest[7:] + ".mp4",
                                "headers": {"Authorization": "Bearer " + token, "Content-Type": "video/mp4"}, "body": data})
    assert upload["status_code"] == 201
    request = _motion("worker-asset://" + digest[7:] + ".mp4", "sha256:" + "a" * 64)
    response = pv.urllib_transport(wc.build_job_request(request, base_url=base, token=token))
    assert response["status_code"] == 422
    assert not list((payload.parent / "jobs").glob("*/job.json"))


def test_worker_rejects_100mib_content_length_without_reading_body(worker, monkeypatch):
    import worker_server
    base, token, _payload = worker
    monkeypatch.setattr(worker_server, "MAX_ASSET_BYTES", 5)
    response = pv.urllib_transport({"method": "PUT", "url": base + "/v1/assets/" + "a" * 64 + ".mp4",
                                   "headers": {"Authorization": "Bearer " + token, "Content-Type": "video/mp4"},
                                   "body": b"12345"})
    assert response["status_code"] == 413
