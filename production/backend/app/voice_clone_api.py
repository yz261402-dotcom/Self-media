"""音色克隆 API；上传不自动试听，远端未知状态不会重试。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

from .contracts import ApiError
from .tts import cloning
from .tts.errors import TtsBlocked, TtsRejected

router = APIRouter(prefix="/api/v1")


def _error(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": ApiError(
        code=code, message=message, retryable=False).model_dump()})


@router.post("/voice-clones")
async def create_voice_clone(request: Request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > cloning.MAX_REQUEST_BYTES:
            return _error("VOICE_CLONE_TOO_LARGE", "原始音频不能超过 20,000,000 字节", 413)
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return _error("VOICE_CLONE_REQUEST_INVALID", "请求必须为有效 JSON")
    try:
        return await run_in_threadpool(cloning.create_clone, payload)
    except TtsRejected as exc:
        return _error(exc.code, exc.message, 409 if exc.code == "VOICE_CLONE_REQUEST_CONFLICT" else 422)
    except TtsBlocked as exc:
        return _error(exc.code, exc.message, 409)


@router.get("/voice-clones/{request_id}")
def get_voice_clone(request_id: str):
    job = cloning.get_clone(request_id)
    if job is None:
        return _error("VOICE_CLONE_NOT_FOUND", "音色克隆作业不存在", 404)
    return job
