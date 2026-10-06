"""FFmpeg/FFprobe 运行器：定位、probe、完整解码校验、响度实测、黑帧/冻结检测。

不做重试、不吞错；失败一律抛 VideoBlocked，stderr 摘要进 detail。
响度用 loudnorm print_format=json 的**实测值**，不以滤镜配置替代实测（AC-MEDIA-02）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .errors import VideoBlocked

_ENV_BIN = "AVATAR_STUDIO_FFMPEG_BIN"
_KNOWN_DIRS = (r"C:\ffmpeg\bin",)


def _candidates() -> List[str]:
    out: List[str] = []
    env_dir = os.environ.get(_ENV_BIN)
    if env_dir:
        out.append(os.path.join(env_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    found_on_path = shutil.which("ffmpeg")
    if found_on_path:
        out.append(found_on_path)
    for d in _KNOWN_DIRS:
        exe = os.path.join(d, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        out.append(exe)
    return [p for p in out if p and os.path.isfile(p)]


_FFMPEG: Optional[str] = None
_FFPROBE: Optional[str] = None


def ffmpeg_bin() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        found = _candidates()
        if not found:
            raise VideoBlocked(
                "FFMPEG_NOT_AVAILABLE",
                f"未找到 ffmpeg；设置 {_ENV_BIN} 指向 bin 目录或安装到 {_KNOWN_DIRS[0]}")
        _FFMPEG = found[0]
    return _FFMPEG


def ffprobe_bin() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        exe = ffmpeg_bin()
        probe = os.path.join(os.path.dirname(exe),
                             "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if not os.path.isfile(probe):
            raise VideoBlocked("FFPROBE_NOT_AVAILABLE", f"ffmpeg 旁边找不到 ffprobe: {probe}")
        _FFPROBE = probe
    return _FFPROBE


def _run(cmd: List[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise VideoBlocked("FFMPEG_TIMEOUT", f"ffmpeg 超时（{timeout}s）",
                           detail={"cmd": cmd[0]}) from e
    except OSError as e:
        raise VideoBlocked("FFMPEG_SPAWN_FAILED", f"无法启动 ffmpeg: {e}") from e


def run_ffmpeg(args: List[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    return _run([ffmpeg_bin(), "-hide_banner", "-nostdin", *args], timeout=timeout)


def probe(path: str) -> Dict[str, Any]:
    """ffprobe JSON（流+格式）。文件不存在/不可解析即 blocked。"""
    if not os.path.isfile(path):
        raise VideoBlocked("MEDIA_FILE_MISSING", f"媒体文件不存在: {path}")
    cp = _run([ffprobe_bin(), "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path], timeout=120)
    if cp.returncode != 0:
        raise VideoBlocked("PROBE_FAILED",
                           f"ffprobe 失败: {cp.stderr.decode('utf-8', 'replace')[:300]}")
    return json.loads(cp.stdout.decode("utf-8"))


def full_decode_check(path: str) -> Dict[str, Any]:
    """完整解码（AC-MEDIA-01/03）：任何 error 级输出即失败。"""
    cp = _run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error",
               "-i", path, "-map", "0", "-f", "null", "-"], timeout=900)
    errors = cp.stderr.decode("utf-8", "replace").strip()
    if cp.returncode != 0 or errors:
        raise VideoBlocked("DECODE_ERRORS",
                           f"完整解码发现错误（{len(errors)} 字节错误输出）",
                           detail={"stderr_head": errors[:500]})
    return {"decoded": True}


def _extract_json_objects(text: str) -> List[Dict[str, Any]]:
    objs: List[Dict[str, Any]] = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            objs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            continue
    return objs


def measure_loudness(path: str) -> Dict[str, float]:
    """对文件音轨实测综合响度/true peak（loudnorm 分析模式）。"""
    cp = _run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", path,
               "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
               "-f", "null", "-"], timeout=900)
    if cp.returncode != 0:
        raise VideoBlocked("LOUDNESS_MEASURE_FAILED", "响度检测未执行成功",
                           detail={"stderr_tail": cp.stderr.decode("utf-8", "replace")[-1000:]})
    for obj in _extract_json_objects(cp.stderr.decode("utf-8", "replace")):
        if "input_i" in obj:
            out = {"input_i": float(obj["input_i"]), "input_tp": float(obj["input_tp"]),
                   "input_lra": float(obj.get("input_lra", 0.0)),
                   "input_thresh": float(obj.get("input_thresh", 0.0))}
            if "target_offset" in obj:
                out["target_offset"] = float(obj["target_offset"])
            return out
    raise VideoBlocked("LOUDNESS_MEASURE_FAILED",
                       "未能从 loudnorm 输出解析实测响度",
                       detail={"stderr_head": cp.stderr.decode("utf-8", "replace")[:300]})


def detect_black_freeze(path: str, *, min_duration: float = 0.5) -> Dict[str, Any]:
    """黑帧/冻结检测（AC-MEDIA-03 粗筛）：返回事件列表，告警不在此处判死。"""
    vf = (f"blackdetect=d={min_duration}:pix_th=0.10,"
          f"freezedetect=n=-60dB:d={min_duration}")
    cp = _run([ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", path,
               "-map", "0:v:0", "-vf", vf, "-an", "-f", "null", "-"], timeout=900)
    text = cp.stderr.decode("utf-8", "replace")
    if cp.returncode != 0:
        raise VideoBlocked("VIDEO_DETECTION_FAILED", "黑帧/冻结检测未执行成功",
                           detail={"stderr_tail": text[-1000:]})
    black = [ln for ln in text.splitlines() if "black_start" in ln]
    frozen = [ln for ln in text.splitlines() if "freeze_start" in ln]
    return {"black_events": black, "freeze_events": frozen}
