"""发布任务 HTTP API。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/api/v1")


@router.get('/publish/douyin/page')
def inspect_douyin_page():
    """本机诊断仅返回控件类型/标签，不返回输入值、Cookie 或网页内部状态。"""
    adapter = service.get_adapter('douyin')
    return adapter.inspect() if hasattr(adapter, 'inspect') else {'state': 'mock', 'is_mock': True}


def _raise(exc: service.PublishBlocked):
    raise HTTPException(status_code=exc.status,
                        detail={"error": {"code": exc.code, "message": exc.message,
                                           **({"action": exc.action} if exc.action else {}),
                                           "retryable": exc.retryable}})


@router.post("/works/{work_id}/publish/start")
def start_publish(work_id: str, payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        return service.start_task(
            work_id, platform=str(payload.get("platform") or "douyin"),
            auto_submit=payload.get("auto_submit") is True,
            idempotency_key=payload.get("idempotency_key"), title=payload.get("title"),
            description=payload.get("description"), tags=payload.get("tags"),
            scheduled_at=payload.get("scheduled_at"),
        )
    except service.PublishBlocked as exc:
        _raise(exc)


@router.post("/works/{work_id}/publish/confirm")
def confirm_publish(work_id: str, payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        return service.confirm_task(work_id, task_id=payload.get("task_id"),
                                    confirmed=payload.get("confirmed") is True,
                                    actor_id=str(payload.get("actor_id") or ""),
                                    package_digest=payload.get("package_digest"),
                                    confirmation_digest=payload.get("confirmation_digest"))
    except service.PublishBlocked as exc:
        _raise(exc)


@router.post("/works/{work_id}/publish/cancel")
def cancel_publish(work_id: str, payload: Dict[str, Any] | None = None):
    payload = payload or {}
    try:
        return service.cancel_task(work_id, task_id=payload.get("task_id"),
                                   reason=str(payload.get("reason") or ""))
    except service.PublishBlocked as exc:
        _raise(exc)


@router.get("/works/{work_id}/publish/status")
def publish_status(work_id: str, task_id: str | None = None):
    task = service.get_task(work_id, task_id)
    if not task:
        return {"id": None, "work_id": work_id, "state": "draft", "task": None}
    return task


@router.get("/publish/status/{work_id}")
def publish_status_alias(work_id: str, task_id: str | None = None):
    return publish_status(work_id, task_id)


@router.get("/works/{work_id}/publish/tasks")
def publish_tasks(work_id: str):
    return {"work_id": work_id, "tasks": service.list_tasks(work_id)}


@router.post("/works/{work_id}/publish/resume")
def resume_publish(work_id: str, payload: Dict[str, Any]):
    if not payload.get("task_id"):
        _raise(service.PublishBlocked("PUBLISH_VERSION_REQUIRED", "请携带当前发布任务编号。", 422))
    try:
        return service.resume_task(work_id, task_id=payload["task_id"])
    except service.PublishBlocked as exc:
        _raise(exc)


@router.post("/publish/douyin/login")
def login_douyin():
    try:
        return service.prepare_login()
    except service.PublishBlocked as exc:
        _raise(exc)


@router.get("/publish/douyin/session")
def douyin_session():
    try:
        return service.session_status()
    except service.PublishBlocked as exc:
        _raise(exc)
