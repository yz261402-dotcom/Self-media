"""创作向导的确定性后期接口；生成调用继续使用已有媒体 Provider 端点。"""
from fastapi import APIRouter, HTTPException

from . import delivery, engine, storage as st
from .contracts import Task
from .digital_human import gates

router = APIRouter(prefix="/api/v1")


def _error(code, message, status=409):
    raise HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


def _work(work_id):
    work = st.get("works", work_id)
    if not work:
        _error("WORK_NOT_FOUND", "作品不存在", 404)
    if work.get("deleted_at"):
        _error("WORK_DELETED", "作品已删除，不能继续制作", 404)
    return work


@router.get("/works/{work_id}/postprocess")
def postprocess_status(work_id: str):
    _work(work_id)
    return delivery.status(work_id)


@router.post("/works/{work_id}/postprocess", response_model=Task)
async def start_postprocess(work_id: str):
    work = _work(work_id)
    video = st.get("videos", work.get("video_asset_id") or "")
    if not video or not video.get("confirmed") or video.get("work_id") != work_id:
        _error("VIDEO_CONFIRMATION_REQUIRED", "请先观看并确认本作品的口播原片")
    gate = gates.video_gate(video["id"])
    if not gate["ok"]:
        _error("VIDEO_REVIEW_REQUIRED", "；".join(gate["reasons"]))
    current = st.get("tasks", work.get("current_task_id") or "")
    if current:
        current = engine.get_task(current["id"])
    if current and current.get("state") in ("queued", "running", "waiting_provider"):
        if current.get("stage") == "postprocess":
            return current
        _error("TASK_BUSY", "当前作品仍有任务运行，请等待完成后处理成片")
    # 一次显式重做建立新执行；失败、旧素材、旧确认完整保留。
    task = Task(id=st.new_id("task"), work_id=work_id, mode="production",
                stage="postprocess", state="queued", route=work.get("route", "M2"),
                route_confirmed=bool(work.get("route_selection_id"))).model_dump()
    with st.transaction() as conn:
        work = _work(work_id)
        st.insert_row(conn, "tasks", task["id"], task, {"work_id": work_id})
        work["current_task_id"] = task["id"]
        st.insert_row(conn, "works", work_id, work)
    engine.schedule(task["id"])
    return task


@router.post("/works/{work_id}/postprocess-confirmation")
def confirm_postprocess(work_id: str, payload: dict):
    work = _work(work_id)
    if not payload.get("execution_id"):
        _error("PREVIEW_VERSION_REQUIRED", "请先加载成片预览，再确认当前版本")
    try:
        delivery.confirm_review(work_id, actor_id=str(payload.get("actor_id") or ""),
                                review_ack=payload.get("review_ack") is True,
                                execution_id=payload.get("execution_id"))
        task = st.get("tasks", work.get("current_task_id") or "")
        if not task or task.get("state") == "cancelled":
            _error("TASK_NOT_READY", "制作任务已取消或不存在")
        result = delivery.build_delivery(task, work)
    except delivery.DeliveryBlocked as exc:
        _error(exc.code, exc.message)
    # 文件打包在事务外执行；提交完成状态时重新核对，避免覆盖并发取消或新任务。
    with st.transaction() as conn:
        current_work = _work(work_id)
        current_task = st.get("tasks", task["id"]) or {}
        if (current_work.get("current_task_id") != task["id"]
                or current_task.get("state") in (None, "cancelled", "failed", "blocked")):
            _error("TASK_NOT_READY", "制作任务已取消或已被新任务替代")
        try:
            delivery.download_path(result["package_id"])
        except delivery.DeliveryBlocked as exc:
            _error(exc.code, exc.message)
        current_task.update(pending_selection_ids=[], last_error=None, block_reason=None,
                            state="succeeded", stage="publish_package",
                            stage_rows=st.stage_rows("publish_package", "succeeded"))
        st.insert_row(conn, "tasks", task["id"], current_task, {"work_id": work_id})
    engine.emit(task["id"], "package.ready", "成片已确认，下载包已生成", "publish_package", "succeeded")
    return delivery.public_package(st.get("packages", result["package_id"]))
