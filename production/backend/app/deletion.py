"""Transactional visibility deletion; financial records and shared media remain intact."""
from __future__ import annotations

import time

from . import storage as st
from .contracts import now_iso


class DeletionRejected(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def require_work(work_id: str) -> dict:
    row = st.get("works", work_id)
    if not row:
        raise DeletionRejected("WORK_NOT_FOUND", "作品不存在", 404)
    if row.get("deleted_at"):
        raise DeletionRejected("WORK_DELETED", "作品已删除，不能继续制作", 404)
    return row


def require_identity(identity_id: str) -> dict:
    row = st.get("identities", identity_id)
    if not row:
        raise DeletionRejected("IDENTITY_NOT_FOUND", "人物不存在", 404)
    if row.get("deleted_at"):
        raise DeletionRejected("IDENTITY_DELETED", "人物已删除，不能继续使用", 404)
    return row


def _work_blockers(work_id: str) -> list[str]:
    reasons = []
    states = {
        "tasks": ({"queued", "running", "waiting_provider", "retry_wait"}, "制作任务"),
        "content_jobs": ({"queued", "running", "waiting_external", "unknown"}, "内容任务"),
        "provider_jobs": ({"queued", "leased", "running", "sending", "submitted", "waiting_provider", "waiting_external", "unknown"}, "外部调用"),
        "provider_runs": ({"submitted", "waiting_provider", "waiting_external", "unknown"}, "视频生成"),
        "media_jobs": ({"queued", "running", "sending", "unknown", "stitch_queued", "stitching"}, "媒体任务"),
        "publish_tasks": ({"draft", "uploading", "awaiting_login", "ready_to_confirm", "submitting", "needs_verification"}, "发布任务"),
    }
    for table, (blocking, label) in states.items():
        for row in st.all_rows(table, "work_id=?", [work_id]):
            state = row.get("state", "")
            # 已有成功结果的调用已经结束执行。即使费用尚待核对，也不应把
            # 作品永久卡在软删除入口；provider_jobs 与费用台账会随作品保留，
            # 供后续对账。运行中、结果未知或已取消但仍需结算的调用继续阻挡。
            # 若记录只有 succeeded 状态却没有结果摘要，仍按异常记录拦截，
            # 避免把不完整的账本误当作已完成调用。
            success_recorded = (
                table == "provider_jobs"
                and state == "succeeded"
                and row.get("result") is not None
                and bool(row.get("result_digest"))
            )
            pending = state in blocking or (
                bool(row.get("settlement_required")) and not success_recorded
            )
            if table == "publish_tasks":
                pending |= bool(row.get("attempt_started_at")) and state != "submitted"
            if table == "media_jobs":
                pending |= any(shot.get("image_state") in ("sending", "unknown")
                               or shot.get("video_state") in ("sending", "unknown", "waiting_provider")
                               for shot in row.get("shots", []))
            if pending:
                reasons.append(f"{label} {row.get('id', '')} 尚未完成或待核对（{state}）")
    # The batch claim exists before its individual provider jobs are created.
    claim = st.get("resource_locks", "storyboard_submit:" + work_id) or {}
    if claim.get("expires_at", 0) > time.time():
        reasons.append("分镜提交流程正在执行，请等待原请求返回")
    return reasons


def _identity_users(identity_id: str) -> list[dict]:
    live = {w["id"]: w for w in st.all_rows("works") if not w.get("deleted_at")}
    used = {wid for wid, w in live.items() if w.get("identity_id") == identity_id}
    for variant in st.all_rows("variants"):
        if variant.get("identity_id") != identity_id:
            continue
        wid = variant.get("work_id")
        if wid in live:
            used.add(wid)
        used.update(wid for wid, w in live.items() if w.get("variant_id") == variant.get("id"))
    for graph in st.all_rows("media_jobs"):
        if graph.get("work_id") not in live:
            continue
        reference = graph.get("identity_reference") or {}
        if (graph.get("identity_id") == identity_id or reference.get("identity_id") == identity_id):
            used.add(graph["work_id"])
    return [live[wid] for wid in live if wid in used]


def delete_object(table: str, object_id: str, confirmed: object) -> dict:
    if confirmed is not True:
        raise DeletionRejected("DELETE_CONFIRMATION_REQUIRED", "请明确确认删除操作", 422)
    with st.transaction() as conn:
        row = st.get(table, object_id)
        if not row:
            label = "作品" if table == "works" else "人物"
            raise DeletionRejected("WORK_NOT_FOUND" if table == "works" else "IDENTITY_NOT_FOUND", label + "不存在", 404)
        if row.get("deleted_at"):
            return {"id": object_id, "deleted_at": row["deleted_at"]}
        if table == "works":
            reasons = _work_blockers(object_id)
        else:
            users = _identity_users(object_id)
            reasons = ["人物仍被作品引用：" + "、".join(w.get("name") or w["id"] for w in users)] if users else []
            lock = st.get("resource_locks", "identity_image:" + object_id) or {}
            if lock.get("expires_at", 0) > time.time():
                reasons.append("人物主图正在生成，请等待原请求返回")
        if reasons:
            raise DeletionRejected("WORK_DELETE_BLOCKED" if table == "works" else "IDENTITY_DELETE_BLOCKED", "；".join(reasons))
        row.update(deleted_at=now_iso(), deleted_by="user_local")
        st.insert_row(conn, table, object_id, row)
        return {"id": object_id, "deleted_at": row["deleted_at"]}
