"""用真实 FFmpeg 验证 Wan 预览完整恢复历史原音轨；不调用任何模型。"""
from __future__ import annotations

import array
import hashlib
import importlib
import importlib.util
import math
import wave
from pathlib import Path

import pytest

from app import storage as st
from app.digital_human import worker_client
from app.digital_human.errors import TalkingHeadBlocked
from app.tts import audio as tts_audio
from app.video import ffmpeg

SR = 16000


def _module():
    assert importlib.util.find_spec("app.digital_human.wan_audio") is not None, \
        "Wan 需要独立原音轨恢复模块"
    return importlib.import_module("app.digital_human.wan_audio")


def _hash(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _wav(path, seconds):
    # 持续变化的波形同时覆盖开头和结尾，不能靠固定纯音冒充未裁剪时序。
    samples = array.array("h", (
        int(11000 * math.sin(2 * math.pi * (210 * i / SR + 17 * (i / SR) ** 2))
            + 3000 * math.sin(2 * math.pi * 731 * i / SR))
        for i in range(int(seconds * SR))))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SR)
        output.writeframes(samples.tobytes())
    return samples


def _raw(path, *, video_seconds=10, audio_seconds=10, include_audio=True):
    args = ["-y", "-f", "lavfi", "-i", f"color=c=blue:s=160x90:r=25:d={video_seconds}"]
    if include_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=880:sample_rate={SR}:duration={audio_seconds}"]
    args += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-bf", "0"]
    if include_audio:
        args += ["-c:a", "aac"]
    result = ffmpeg.run_ffmpeg([*args, str(path)], timeout=40)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return path.read_bytes()


@pytest.fixture()
def media(tmp_path, monkeypatch):
    workspace = Path(tmp_path)
    runtime = workspace / "production" / "backend" / "runtime"
    source_dir = runtime / "audio" / "wan_fixture"
    source_dir.mkdir(parents=True)
    monkeypatch.setattr(worker_client, "_workspace_root", lambda: workspace)
    monkeypatch.setattr(tts_audio, "runtime_root", lambda: str(runtime))
    source_path = source_dir / "historical.wav"
    samples = _wav(source_path, 10)
    work_id = st.new_id("wan_audio_work")
    audio_id = st.new_id("audio")
    record = {"id": audio_id, "work_id": work_id, "kind": "full", "confirmed": True,
              "is_mock": False, "file_hash": _hash(source_path.read_bytes()), "duration_ms": 10000,
              "storage_uri": "audio/wan_fixture/historical.wav"}
    st.put("audios", audio_id, record, {"work_id": work_id})
    run = {"id": st.new_id("run"), "work_id": work_id, "route": "WAN_3_0",
           "provider_params": {"audio_duration_ms": 10000, "input_snapshot": {
               "audio_asset_id": audio_id, "audio_hash": record["file_hash"]}}}
    # 当前作品故意指向新音轨，恢复旧运行时必须仍然使用上方历史音轨。
    st.put("works", work_id, {"id": work_id, "name": "Wan 音轨恢复夹具", "brief": {},
                              "final_audio_id": "newer_audio_not_for_this_run"})
    raw = _raw(workspace / "provider.mp4")
    return {"run": run, "record": record, "source": source_path, "samples": samples,
            "raw": raw, "runtime": runtime, "workspace": workspace}


def _prepare(media, **changes):
    return _module().prepare_original(media["run"], {"data": media["raw"], "probe": {
        "duration_ms": 10000, "has_video_track": True, "has_audio_track": True}, **changes})


def _decoded(path):
    result = ffmpeg.run_ffmpeg(["-v", "error", "-i", str(path), "-map", "0:a:0",
                               "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"], timeout=40)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    samples = array.array("h")
    samples.frombytes(result.stdout)
    return samples


def _correlation(left, right):
    size = min(len(left), len(right))
    lmean, rmean = sum(left[:size]) / size, sum(right[:size]) / size
    numerator = sum((left[i] - lmean) * (right[i] - rmean) for i in range(size))
    lnorm = sum((left[i] - lmean) ** 2 for i in range(size))
    rnorm = sum((right[i] - rmean) ** 2 for i in range(size))
    return numerator / math.sqrt(lnorm * rnorm)


def test_restores_complete_historical_audio_and_retains_provider_raw(media):
    out = _prepare(media)
    evidence = out["audio_restoration"]
    assert evidence["mode"] == "confirmed_original_audio_restored"
    assert evidence["wan_native_audio_preservation_verified"] is False
    assert evidence["audio_asset_id"] == media["record"]["id"]
    assert evidence["audio_hash"] == media["record"]["file_hash"]
    assert evidence["raw_file_hash"] == _hash(media["raw"])
    raw_path = media["runtime"] / evidence["raw_storage_uri"]
    preview = media["runtime"] / evidence["preview_storage_uri"]
    assert raw_path.read_bytes() == media["raw"]
    assert raw_path.with_suffix(".sha256").read_text("utf-8").strip() == _hash(media["raw"])
    assert out["data"] == preview.read_bytes() and out["data"] != media["raw"]
    assert out["probe"]["duration_ms"] == 10000
    assert out["probe"]["video_duration_ms"] >= 10000
    assert out["probe"]["audio_duration_ms"] >= 10000
    assert out["probe"]["file_hash"] == _hash(out["data"])
    assert out["probe"]["width"] == 160 and out["probe"]["height"] == 90
    actual = _decoded(preview)
    expected = media["samples"]
    assert len(actual) >= len(expected), "AAC 解码必须覆盖完整原音轨样本"
    assert _correlation(expected, actual[:len(expected)]) > .98
    assert _correlation(expected[-SR:], actual[len(expected) - SR:len(expected)]) > .98
    assert "checks" not in out and "approved" not in evidence
    # 同一作业相同下载重复回收应复用同一不可变结果。
    repeat = _prepare(media)
    assert repeat["probe"]["file_hash"] == out["probe"]["file_hash"]


@pytest.mark.parametrize("corruption", ["snapshot", "file"])
def test_wrong_audio_hash_blocks_after_preserving_raw(media, corruption):
    if corruption == "snapshot":
        media["run"]["provider_params"]["input_snapshot"]["audio_hash"] = "sha256:" + "0" * 64
    else:
        _wav(media["source"], 2)
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_AUDIO_HASH_MISMATCH"
    assert (media["runtime"] / "wan3" / media["run"]["id"] / "raw.mp4").read_bytes() == media["raw"]


def test_short_video_cannot_hide_behind_long_container_audio(media):
    media["raw"] = _raw(media["workspace"] / "video_short.mp4", video_seconds=8)
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_VIDEO_TOO_SHORT"
    assert error.value.detail["video_duration_ms"] == 8000
    assert error.value.detail["audio_duration_ms"] == 10000
    assert (media["runtime"] / "wan3" / media["run"]["id"] / "raw.mp4").read_bytes() == media["raw"]


def test_raw_without_audio_is_preserved_but_blocked(media):
    media["raw"] = _raw(media["workspace"] / "silent_provider.mp4", include_audio=False)
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_OUTPUT_AUDIO_MISSING"
    assert (media["runtime"] / "wan3" / media["run"]["id"] / "raw.mp4").exists()


@pytest.mark.parametrize("run_id", ["../escape", "nested/x", "C:\\escape", "", ".."])
def test_run_path_cannot_escape_its_runtime_directory(media, run_id):
    media["run"]["id"] = run_id
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_RUN_PATH_INVALID"
    assert not (media["runtime"] / "wan3").exists()


def test_same_run_refuses_different_raw_bytes_without_overwriting_first(media):
    _prepare(media)
    original = media["raw"]
    media["raw"] = _raw(media["workspace"] / "changed_provider.mp4", audio_seconds=11)
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_ARTIFACT_CONFLICT"
    assert (media["runtime"] / "wan3" / media["run"]["id"] / "raw.mp4").read_bytes() == original


def test_registered_duration_mismatch_blocks_restoration(media):
    media["record"]["duration_ms"] = 9360
    st.put("audios", media["record"]["id"], media["record"], {"work_id": media["run"]["work_id"]})
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_AUDIO_DURATION_MISMATCH"


@pytest.mark.parametrize("declared_ms,allowed", [(9950, True), (10001, False)])
def test_mp3_duration_tolerance_requires_the_same_billed_generation_seconds(media, declared_ms, allowed):
    mp3 = media["source"].with_suffix(".mp3")
    result = ffmpeg.run_ffmpeg(["-i", str(media["source"]), "-c:a", "libmp3lame", str(mp3)], timeout=40)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    digest = _hash(mp3.read_bytes())
    media["record"].update(storage_uri="audio/wan_fixture/historical.mp3", file_hash=digest,
                            duration_ms=declared_ms)
    st.put("audios", media["record"]["id"], media["record"], {"work_id": media["run"]["work_id"]})
    media["run"]["provider_params"]["input_snapshot"]["audio_hash"] = digest
    media["run"]["provider_params"]["audio_duration_ms"] = declared_ms
    if allowed:
        out = _prepare(media)
        assert out["audio_restoration"]["audio_duration_ms"] == 10000
        assert out["audio_restoration"]["decoded_source_samples"] == SR * 10
    else:
        with pytest.raises(TalkingHeadBlocked) as error:
            _prepare(media)
        assert error.value.code == "WAN_AUDIO_DURATION_MISMATCH"


@pytest.mark.parametrize("sample_count,registered_ms", [(SR - 1, 1000), (15 * SR + 1, 15000)])
def test_wav_real_sample_duration_cannot_round_inside_the_allowed_boundary(media, sample_count, registered_ms):
    _wav(media["source"], sample_count / SR)
    digest = _hash(media["source"].read_bytes())
    media["record"].update(file_hash=digest, duration_ms=registered_ms)
    st.put("audios", media["record"]["id"], media["record"], {"work_id": media["run"]["work_id"]})
    media["run"]["provider_params"]["input_snapshot"]["audio_hash"] = digest
    media["run"]["provider_params"]["audio_duration_ms"] = registered_ms
    with pytest.raises(TalkingHeadBlocked) as error:
        _prepare(media)
    assert error.value.code == "WAN_AUDIO_DURATION_MISMATCH"
