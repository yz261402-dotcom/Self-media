"""用户动作参考上传：本地校验、预览、人工命名后登记为 M2 动作模板。

上传阶段只落本机工作区，不调用模型，也不把本地路径伪装成算力云 URL。
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import storage as st
from .avatar import motion, person_image, registry
from .video import ffmpeg
from .contracts import AvatarAsset, MotionReference, now_iso

router = APIRouter(prefix="/api/v1")

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MIN_DURATION_SECONDS = 3.0
MAX_DURATION_SECONDS = 60.0
ALLOWED_SUFFIXES = {".mp4", ".mov"}
UPLOADABLE_MOTION_KEYS = {"smile", "nod", "head_turn_small", "serious"}


def _error(code: str, message: str, status: int = 422):
    raise HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


def _identity(identity_id: str) -> dict:
    row = registry.get_identity(identity_id)
    if not row:
        _error("IDENTITY_NOT_FOUND", "人物不存在", 404)
    if row.get("discarded_at"):
        _error("IDENTITY_DISCARDED", "请先恢复人物，再添加动作参考")
    return row


def _upload_dir(upload_id: str) -> Path:
    root = Path(person_image.workspace_root()).resolve()
    target = root / "assets" / "动作参考" / "用户上传" / upload_id
    if not target.resolve().is_relative_to(root):
        _error("MOTION_UPLOAD_PATH_INVALID", "动作存储目录不在本项目中", 409)
    return target


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _preview_url(upload_id: str) -> str:
    return f"/api/v1/motion-uploads/{quote(upload_id, safe='')}/preview"


def _row_view(row: dict) -> dict:
    out = {
        "id": row["id"], "identity_id": row["identity_id"], "state": row.get("state", "pending"),
        "original_filename": row.get("original_filename", "动作参考.mp4"),
        "duration_seconds": row.get("duration_seconds"), "width": row.get("width"),
        "height": row.get("height"), "size_bytes": row.get("size_bytes", 0),
        "preview_url": _preview_url(row["id"]), "reference_id": row.get("reference_id"),
        "created_at": row.get("created_at"),
    }
    if row.get("error"):
        out["error"] = row["error"]
    return out


def _video_meta(path: str) -> Dict[str, Any]:
    """确认有视频轨、可解码、时长在动作参考范围内。"""
    try:
        # 上传者声明的扩展名不代表容器格式。限制 demuxer/协议，避免把伪装的
        # 播放列表当作视频读取，亦不允许素材检测过程发起网络访问。
        cp = ffmpeg._run([
            ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", "mov", "-show_format", "-show_streams", "-of", "json", path,
        ], timeout=20)
        if cp.returncode != 0:
            _error("MOTION_UPLOAD_PROBE_FAILED", "动作视频无法读取，请上传可正常播放的 MP4/MOV 文件")
        info = json.loads(cp.stdout.decode("utf-8"))
    except HTTPException:
        raise
    except Exception as exc:  # VideoBlocked 的 code 不能透给用户之外的堆栈
        code = getattr(exc, "code", "MOTION_UPLOAD_PROBE_FAILED")
        _error("MOTION_UPLOAD_PROBE_FAILED" if code == "PROBE_FAILED" else str(code),
               "动作视频无法读取，请上传可正常播放的 MP4/MOV 文件")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        _error("MOTION_UPLOAD_NO_VIDEO", "文件没有视频轨，不能作为动作参考")
    try:
        duration = float((info.get("format") or {}).get("duration") or video.get("duration") or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (TypeError, ValueError):
        _error("MOTION_UPLOAD_METADATA_INVALID", "无法读取动作视频的时长或画面尺寸")
    if not math.isfinite(duration) or duration < MIN_DURATION_SECONDS or duration > MAX_DURATION_SECONDS:
        _error("MOTION_UPLOAD_DURATION_INVALID",
               f"动作参考时长需在 {MIN_DURATION_SECONDS:g}–{MAX_DURATION_SECONDS:g} 秒之间，当前 {duration:.2f} 秒")
    if min(width, height) < 128 or max(width, height) > 4096 or width * height > 4096 * 2160:
        _error("MOTION_UPLOAD_METADATA_INVALID", "画面最短边至少 128 像素，最大支持约 4K，请先调整视频尺寸")
    return {"duration_seconds": round(duration, 3), "width": width, "height": height}


def _validate_and_normalize(input_path: str, output_path: str) -> Dict[str, Any]:
    """保留构图，最长边限制 1280；完整解码后输出 25fps 无声 H.264。"""
    _video_meta(input_path)
    cp = ffmpeg.run_ffmpeg([
        "-y", "-v", "error", "-xerror", "-threads", "2", "-protocol_whitelist", "file",
        "-i", input_path, "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-vf", "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=25",
        "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", output_path,
    ], timeout=180)
    if cp.returncode != 0 or not os.path.isfile(output_path):
        _error("MOTION_UPLOAD_NORMALIZE_FAILED", "动作视频无法规范化，请检查文件后重试", 409)
    normalized_meta = _video_meta(output_path)
    if Path(output_path).stat().st_size > MAX_UPLOAD_BYTES:
        _error("MOTION_UPLOAD_NORMALIZED_TOO_LARGE", "规范化动作文件超过 100 MiB，请缩短参考视频")
    digest = _file_hash(Path(output_path))
    return {**normalized_meta, "path": output_path, "file_hash": digest}


async def _save_request_body(request: Request, target: Path) -> int:
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            too_large = int(declared_size) > MAX_UPLOAD_BYTES
        except ValueError:
            _error("MOTION_UPLOAD_SIZE_INVALID", "上传请求的文件大小无效")
        if too_large:
            _error("MOTION_UPLOAD_TOO_LARGE", "动作参考文件不能超过 100 MiB", 413)
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    _error("MOTION_UPLOAD_TOO_LARGE", "动作参考文件不能超过 100 MiB", 413)
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except Exception:
        target.unlink(missing_ok=True)
        _error("MOTION_UPLOAD_WRITE_FAILED", "动作参考无法写入本地工作区", 409)
    if total == 0:
        target.unlink(missing_ok=True)
        _error("MOTION_UPLOAD_EMPTY", "不能上传空文件")
    return total


@router.post("/identities/{identity_id}/motion-uploads")
async def upload_motion(identity_id: str, request: Request):
    _identity(identity_id)
    original = unquote(request.headers.get("x-file-name") or "动作参考.mp4").strip()
    original = original.replace("\\", "/").rsplit("/", 1)[-1]
    if not original or len(original) > 255 or any(ord(c) < 32 for c in original):
        _error("MOTION_UPLOAD_FILENAME_INVALID", "文件名无效，请重命名后上传")
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        _error("MOTION_UPLOAD_FORMAT_UNSUPPORTED", "动作参考只支持 MP4 或 MOV 文件")
    upload_id = st.new_id("mup")
    directory = _upload_dir(upload_id)
    # 使用固定内部文件名，原始文件不与规范化产物同名。
    source = directory / ("原始上传" + suffix)
    normalized = directory / "动作参考.mp4"
    stored = False
    try:
        size = await _save_request_body(request, source)
        result = await asyncio.to_thread(_validate_and_normalize, str(source), str(normalized))
        normalized_path = Path(result.get("path") or normalized)
        if not normalized_path.is_file():
            _error("MOTION_UPLOAD_NORMALIZE_FAILED", "动作视频规范化文件不存在", 409)
        row = {
            "id": upload_id, "identity_id": identity_id, "state": "pending",
            "original_filename": original, "source_uri": str(source.relative_to(person_image.workspace_root())).replace("\\", "/"),
            "storage_uri": str(normalized_path.relative_to(person_image.workspace_root())).replace("\\", "/"),
            "original_hash": _file_hash(source),
            "file_hash": result["file_hash"], "size_bytes": size, "duration_seconds": result["duration_seconds"],
            "width": result["width"], "height": result["height"], "reference_id": None,
            "created_at": now_iso(), "error": None,
        }
        _identity(identity_id)  # 校验期间人物可能已丢弃。
        st.put("motion_uploads", upload_id, row, {"identity_id": identity_id})
        stored = True
        return _row_view(row)
    except HTTPException:
        raise
    except Exception as exc:
        _error("MOTION_UPLOAD_INVALID", f"动作视频校验失败：{type(exc).__name__}")
    finally:
        if not stored:
            # 只清理本次随机目录里自己创建的两份文件，不递归操作用户目录。
            source.unlink(missing_ok=True)
            normalized.unlink(missing_ok=True)
            if directory.exists():
                try:
                    directory.rmdir()
                except OSError:
                    pass


@router.get("/identities/{identity_id}/motion-uploads")
def list_motion_uploads(identity_id: str):
    _identity(identity_id)
    return {"uploads": [_row_view(r) for r in st.all_rows("motion_uploads", "identity_id=?", [identity_id])]}


@router.get("/motion-uploads/{upload_id}/preview")
def preview_motion_upload(upload_id: str):
    row = st.get("motion_uploads", upload_id)
    if not row:
        _error("MOTION_UPLOAD_NOT_FOUND", "动作上传记录不存在", 404)
    target = _verified_upload_path(row)
    if not target or not os.path.isfile(target):
        _error("MOTION_UPLOAD_FILE_MISSING", "动作预览文件不存在，请重新上传", 404)
    return FileResponse(target, media_type="video/mp4", headers={"Cache-Control": "no-store"})


def _verified_upload_path(row: dict) -> Optional[str]:
    target = person_image.resolve_media_path(row.get("storage_uri") or "")
    if not target or not os.path.isfile(target):
        _error("MOTION_UPLOAD_FILE_MISSING", "动作预览文件不存在，请重新上传", 409)
    actual = _file_hash(Path(target))
    if actual != row.get("file_hash"):
        _error("MOTION_UPLOAD_HASH_MISMATCH", "动作文件已变化，不能沿用此前检查结果", 409)
    return target


def _confirm_motion_upload(upload_id: str, payload: Dict[str, Any]):
    row = st.get("motion_uploads", upload_id)
    if not row:
        _error("MOTION_UPLOAD_NOT_FOUND", "动作上传记录不存在", 404)
    actor = str(payload.get("actor_id") or "")
    if actor != "user_local":
        _error("ACTOR_NOT_USER", "动作模板确认必须由用户完成")
    if payload.get("confirmed") is not True:
        _error("MOTION_CONFIRMATION_REQUIRED", "请先预览动作视频并确认采用")
    label = str(payload.get("label") or "").strip()
    description = str(payload.get("description") or "").strip()
    source_basis = str(payload.get("source_basis") or "").strip()
    motion_key = str(payload.get("motion_key") or "").strip()
    if not (1 <= len(label) <= 60) or not source_basis or len(source_basis) > 300:
        _error("MOTION_CONFIRMATION_INVALID", "请填写 1–60 字的动作名称和具体来源说明")
    if len(description) > 300 or motion_key not in UPLOADABLE_MOTION_KEYS:
        _error("MOTION_CONFIRMATION_INVALID", "动作类型或描述不符合要求")
    _identity(row["identity_id"])
    _verified_upload_path(row)
    if row.get("reference_id"):
        existing = st.get("motion_references", row["reference_id"])
        if not existing or existing.get("approval_state") != "approved":
            _error("MOTION_UPLOAD_REFERENCE_INVALID", "已保存的动作被撤销或缺失，请检查动作记录", 409)
        return existing
    asset = AvatarAsset(
        id=st.new_id("img"), identity_id=row["identity_id"], kind="motion",
        storage_uri=row["storage_uri"], file_hash=row["file_hash"], width=row["width"], height=row["height"],
        source_evidence=f"{actor} 上传 {row['original_filename']}；原始文件 {row['original_hash']}；来源说明：{source_basis}",
    )
    ref = MotionReference(
        id=st.new_id("mot"), identity_id=row["identity_id"], motion_key=motion_key, asset_id=asset.id,
        label=label, description=description, approval_state="approved",
        supported_params={}, loop_seconds=row["duration_seconds"],
        note="用户预览确认的动作参考；类型仅用于分类，实际动作来自视频。仅迁移表情与头姿；长片重复可能有循环接缝。",
        source_asset_hash=asset.file_hash, confirmed_by=actor, confirmed_at=now_iso(),
    )
    st.insert_row(st.conn(), "avatar_assets", asset.id, asset.model_dump(), {"identity_id": row["identity_id"]})
    st.insert_row(st.conn(), "motion_references", ref.id, ref.model_dump(), {"identity_id": row["identity_id"]})
    row.update(state="confirmed", reference_id=ref.id, label=label)
    st.insert_row(st.conn(), "motion_uploads", upload_id, row, {"identity_id": row["identity_id"]})
    return ref.model_dump()


@router.post("/motion-uploads/{upload_id}/confirm")
def confirm_motion_upload(upload_id: str, payload: Dict[str, Any]):
    with st.transaction():
        return _confirm_motion_upload(upload_id, payload)
