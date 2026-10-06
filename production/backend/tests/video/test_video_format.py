"""T07 成片规格与处理逻辑验收（AC-MEDIA-01/02/03/06 可代码化部分）。

用 ffmpeg 合成夹具证明处理逻辑：规格 probe（1080x1920/H.264/AAC/30fps CFR/
yuv420p/faststart）、两遍响度实测（-14 LUFS ±1、TP ≤ -1.5）、字幕烧录、
AI 标识常显、封面 1080×1920、黑帧/冻结粗筛、非 CFR 被拒。
"""
from __future__ import annotations

import os

import pytest

from app.video import ffmpeg as v_ff
from app.video import render as v_render
from app.video import subtitles as v_sub
from app.video import timeline as v_tl
from app.video.errors import VideoBlocked

SCRIPT = "大家好，今天聊聊效率工具。第一，先把事情排进日程。第二，用对工具省时间。"


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    try:
        v_ff.ffmpeg_bin()
    except VideoBlocked:
        pytest.skip("本机未找到 ffmpeg，跳过媒体夹具用例")
    d = tmp_path_factory.mktemp("fmt")
    video = str(d / "src.mp4")
    audio = str(d / "src.wav")
    cp = v_ff.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                          "testsrc2=size=270x480:rate=30:duration=6",
                          "-c:v", "libx264", "-threads:v", "2", "-pix_fmt", "yuv420p", video], timeout=180)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-400:]
    cp = v_ff.run_ffmpeg(["-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                          "-ar", "32000", "-ac", "1", audio], timeout=120)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-400:]
    return video, audio, d


def _build_ass(d) -> str:
    tl = v_tl.EditTimeline.single(6000)
    cues = v_sub.map_cues([
        {"start_ms": 0, "end_ms": 1700, "text": "大家好，今天聊聊效率工具。"},
        {"start_ms": 1700, "end_ms": 3300, "text": "第一，先把事情排进日程。"},
        {"start_ms": 3300, "end_ms": 4700, "text": "第二，用对工具省时间。"},
    ], tl)
    v_sub.check_cues(cues, final_duration_ms=tl.final_duration_ms, script_text=SCRIPT)
    path = str(d / "fixture.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(v_sub.build_ass(cues, title="T"))
    return path


@pytest.fixture(scope="module")
def master(media):
    video, audio, d = media
    ass = _build_ass(d)
    out = str(d / "master.mp4")
    log = v_render.render_master(video_path=video, audio_path=audio,
                                 out_path=out, ass_path=ass)
    return out, log, media[2]


def test_master_spec_passes_probe_and_decode(master):
    out, _, _ = master
    info = v_render.verify_master(out)
    p = info["probe"]
    assert (p["width"], p["height"]) == (1080, 1920)
    assert p["codec"] == "h264" and p["pix_fmt"] == "yuv420p"
    assert p["fps"] == "30/1"
    assert p["audio_codec"] == "aac" and info["audio_sample_rate"] == 48000
    assert abs(p["duration"] - 6.0) < 0.2


def test_master_loudness_measured_not_assumed(master):
    out, _, _ = master
    m = v_ff.measure_loudness(out)
    assert abs(m["input_i"] - (-14.0)) <= 1.0
    assert m["input_tp"] <= -1.5


def test_loudnorm_target_keeps_encode_headroom():
    """处理目标必须比验收门槛低 ≥0.5 dB：AAC 编码会抬高真实峰值。

    真实事故：以 TP=-1.5 处理真实 OmniHuman 原片 + 真实 MiniMax 音频，
    成片实测 true peak = -1.49 > -1.5，被 verify_master 拦下。
    修法是给**处理目标**留编码裕度，验收门槛保持 -1.5 不放宽。
    """
    assert v_render.TP_PROCESS <= v_render.TARGET_TP - 0.5, (
        f"处理目标 {v_render.TP_PROCESS} 未给 AAC 编码留足裕度，"
        f"验收门槛 {v_render.TARGET_TP} 会被编码溢出击穿")
    af = v_render._loudnorm_second_pass(
        {"input_i": -20.0, "input_tp": -3.0, "input_lra": 5.0, "input_thresh": -30.0})
    assert f"TP={v_render.TP_PROCESS}" in af, af
    assert f"TP={v_render.TARGET_TP}:" not in af, "不得直接用验收门槛当处理目标"


def test_loudnorm_processing_target_offsets_aac_integrated_loudness_loss():
    """AAC 成片实测会低于 loudnorm 的 PCM 目标，处理目标需留响度余量。"""
    quiet = {"input_i": -24.0, "input_tp": -3.0, "input_lra": 5.0, "input_thresh": -30.0}
    normal = {"input_i": -20.0, "input_tp": -3.0, "input_lra": 5.0, "input_thresh": -30.0}
    assert v_render._processing_lufs(quiet) == v_render.PROCESS_LUFS
    assert v_render._processing_lufs(normal) == v_render.TARGET_LUFS
    af = v_render._loudnorm_second_pass(
        quiet)
    assert f"I={v_render.PROCESS_LUFS}" in af


def test_black_freeze_probe_runs(master):
    out, _, _ = master
    ev = v_ff.detect_black_freeze(out)
    assert isinstance(ev["black_events"], list)
    assert isinstance(ev["freeze_events"], list)


def test_subtitles_actually_burned_into_frames(master, media):
    """同一管线不带 ASS 重渲染，两版做 PSNR：烧录字幕必然带来画面差异。"""
    out, _, d = master
    video, audio, _ = media
    plain = str(d / "plain.mp4")
    cp = v_ff.run_ffmpeg(["-y", "-i", video, "-i", audio,
                          "-map", "0:v:0", "-map", "1:a:0",
                          "-vf", v_render._base_video_filters(v_render._find_font(), None),
                          "-af", "aresample=48000",
                          "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                          "-pix_fmt", "yuv420p", "-r", "30",
                          v_render._cvs_flag(), "cfr",
                          "-c:a", "aac", "-b:a", "192k", "-shortest", plain],
                         timeout=600)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-400:]
    cp = v_ff.run_ffmpeg(["-i", out, "-i", plain, "-filter_complex", "psnr",
                          "-f", "null", "-"], timeout=600)
    text = cp.stderr.decode("utf-8", "replace")
    import re
    m = re.search(r"average:([0-9.]+)", text)
    assert m, "未解析到 PSNR"
    assert float(m.group(1)) < 40.0  # 烧录字幕带来显著画面差异


def test_ai_label_persistent_from_first_frame(master, media):
    """AI 标识首帧即存在：首帧与无标识版在标识区域应有差异（粗检）。"""
    out, _, d = master
    video, audio, _ = media
    nolabel = str(d / "nolabel.mp4")
    font = v_render._find_font()
    cp = v_ff.run_ffmpeg(["-y", "-i", video, "-i", audio,
                          "-map", "0:v:0", "-map", "1:a:0",
                          "-vf", ",".join([
                              "scale=1080:1920:force_original_aspect_ratio=decrease",
                              "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black", "fps=30"]),
                          "-af", "aresample=48000",
                          "-c:v", "libx264", "-threads:v", "2", "-crf", "18", "-pix_fmt", "yuv420p",
                          "-r", "30", v_render._cvs_flag(), "cfr",
                          "-c:a", "aac", "-shortest", nolabel], timeout=600)
    assert cp.returncode == 0
    # 提取两版首帧，直接比对标识区域（右下角 320x120）像素是否不同
    import subprocess
    a = str(d / "first_labeled.png")
    b = str(d / "first_plain.png")
    for src, dst in ((out, a), (nolabel, b)):
        cp = subprocess.run([v_ff.ffmpeg_bin(), "-y", "-i", src, "-frames:v", "1",
                             "-vf", "crop=320:120:720:1760", dst],
                            capture_output=True, timeout=120)
        assert cp.returncode == 0
    with open(a, "rb") as f1, open(b, "rb") as f2:
        assert f1.read() != f2.read(), "标识区域应因 AI 标识而不同"


def test_cover_is_1080x1920_with_title(master):
    out, _, d = master
    cover = str(d / "cover.jpg")
    v_render.render_cover(video_path=out, out_path=cover, title="效率工具两件事")
    info = v_ff.probe(cover)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)


def test_non_cfr_output_is_rejected(media):
    """负例：25fps 非 CFR 输出必须被规格验收拒绝（AC-MEDIA-01 时间戳检查）。"""
    video, audio, d = media
    bad = str(d / "bad_fps.mp4")
    cp = v_ff.run_ffmpeg(["-y", "-i", video, "-i", audio,
                          "-map", "0:v:0", "-map", "1:a:0",
                          "-vf", "scale=1080:1920", "-r", "25",
                          "-c:v", "libx264", "-threads:v", "2", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-shortest", bad], timeout=600)
    assert cp.returncode == 0
    with pytest.raises(VideoBlocked) as e:
        v_render.verify_master(bad)
    assert e.value.code == "FPS_NOT_CFR"


def test_probe_rejects_missing_file():
    with pytest.raises(VideoBlocked) as e:
        v_ff.probe("Z:/definitely/missing.mp4")
    assert e.value.code == "MEDIA_FILE_MISSING"
