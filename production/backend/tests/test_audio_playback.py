"""列表与合成响应必须带可播放的本地音频地址，而不只是 storage_uri。"""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.main import app
from app.avatar import person_image


@pytest.mark.parametrize("uri_kind", ["runtime_relative", "workspace_relative", "remote_with_local_copy"])
def test_real_audio_list_returns_verified_playback_url(tmp_path, monkeypatch, uri_kind):
    monkeypatch.setattr(person_image, "workspace_root", lambda: str(tmp_path))
    wid, aid = st.new_id("play"), st.new_id("audio")
    rel = f"production/backend/runtime/audio/{wid}/{aid}.wav"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    payload = b"RIFF-playback-http-fixture"
    target.write_bytes(payload)
    uri = {"runtime_relative": f"audio/{wid}/{aid}.wav", "workspace_relative": rel,
           "remote_with_local_copy": f"https://example.invalid/{aid}.wav"}[uri_kind]
    st.put("works", wid, {"id": wid})
    audio = {"id": aid, "work_id": wid, "storage_uri": uri, "url": None,
             "file_hash": "sha256:" + hashlib.sha256(payload).hexdigest(), "is_mock": False}
    st.put("audios", aid, audio, {"work_id": wid})
    client = TestClient(app)
    result = client.get(f"/api/v1/works/{wid}/audios").json()[0]
    assert result["url"] and result["url"].startswith("/api/v1/media?")
    assert client.get(result["url"]).content == payload
    target.write_bytes(b"changed")
    assert client.get(f"/api/v1/works/{wid}/audios").json()[0]["url"] is None


def test_mock_or_outside_audio_has_no_playback_url(tmp_path, monkeypatch):
    from app.main import _audio_out
    monkeypatch.setattr(person_image, "workspace_root", lambda: str(tmp_path))
    for row in [{"is_mock": True, "url": "/fake.wav"}, {"storage_uri": "../outside.wav"}]:
        assert _audio_out(row).get("url") is None
