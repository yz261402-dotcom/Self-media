"""本地登记图像/底片/音频 → 受控 Worker；真实 HTTP 与读取，不运行 GPU。"""
from __future__ import annotations

import hashlib
import json
import sys
import io
import wave
from pathlib import Path

import pytest

from app import storage as st
from app.digital_human import providers as pv, worker_client as wc
from app.digital_human.errors import TalkingHeadBlocked
from test_m2_worker_transport import WORKERS_SRC, worker  # noqa: F401
from test_motion_uploads import real_video  # noqa: F401 - FFmpeg 技术测试片


@pytest.fixture()
def registered(tmp_path, monkeypatch, real_video):
    import threading
    root = Path(tmp_path)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(root))
    monkeypatch.setattr(st, "DB_PATH", str(root / "registered-inputs.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new("RGB", (128, 128), (180, 180, 180)).save(image_bytes, format="PNG")
    audio_bytes = io.BytesIO()
    with wave.open(audio_bytes, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    files = {}
    for name, table, uri, body, kind in [
        ("image", "avatar_assets", "assets/人物/形象.png", image_bytes.getvalue(), "variant"),
        ("plate", "videos", "video/work_test/vid_test.mp4", real_video, "plate"),
        ("audio", "audios", "audio/work_test/audio_test.wav", audio_bytes.getvalue(), "full"),
    ]:
        path = root / uri if uri.startswith("assets/") else root / "production/backend/runtime" / uri
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        row = {"id": st.new_id("test"), "storage_uri": uri, "file_hash": digest, "is_mock": False, "kind": kind}
        st.put(table, row["id"], row)
        files[name] = {"row": row, "table": table, "path": path, "body": body}
    return files


def _stage_inputs(worker, request, expected):
    base, token, payload = worker
    response = wc.transport(request, base_url=base, token=token, poll_interval=0.02, max_wait=10)
    parsed = pv.parse_m2_response(response)
    assert parsed["state"] == "succeeded", parsed
    job = json.loads((payload.parent / "jobs" / parsed["remote_job_id"] / "job.json").read_text(encoding="utf-8"))
    sys.path.insert(0, str(WORKERS_SRC))
    try:
        import runner_common
    finally:
        sys.path.pop(0)
    for key, item in expected.items():
        assert job["args"][key].startswith("file:///"), job["args"]
        target = payload.parent / (key + item["path"].suffix)
        runner_common.fetch_and_verify(job["args"][key], str(target), item["row"]["file_hash"], key)
        assert target.read_bytes() == item["body"]
    return job


def test_registered_image_and_lipsync_assets_are_readable_by_runner(worker, registered):
    image, plate, audio = (registered[key] for key in ("image", "plate", "audio"))
    motion = pv.build_liveportrait_request(source_image_url=image["row"]["storage_uri"],
        source_hash=image["row"]["file_hash"], motion_ref_url="https://example.test/original.mp4",
        motion_hash="sha256:" + "a" * 64, output_seconds=4.2)
    job = _stage_inputs(worker, motion, {"source_image_url": image})
    assert job["args"]["motion_ref_url"] == "https://example.test/original.mp4"
    lipsync = pv.build_musetalk_request(plate_url=plate["row"]["storage_uri"], plate_hash=plate["row"]["file_hash"],
                                      audio_url=audio["row"]["storage_uri"], audio_hash=audio["row"]["file_hash"])
    _stage_inputs(worker, lipsync, {"plate_url": plate, "audio_url": audio})


@pytest.mark.parametrize("mode", ["unregistered", "mock", "wrong_hash", "outside_scope", "wrong_avatar_kind"])
def test_local_image_is_never_uploaded_without_valid_registration(registered, mode):
    item = registered["image"]
    row = dict(item["row"])
    if mode == "unregistered":
        row["storage_uri"] = "assets/人物/未登记.png"
    elif mode == "mock":
        row["is_mock"] = True
        st.put(item["table"], row["id"], row)
    elif mode == "wrong_hash":
        row["file_hash"] = "sha256:" + "b" * 64
    elif mode == "outside_scope":
        row["storage_uri"] = "production/backend/.env"
        st.put(item["table"], row["id"], row)
    elif mode == "wrong_avatar_kind":
        row["kind"] = "motion"
        st.put(item["table"], row["id"], row)
    request = pv.build_liveportrait_request(source_image_url=row["storage_uri"], source_hash=row["file_hash"],
        motion_ref_url="https://example.test/a.mp4", motion_hash="sha256:" + "a" * 64, output_seconds=4.2)
    seen = []
    def send(req):
        seen.append(req)
        return {"status_code": 200, "json": {"job_id": "bad", "state": "succeeded"}}
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(request, base_url="http://worker.test", token="t", inner=send)
    assert exc.value.code.startswith("M2_LOCAL_INPUT_")
    assert seen == []


def test_invalid_second_lipsync_input_stops_before_uploading_first(registered):
    plate, audio = registered["plate"], registered["audio"]
    audio["path"].write_bytes(b"tampered")
    request = pv.build_musetalk_request(plate_url=plate["row"]["storage_uri"], plate_hash=plate["row"]["file_hash"],
        audio_url=audio["row"]["storage_uri"], audio_hash=audio["row"]["file_hash"])
    seen = []
    with pytest.raises(TalkingHeadBlocked) as exc:
        wc.transport(request, base_url="http://worker.test", token="t", inner=lambda req: seen.append(req))
    assert exc.value.code == "M2_LOCAL_INPUT_HASH_MISMATCH"
    assert seen == []


def test_workspace_prefixed_audio_uri_is_supported(worker, registered):
    item = registered["audio"]
    row = dict(item["row"], storage_uri="production/backend/runtime/" + item["row"]["storage_uri"])
    st.put(item["table"], row["id"], row)
    item["row"] = row
    request = pv.build_musetalk_request(plate_url="https://example.test/plate.mp4", plate_hash="sha256:" + "a" * 64,
        audio_url=row["storage_uri"], audio_hash=row["file_hash"])
    _stage_inputs(worker, request, {"audio_url": item})


def test_worker_rejects_executable_extension(worker):
    base, token, _payload = worker
    body = b"executable payload"
    digest = hashlib.sha256(body).hexdigest()
    response = pv.urllib_transport({"method": "PUT", "url": base + "/v1/assets/" + digest + ".py",
        "headers": {"Authorization": "Bearer " + token}, "body": body})
    assert response["status_code"] == 404
