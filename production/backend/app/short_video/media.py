"""Immutable native clips and ordered FFmpeg stitching without timeline overlap."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from ..avatar import person_image
from ..digital_human.wan_audio import _probe, _stream_seconds
from ..video import ffmpeg
from . import service


def artifact_dir(graph: dict) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", graph["id"]):
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "任务图标识不是安全产物路径")
    root = Path(person_image.workspace_root()).resolve()
    folder = root / "production/backend/runtime/wan_storyboards" / graph["id"]
    if not folder.resolve().is_relative_to(root) or any(p.is_symlink() for p in [folder, *folder.parents] if p.is_relative_to(root)):
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "分镜产物目录越界")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _uri(path: Path) -> str:
    return path.relative_to(Path(person_image.workspace_root()).resolve()).as_posix()


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _decode(path: Path) -> dict:
    info = _probe(path)
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise service.StoryboardError("STORYBOARD_NATIVE_TRACK_MISSING", "原片必须包含视频和 Wan 原生音轨")
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(path),
                           "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], timeout=900)
    if cp.returncode or cp.stderr.strip():
        raise service.StoryboardError("STORYBOARD_CLIP_DECODE_FAILED", "原片无法完整解码，已保留文件")
    return {"duration_ms": round(max(_stream_seconds(video), _stream_seconds(audio)) * 1000),
            "video_duration_ms": round(_stream_seconds(video) * 1000),
            "audio_duration_ms": round(_stream_seconds(audio) * 1000), "width": video["width"], "height": video["height"],
            "has_video_track": True, "has_audio_track": True}


def save_clip(graph: dict, shot: dict, data: bytes) -> dict:
    if not isinstance(data, bytes) or not data:
        raise service.StoryboardError("STORYBOARD_CLIP_EMPTY", "下载原片为空")
    attempt = shot.get("video_attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "视频尝试编号无效")
    suffix = f"_attempt_{attempt:03d}" if attempt else ""
    path = artifact_dir(graph) / f"shot_{shot['shot_index']:03d}{suffix}_raw.mp4"
    digest = service._store(path, data)
    meta = _decode(path)
    expected = shot["duration_seconds"] * 1000
    if any(abs(meta[k] - expected) > 250 for k in ("video_duration_ms", "audio_duration_ms")):
        raise service.StoryboardError("STORYBOARD_CLIP_DURATION_MISMATCH", "原片实测时长不符合已确认的镜头时长")
    return {**meta, "storage_uri": _uri(path), "file_hash": digest, "native_audio": True,
            "remote_task_id": shot["remote_task_id"], "video_job_id": shot["video_job_id"]}


def local_clip(clip: dict) -> Path:
    root = Path(person_image.workspace_root()).resolve()
    uri = clip.get("storage_uri")
    if not isinstance(uri, str) or not uri or ":" in uri or ".." in Path(uri).parts or Path(uri).is_absolute():
        raise service.StoryboardError("STORYBOARD_CLIP_PATH_INVALID", "原片路径无效")
    path = (root / uri).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise service.StoryboardError("STORYBOARD_CLIP_MISSING", "原片文件缺失或路径越界")
    if _file_hash(path) != clip.get("file_hash"):
        raise service.StoryboardError("STORYBOARD_CLIP_CHANGED", "原片字节变化，原审核已失效")
    return path


def verify_clips(graph: dict) -> list[Path]:
    paths = []
    for shot in sorted(graph["shots"], key=lambda s: s["shot_index"]):
        clip = shot.get("clip") or {}
        if (shot["video_state"] != "succeeded" or clip.get("native_audio") is not True
                or not clip.get("has_audio_track") or clip.get("remote_task_id") != shot.get("remote_task_id")):
            raise service.StoryboardError("STORYBOARD_CLIP_UNVERIFIED", "镜头原片尚未完成真实回收与审核")
        paths.append(local_clip(clip))
    return paths


def stitch_filter(durations: list[float], *, width: int, height: int, transition_seconds: float = .06) -> str:
    filters, tracks = [], []
    for index, seconds in enumerate(durations):
        duration = f"{seconds:g}"
        video = (f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,"
                 f"tpad=stop_mode=clone:stop_duration=0.25,trim=duration={duration},setpts=PTS-STARTPTS[v{index}]")
        audio = (f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"apad=whole_dur={duration},atrim=duration={duration},asetpts=PTS-STARTPTS")
        if index:
            audio += f",afade=t=in:st=0:d={transition_seconds:g}"
        if index < len(durations) - 1:
            audio += f",afade=t=out:st={seconds-transition_seconds:g}:d={transition_seconds:g}"
        filters.extend((video, audio + f"[a{index}]"))
        tracks.append(f"[v{index}][a{index}]")
    filters.append("".join(tracks) + f"concat=n={len(durations)}:v=1:a=1[vout][aout]")
    return ";".join(filters)


def stitch(graph: dict) -> dict:
    paths = verify_clips(graph)
    folder = artifact_dir(graph)
    final = folder / "stitched.mp4"
    if final.is_symlink():
        raise service.StoryboardError("STORYBOARD_ASSET_PATH_INVALID", "成片路径越界")
    # Worker retries render to distinct temporary files; original native clips are immutable.
    temporary = folder / ("stitch_" + uuid.uuid4().hex + ".mp4")
    ordered = sorted(graph["shots"], key=lambda s: s["shot_index"])
    durations = [s["clip"]["duration_ms"] / 1000 for s in ordered]
    expected = round(sum(durations) * 1000)
    def checked_result(path: Path, meta: dict) -> dict:
        if any(abs(meta[k] - expected) > 150 for k in ("video_duration_ms", "audio_duration_ms")):
            raise service.StoryboardError("STORYBOARD_STITCH_DURATION_MISMATCH", "成片声画时长与镜头总时长不符")
        return {**meta, "storage_uri": _uri(path), "file_hash": _file_hash(path), "native_audio": True,
                "transition": "60ms_native_audio_fade_at_cuts", "shot_order": [s["shot_index"] for s in ordered],
                "input_clip_hashes": [s["clip"]["file_hash"] for s in ordered]}
    if final.exists():
        return checked_result(final, _decode(final))
    width, height = {"480p": (480, 854), "720p": (720, 1280), "1080p": (1080, 1920)}[graph["resolution"]]
    args = ["-v", "error", "-protocol_whitelist", "file"]
    for path in paths:
        args.extend(["-i", str(path)])
    args.extend(["-filter_complex", stitch_filter(durations, width=width, height=height),
                 "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "fast",
                 "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(temporary)])
    cp = ffmpeg.run_ffmpeg(args, timeout=1800)
    if cp.returncode:
        raise service.StoryboardError("STORYBOARD_STITCH_FAILED", "FFmpeg 拼接失败，原片已保留")
    meta = _decode(temporary)
    digest = _file_hash(temporary)
    if final.exists():
        if _file_hash(final) != digest:
            raise service.StoryboardError("STORYBOARD_ARTIFACT_CONFLICT", "原始产物字节冲突，停止覆盖")
    else:
        temporary.rename(final)
    return checked_result(final, meta)
