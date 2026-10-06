"""发布任务：先上传准备，再明确确认；不确定提交只核验，绝不自动重发。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from .. import delivery, storage as st
from ..contracts import now_iso
from .adapters import AdapterError, adapter_for, caption_text

SESSION_DIR = Path(__file__).resolve().parents[2] / "runtime" / "publish" / "douyin-profile"


class PublishBlocked(Exception):
    def __init__(self, code: str, message: str, status: int = 409, *, action: str | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.action, self.retryable = action, retryable


def get_adapter(platform):
    return adapter_for(platform)


def resolve_publish_package(work_id):
    work = st.get("works", work_id)
    if not work:
        raise PublishBlocked("WORK_NOT_FOUND", "作品不存在。", 404)
    if work.get("deleted_at"):
        raise PublishBlocked("WORK_DELETED", "作品已删除，不能继续发布。", 404)
    package = delivery.current_package(work_id)
    if not package:
        raise PublishBlocked("FINAL_REVIEW_REQUIRED", "请先观看并确认成片，生成发布就绪包。",
                             action="confirm_postprocess")
    try:
        delivery.download_path(package["id"])
        record = delivery.latest_execution(work)
        if not record:
            raise delivery.DeliveryBlocked("RENDER_REQUIRED", "未找到已验证成片。")
        files = delivery.verified_outputs(record)
    except delivery.DeliveryBlocked as exc:
        raise PublishBlocked(exc.code, exc.message) from exc
    return package, next(p for f, p in files if f["kind"] == "master"), next(p for f, p in files if f["kind"] == "cover")


def _session_dir():
    return Path(os.getenv("AVATAR_STUDIO_DOUYIN_SESSION_DIR") or SESSION_DIR).resolve()


def _browser_key(session_dir):
    return "publish_browser_" + hashlib.sha256(str(Path(session_dir).resolve()).encode()).hexdigest()[:24]


def _write(conn, task):
    task["updated_at"] = now_iso()
    st.insert_row(conn, "publish_tasks", task["id"], task, {"work_id": task["work_id"]})
    return dict(task)


def _claim_browser(conn, task):
    key = _browser_key(task["session_dir"])
    lock = st.get("resource_locks", key)
    if lock and lock.get("publish_task_id") != task["id"]:
        owner = st.get("publish_tasks", lock.get("publish_task_id", "")) or {}
        if owner.get("state") not in ("submitted", "cancelled", "failed") or owner.get("attempt_started_at") and owner.get("state") != "submitted":
            raise PublishBlocked("PUBLISH_BROWSER_BUSY", "专用浏览器正在处理其他作品，请先完成或取消该任务。")
    st.insert_row(conn, "resource_locks", key, {"id": key, "publish_task_id": task["id"], "created_at": now_iso()})


def _release_browser(conn, task):
    key = _browser_key(task["session_dir"])
    lock = st.get("resource_locks", key) or {}
    if lock.get("publish_task_id") == task["id"]:
        conn.execute("DELETE FROM resource_locks WHERE id=?", [key])


def list_tasks(work_id, platform="douyin"):
    return sorted([r for r in st.all_rows("publish_tasks", "work_id=?", [work_id]) if r.get("platform") == platform],
                  key=lambda r: r.get("created_at", ""), reverse=True)


def get_task(work_id, task_id=None, platform="douyin"):
    if task_id:
        row = st.get("publish_tasks", task_id)
        return row if row and row.get("work_id") == work_id else None
    return next(iter(list_tasks(work_id, platform)), None)


def _require_task(work_id, task_id=None):
    task = get_task(work_id, task_id)
    if not task:
        raise PublishBlocked("PUBLISH_TASK_NOT_FOUND", "发布任务不存在。", 404)
    return task


def _check_inputs(task):
    package, video, cover = resolve_publish_package(task["work_id"])
    if package["id"] != task["package_id"] or package.get("manifest_digest", "") != task["package_digest"]:
        raise PublishBlocked("PUBLISH_INPUT_CHANGED", "交付包已变化，请创建新版本的发布任务。")
    return video, cover


def _arguments(task, video, cover):
    return dict(session_dir=Path(task["session_dir"]), task_id=task["id"], video_path=video, cover_path=cover,
                title=task["title"], description=task["description"], tags=task["tags"],
                scheduled_at=task.get("scheduled_at"))


def _spawn(target, *args):
    # 每个线程独立 SQLite 连接；浏览器占用在事务中持久化。长上传不持有全局锁。
    def run():
        try:
            target(*args)
        finally:
            conn = getattr(st._local, "conn", None)
            if conn is not None:
                conn.close()
                del st._local.conn
    threading.Thread(target=run, name="douyin-publish", daemon=True).start()


def _safe_error(exc, fallback):
    if isinstance(exc, (AdapterError, PublishBlocked)):
        return {"code": exc.code, "message": exc.message}
    return {"code": fallback, "message": "浏览器操作未完成，请查看发布页面后继续。"}


def _confirmation_digest(task):
    snapshot = {key: task.get(key) for key in ("id", "package_digest", "title", "description", "tags",
                                               "scheduled_at", "account_id", "revision")}
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _metadata(package, title, description, tags, scheduled_at):
    if scheduled_at:
        raise PublishBlocked("SCHEDULE_NOT_SUPPORTED", "首版只支持立即投稿，定时发布尚未接入。", 422)
    title = package.get("title", "") if title is None else title
    description = package.get("description", "") if description is None else description
    tags = package.get("tags", []) if tags is None else tags
    if not isinstance(title, str) or not isinstance(description, str) or not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise PublishBlocked("PUBLISH_METADATA_INVALID", "标题和简介必须为文本，话题必须为文本列表。", 422)
    title, description = title.strip(), description.strip()
    if not 1 <= len(title) <= 30 or len(caption_text(description, tags)) > 1000:
        raise PublishBlocked("DOUYIN_TEXT_LIMIT", "标题需为 1–30 字，简介、话题与 AI 声明合计不超过 1000 字。", 422)
    return {"title": title, "description": description, "tags": list(tags), "scheduled_at": None}


def _prepare(task_id, adapter):
    task = st.get("publish_tasks", task_id)
    if not task or task["state"] != "uploading":
        return
    try:
        video, cover = _check_inputs(task)
        result = adapter.prepare(**_arguments(task, video, cover))
        state = result.get("state")
        if state == "ready_to_confirm" and not result.get("account_id"):
            result = {"state": "needs_verification", "error": {"code": "DOUYIN_ACCOUNT_UNVERIFIED", "message": "尚未核验投稿账号，请检查专用浏览器。"}}
        elif state not in ("ready_to_confirm", "awaiting_login", "needs_verification"):
            result = {"state": "failed", "error": {"code": "PUBLISH_PREPARE_UNVERIFIED", "message": "未确认视频和文案已准备就绪。"}}
    except Exception as exc:
        state = exc.state if isinstance(exc, AdapterError) and exc.state in ("awaiting_login", "needs_verification") else "failed"
        result = {"state": state, "error": _safe_error(exc, "PUBLISH_PREPARE_FAILED")}
    with st.transaction() as conn:
        current = st.get("publish_tasks", task_id)
        if not current or current["state"] != "uploading":
            return
        current.update(state=result["state"], error=result.get("error"), account_id=result.get("account_id"),
                       account_label=result.get("account_label"), preparation=result.get("preparation"))
        current["confirmation_digest"] = _confirmation_digest(current) if current["state"] == "ready_to_confirm" else None
        _write(conn, current)
        if current["state"] == "failed":
            _release_browser(conn, current)


def _submit(task_id, adapter):
    task = st.get("publish_tasks", task_id)
    if not task or task["state"] != "submitting":
        return
    submission_attempted = None
    try:
        video, cover = _check_inputs(task)
        result = adapter.submit(**_arguments(task, video, cover), expected_account_id=task["account_id"])
        receipt = result.get("receipt") or {}
        ui_ack = receipt.get("kind") == "ui_ack" and receipt.get("text") in ("发布成功", "作品已发布", "提交成功")
        if result.get("state") != "submitted" or not (result.get("provider_ref") or ui_ack):
            result = {"state": "needs_verification", "error": result.get("error") or {
                "code": "PUBLISH_SUBMIT_UNVERIFIED", "message": "未取得确定的提交回执，请检查抖音作品列表；系统不会重发。"}}
        else:
            submission_attempted = True
    except Exception as exc:
        # 只有浏览器适配器明确证明“发布按钮没有被点击”，才能允许重新准备。
        # bool identity 判断避免 0 / "false" 等不确定元数据被当作无提交证据。
        if isinstance(exc, AdapterError) and exc.detail.get("submission_attempted") is False:
            submission_attempted = False
            result = {"state": "awaiting_login" if exc.state == "awaiting_login" else "failed",
                      "error": _safe_error(exc, "PUBLISH_PRE_SUBMIT_CHECK_FAILED")}
        else:
            result = {"state": "needs_verification", "error": _safe_error(exc, "PUBLISH_SUBMIT_UNVERIFIED")}
    with st.transaction() as conn:
        current = st.get("publish_tasks", task_id)
        if not current or current["state"] != "submitting":
            return
        current.setdefault("submission_attempts", []).append({
            "started_at": current.get("attempt_started_at"), "finished_at": now_iso(),
            "phase": "pre_submit_checks" if submission_attempted is False else "submission",
            "submission_attempted": submission_attempted, "outcome": result["state"],
            "confirmation_digest": (current.get("confirmation") or {}).get("confirmation_digest"),
            "error": result.get("error"),
        })
        current.update(state=result["state"], error=result.get("error"), provider_ref=result.get("provider_ref"),
                       provider_url=result.get("url"), receipt=result.get("receipt"))
        if submission_attempted is False:
            current.update(attempt_started_at=None, confirmation_digest=None, confirmation=None)
            _release_browser(conn, current)
        elif current["state"] == "submitted":
            current["submitted_at"] = now_iso()
            _release_browser(conn, current)
        _write(conn, current)


def start_task(work_id, *, platform="douyin", auto_submit=False, idempotency_key=None,
               title=None, description=None, tags=None, scheduled_at=None):
    if platform != "douyin":
        raise PublishBlocked("PLATFORM_UNSUPPORTED", "目前仅支持抖音创作者中心。", 422)
    package, _, _ = resolve_publish_package(work_id)
    metadata = _metadata(package, title, description, tags, scheduled_at)
    # 客户端 key 无法改变一个包只能提交一次的事实；修改文案不能绕过此门禁。
    key = hashlib.sha256(f"{platform}:{work_id}:{package['id']}:{package.get('manifest_digest', '')}".encode()).hexdigest()
    adapter = get_adapter(platform)
    with st.transaction() as conn:
        current_work = st.get("works", work_id) or {}
        if current_work.get("deleted_at"):
            raise PublishBlocked("WORK_DELETED", "作品已删除，不能继续发布。", 404)
        existing = next((r for r in list_tasks(work_id, platform) if r.get("idempotency_key") == key), None)
        if existing and (existing["state"] != "cancelled" or existing.get("attempt_started_at")):
            return existing
        if existing:
            task = existing
            task.setdefault("metadata_history", []).append({key: task.get(key) for key in (
                "title", "description", "tags", "scheduled_at", "revision", "updated_at", "confirmation_digest")})
            task.update(**metadata, state="uploading", error=None, confirmation_digest=None,
                        account_id=None, account_label=None, revision=task.get("revision", 1) + 1)
        else:
            task = {"id": st.new_id("publish"), "work_id": work_id, "platform": platform,
                "adapter": getattr(adapter, "name", type(adapter).__name__), "is_mock": bool(getattr(adapter, "is_mock", False)),
                "state": "uploading", "idempotency_key": key, "package_id": package["id"],
                "package_digest": package.get("manifest_digest", ""),
                **metadata, "revision": 1, "metadata_history": [], "confirmation_digest": None,
                "auto_submit": False, "requires_user_confirmation": True, "session_dir": str(_session_dir()),
                "account_id": None, "account_label": None, "created_at": now_iso(), "updated_at": now_iso(),
                "provider_ref": None, "provider_url": None, "error": None, "attempt_started_at": None}
        _claim_browser(conn, task)
        response = _write(conn, task)
    _spawn(_prepare, task["id"], adapter)
    return response


def resume_task(work_id, *, task_id):
    task = _require_task(work_id, task_id)
    _check_inputs(task)
    adapter = get_adapter(task["platform"])
    with st.transaction() as conn:
        task = _require_task(work_id, task_id)
        if task.get("attempt_started_at") or task["state"] in ("needs_verification", "submitting", "submitted"):
            raise PublishBlocked("PUBLISH_SUBMISSION_LOCKED", "此任务提交结果需核验，不能重新上传或重发。")
        if task["state"] in ("uploading", "ready_to_confirm"):
            return task
        if task["state"] not in ("failed", "awaiting_login", "cancelled"):
            raise PublishBlocked("PUBLISH_TASK_NOT_RESUMABLE", "该任务不能继续，请检查任务状态。")
        _claim_browser(conn, task)
        task.update(state="uploading", error=None, confirmation_digest=None, revision=task.get("revision", 1) + 1)
        response = _write(conn, task)
    _spawn(_prepare, task["id"], adapter)
    return response


def confirm_task(work_id, *, task_id=None, confirmed=False, actor_id="", package_digest=None, confirmation_digest=None):
    if confirmed is not True or not actor_id.startswith("user"):
        raise PublishBlocked("USER_CONFIRMATION_REQUIRED", "请确认当前成片、文案和投稿账号后再提交。", 422)
    if not task_id or not package_digest:
        raise PublishBlocked("PUBLISH_VERSION_REQUIRED", "确认时必须携带当前发布任务和交付包版本。", 422)
    task = _require_task(work_id, task_id)
    if package_digest != task.get("package_digest"):
        raise PublishBlocked("PUBLISH_INPUT_CHANGED", "交付包版本不一致，请刷新发布页面。")
    _check_inputs(task)
    adapter = get_adapter(task["platform"])
    with st.transaction() as conn:
        task = _require_task(work_id, task_id)
        if task["state"] == "submitted":
            return task
        if task.get("attempt_started_at") or task["state"] in ("submitting", "needs_verification"):
            raise PublishBlocked("PUBLISH_SUBMISSION_LOCKED", "正在提交或结果未确定；为避免重复发布，请先核验抖音作品列表。")
        if not confirmation_digest or confirmation_digest != task.get("confirmation_digest"):
            raise PublishBlocked("PUBLISH_CONFIRMATION_STALE", "发布文案、账号或准备版本已变化，请刷新核对后重新确认。")
        if task["state"] != "ready_to_confirm" or not task.get("account_id"):
            raise PublishBlocked("PUBLISH_NOT_READY", "尚未完成上传、文案填写与账号核验，请先继续准备。")
        _claim_browser(conn, task)
        task.update(state="submitting", attempt_started_at=now_iso(),
                    confirmation={"actor_id": actor_id, "confirmed_at": now_iso(), "package_digest": package_digest,
                                  "account_id": task["account_id"], "confirmation_digest": confirmation_digest}, error=None)
        task.setdefault("confirmation_history", []).append(dict(task["confirmation"]))
        response = _write(conn, task)
    _spawn(_submit, task["id"], adapter)
    return response


def cancel_task(work_id, *, task_id=None, reason=""):
    with st.transaction() as conn:
        task = _require_task(work_id, task_id)
        if task.get("attempt_started_at") or task["state"] in ("submitted", "submitting", "needs_verification"):
            raise PublishBlocked("PUBLISH_SUBMISSION_LOCKED", "已进入提交或结果不确定，不能按未发布取消；请先核验平台记录。")
        if task["state"] == "uploading":
            raise PublishBlocked("PUBLISH_PREPARING", "正在上传，等本次页面操作完成后可取消。")
        task.update(state="cancelled", error={"code": "PUBLISH_CANCELLED", "message": reason or "用户取消发布"})
        _release_browser(conn, task)
        return _write(conn, task)


def prepare_login():
    session = _session_dir()
    lock = st.get("resource_locks", _browser_key(session)) or {}
    task = st.get("publish_tasks", lock.get("publish_task_id", "")) or {}
    if task.get("state") in ("uploading", "ready_to_confirm", "submitting", "needs_verification"):
        raise PublishBlocked("PUBLISH_BROWSER_BUSY", "专用浏览器已被发布任务占用，请先处理当前任务。")
    try:
        result = get_adapter("douyin").login(session)
    except AdapterError as exc:
        raise PublishBlocked(exc.code, exc.message, 503) from exc
    state = result.get("state")
    if state not in ("awaiting_login", "logged_in"):
        raise PublishBlocked("DOUYIN_LOGIN_UNVERIFIED", "尚未确认登录状态。", 409)
    return {**result, "logged_in": state == "logged_in" and bool(result.get("account_id"))}


def session_status():
    try:
        return get_adapter("douyin").status(_session_dir())
    except AdapterError as exc:
        raise PublishBlocked(exc.code, exc.message, 503) from exc


def recover_pending():
    """只标记重启中断，不自动重试任何发布操作。"""
    count = 0
    with st.transaction() as conn:
        for task in st.all_rows("publish_tasks"):
            if task["state"] == "submitting" or task.get("attempt_started_at") and task["state"] not in ("submitted", "needs_verification"):
                task.update(state="needs_verification", error={"code": "PUBLISH_RESTART_AFTER_SUBMIT", "message": "提交期间服务重启，请先核验抖音作品列表；系统不会重发。"})
            elif task["state"] in ("draft", "uploading", "ready_to_confirm"):
                task.update(state="failed", error={"code": "PUBLISH_PREPARATION_INTERRUPTED", "message": "服务重启中断准备，请点击继续上传后重新确认。"})
                _release_browser(conn, task)
            else:
                continue
            _write(conn, task)
            count += 1
    return count
