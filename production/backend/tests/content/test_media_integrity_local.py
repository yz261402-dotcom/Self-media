"""只运行本地 FFmpeg，不初始化数据库、不访问运行资产或调用 provider。"""
import json
import subprocess

import pytest

from app.content import media, visual_pipeline as vp
from app.content.errors import ContentRejected
from app.video import ffmpeg


@pytest.fixture
def audio_longer_video(tmp_path):
    output = tmp_path / "audio-longer.mp4"
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=10:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2.3", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(output)], check=True, capture_output=True)
    return output


def test_longer_audio_tail_does_not_extend_visual_sampling(audio_longer_video):
    metadata = media.probe_video(str(audio_longer_video))
    assert metadata["duration_ms"] == 2000
    media.validate_video_complete(str(audio_longer_video))
    frames = vp.sample_frames(str(audio_longer_video), metadata["duration_ms"])
    assert frames[0]["timestamp_ms"] == 0
    assert frames[-1]["timestamp_ms"] == 1900
    assert all(frame["timestamp_ms"] < 2000 for frame in frames)


def test_format_duration_remains_fallback_when_video_duration_is_absent(monkeypatch):
    payload = {"format": {"duration": "2.3"},
               "streams": [{"codec_type": "video", "width": 320, "height": 240}]}
    monkeypatch.setattr(media.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b""))
    assert media.probe_video("no-stream-duration.webm")["duration_ms"] == 2300


def test_corrupt_faststart_video_is_still_rejected(audio_longer_video, tmp_path):
    data = audio_longer_video.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[:len(data) // 2])
    assert media.probe_video(str(truncated))["duration_ms"] == 2000
    with pytest.raises(ContentRejected) as integrity_error:
        media.validate_video_complete(str(truncated))
    assert integrity_error.value.code == "MEDIA_INCOMPLETE"
    with pytest.raises(ContentRejected) as sampling_error:
        vp.sample_frames(str(truncated), 2000)
    assert sampling_error.value.code == "MEDIA_INCOMPLETE"
    assert truncated.read_bytes() == data[:len(data) // 2]


def test_webm_video_duration_tag_excludes_longer_audio_tail(tmp_path):
    output = tmp_path / "audio-longer.webm"
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=10:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2.3", "-c:v", "libvpx-vp9", "-deadline", "realtime",
                    "-cpu-used", "8", "-c:a", "libopus", str(output)], check=True, capture_output=True)
    metadata = media.probe_video(str(output))
    # Opus 的预滚会令 WebM 时间线多出约 7ms，仍不能以 2.3s 音轨作为视频时长。
    assert 2000 <= metadata["duration_ms"] <= 2020
    media.validate_video_complete(str(output))
    frames = vp.sample_frames(str(output), metadata["duration_ms"])
    assert frames[0]["timestamp_ms"] == 0
    assert 1900 <= frames[-1]["timestamp_ms"] <= 1920


@pytest.mark.parametrize("tag", ["", "N/A", "bad-value", "00:00:00.000000000", "00:61:02", "00:00:61"])
def test_invalid_video_duration_tag_does_not_mask_valid_format_duration(monkeypatch, tag):
    payload = {"format": {"duration": "2.3"}, "streams": [{"codec_type": "video", "width": 320,
                "height": 240, "tags": {"DURATION": tag}}]}
    monkeypatch.setattr(media.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b""))
    assert media.probe_video("invalid-duration-tag.webm")["duration_ms"] == 2300
