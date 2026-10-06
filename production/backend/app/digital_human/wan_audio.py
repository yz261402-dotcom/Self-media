"""保留 Wan 原片，并用本次提交绑定的完整原音轨制作待审核预览。

恢复音轨不是 Wan 原生保持音轨的证据，也不代表人物或口型审核通过。
所有产物按 run 隔离且不可覆盖；过期运行只读取其历史输入快照。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict

from .. import storage as st
from ..tts import audio as tts_audio
from ..video import ffmpeg
from . import worker_client
from .errors import TalkingHeadBlocked


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _artifact_path(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise TalkingHeadBlocked("WAN_RUN_PATH_INVALID", "Wan 产物路径越出运行目录，停止写入。")
    return path


def _store_immutable(path: Path, data: bytes) -> None:
    """同名只接受相同内容；重复查询不可静默覆盖已保留证据。"""
    try:
        with path.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise TalkingHeadBlocked("WAN_ARTIFACT_CONFLICT", "同一 Wan 运行返回了不同产物，原文件已保留，请先核查原任务。")


def _probe(path: Path) -> Dict[str, Any]:
    try:
        # 禁用远程协议，探测本地不可变文件；任何下载内容都不能再次主动联网。
        process = ffmpeg._run([ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file",
                               "-show_format", "-show_streams", "-print_format", "json", str(path)], timeout=60)
        if process.returncode:
            raise ValueError("ffprobe failed")
        info = json.loads(process.stdout)
        if not isinstance(info.get("streams"), list):
            raise ValueError("streams missing")
        return info
    except Exception as error:
        raise TalkingHeadBlocked("WAN_MEDIA_PROBE_FAILED", "无法真实探测 Wan 媒体；原片已保留，未登记为合格预览。") from error


def _stream_seconds(stream: Dict[str, Any]) -> float:
    """只使用流时长，绝不让另一条音轨撑大的容器时长替代画面时长。"""
    try:
        if stream.get("duration_ts") is not None and stream.get("time_base"):
            seconds = float(int(stream["duration_ts"]) * Fraction(stream["time_base"]))
        else:
            seconds = float(stream["duration"])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("invalid stream duration")
        return seconds
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as error:
        raise TalkingHeadBlocked("WAN_MEDIA_DURATION_UNKNOWN", "媒体流缺少可信时长，无法确认覆盖完整原音频。") from error


def _audio_input(run: Dict[str, Any], directory: Path) -> tuple[Dict[str, Any], Path, float, int]:
    params = run.get("provider_params") or {}
    snapshot = params.get("input_snapshot") or {}
    audio_id, digest = snapshot.get("audio_asset_id"), snapshot.get("audio_hash")
    record = st.get("audios", audio_id or "")
    if not record or record.get("is_mock") or record.get("work_id") != run.get("work_id"):
        raise TalkingHeadBlocked("WAN_AUDIO_SNAPSHOT_INVALID", "Wan 运行绑定的历史原音频记录不可用；不会改用当前作品配音。")
    if not digest or record.get("file_hash") != digest:
        raise TalkingHeadBlocked("WAN_AUDIO_HASH_MISMATCH", "历史原音频哈希与本次提交快照不一致，停止恢复。")
    try:
        original_path = worker_client._registered_input_path("audio_url", str(record.get("storage_uri") or ""), digest)
        data = original_path.read_bytes()
    except (TalkingHeadBlocked, OSError) as error:
        raise TalkingHeadBlocked("WAN_AUDIO_PATH_INVALID", "历史原音频文件缺失、未登记或路径越界，停止恢复。") from error
    if _hash(data) != digest:
        raise TalkingHeadBlocked("WAN_AUDIO_HASH_MISMATCH", "历史原音频文件已变化，与本次提交哈希不一致。")
    # 先复制校验过的字节，避免探测和复用之间原输入被改写。
    source = _artifact_path(directory, "original_audio" + original_path.suffix.lower())
    _store_immutable(source, data)
    info = _probe(source)
    audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if len(audios) != 1 or any(s.get("codec_type") == "video" for s in info["streams"]):
        raise TalkingHeadBlocked("WAN_AUDIO_FORMAT_INVALID", "历史原音频必须是单一音轨文件。")
    sample_rate, channels = int(audios[0].get("sample_rate") or 0), int(audios[0].get("channels") or 0)
    if sample_rate <= 0 or channels <= 0:
        raise TalkingHeadBlocked("WAN_AUDIO_FORMAT_INVALID", "原音频缺少采样率或声道信息。")
    process = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(source),
                                 "-map", "0:a:0", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"], timeout=60)
    if process.returncode or process.stderr.strip() or not process.stdout:
        raise TalkingHeadBlocked("WAN_AUDIO_DECODE_FAILED", "完整原音频解码失败，停止恢复。")
    frame_bytes = channels * 2
    if len(process.stdout) % frame_bytes:
        raise TalkingHeadBlocked("WAN_AUDIO_DECODE_FAILED", "原音频解码样本不完整，停止恢复。")
    sample_count = len(process.stdout) // frame_bytes
    seconds = sample_count / sample_rate
    actual_ms = (sample_count * 1000 + sample_rate - 1) // sample_rate
    if not sample_rate <= sample_count <= 15 * sample_rate:
        raise TalkingHeadBlocked("WAN_AUDIO_DURATION_MISMATCH", "历史原音频真实时长不在 1–15 秒范围内。")
    actual_generation_seconds = max(2, (sample_count + sample_rate - 1) // sample_rate)
    tolerance = 100 if (info.get("format") or {}).get("format_name") == "mp3" else 1
    registered_ms = int(record.get("duration_ms") or 0)
    if (not 1000 <= registered_ms <= 15000 or abs(actual_ms - registered_ms) > tolerance
            or max(2, (registered_ms + 999) // 1000) != actual_generation_seconds):
        raise TalkingHeadBlocked("WAN_AUDIO_DURATION_MISMATCH", "历史原音频真实时长与登记记录不一致，须先核对。",
                                 {"actual_duration_ms": actual_ms, "registered_duration_ms": record.get("duration_ms")})
    if params.get("audio_duration_ms") is not None:
        submitted_ms = int(params["audio_duration_ms"])
        if (not 1000 <= submitted_ms <= 15000 or abs(actual_ms - submitted_ms) > tolerance
                or max(2, (submitted_ms + 999) // 1000) != actual_generation_seconds):
            raise TalkingHeadBlocked("WAN_AUDIO_DURATION_MISMATCH", "历史原音频真实时长与提交参数不一致，须先核对。")
    return record, source, seconds, sample_count


def prepare_original(run: Dict[str, Any], downloaded: Dict[str, Any]) -> Dict[str, Any]:
    """返回真实待审预览及恢复证据；不填写任何身份、口型或音轨审核 pass。"""
    run_id = run.get("id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", run_id):
        raise TalkingHeadBlocked("WAN_RUN_PATH_INVALID", "Wan 运行标识不能作为安全产物路径。")
    runtime = Path(tts_audio.runtime_root()).resolve()
    base = runtime / "wan3"
    directory = base / run_id
    if base.is_symlink() or directory.is_symlink() or not directory.resolve().is_relative_to(runtime):
        raise TalkingHeadBlocked("WAN_RUN_PATH_INVALID", "Wan 产物目录越界，停止写入。")
    directory.mkdir(parents=True, exist_ok=True)
    data = downloaded.get("data")
    if not isinstance(data, bytes) or not data:
        raise TalkingHeadBlocked("WAN_RAW_EMPTY", "Wan 原片下载内容为空，无法保留原片。")
    raw = _artifact_path(directory, "raw.mp4")
    _store_immutable(raw, data)
    raw_hash = _hash(data)
    _store_immutable(_artifact_path(directory, "raw.sha256"), (raw_hash + "\n").encode("ascii"))
    raw_info = _probe(raw)
    video = next((s for s in raw_info["streams"] if s.get("codec_type") == "video"), None)
    if not video:
        raise TalkingHeadBlocked("WAN_OUTPUT_VIDEO_MISSING", "Wan 原片没有视频轨；原文件已保留。")
    if not any(s.get("codec_type") == "audio" for s in raw_info["streams"]):
        raise TalkingHeadBlocked("WAN_OUTPUT_AUDIO_MISSING", "Wan 原片没有音轨；原文件已保留，不冒充原生有声输出。")
    record, original_audio, audio_seconds, sample_count = _audio_input(run, directory)
    video_seconds = _stream_seconds(video)
    video_start = float(video.get("start_time") or 0)
    if video_start > .001 or video_seconds + 1e-9 < audio_seconds:
        raise TalkingHeadBlocked("WAN_VIDEO_TOO_SHORT", "Wan 画面不能完整覆盖原音频；禁止截字、加速、补帧或 -shortest。",
                                 {"video_duration_ms": round(video_seconds * 1000),
                                  "audio_duration_ms": round(audio_seconds * 1000),
                                  "video_start_ms": round(video_start * 1000)})
    temp = _artifact_path(directory, "preview-" + uuid.uuid4().hex + ".mp4")
    preview = _artifact_path(directory, "preview.mp4")
    try:
        result = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(raw),
                                    "-protocol_whitelist", "file", "-i", str(original_audio),
                                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                                    "-t", f"{audio_seconds:.9f}", "-movflags", "+faststart", str(temp)], timeout=120)
        if result.returncode or result.stderr.strip() or not temp.is_file():
            raise TalkingHeadBlocked("WAN_AUDIO_RESTORATION_FAILED", "恢复完整原音轨失败；供应商原片已保留。")
        preview_data = temp.read_bytes()
        _store_immutable(preview, preview_data)
    finally:
        if temp.is_file():
            temp.unlink()
    info = _probe(preview)
    output_video = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    output_audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), {})
    video_duration, audio_duration = _stream_seconds(output_video), _stream_seconds(output_audio)
    if video_duration + 1e-9 < audio_seconds or audio_duration + 1e-9 < audio_seconds:
        raise TalkingHeadBlocked("WAN_RESTORED_AUDIO_INCOMPLETE", "恢复预览没有完整覆盖原音频；不得登记为合格原片。")
    try:
        ffmpeg.full_decode_check(str(preview))
    except Exception as error:
        raise TalkingHeadBlocked("WAN_PREVIEW_DECODE_FAILED", "恢复预览无法完整解码；供应商原片仍保留。") from error
    try:
        fps = float(Fraction(output_video.get("avg_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        fps = None
    probe = {"container": "mp4", "has_video_track": True, "has_audio_track": True,
             "duration_ms": round(float(info["format"]["duration"]) * 1000),
             "video_duration_ms": round(video_duration * 1000),
             "audio_duration_ms": round(audio_duration * 1000), "width": output_video.get("width"),
             "height": output_video.get("height"), "fps": fps, "file_hash": _hash(preview_data),
             "bytes": len(preview_data), "truncated": False, "probe_source": "ffprobe_and_full_decode"}
    evidence = {"mode": "confirmed_original_audio_restored", "wan_native_audio_preservation_verified": False,
                "audio_asset_id": record["id"], "audio_hash": record["file_hash"],
                "audio_duration_ms": round(audio_seconds * 1000), "decoded_source_samples": sample_count,
                "raw_storage_uri": raw.relative_to(runtime).as_posix(), "raw_file_hash": raw_hash,
                "raw_video_duration_ms": round(video_seconds * 1000),
                "preview_storage_uri": preview.relative_to(runtime).as_posix(),
                "preview_file_hash": probe["file_hash"], "audio_codec": output_audio.get("codec_name"),
                "note": "预览音轨由本次提交绑定的完整原音频恢复，并非 Wan 原生保持音轨的实测证据；"
                        "身份、口型和音轨仍须独立审核。"}
    _store_immutable(_artifact_path(directory, "audio_restoration.json"),
                     json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
    return {**downloaded, "data": preview_data, "probe": probe, "audio_restoration": evidence}
