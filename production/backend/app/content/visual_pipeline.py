"""视觉分析的本地媒体处理：抽帧、镜头切点、音频信号。不联网、不调用任何模型。

所有 ffmpeg 调用都是参数列表（`shell=False`）并带超时；ffmpeg 缺失、超时、无视频流分别给稳定错误码。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional
from fractions import Fraction

from ..video import ffmpeg
from .errors import ContentRejected

MAX_BASE_FRAMES = 60
MAX_CUT_FRAMES = 30
MIN_FRAME_GAP_MS = 300
MAX_EDGE = 768
# ffmpeg mjpeg 的 -q:v 取值 2..31、越小画质越高；首档约等于 JPEG quality 75，其后逐级降质
QUALITY_STEPS = (5, 8, 12, 18)
MAX_FRAME_BYTES = 180 * 1024
SCENE_THRESHOLD = 0.35
FRAME_TIMEOUT = 60
CUT_TIMEOUT = 300
AUDIO_TIMEOUT = 120


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, shell=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ContentRejected("视频处理超时，请缩短视频后重试", "MEDIA_PROCESS_TIMEOUT") from None
    except (FileNotFoundError, OSError):
        raise ContentRejected("找不到或无法启动 ffmpeg，无法处理视频", "MEDIA_TOOL_UNAVAILABLE") from None


def _ffmpeg(*args: str) -> List[str]:
    try:
        return [ffmpeg.ffmpeg_bin(), "-hide_banner", "-nostdin", "-protocol_whitelist", "file", *args]
    except Exception:  # noqa: BLE001  VideoBlocked：找不到 ffmpeg
        raise ContentRejected("找不到 ffmpeg，无法处理视频", "MEDIA_TOOL_UNAVAILABLE") from None


def plan_timestamps(*, duration_ms: int, cuts_ms: List[int]) -> List[int]:
    """短视频 2fps、长视频均匀采样；基础最多 60 帧含首尾，切点最多追加 30 帧。"""
    if duration_ms <= 0:
        return [0]
    step = max(500, math.ceil(duration_ms / MAX_BASE_FRAMES))
    # EOF 本身无可解码帧；保留截止前 100ms 的画面，短于 100ms 时采起始帧。
    tail = max(0, duration_ms - 100)
    base = list(range(0, tail + 1, step))[:MAX_BASE_FRAMES] or [0]
    if base[-1] != tail:
        if len(base) == MAX_BASE_FRAMES:
            base[-1] = tail
        else:
            base.append(tail)
    out = list(base)
    added = 0
    for cut in sorted(set(int(c) for c in cuts_ms)):
        if added >= MAX_CUT_FRAMES:
            break
        if not 0 <= cut < duration_ms or any(abs(cut - t) < MIN_FRAME_GAP_MS for t in out):
            continue
        out.append(cut)
        added += 1
    return sorted(out)


def _grab(path: str, timestamp_ms: int, q: int) -> bytes:
    cmd = _ffmpeg("-v", "error", "-xerror", "-ss", f"{timestamp_ms / 1000:.3f}", "-i", path, "-an", "-frames:v", "1",
                  "-vf", f"scale='if(gt(iw,ih),min({MAX_EDGE},iw),-2)':'if(gt(iw,ih),-2,min({MAX_EDGE},ih))'",
                  "-q:v", str(q), "-f", "image2pipe", "-vcodec", "mjpeg", "-")
    cp = _run(cmd, FRAME_TIMEOUT)
    return cp.stdout if cp.returncode == 0 and not cp.stderr.strip() else b""


def _frame_jpeg(path: str, timestamp_ms: int) -> bytes:
    """长边 ≤768 的 JPEG；超过单帧上限时逐级降低画质，仍超限则返回空（不发送超大图）。"""
    for q in QUALITY_STEPS:
        data = _grab(path, timestamp_ms, q)
        if not data:
            return b""
        if len(data) <= MAX_FRAME_BYTES:
            return data
    return b""


def _tail_frame_interval(path: str) -> Optional[int]:
    """尾部无向前可解码帧时，读取实际帧率确定最后一帧的显示区间；不再全片解码。"""
    cp = _run([ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
               "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", path], 30)
    if cp.returncode != 0:
        return None
    try:
        stream = (json.loads(cp.stdout.decode("utf-8"))["streams"] or [None])[0] or {}
        for field in ("avg_frame_rate", "r_frame_rate"):
            try:
                rate = float(Fraction(str(stream.get(field) or "0/1")))
            except (ValueError, ZeroDivisionError):
                continue
            if rate > 0 and math.isfinite(rate):
                return max(100, math.ceil(1000 / rate))
    except (ValueError, KeyError, TypeError):
        return None
    return None


def sample_frames(path: str, duration_ms: int) -> List[Dict[str, Any]]:
    """返回按时间排序的 [{timestamp_ms, jpeg_bytes, sha256}]；任何计划帧缺失都不得作为全片分析输入。"""
    if not path:
        raise ContentRejected("缺少视频文件", "MEDIA_INVALID")
    cuts = detect_scene_cuts(path)
    frames: List[Dict[str, Any]] = []
    tail_interval: Optional[int] = None
    for ts in plan_timestamps(duration_ms=duration_ms, cuts_ms=cuts):
        data = _frame_jpeg(path, ts)
        if not data:
            if tail_interval is None:
                tail_interval = _tail_frame_interval(path)
            # 低帧率最后一帧持续显示到 EOF；向前 seek 会返回空，不代表内容缺失。
            # 只允许在这最后一个帧间隔内回退，中段缺帧始终拒绝。
            if tail_interval is not None and ts >= duration_ms - tail_interval:
                data = _frame_jpeg(path, max(0, duration_ms - tail_interval))
        if not data:
            raise ContentRejected(f"无法提取视频 {ts / 1000:.3f} 秒处的画面，视频可能不完整、损坏或画面无法处理；"
                                  "请重新获取或上传完整原视频", "MEDIA_INCOMPLETE")
        frames.append({"timestamp_ms": ts, "jpeg_bytes": data, "sha256": "sha256:" + hashlib.sha256(data).hexdigest()})
    if not frames:
        raise ContentRejected("视频里没有可抽取的画面帧，请确认文件包含视频轨", "MEDIA_INVALID")
    return frames


def detect_scene_cuts(path: str) -> List[int]:
    """镜头切点（毫秒）；全片扫描中的解码错误直接阻止后续付费分析。"""
    cmd = _ffmpeg("-v", "info", "-xerror", "-err_detect", "explode", "-i", path, "-an", "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                  "-f", "null", "-")
    cp = _run(cmd, CUT_TIMEOUT)
    text = cp.stderr.decode("utf-8", "replace")
    if cp.returncode != 0 and "does not contain any stream" in text:
        raise ContentRejected("视频里没有视频轨", "MEDIA_INVALID")
    if cp.returncode != 0:
        raise ContentRejected("视频无法完整解码，请重新获取或上传完整原视频", "MEDIA_INCOMPLETE")
    cuts = [int(round(float(m.group(1)) * 1000)) for m in re.finditer(r"pts_time:([0-9.]+)", text)]
    return sorted(set(cuts))


def has_audio(path: str) -> bool:
    cp = _run([ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file", "-select_streams", "a",
               "-show_entries", "stream=index", "-of", "csv=p=0", path], 30)
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _decode_mono_16k(path: str, wav_path: str) -> bool:
    cp = _run(_ffmpeg("-y", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", wav_path),
              AUDIO_TIMEOUT)
    return cp.returncode == 0 and os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44


def _beat_track(wav_path: str) -> Optional[Dict[str, Any]]:
    """librosa 不可用或失败时返回 None：BPM 允许为空，不阻断视觉分析。"""
    try:
        import librosa  # type: ignore
        y, sr = librosa.load(wav_path, sr=16000, mono=True)
        if len(y) < sr:  # 不足 1 秒无法估计节拍
            return None
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(tempo if not hasattr(tempo, "__len__") else tempo[0])
        times = [int(round(t * 1000)) for t in librosa.frames_to_time(beats, sr=sr)]
        if tempo <= 0 or not times:
            return None
        return {"tempo_bpm": round(tempo, 1), "beat_times_ms": times, "confidence": 0.5}
    except Exception:  # noqa: BLE001
        return None


def _rms_stats(wav_path: str) -> Dict[str, float]:
    """无需第三方库的简单能量统计，用来区分“有声音”与“基本静音”。"""
    import array
    import wave
    with wave.open(wav_path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    if not samples:
        return {"rms": 0.0}
    step = max(1, len(samples) // 200_000)
    picked = samples[::step]
    return {"rms": math.sqrt(sum(s * s for s in picked) / len(picked)) / 32768.0}


def analyze_audio(path: str) -> Dict[str, Any]:
    """本地音频信号。不做语音识别：无法判断是否有人声时给 uncertain，绝不伪造转写。"""
    empty = {"speech_presence": "none", "music_presence": "none", "tempo_bpm": None, "beat_times_ms": [],
             "confidence": 0.0}
    if not has_audio(path):
        return dict(empty)
    fd, wav = tempfile.mkstemp(prefix="visual-audio-", suffix=".wav")
    os.close(fd)
    try:
        if not _decode_mono_16k(path, wav):
            return dict(empty, speech_presence="uncertain", music_presence="uncertain")
        if _rms_stats(wav)["rms"] < 0.003:
            return dict(empty)
        beat = _beat_track(wav)
        out = {"speech_presence": "uncertain", "music_presence": "music" if beat else "uncertain",
               "tempo_bpm": None, "beat_times_ms": [], "confidence": 0.0}
        if beat:
            out.update(beat)
        return out
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
