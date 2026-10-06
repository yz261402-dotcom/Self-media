"""Burn verified native-sound subtitles into a separate immutable movie."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import uuid

from ..video import asr, ffmpeg, subtitles
from ..video.errors import VideoBlocked
from . import media, service


def _folder(graph: dict, job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", job_id):
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "字幕作业标识不是安全产物路径")
    parent = media.artifact_dir(graph)
    folder = parent / "subtitles" / job_id
    if any(path.is_symlink() for path in (folder, folder.parent)):
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "字幕产物目录越界")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _path(folder: Path, name: str) -> Path:
    path = folder / name
    if path.is_symlink() or path.resolve().parent != folder.resolve():
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "字幕产物路径越界")
    return path


def _store_text(path: Path, text: str) -> None:
    service._store(path, text.encode("utf-8"))


def _file(path: Path, kind: str) -> dict:
    return {"kind": kind, "storage_uri": media._uri(path),
            "file_hash": media._file_hash(path), "bytes": path.stat().st_size}


def _filter_path(path: Path) -> str:
    """Two FFmpeg parsing levels, with no shell involved.

    First escape the filter option's colon/apostrophe/backslash, then escape
    filtergraph delimiters and backslashes. This covers Windows drive letters
    and filenames containing quotes, spaces, commas and brackets.
    """
    text = str(path).replace("\\", "/")
    option = "".join("\\" + char if char in "\\':" else char for char in text)
    return "".join("\\" + char if char in "\\'[],; " else char for char in option)


def _audio_packets(path: Path) -> str:
    # Hash encoded packet payloads, not containers or a re-encoded audio file.
    # Stream copy preserves these bytes even when the MP4 metadata changes.
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(path),
                           "-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"], timeout=900)
    if cp.returncode:
        raise VideoBlocked("SUBTITLE_AUDIO_HASH_FAILED", "无法核验字幕版原生音轨，未登记产物")
    text = cp.stdout.decode("ascii", "replace").strip()
    if not re.fullmatch(r"SHA256=[0-9a-fA-F]{64}", text):
        raise VideoBlocked("SUBTITLE_AUDIO_HASH_FAILED", "原生音轨哈希回执无效，未登记产物")
    return "sha256:" + text.split("=", 1)[1].lower()


def _cue_objects(doc: dict) -> list[subtitles.Cue]:
    result = []
    for index, item in enumerate(doc.get("cues") or []):
        try:
            start, end = item["start_ms"], item["end_ms"]
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or int(value) != value for value in (start, end)) or end <= start:
                raise ValueError("invalid interval")
            text = item["text"]
            if not isinstance(text, str):
                raise ValueError("invalid text")
        except (TypeError, ValueError, KeyError, OverflowError) as error:
            raise VideoBlocked("ALIGN_TIMING_INVALID", "字幕对齐区间必须为有效毫秒时间轴") from error
        if int(start) // 10 >= int(end) // 10:
            raise VideoBlocked("CUE_ASS_TIMING_COLLAPSED", "字幕区间短于 ASS 时间精度，未生成不可见字幕")
        result.append(subtitles.Cue(index=index, start_ms=int(start), end_ms=int(end), text=text))
    return result


def _plain_text(text: str) -> str:
    # Libass has no general literal escape for braces. Equal-looking fullwidth
    # glyphs display these characters without admitting override tags or \N.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _ass(cues: list[subtitles.Cue], width: int, height: int) -> str:
    safe = [subtitles.Cue(c.index, c.start_ms, c.end_ms, _plain_text(c.text)) for c in cues]
    text = subtitles.build_ass(safe, play_res_x=width, play_res_y=height, title="Wan native sound subtitles")
    # Keep the existing frozen proportions without mutating its global STYLE.
    scale = min(width / 1080, height / 1920)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Style: Default,"):
            fields = line.split(",")
            fields[2] = f"{max(12, subtitles.STYLE['font_size'] * scale):.2f}"
            fields[16] = f"{max(1, subtitles.STYLE['outline'] * scale):.2f}"
            fields[19] = fields[20] = str(max(8, round(subtitles.STYLE["margin_h"] * scale)))
            fields[21] = str(max(12, round(subtitles.STYLE["margin_v"] * scale)))
            lines[index] = ",".join(fields)
    return "\n".join(lines) + "\n"


def _verified_output(source_meta: dict, output: Path, source_hash: str) -> tuple[dict, str]:
    metadata = media._decode(output)
    if (metadata["width"], metadata["height"]) != (source_meta["width"], source_meta["height"]):
        raise VideoBlocked("SUBTITLE_RESOLUTION_CHANGED", "字幕烧录改变了原片画幅，未登记产物")
    if any(metadata[key] != source_meta[key] for key in ("video_duration_ms", "audio_duration_ms")):
        raise VideoBlocked("SUBTITLE_DURATION_CHANGED", "字幕烧录改变了原片声画时长，未登记产物")
    audio_hash = _audio_packets(output)
    if audio_hash != source_hash:
        raise VideoBlocked("SUBTITLE_NATIVE_AUDIO_CHANGED", "字幕版未完整保持原生音轨，未登记产物")
    return metadata, audio_hash


def render(graph: dict, *, job_id: str) -> dict:
    """Return only independently decoded and verified native-subtitle media."""
    folder = _folder(graph, job_id)
    original = graph.get("output") or {}
    if original.get("native_audio") is not True or not graph.get("video_id"):
        raise VideoBlocked("SUBTITLE_SOURCE_INVALID", "需要当前已生成且含原生音轨的成片")
    source = media.local_clip(original)
    source_ref = {"video_id": graph["video_id"], "file_hash": original["file_hash"]}
    source_receipt = _path(folder, "source.json")
    if source_receipt.exists():
        try:
            bound = json.loads(source_receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise VideoBlocked("SUBTITLE_RECEIPT_INVALID", "字幕原片绑定回执损坏") from error
        if bound != source_ref:
            raise VideoBlocked("SUBTITLE_SOURCE_CHANGED", "字幕作业绑定的原成片已变化")
    else:
        _store_text(source_receipt, json.dumps(source_ref, ensure_ascii=False, sort_keys=True))
    receipt = _path(folder, "result.json")
    if receipt.exists():
        try:
            result = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise VideoBlocked("SUBTITLE_RECEIPT_INVALID", "字幕产物回执损坏，原产物已保留") from error
        if result.get("subtitle_source") != source_ref:
            raise VideoBlocked("SUBTITLE_SOURCE_CHANGED", "字幕作业绑定的原成片已变化")
        media.local_clip(result)
        for row in result.get("subtitle_files") or []:
            media.local_clip(row)
        return result
    source_meta = media._decode(source)
    source_packet_hash = _audio_packets(source)
    audio = _path(folder, "native.wav")
    audio_receipt = _path(folder, "audio.json")
    audio_proof = None
    if audio_receipt.exists():
        try:
            audio_proof = json.loads(audio_receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise VideoBlocked("SUBTITLE_AUDIO_CACHE_CHANGED", "字幕识别音轨缓存回执损坏") from error
        if audio_proof.get("source") != source_ref or (audio.exists() and audio_proof.get("file_hash") != media._file_hash(audio)):
            raise VideoBlocked("SUBTITLE_AUDIO_CACHE_CHANGED", "字幕识别音轨缓存已变化，未调用识别")
    if not audio.exists() or audio_proof is None:
        temporary_audio = _path(folder, "extract_" + uuid.uuid4().hex[:12] + ".wav")
        cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(source),
                               "-map", "0:a:0", "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                               str(temporary_audio)], timeout=900)
        if cp.returncode:
            raise VideoBlocked("SUBTITLE_AUDIO_EXTRACTION_FAILED", "原生音轨提取失败，原片已保留")
        if audio.exists():
            if media._file_hash(temporary_audio) != media._file_hash(audio):
                raise VideoBlocked("SUBTITLE_AUDIO_CACHE_CHANGED", "现有识别音轨与原成片不一致，未调用识别")
            temporary_audio.unlink()
        else:
            temporary_audio.rename(audio)
        expected_audio_proof = {"source": source_ref, "file_hash": media._file_hash(audio),
                                "sample_rate": 16000, "channels": 1, "codec": "pcm_s16le"}
        if audio_proof is not None and expected_audio_proof != audio_proof:
            raise VideoBlocked("SUBTITLE_AUDIO_CACHE_CHANGED", "重新提取的原生音轨与缓存回执不一致")
        _store_text(audio_receipt, json.dumps(expected_audio_proof, ensure_ascii=False, sort_keys=True))
    adapter = asr.require_adapter()
    if adapter.alignment_source != "forced_alignment" or not callable(getattr(adapter, "transcribe_native", None)):
        raise VideoBlocked("ASR_ADAPTER_UNSUPPORTED", "当前适配器未提供实际音轨识别与强制对齐")
    doc = adapter.transcribe_native(str(audio), cache_path=_path(folder, "inference.json"), attempt_id=job_id)
    transcript = doc.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise VideoBlocked("ASR_NATIVE_TRANSCRIPT_EMPTY", "原生音轨未识别到有效对白，未生成字幕")
    cues = _cue_objects(doc)
    checked = subtitles.check_cues(cues, final_duration_ms=min(source_meta["video_duration_ms"], source_meta["audio_duration_ms"]),
                                   script_text=transcript)
    ass_path, srt_path = _path(folder, "subtitles.ass"), _path(folder, "subtitles.srt")
    _store_text(ass_path, _ass(cues, source_meta["width"], source_meta["height"]))
    # Flatten only embedded line terminators: each SRT row remains an actual
    # cue, rather than allowing inference text to add timing/header syntax.
    srt_cues = [subtitles.Cue(c.index, c.start_ms, c.end_ms, c.text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")) for c in cues]
    _store_text(srt_path, subtitles.build_srt(srt_cues))
    final = _path(folder, "subtitled.mp4")
    if final.exists():
        meta, output_packet_hash = _verified_output(source_meta, final, source_packet_hash)
    else:
        temporary = _path(folder, "render_" + uuid.uuid4().hex[:12] + ".mp4")
        args = ["-v", "error", "-protocol_whitelist", "file", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0", "-vf", "ass=filename=" + _filter_path(ass_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart", str(temporary)]
        cp = ffmpeg.run_ffmpeg(args, timeout=1800)
        if cp.returncode:
            raise VideoBlocked("SUBTITLE_BURN_FAILED", "FFmpeg 字幕烧录失败，原片与识别证据已保留",
                               detail={"stderr_tail": cp.stderr.decode("utf-8", "replace")[-2000:]})
        meta, output_packet_hash = _verified_output(source_meta, temporary, source_packet_hash)
        temporary.rename(final)
    # Verify the source once more after long-running inference/render.
    media.local_clip(original)
    result = {**meta, "storage_uri": media._uri(final), "file_hash": media._file_hash(final),
              "native_audio": True, "subtitles_burned": True, "subtitle_source": source_ref,
              "transition": original.get("transition"), "shot_order": original.get("shot_order", []),
              "input_clip_hashes": original.get("input_clip_hashes", []),
              "subtitle_files": [_file(ass_path, "subtitles_ass"), _file(srt_path, "subtitles_srt")],
              "subtitle_checks": {"outcome": "pass", "alignment_source": "forced_alignment",
                  "transcript": transcript, "cue_count": len(cues), "coverage_chars": int(checked["coverage_chars"]),
                  "source_audio_hash": media._file_hash(audio), "source_audio_packet_hash": source_packet_hash,
                  "output_audio_packet_hash": output_packet_hash, "audio_codec": "stream_copy",
                  "source_video_duration_ms": source_meta["video_duration_ms"],
                  "source_audio_duration_ms": source_meta["audio_duration_ms"],
                  "asr_job_id": (doc.get("asr_job") or {}).get("job_id"),
                  "align_job_id": (doc.get("align_job") or {}).get("job_id"),
                  "decoded": True}}
    _store_text(receipt, json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
