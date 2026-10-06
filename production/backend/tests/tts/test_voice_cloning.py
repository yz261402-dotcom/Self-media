"""音色克隆的离线契约、真实文件校验、去重与首次费用状态。"""
from __future__ import annotations

import base64
import importlib
import io
import json
import re
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage as st
from app.tts import audio
from app.tts.errors import TtsBlocked, TtsRejected


def module():
    assert importlib.util.find_spec("app.tts.cloning") is not None, "音色克隆服务尚未实现"
    return importlib.import_module("app.tts.cloning")


def wav_bytes(seconds=10):
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * int(8000 * seconds))
    return out.getvalue()


def payload(**overrides):
    return {"request_id": str(uuid.uuid4()), "name": "本人授权音色",
            "filename": "样本.wav", "content_base64": base64.b64encode(wav_bytes()).decode(),
            "source_evidence": "本人录音，授权本项目合成并商业使用。",
            "rights_ack": True, "activation_cost_ack": True, **overrides}


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "runtime_root", lambda: str(tmp_path))


class Transport:
    def __init__(self, clone=None, upload=None):
        self.calls = []
        self.upload = upload or {"status_code": 200, "body": {
            "base_resp": {"status_code": 0}, "file": {"file_id": 987654321}}}
        self.clone = clone or {"status_code": 200, "body": {
            "base_resp": {"status_code": 0}, "demo_audio": "", "input_sensitive": False}}

    def __call__(self, req):
        self.calls.append(req)
        response = self.upload if req["url"].endswith("/files/upload") else self.clone
        if isinstance(response, Exception):
            raise response
        return response


def create(data=None, transport=None):
    return module().create_clone(data or payload(), transport=transport or Transport(), mode="integration")


def test_upload_and_clone_without_paid_preview_preserve_source(tmp_path):
    data, transport = payload(), Transport()
    result = create(data, transport)
    voice, job = result["voice"], result["clone_job"]
    assert job["state"] == "succeeded"
    assert len(transport.calls) == 2
    upload, clone = transport.calls
    assert upload["data"] == {"purpose": "voice_clone"}
    assert upload["files"]["file"][1] == wav_bytes()
    assert "Content-Type" not in upload.get("headers", {})
    assert clone["json"] == {"file_id": 987654321, "voice_id": voice["remote_voice_id"]}
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{6,254}[A-Za-z0-9]", voice["remote_voice_id"])
    assert Path(job["source_path"]).read_bytes() == wav_bytes()
    assert voice["approval_state"] == "pending"
    assert not voice["baseline_audio_asset_id"] and not voice["baseline_audio_hash"]
    meta = voice["clone_metadata"]
    assert meta["activation_state"] == "pending" and meta["activated_at"] is None
    assert datetime.fromisoformat(meta["expires_at"]) - datetime.fromisoformat(meta["created_at"]) == timedelta(days=7)
    license_row = st.get("license_records", voice["license_id"])
    assert license_row["verified_by"].startswith("user")
    assert "非法律" in license_row["evidence"]
    assert st.get("source_records", voice["source_record_id"])


def test_duplicate_replays_same_job_and_different_input_conflicts():
    data, transport = payload(), Transport()
    first = create(data, transport)
    second = create(data, transport)
    assert second["clone_job"]["id"] == first["clone_job"]["id"]
    assert len(transport.calls) == 2
    with pytest.raises(TtsRejected, match="相同 request_id"):
        create({**data, "name": "另一音色"}, transport)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("change,code", [
    ({"rights_ack": False}, "VOICE_CLONE_RIGHTS_REQUIRED"),
    ({"activation_cost_ack": False}, "VOICE_CLONE_COST_ACK_REQUIRED"),
    ({"source_evidence": "  "}, "VOICE_CLONE_SOURCE_REQUIRED"),
    ({"filename": "bad.flac"}, "VOICE_CLONE_FORMAT_INVALID"),
    ({"content_base64": "!?"}, "VOICE_CLONE_BASE64_INVALID"),
    ({"content_base64": base64.b64encode(b"not audio").decode()}, "VOICE_CLONE_AUDIO_INVALID"),
    ({"filename": "fake.mp3"}, "VOICE_CLONE_FORMAT_INVALID"),
    ({"content_base64": base64.b64encode(wav_bytes(9)).decode()}, "VOICE_CLONE_DURATION_INVALID"),
    ({"content_base64": base64.b64encode(wav_bytes(301)).decode()}, "VOICE_CLONE_DURATION_INVALID"),
])
def test_invalid_input_never_reaches_provider(change, code):
    transport = Transport()
    with pytest.raises(TtsRejected) as err:
        create(payload(**change), transport)
    assert err.value.code == code
    assert transport.calls == []


def test_oversized_audio_rejected_before_decode():
    transport = Transport()
    with pytest.raises(TtsRejected) as err:
        create(payload(content_base64=base64.b64encode(b"0" * 20_000_001).decode()), transport)
    assert err.value.code == "VOICE_CLONE_TOO_LARGE"
    assert not transport.calls


def test_truncated_wav_is_not_accepted_even_when_header_looks_long_enough():
    with pytest.raises(TtsRejected) as err:
        create(payload(content_base64=base64.b64encode(wav_bytes(12)[:-8000]).decode()))
    assert err.value.code == "VOICE_CLONE_AUDIO_INVALID"


@pytest.mark.parametrize("response,state", [
    ({"status_code": 200, "body": {"base_resp": {"status_code": 2038}}}, "failed"),
    ({"status_code": None, "error": "network_error"}, "unknown"),
    (TimeoutError("Authorization: Bearer NEVER_SAVE_ME"), "unknown"),
    ({"status_code": 200, "body": {}}, "unknown"),
])
def test_provider_failures_persist_without_retry_or_fabricated_voice(response, state):
    data, transport = payload(), Transport(clone=response)
    result = create(data, transport)
    assert result["voice"] is None
    assert result["clone_job"]["state"] == state
    assert module().get_clone(data["request_id"])["state"] == state
    assert "NEVER_SAVE_ME" not in json.dumps(result)
    create(data, transport)
    assert len(transport.calls) == 2


def test_mock_is_persisted_but_never_uploads_or_creates_voice():
    data, transport = payload(), Transport()
    result = module().create_clone(data, transport=transport, mode="mock")
    assert result["clone_job"]["state"] == "blocked"
    assert result["voice"] is None and transport.calls == []


def test_first_use_charge_reads_current_voice_and_only_success_activates():
    mod = module()
    voice = create()["voice"]
    assert mod.synthesis_charge(voice, "你好A") == 991
    mod.begin_activation(voice["id"], "job-first")
    with pytest.raises(TtsBlocked):
        mod.synthesis_charge(voice, "你好A")
    with pytest.raises(TtsBlocked):
        mod.begin_activation(voice["id"], "job-second")
    mod.mark_activated(voice["id"], "job-first")
    assert mod.synthesis_charge(voice, "你好A") == 1
    mod.mark_activation_unknown(voice["id"], "job-later")
    assert mod.synthesis_charge(voice, "你好A") == 1


def test_known_rejection_releases_activation_but_uncertain_failure_blocks():
    mod = module()
    voice = create()["voice"]
    mod.begin_activation(voice["id"], "job-first")
    mod.cancel_activation(voice["id"], "job-first")
    assert mod.synthesis_charge(voice, "你好") == 991
    mod.begin_activation(voice["id"], "job-second")
    mod.mark_activation_unknown(voice["id"], "job-second")
    with pytest.raises(TtsBlocked) as err:
        mod.synthesis_charge(voice, "你好")
    assert err.value.code == "VOICE_CLONE_ACTIVATION_UNKNOWN"


@pytest.mark.parametrize("change,code", [
    ({"expires_at": "2020-01-01T00:00:00+00:00"}, "VOICE_CLONE_EXPIRED"),
    ({"activation_cost_ack": False}, "VOICE_CLONE_COST_ACK_REQUIRED"),
])
def test_expired_or_unacknowledged_voice_cannot_be_synthesized(change, code):
    voice = create()["voice"]
    updated = st.get("voices", voice["id"])
    updated["clone_metadata"].update(change)
    st.put("voices", voice["id"], updated)
    with pytest.raises(TtsBlocked) as err:
        module().synthesis_charge(voice, "你好")
    assert err.value.code == code


def test_nonclone_has_zero_extra_charge():
    assert module().synthesis_charge({"id": "not-a-clone"}, "你好") == 0


def test_api_preserves_contract_and_mock_errors(monkeypatch):
    mod = module()
    api = importlib.import_module("app.voice_clone_api")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    data = payload()
    response = client.post("/api/v1/voice-clones", json=data)
    assert response.status_code == 200
    assert response.json()["voice"] is None
    job = response.json()["clone_job"]
    assert job["state"] == "blocked"
    assert client.get(f"/api/v1/voice-clones/{data['request_id']}").json()["id"] == job["id"]
    bad = client.post("/api/v1/voice-clones", json=payload(rights_ack=False))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VOICE_CLONE_RIGHTS_REQUIRED"
    assert bad.json()["error"]["retryable"] is False
    assert client.get("/api/v1/voice-clones/not-found").status_code == 404


@pytest.mark.parametrize("suffix,codec", [(".mp3", "libmp3lame"), (".m4a", "aac")])
def test_actual_compressed_audio_is_decoded_and_preserved(tmp_path, suffix, codec):
    from app.video import ffmpeg

    source = Path(tmp_path) / "original.wav"
    source.write_bytes(wav_bytes(11))
    compressed = Path(tmp_path) / ("source" + suffix)
    result = ffmpeg.run_ffmpeg(["-y", "-v", "error", "-i", str(source), "-c:a", codec, str(compressed)])
    assert result.returncode == 0
    raw = compressed.read_bytes()
    transport = Transport()
    output = create(payload(filename="source" + suffix, content_base64=base64.b64encode(raw).decode()), transport)
    assert output["clone_job"]["state"] == "succeeded"
    assert output["clone_job"]["audio_metadata"]["decoded"] is True
    assert transport.calls[0]["files"]["file"][1] == raw


def _recorder_adts(tmp_path):
    from app.video import ffmpeg

    source = Path(tmp_path) / "recording.wav"
    source.write_bytes(wav_bytes(11))
    recording = Path(tmp_path) / "recording.m4a"
    result = ffmpeg.run_ffmpeg([
        "-y", "-v", "error", "-i", str(source), "-c:a", "aac", "-f", "adts", str(recording),
    ])
    assert result.returncode == 0
    return recording.read_bytes()


def test_recorder_adts_named_m4a_uploads_lossless_m4a_and_preserves_source(tmp_path):
    from app.video import ffmpeg

    raw = _recorder_adts(tmp_path)
    data = payload(filename="recording.m4a", content_base64=base64.b64encode(raw).decode())
    transport = Transport()
    result = create(data, transport)
    job = result["clone_job"]
    assert job["state"] == "succeeded"
    assert Path(job["source_path"]).read_bytes() == raw
    uploaded = transport.calls[0]["files"]["file"]
    assert uploaded[0].endswith(".m4a") and uploaded[2] == "audio/mp4"
    assert uploaded[1] != raw and uploaded[1][4:8] == b"ftyp"
    upload_path = Path(job["upload_path"])
    assert upload_path.read_bytes() == uploaded[1]
    assert job["audio_metadata"]["decoded"] is True
    pcm = []
    for path in [Path(job["source_path"]), upload_path]:
        decoded = ffmpeg.run_ffmpeg([
            "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "pipe:1",
        ])
        assert decoded.returncode == 0 and not decoded.stderr.strip()
        pcm.append(decoded.stdout)
    assert pcm[0] == pcm[1]
    assert create(data, transport)["clone_job"]["id"] == job["id"]
    assert len(transport.calls) == 2


def test_broken_recorder_adts_never_reaches_provider(tmp_path):
    raw = _recorder_adts(tmp_path)[:-3]
    transport = Transport()
    with pytest.raises(TtsRejected) as err:
        create(payload(filename="recording.m4a", content_base64=base64.b64encode(raw).decode()), transport)
    assert err.value.code == "VOICE_CLONE_AUDIO_INVALID"
    assert not transport.calls


def test_concurrent_duplicate_does_not_upload_twice():
    data, transport = payload(), Transport()
    started, release = Event(), Event()

    def slow(req):
        if req["url"].endswith("/files/upload"):
            started.set()
            assert release.wait(5)
        return transport(req)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create, data, slow)
        try:
            assert started.wait(5)
            replay = create(data, transport)
            assert replay["clone_job"]["state"] == "processing"
            assert replay["voice"] is None
        finally:
            release.set()
        assert first.result()["clone_job"]["state"] == "succeeded"
    assert len(transport.calls) == 2


def test_real_http_transport_sends_multipart_and_never_sends_preview(monkeypatch):
    import httpx

    real_client = httpx.Client
    requests = []
    monkeypatch.setenv("AVATAR_STUDIO_MINIMAX_API_KEY", "unit-test-only-secret")

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/files/upload"):
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert b'name="purpose"' in request.content and b"voice_clone" in request.content
            assert wav_bytes() in request.content
            return httpx.Response(200, json={"base_resp": {"status_code": 0}, "file": {"file_id": 123}})
        body = json.loads(request.content)
        assert set(body) == {"file_id", "voice_id"}
        return httpx.Response(200, json={"base_resp": {"status_code": 0}})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    mod = module()
    result = create(transport=mod.http_transport)
    assert result["clone_job"]["state"] == "succeeded"
    assert len(requests) == 2
    assert "unit-test-only-secret" not in json.dumps(result)


def test_explicit_provider_failure_during_upload_never_calls_clone():
    transport = Transport(upload={"status_code": 200, "body": {"base_resp": {"status_code": 1008}}})
    result = create(transport=transport)
    assert result["clone_job"]["state"] == "failed"
    assert result["voice"] is None and len(transport.calls) == 1


def test_wrong_job_cannot_mark_voice_activated():
    mod = module()
    voice = create()["voice"]
    mod.begin_activation(voice["id"], "first")
    with pytest.raises(TtsBlocked):
        mod.mark_activated(voice["id"], "unrelated")
    assert st.get("voices", voice["id"])["clone_metadata"]["activated_at"] is None
