"""真实 FFmpeg 验证短动作参考能覆盖目标音频；不执行模型或 GPU。"""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from app.video import ffmpeg


WORKERS_SRC = Path(__file__).resolve().parents[3] / "workers" / "m2_gpu"


@pytest.fixture()
def driving_media(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(WORKERS_SRC))
    monkeypatch.setenv("PATH", str(Path(ffmpeg.ffmpeg_bin()).parent) + os.pathsep + os.environ.get("PATH", ""))
    import runner_liveportrait as runner

    clip = Path(tmp_path) / "three_seconds.mp4"
    result = ffmpeg.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
        "-t", "3.2", "-c:v", "libx264", "-threads", "2", str(clip),
    ], timeout=30)
    assert result.returncode == 0
    return runner, clip, tmp_path


def test_short_reference_repeats_beyond_twice_to_cover_final_audio(driving_media):
    runner, clip, directory = driving_media
    output, notes = runner.prepare_driving(str(clip), 23.2, str(directory))
    info = ffmpeg.probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == math.ceil(23.2 * 25)
    assert float(info["format"]["duration"]) == pytest.approx(23.2, abs=.001)
    assert any("预计 7 处循环接缝" in note for note in notes)
    ffmpeg.full_decode_check(output)


def test_longer_reference_is_trimmed_without_claiming_a_loop(driving_media):
    runner, clip, directory = driving_media
    output, notes = runner.prepare_driving(str(clip), 2.5, str(directory))
    info = ffmpeg.probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == math.ceil(2.5 * 25)
    assert not notes
