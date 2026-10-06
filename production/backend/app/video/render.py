"""真实渲染管线（AC-MEDIA-01/02/03、AC-MEDIA-06 的确定性部分）。

全部走 FFmpeg 子进程，参数即规格：
- 视频：1080×1920、H.264(libx264)、yuv420p、30 fps **CFR**、faststart；
- 音频：AAC-LC、48 kHz、192k、两遍响度（loudnorm 实测 → 线性应用），目标
  综合响度 -14 LUFS、true peak ≤ -1.5 dBTP，成品**实测**留证；
- 字幕：libass 烧录（ASS 来自终时间轴），AI 标识 drawtext 首帧至末帧常显；
- 封面：取帧 + 标题 + AI 标识，1080×1920；
- 验收：probe 规格、完整解码零错误、响度实测、黑帧/冻结粗筛、moov 前置。

清晰度采用固定缩放、轻量去噪与锐化参数；不宣称真实超分或增加原生细节。
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Dict, List, Optional

from . import ffmpeg as ff
from .errors import VideoBlocked

TARGET_LUFS = -14.0
# loudnorm 的第二遍作用在编码前 PCM；AAC 编码后综合响度在短口播上会再下降，
# 因此处理目标需要高于最终验收目标，最终文件仍由 verify_master 实测把关。
PROCESS_LUFS = -12.0
QUIET_INPUT_LUFS = -23.0
TARGET_TP = -1.5          # 验收门槛：成片实测 true peak 不得高于此值
# 处理目标：比验收门槛再低 1.0 dB 的"编码裕度"。
# 真实证据：loudnorm 归一化的是**编码前** PCM，AAC 有损编码会引入峰值溢出——
# 真实 OmniHuman 原片 + 真实 MiniMax 音频以 TP=-1.5 渲染后，成片实测 -1.49，
# 被 verify_master 正确拦下（TRUEPEAK_OVER）。故处理目标留裕度，验收门槛不放宽。
TP_PROCESS = -2.5
WIDTH, HEIGHT, FPS = 1080, 1920, 30
AUDIO_SR = 48000

_DEFAULT_FONT_DIR = r"C:\Windows\Fonts"
_FONT_CANDIDATES = ("msyh.ttc", "msyhbd.ttc", "simhei.ttf")


def _find_font() -> str:
    for name in _FONT_CANDIDATES:
        p = os.path.join(_DEFAULT_FONT_DIR, name)
        if os.path.isfile(p):
            return p
    raise VideoBlocked("FONT_NOT_AVAILABLE",
                       f"默认字体目录 {_DEFAULT_FONT_DIR} 下未找到中文字体 "
                       f"{_FONT_CANDIDATES}")


def _esc_filter_path(path: str) -> str:
    """ffmpeg 滤镜参数内的路径转义（Windows 盘符冒号最关键）。"""
    p = path.replace("\\", "/")
    p = p.replace("'", r"\'").replace(":", r"\:")
    p = re.sub(r",", r"\,", p)
    return p


def _esc_drawtext(text: str) -> str:
    t = text.replace("\\", "\\\\").replace("'", r"\'")
    t = t.replace(":", r"\:").replace("%", r"\%")
    return t


def _cvs_flag() -> str:
    """ffmpeg ≥5 用 -fps_mode cfr，旧版用 -vsync cfr。"""
    cp = subprocess.run([ff.ffmpeg_bin(), "-version"], capture_output=True, timeout=30)
    m = re.search(r"ffmpeg version \w+-(\d+)\.(\d+)", cp.stdout.decode("utf-8", "replace"))
    if m and int(m.group(1)) >= 5:
        return "-fps_mode"
    return "-vsync"


def _processing_lufs(m: Dict[str, float]) -> float:
    """仅为明显偏低的输入补偿 AAC 编码后的响度损失。"""
    return PROCESS_LUFS if float(m.get("input_i", TARGET_LUFS)) <= QUIET_INPUT_LUFS else TARGET_LUFS


def _loudnorm_second_pass(m: Dict[str, float]) -> str:
    parts = [f"loudnorm=I={_processing_lufs(m)}:TP={TP_PROCESS}:LRA=11",
             f"measured_I={m['input_i']}", f"measured_TP={m['input_tp']}",
             f"measured_LRA={m['input_lra']}", f"measured_thresh={m['input_thresh']}",
             "linear=true"]
    if "target_offset" in m:
        parts.append(f"offset={m['target_offset']}")
    return ":".join(parts)


def _ai_label_filter(font: str) -> str:
    return (f"drawtext=fontfile='{_esc_filter_path(font)}'"
            f":text='{_esc_drawtext('AI 生成内容')}'"
            ":fontsize=34:fontcolor=white@0.9:box=1:boxcolor=black@0.45"
            ":boxborderw=12:x=w-tw-40:y=h-th-40")


def _base_video_filters(font: str, ass_path: Optional[str]) -> str:
    chain = [
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black",
        "setsar=1",
        "hqdn3d=1.5:1.5:3:3",
        "unsharp=5:5:0.35:5:5:0",
        f"fps={FPS}",
    ]
    if ass_path:
        chain.append(f"ass=filename='{_esc_filter_path(ass_path)}'"
                     f":fontsdir='{_esc_filter_path(_DEFAULT_FONT_DIR)}'")
    chain.append(_ai_label_filter(font))
    return ",".join(chain)


def render_master(*, video_path: str, audio_path: str, out_path: str,
                  ass_path: Optional[str] = None) -> Dict[str, Any]:
    """两遍响度 + 合成 + 烧录。返回 loudness 测量与滤镜参数留证。"""
    if not os.path.isfile(video_path) or not os.path.isfile(audio_path):
        raise VideoBlocked("MEDIA_FILE_MISSING",
                           f"渲染输入缺失: video={video_path} audio={audio_path}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    font = _find_font()

    m1 = ff.measure_loudness(audio_path)
    cvs = _cvs_flag()
    args: List[str] = ["-y", "-threads", "2", "-filter_threads", "2",
                       "-i", video_path, "-i", audio_path,
                       "-map", "0:v:0", "-map", "1:a:0",
                       "-vf", _base_video_filters(font, ass_path),
                       "-af", _loudnorm_second_pass(m1),
                       "-c:v", "libx264", "-threads:v", "2", "-preset", "medium", "-crf", "18",
                       "-profile:v", "high", "-pix_fmt", "yuv420p",
                       "-r", str(FPS), cvs, "cfr",
                       "-video_track_timescale", "15360",
                       "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_SR),
                       "-movflags", "+faststart",
                       "-shortest", out_path]
    cp = ff.run_ffmpeg(args, timeout=1800)
    if cp.returncode != 0:
        raise VideoBlocked("RENDER_FAILED",
                           "ffmpeg 渲染失败",
                           detail={"stderr_tail":
                                   cp.stderr.decode("utf-8", "replace")[-4000:]})
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise VideoBlocked("RENDER_EMPTY", "ffmpeg 报成功但输出文件缺失/为空")
    return {"loudness_input_pass1": m1, "video_filters": _base_video_filters(font, ass_path),
            "audio_filter": _loudnorm_second_pass(m1), "cvs_flag": cvs}


def render_cover(*, video_path: str, out_path: str, title: str) -> Dict[str, Any]:
    """封面：取 1s 处帧 + 标题（与稿件一致由调用方保证）+ AI 标识，1080×1920。"""
    font = _find_font()
    title_filter = (f"drawtext=fontfile='{_esc_filter_path(font)}'"
                    f":text='{_esc_drawtext(title)}'"
                    ":fontsize=72:fontcolor=white:box=1:boxcolor=black@0.55"
                    ":boxborderw=20:x=(w-tw)/2:y=220"
                    if title else "")
    chain = [f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
             f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"]
    if title_filter:
        chain.append(title_filter)
    chain.append(_ai_label_filter(font))
    args = ["-y", "-ss", "1.0", "-i", video_path, "-frames:v", "1",
            "-vf", ",".join(chain), "-q:v", "2", out_path]
    cp = ff.run_ffmpeg(args, timeout=300)
    if cp.returncode != 0 or not os.path.isfile(out_path):
        raise VideoBlocked("COVER_FAILED", "封面生成失败",
                           detail={"stderr_tail":
                                   cp.stderr.decode("utf-8", "replace")[-400:]})
    return {"cover_path": out_path, "title": title}


def verify_master(path: str) -> Dict[str, Any]:
    """AC-MEDIA-01/02/03 的可代码化部分。任何一项不过即 VideoBlocked。"""
    info = ff.probe(path)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None or a is None:
        raise VideoBlocked("STREAM_MISSING", "成片缺少视频轨或音频轨")
    if (v.get("width"), v.get("height")) != (WIDTH, HEIGHT):
        raise VideoBlocked("RESOLUTION_MISMATCH",
                           f"分辨率 {v.get('width')}x{v.get('height')} ≠ {WIDTH}x{HEIGHT}")
    if v.get("codec_name") != "h264":
        raise VideoBlocked("CODEC_MISMATCH", f"视频编码 {v.get('codec_name')} ≠ h264")
    if v.get("pix_fmt") != "yuv420p":
        raise VideoBlocked("PIXFMT_MISMATCH", f"像素格式 {v.get('pix_fmt')} ≠ yuv420p")
    if v.get("r_frame_rate") != f"{FPS}/1" or v.get("avg_frame_rate") != f"{FPS}/1":
        raise VideoBlocked("FPS_NOT_CFR",
                           f"帧率 {v.get('r_frame_rate')}/{v.get('avg_frame_rate')} "
                           f"≠ {FPS}/1 CFR")
    if a.get("codec_name") != "aac":
        raise VideoBlocked("AUDIO_CODEC_MISMATCH", f"音频编码 {a.get('codec_name')} ≠ aac")
    if int(a.get("sample_rate", 0)) != AUDIO_SR:
        raise VideoBlocked("AUDIO_SR_MISMATCH",
                           f"采样率 {a.get('sample_rate')} ≠ {AUDIO_SR}")
    # CFR 时间戳：nb_frames ≈ duration*fps（容差 2 帧以内）
    dur = float(info["format"]["duration"])
    nb = int(v.get("nb_frames", 0))
    if nb and abs(nb - dur * FPS) > 2:
        raise VideoBlocked("CFR_TIMESTAMP_MISMATCH",
                           f"帧数 {nb} 与 {dur:.3f}s×{FPS} 偏差超过 2 帧")
    # faststart：moov 必须在 mdat 之前
    with open(path, "rb") as f:
        head = f.read(8 * 1024 * 1024)
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1 or (mdat != -1 and moov > mdat):
        raise VideoBlocked("FASTSTART_MISSING", "moov 不在 mdat 之前（faststart 未生效）")
    # 完整解码零错误
    ff.full_decode_check(path)
    # 响度实测（成品 AAC 音轨）
    m = ff.measure_loudness(path)
    if abs(m["input_i"] - TARGET_LUFS) > 1.0:
        raise VideoBlocked("LOUDNESS_OUT_OF_TOLERANCE",
                           f"实测综合响度 {m['input_i']:.1f} LUFS 偏离 {TARGET_LUFS}±1")
    if m["input_tp"] > TARGET_TP:
        raise VideoBlocked("TRUEPEAK_OVER", f"实测 true peak {m['input_tp']:.2f} > {TARGET_TP}")
    # 黑帧/冻结粗筛（告警留证，判死由质量 Agent/人工复核）
    events = ff.detect_black_freeze(path)
    return {"probe": {"width": v["width"], "height": v["height"],
                      "codec": v["codec_name"], "pix_fmt": v["pix_fmt"],
                      "fps": v["r_frame_rate"], "nb_frames": nb,
                      "audio_codec": a["codec_name"], "sample_rate": a["sample_rate"],
                      "duration": dur},
            "loudness_measured": m, "black_freeze_events": events,
            "audio_sample_rate": int(a.get("sample_rate", 0))}
