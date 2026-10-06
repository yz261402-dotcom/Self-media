"""Locally validated reference images and durable, single-attempt avatar edits."""
from __future__ import annotations

import hashlib
import io
import json
import time
import warnings
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from .. import storage as st
from ..contracts import now_iso
from . import person_image

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
RUNNING_TIMEOUT_SECONDS = 720
CONTENT_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class ReferenceImageError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _media_url(uri: str) -> str:
    return "/api/v1/media?uri=" + quote(uri, safe="")


def _normalize(data: bytes, content_type: str) -> tuple[bytes, int, int]:
    if content_type not in CONTENT_FORMATS:
        raise ReferenceImageError("IMAGE_REFERENCE_FORMAT_UNSUPPORTED", "参考图片只支持 PNG、JPEG 或 WebP")
    if not data:
        raise ReferenceImageError("IMAGE_REFERENCE_EMPTY", "参考图片不能为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ReferenceImageError("IMAGE_REFERENCE_TOO_LARGE", "参考图片不能超过 10 MiB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != CONTENT_FORMATS[content_type] or image.width * image.height > MAX_PIXELS:
                    raise ReferenceImageError("IMAGE_REFERENCE_FORMAT_INVALID", "参考图片格式或像素尺寸不符合要求")
                if getattr(image, "n_frames", 1) != 1:
                    raise ReferenceImageError("IMAGE_REFERENCE_ANIMATED", "请上传单帧静态图片")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue(), normalized.width, normalized.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ReferenceImageError("IMAGE_REFERENCE_INVALID", "参考图片无法完整解析，请上传有效图片") from None


def _target(uri: str) -> Path:
    path = person_image.resolve_media_path(uri)
    root = Path(person_image.workspace_root()).resolve()
    raw_path = root / uri
    if (not path or not Path(path).is_relative_to(root)
            or any(p.is_symlink() for p in [raw_path, *raw_path.parents] if p.is_relative_to(root))):
        raise ReferenceImageError("IMAGE_REFERENCE_PATH_INVALID", "参考图片路径无效", 409)
    return Path(path)


def save_reference(data: bytes, *, content_type: str, filename: str = "") -> dict:
    normalized, width, height = _normalize(data, content_type)
    image_id = "ref_" + uuid4().hex
    uri = f"assets/avatar-references/{image_id}.png"
    path = _target(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(normalized)
    record = {"id": image_id, "storage_uri": uri, "file_hash": _hash(normalized), "width": width,
              "height": height, "preview_url": _media_url(uri)}
    try:
        st.put("avatar_reference_images", image_id, {**record, "original_filename": filename,
               "original_hash": _hash(data), "created_at": now_iso()})
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return record


def get_reference(image_id: str) -> tuple[dict, bytes]:
    record = st.get("avatar_reference_images", image_id)
    if not record:
        raise ReferenceImageError("IMAGE_REFERENCE_NOT_FOUND", "参考图片不存在，请重新上传", 404)
    path = _target(record["storage_uri"])
    if not path.is_file() or path.stat().st_size > MAX_PIXELS * 3 + 100_000:
        raise ReferenceImageError("IMAGE_REFERENCE_FILE_MISSING", "参考图片文件不存在或大小无效", 409)
    data = path.read_bytes()
    if _hash(data) != record.get("file_hash"):
        raise ReferenceImageError("IMAGE_REFERENCE_HASH_MISMATCH", "参考图片已变化，请重新上传", 409)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.width * image.height > MAX_PIXELS:
                raise ValueError("invalid stored image")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ReferenceImageError("IMAGE_REFERENCE_INVALID", "参考图片无法完整解析", 409) from None
    return {k: record[k] for k in ("id", "storage_uri", "file_hash", "width", "height", "preview_url")}, data


def edit_transport(request: dict, timeout: int = person_image.DEFAULT_TIMEOUT_S) -> dict:
    return person_image.httpx_transport(request, timeout=timeout)


def _view(record: dict) -> dict:
    return {k: record.get(k) for k in ("request_id", "state", "result", "error")}


def _recovered_result(conn, record: dict) -> dict | None:
    """Rebuild a completed paid result from its uniquely marked, verified hero asset."""
    from . import registry

    request_id = record["request_id"]
    marker = f"request_id={request_id}"
    matches = [asset for asset in st.all_rows("avatar_assets")
               if marker in asset.get("source_evidence", "").split()]
    if len(matches) != 1:
        return None
    asset = matches[0]
    path = person_image.resolve_media_path(asset.get("storage_uri") or "")
    if not path or not Path(path).is_file() or Path(path).is_symlink():
        return None
    try:
        with Path(path).open("rb") as stream:
            if "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest() != asset.get("file_hash"):
                return None
        with Image.open(path) as image:
            image.load()
            if image.size != (asset.get("width"), asset.get("height")):
                return None
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    identity = registry.get_identity(asset["identity_id"])
    if not identity or identity.get("frozen"):
        return None
    if identity.get("hero_asset_id") != asset["id"]:
        identity["hero_asset_id"] = asset["id"]
        identity["license_id"] = asset.get("license_id")
        st.insert_row(conn, "identities", identity["id"], identity)
    args = record.get("args") or {}
    reference = st.get("avatar_reference_images", args.get("reference_image_id") or "")
    if not reference:
        return None
    evidence = asset.get("source_evidence", "")
    request_digest = next((word.split("=", 1)[1] for word in evidence.split()
                           if word.startswith("request_digest=")), "")
    return {"identity": identity, "asset": asset,
            "prompt": person_image.compose_reference_prompt(appearance=args.get("appearance", ""),
                                                              free_text=args.get("free_text", "")),
            "request_digest": request_digest, "usage": {}, "image_url": _media_url(asset["storage_uri"]),
            "reference_image": {k: reference[k] for k in
                                ("id", "storage_uri", "file_hash", "width", "height", "preview_url")},
            "request_id": request_id,
            "warnings": ["AI 图生图结果可能与参考人物有差异，请确认形象", "身份未定版，须经人工审核后才能进入生产"]}


def get_generation(request_id: str) -> dict:
    with st.transaction() as conn:
        row = conn.execute("SELECT data FROM avatar_image_generations WHERE id=?", [request_id]).fetchone()
        if not row:
            raise ReferenceImageError("IMAGE_GENERATION_NOT_FOUND", "图生图作业不存在", 404)
        record = json.loads(row["data"])
        if record["state"] in ("running", "unknown"):
            recovered = _recovered_result(conn, record)
            if recovered:
                record.update(state="succeeded", result=recovered, error=None)
                st.insert_row(conn, "avatar_image_generations", request_id, record)
        if record["state"] == "running" and time.time() - record["started_at"] > RUNNING_TIMEOUT_SECONDS:
            record.update(state="unknown", error={"code": "IMAGE_GENERATION_UNKNOWN",
                          "message": "图生图请求中断或超时，请核对原作业；不会自动重发"})
            st.insert_row(conn, "avatar_image_generations", request_id, record)
    return _view(record)


def generate_from_reference(payload: dict, *, transport=None) -> dict:
    if payload.get("paid_ack") is not True:
        raise ReferenceImageError("IMAGE_GENERATION_CONFIRMATION_REQUIRED", "请先确认本次图生图可能产生的费用")
    reference_record = st.get("avatar_reference_images", payload["reference_image_id"])
    if not reference_record:
        raise ReferenceImageError("IMAGE_REFERENCE_NOT_FOUND", "参考图片不存在，请重新上传", 404)
    size = payload.get("size") or person_image.DEFAULT_SIZE
    model = person_image.build_request(prompt="reference avatar", size=size)["model"]
    args = {"reference_image_id": reference_record["id"], "reference_sha256": reference_record["file_hash"],
            "appearance": payload.get("appearance") or "", "free_text": payload.get("free_text") or "",
            "size": size, "model": model}
    digest = person_image.request_digest(args)
    request_id = payload["request_id"]
    previous = st.get("avatar_image_generations", request_id)
    if previous:
        if previous["input_digest"] != digest:
            raise ReferenceImageError("IMAGE_GENERATION_REQUEST_CONFLICT", "请求编号已使用，请勿变更同一请求的图片或描述", 409)
        status = get_generation(request_id)
        if status["state"] == "succeeded":
            return status["result"]
        error = status.get("error") or {"code": "IMAGE_GENERATION_RUNNING", "message": "原作业仍在处理中，请查询原作业"}
        raise ReferenceImageError(error["code"], error["message"], 409)
    reference, data = get_reference(payload["reference_image_id"])
    with st.transaction() as conn:
        row = conn.execute("SELECT data FROM avatar_image_generations WHERE id=?", [request_id]).fetchone()
        if row:
            record = json.loads(row["data"])
            if record["input_digest"] != digest:
                raise ReferenceImageError("IMAGE_GENERATION_REQUEST_CONFLICT", "请求编号已使用，请勿变更同一请求的图片或描述", 409)
            if record["state"] == "succeeded":
                return record["result"]
            if record["state"] in ("running", "unknown"):
                recovered = _recovered_result(conn, record)
                if recovered:
                    record.update(state="succeeded", result=recovered, error=None)
                    st.insert_row(conn, "avatar_image_generations", request_id, record)
                    return recovered
            error = record.get("error") or {"code": "IMAGE_GENERATION_RUNNING", "message": "原作业仍在处理中，请查询原作业"}
            raise ReferenceImageError(error["code"], error["message"], 409)
        record = {"request_id": request_id, "input_digest": digest, "started_at": time.time(),
                  "state": "running", "args": args, "result": None, "error": None}
        st.insert_row(conn, "avatar_image_generations", request_id, record)
    try:
        produced = person_image.generate_candidate_hero(appearance=args["appearance"], free_text=args["free_text"],
            size=size, model=model, reference_image_bytes=data, reference_sha256=reference["file_hash"],
            reference_uri=reference["storage_uri"], request_id=request_id, transport=transport or edit_transport)
        result = {"identity": produced["identity"], "asset": produced["asset"], "prompt": produced["prompt"],
                  "request_digest": produced["request_digest"], "usage": produced.get("usage") or {},
                  "image_url": _media_url(produced["asset"]["storage_uri"]), "reference_image": reference,
                  "request_id": request_id, "warnings": ["AI 图生图结果可能与参考人物有差异，请确认形象", "身份未定版，须经人工审核后才能进入生产"]}
        record.update(state="succeeded", result=result)
        st.put("avatar_image_generations", request_id, record)
        return result
    except BaseException as error:
        known_failure = isinstance(error, person_image.PersonImageRejected) or (
            isinstance(error, person_image.PersonImageBlocked) and error.code == "IMAGE_PROVIDER_NOT_CONFIGURED")
        record.update(state="failed" if known_failure else "unknown", result=None,
                      error={"code": getattr(error, "code", "IMAGE_GENERATION_UNKNOWN"),
                             "message": str(error) if known_failure else "图生图作业结果未知，可能已计费，请先查询原作业；不会自动重发"})
        st.put("avatar_image_generations", request_id, record)
        if not isinstance(error, Exception):
            raise
        raise ReferenceImageError(record["error"]["code"], record["error"]["message"], 409) from error
