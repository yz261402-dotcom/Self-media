"""Reference-photo uploads and explicitly confirmed avatar image edits."""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from .avatar import image_reference
from .avatar.person_image import PersonImageRejected
from .contracts import StrictModel

router = APIRouter(prefix="/api/v1/avatar-identities")


class ReferenceAvatarRequest(StrictModel):
    reference_image_id: str = Field(min_length=1, max_length=100)
    appearance: str = Field(default="", max_length=2000)
    free_text: str = Field(default="", max_length=4000)
    size: str = "1024x1536"
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    paid_ack: bool = Field(default=False, strict=True)


def _error(error):
    raise HTTPException(status_code=getattr(error, "status", 422), detail={"error": {
        "code": error.code, "message": error.message, "retryable": False}})


@router.post("/reference-images")
async def upload_reference(request: Request):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in image_reference.CONTENT_FORMATS:
        _error(image_reference.ReferenceImageError("IMAGE_REFERENCE_FORMAT_UNSUPPORTED", "参考图片只支持 PNG、JPEG 或 WebP"))
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > image_reference.MAX_UPLOAD_BYTES:
                _error(image_reference.ReferenceImageError("IMAGE_REFERENCE_TOO_LARGE", "参考图片不能超过 10 MiB", 413))
        except ValueError:
            _error(image_reference.ReferenceImageError("IMAGE_REFERENCE_SIZE_INVALID", "文件大小无效"))
    filename = unquote(request.headers.get("x-filename") or "reference.png").replace("\\", "/").rsplit("/", 1)[-1]
    if len(filename) > 255 or any(ord(c) < 32 for c in filename):
        _error(image_reference.ReferenceImageError("IMAGE_REFERENCE_FILENAME_INVALID", "参考图片文件名无效"))
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > image_reference.MAX_UPLOAD_BYTES:
            _error(image_reference.ReferenceImageError("IMAGE_REFERENCE_TOO_LARGE", "参考图片不能超过 10 MiB", 413))
        chunks.append(chunk)
    try:
        return image_reference.save_reference(b"".join(chunks), content_type=content_type, filename=filename)
    except image_reference.ReferenceImageError as error:
        _error(error)


@router.post("/candidates/from-image")
def generate_reference_avatar(payload: ReferenceAvatarRequest):
    try:
        return image_reference.generate_from_reference(payload.model_dump())
    except (image_reference.ReferenceImageError, PersonImageRejected) as error:
        _error(error)


@router.get("/image-generations/{request_id}")
def get_reference_generation(request_id: str):
    try:
        return image_reference.get_generation(request_id)
    except image_reference.ReferenceImageError as error:
        _error(error)
