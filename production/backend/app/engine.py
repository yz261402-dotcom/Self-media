"""任务编排推进器（Mock Provider）。

约束：
- 逻辑并行不等于 GPU 并行；单 GPU 重型作业串行排队（此处以单飞行锁表达）。
- 用户选择（SelectionDecision）与质量审批（ReviewDecision）分离，等待选择时停住。
- 提交结果未知时先对账，不盲目重发；取消只停止新提交。
"""
from __future__ import annotations

import asyncio
import os
import weakref
from typing import Any, Dict, List, Optional

from . import storage as st
from .contracts import ApiError, PublishPackage, now_iso
from .mockdata import PACKAGE_FILES

# 编排器选择：agents = Agent 编排（默认）；simple = 无 Agent 的确定性推进
ORCHESTRATOR = os.environ.get("AVATAR_STUDIO_ORCHESTRATOR", "agents").strip().lower()

# 单 GPU 重型作业互斥。锁按**事件循环**持有：生产只有一条循环，等价于全局串行；
# 而 asyncio.Lock 一旦被某个循环 acquire 就绑死那个循环，跨循环再用会直接抛
# `RuntimeError: ... is bound to a different event loop`——编排会把重型阶段丢给
# 后台任务执行，测试里每个 TestClient 一条循环，共用一把锁就会炸在与被测逻辑无关的地方。
_gpu_locks: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()
_scheduled: Dict[str, asyncio.Task] = {}


def _gpu_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _gpu_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _gpu_locks[loop] = lock
    return lock


async def gpu_slot() -> None:
    """重型 GPU 作业串行位：逻辑并行不等于 GPU 并行。"""
    async with _gpu_lock():
        await asyncio.sleep(0)


def _save_task(task: dict) -> dict:
    task["stage_rows"] = st.stage_rows(task["stage"], task["state"])
    st.put("tasks", task["id"], task, {"work_id": task["work_id"]})
    return task


def emit(task_id: str, type_: str, message: str, stage: str | None = None, state: str | None = None) -> None:
    st.add_event(task_id, {"type": type_, "occurred_at": now_iso(), "stage": stage, "state": state, "message": message})


def get_task(task_id: str) -> dict:
    t = st.get("tasks", task_id)
    if not t:
        raise KeyError(task_id)
    t = project_talking_head_status(t)
    t["stage_rows"] = st.stage_rows(t["stage"], t["state"])
    if t.get("stage") == "talking_head" and t.get("mode") == "production":
        row = next(r for r in t["stage_rows"] if r["stage"] == "talking_head")
        if t.get("state") == "waiting_selection":
            row["detail"] = "口播待提交：尚无当前输入的真实生成作业"
        elif t.get("state") == "waiting_provider":
            row["detail"] = "正在等待真实生成作业回收原片"
        elif t.get("state") == "waiting_review":
            row["detail"] = "原片已生成，等待用户检查与确认后制作成片"
    return t


def talking_head_wait(task: dict, work: dict) -> Optional[dict]:
    """纯事实门禁：意图 job_id 不是视频；不提交、不轮询、不替用户确认。"""
    from . import postprocess_worker
    from .digital_human import gates
    from .video.errors import VideoBlocked

    def current_video(video: dict) -> bool:
        if video.get("generation_mode") == "image_prompt_native_audio":
            from .digital_human import wan_native
            return wan_native.is_native_video(video) and wan_native.matches_current(video, work)
        if (video.get("kind") != "original" or video.get("is_mock")
                or video.get("approval_state") in ("revoked", "rejected")
                or video.get("work_id") != work.get("id")
                or video.get("identity_id") != work.get("identity_id")
                or video.get("variant_id") != work.get("variant_id")
                or video.get("audio_asset_id") != work.get("final_audio_id")
                or video.get("route") != work.get("route")):
            return False
        if (work.get("route") == "M2"
                and video.get("motion_ref_id") != (work.get("motion_intent") or {}).get("motion_ref_id")):
            return False
        try:
            path = postprocess_worker._runtime_path(video.get("storage_uri") or "")
            return os.path.isfile(path) and postprocess_worker._sha256_file(path) == str(video.get("file_hash") or "").removeprefix("sha256:")
        except (OSError, ValueError, VideoBlocked):
            return False

    originals = [v for v in st.all_rows("videos", "work_id=?", [work["id"]]) if current_video(v)]
    chosen = next((v for v in originals if v["id"] == work.get("video_asset_id")
                   and v.get("confirmed") and v.get("approval_state") == "approved"), None)
    if chosen and gates.video_gate(chosen["id"])["ok"]:
        return None
    if originals:
        return {"state": "waiting_review", "code": "ORIGINAL_REVIEW_REQUIRED",
                "message": "原片已生成，请观看并确认当前口播原片，再制作字幕与成片"}

    active_states = {"queued", "submitted", "running", "waiting_provider", "unknown"}
    for run in st.all_rows("provider_runs", "work_id=?", [work["id"]]):
        if run.get("is_mock") or run.get("route") != work.get("route") or run.get("state") not in active_states:
            continue
        if run.get("capability") not in ("lipsync", "avatar_video", "avatar_video_cloud", "short_video_cloud"):
            continue
        snapshot = (run.get("provider_params") or {}).get("input_snapshot")
        if (run.get("provider_params") or {}).get("generation_mode") == "image_prompt_native_audio":
            from .digital_human import wan_inputs
            from .digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
            try:
                source = wan_inputs.resolve_source(work)
            except (TalkingHeadBlocked, TalkingHeadRejected):
                continue
            # 未知提交可以没有远端 ID；仍须显示待核对，不能变成“尚未提交”。
            if (run.get("task_id") != task.get("id") or run.get("provider") != "dashscope_wan"
                    or not snapshot or snapshot.get("work_id") != work.get("id")
                    or snapshot.get("source_asset_id") != source.get("asset_id")
                    or snapshot.get("source_asset_hash") != source.get("asset_hash")):
                continue
        elif snapshot:
            if any(snapshot.get(key) != work.get(field) for key, field in (
                    ("work_id", "id"), ("identity_id", "identity_id"),
                    ("variant_id", "variant_id"), ("audio_asset_id", "final_audio_id"))):
                continue
            if work.get("route") == "M2" and snapshot.get("motion_ref_id") != (work.get("motion_intent") or {}).get("motion_ref_id"):
                continue
        elif run.get("task_id") != task.get("id"):
            continue
        message = "真实作业结果未知，请查询原作业或对账，勿重复提交" if run.get("state") == "unknown" else "真实口播作业已提交，等待回收原片"
        return {"state": "waiting_provider", "code": "ORIGINAL_PENDING", "message": message}
    return {"state": "waiting_selection", "code": "VIDEO_SUBMISSION_REQUIRED",
            "message": "口播待提交：请在视频合成中检查条件并提交真实口播作业"}


def project_talking_head_status(task: dict) -> dict:
    """修正旧 MEDIA_NOT_READY 的显示，不覆写原任务、事件、审核或外部作业。"""
    legacy = (task.get("stage") == "postprocess" and task.get("state") == "blocked"
              and task.get("block_reason") == "MEDIA_NOT_READY")
    active = (task.get("stage") == "talking_head"
              and task.get("state") in ("queued", "running", "waiting_provider", "waiting_review", "waiting_selection"))
    if task.get("mode") != "production" or not (legacy or active):
        return task
    work = st.get("works", task.get("work_id")) or {}
    if not work or work.get("current_task_id") != task.get("id"):
        return task
    waiting = talking_head_wait(task, work)
    # 已确认原片仍等待用户显式点击“制作成片”；读状态不得启动后期。
    state = waiting["state"] if waiting else "waiting_review"
    return {**task, "stage": "talking_head", "state": state,
            "last_error": None, "block_reason": None,
            "pending_selection_ids": ["sel_video_submission"] if state == "waiting_selection" else []}


def set_state(task: dict, state: str, stage: str | None = None, event: tuple | None = None) -> dict:
    task["state"] = state
    if stage:
        task["stage"] = stage
    if event:
        emit(task["id"], event[0], event[1], task["stage"], state)
    return _save_task(task)


async def _advance(task_id: str) -> None:
    """按依赖推进；遇到用户选择门即返回，等待显式决定后由 API 重新调度。"""
    try:
        current = get_task(task_id)
        if current.get("state") in ("cancelled", "succeeded", "failed", "blocked"):
            return
        if current.get("stage") == "postprocess":
            await render_postprocess(task_id)
            return
        if ORCHESTRATOR == "agents":
            if current.get("stage") in ("avatar_variant", "tts", "talking_head", "video_review", "publish_package"):
                await _handoff_media(current)
                return
            from .agents import supervisor
            await supervisor.advance(task_id)
            return
        task = get_task(task_id)
        if task["state"] in ("cancelled", "failed", "blocked", "succeeded"):
            return

        if task["stage"] == "topic":
            set_state(task, "running", "script", ("stage.started", "开始资料检索与三角度写稿"))
            await asyncio.sleep(1.2)
            task = get_task(task_id)

        if task["stage"] == "script":
            set_state(task, "running", "content_review", ("stage.started", "逐稿执行事实、查重与口语检查"))
            await asyncio.sleep(1.5)
            task = get_task(task_id)

        if task["stage"] == "content_review":
            task["pending_selection_ids"] = ["sel_script"]
            set_state(task, "waiting_selection", None, ("selection.requested", "三稿已就绪，等待用户选稿"))
            return

        if task["stage"] == "avatar_variant":
            set_state(task, "running", "tts", ("stage.started", "声音、人物与剪辑准备并行推进"))
            await asyncio.sleep(1.2)
            task = get_task(task_id)

        if task["stage"] == "tts":
            task["pending_selection_ids"] = ["sel_audio"]
            set_state(task, "waiting_selection", None, ("selection.requested", "全文音频已合成，等待用户试听确认"))
            return

        if task["stage"] == "talking_head":
            # 真实实现：先原子预留预算并记录幂等键，再提交外部作业
            set_state(task, "waiting_provider", None, ("provider.submitted", f"{task['route']} 路线已提交，等待外部作业"))
            async with _gpu_lock:
                await asyncio.sleep(2.5)
            task = get_task(task_id)
            if task["state"] == "cancelled":
                return
            set_state(task, "running", "postprocess", ("provider.completed", "外部作业完成，进入后期"))
            await asyncio.sleep(1.2)
            task = get_task(task_id)

        if task["stage"] == "postprocess":
            set_state(task, "running", "video_review", ("stage.started", "成片技术检测与人工观感复核"))
            await asyncio.sleep(1.0)
            task = get_task(task_id)

        if task["stage"] == "video_review":
            set_state(task, "running", "publish_package", ("stage.started", "冻结交付版本并生成 manifest"))
            await asyncio.sleep(1.0)
            task = get_task(task_id)

        if task["stage"] == "publish_package":
            build_package(task)
            set_state(task, "succeeded", None, ("package.ready", "发布就绪包已生成，可预览与下载"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # 失败保留证据，不静默继续
        task = st.get("tasks", task_id)
        if task:
            task["state"] = "failed"
            task["last_error"] = ApiError(code="ENGINE_ERROR", message=f"任务执行异常（{type(exc).__name__}），请检查本地日志").model_dump()
            _save_task(task)
            emit(task_id, "task.failed", task["last_error"]["message"], task.get("stage"), "failed")


async def _handoff_media(task: dict) -> None:
    """Media side effects are explicit business tools; content agents stop at selection."""
    work = st.get("works", task["work_id"]) or {}
    if work.get("current_task_id") != task["id"]:
        return
    if (work.get("brief") or {}).get("content_route") == "short_video_storyboard":
        task["pending_selection_ids"] = []
        set_state(task, "waiting_review", "content_review",
                  ("storyboard.ready", "分镜已选定，请准备镜头任务图"))
        return
    stage = task["stage"]
    if stage in ("avatar_variant", "tts"):
        task["pending_selection_ids"] = ["sel_audio"]
        set_state(task, "waiting_selection", "tts",
                  ("selection.requested", "稿件已选定，请合成并确认全文音频"))
    elif stage == "talking_head":
        waiting = talking_head_wait(task, work)
        task["pending_selection_ids"] = (["sel_video_submission"]
                                         if waiting and waiting["state"] == "waiting_selection" else [])
        set_state(task, waiting["state"] if waiting else "waiting_review", "talking_head",
                  ("stage.waiting", waiting["message"] if waiting else "原片已确认，请制作成片"))
    elif stage == "video_review":
        task["pending_selection_ids"] = ["sel_final_video"]
        set_state(task, "waiting_selection", stage,
                  ("selection.requested", "请观看并确认成片"))
    else:
        task["pending_selection_ids"] = []
        set_state(task, "waiting_review", stage,
                  ("stage.waiting", "发布包由业务 Worker 处理，请在发布页确认"))


async def render_postprocess(task_id: str) -> None:
    """耗时处理放入工作线程，轮询与其他作品操作保持可用；结束等待成片确认。"""
    from . import postprocess_worker
    task = get_task(task_id)
    if task["state"] in ("cancelled", "succeeded"):
        return
    set_state(task, "running", "postprocess", ("stage.started", "正在生成字幕、规范化音量并渲染成片"))
    try:
        work = st.get("works", task["work_id"]) or {}
        result = await asyncio.to_thread(postprocess_worker.run_stage, task, work, "postprocess")
        task = get_task(task_id)
        current = st.get("works", task["work_id"]) or {}
        if task["state"] == "cancelled" or current.get("current_task_id") != task_id:
            return
        if result["state"] != "succeeded":
            task["last_error"] = {k: v for k, v in (result.get("error") or {}).items() if k in ApiError.model_fields}
            task["block_reason"] = (result.get("error") or {}).get("code")
            set_state(task, result["state"], "postprocess", ("gate.blocked", (result.get("error") or {}).get("message", "后期尚未完成")))
            return
        task["last_error"] = None
        task["block_reason"] = None
        task["pending_selection_ids"] = ["sel_final_video"]
        set_state(task, "waiting_selection", "video_review", ("selection.requested", "成片已生成，请观看确认字幕、画面和声音，再下载"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        task = get_task(task_id)
        if task["state"] == "cancelled":
            return
        task["last_error"] = ApiError(code="POSTPROCESS_ERROR", message=f"后期处理失败（{type(exc).__name__}），可检查条件后重新处理").model_dump()
        set_state(task, "failed", "postprocess", ("task.failed", task["last_error"]["message"]))


def schedule(task_id: str) -> None:
    """调度推进；同一任务不重复并发执行。"""
    if task_id in _scheduled and not _scheduled[task_id].done():
        return
    _scheduled[task_id] = asyncio.create_task(_advance(task_id))


def build_package(task: dict) -> PublishPackage:
    work = st.get("works", task["work_id"]) or {}
    batch = st.get("script_batches", task["work_id"]) or {}
    chosen = None
    sel = st.all_rows("selections", "work_id=?", [task["work_id"]])
    decided = {s.get("kind"): s.get("selected_ref") for s in sel if s.get("state") == "selected"}
    for c in batch.get("candidates", []):
        if c.get("id") == decided.get("script"):
            chosen = c
            break
    pkg = PublishPackage(
        id=st.new_id("pkg"),
        work_id=task["work_id"],
        task_id=task["id"],
        mode=task.get("mode", "mock"),
        state="ready",
        title=(chosen or {}).get("title", work.get("name", "未命名作品")),
        description=(chosen or {}).get("description", ""),
        tags=(chosen or {}).get("tags", []),
        ai_label="本片人物与声音由 AI 生成（AI 标识规范版本待投产前核对）",
        files=[{"path": p, "bytes": 0, "sha256": "mock"} for p in PACKAGE_FILES],
        video_url=None,
        cover_url=None,
        download_url=f"/api/v1/publish-packages/{st.new_id('pkg')}/download",
        manifest_digest="sha256:mock-package-digest",
    )
    st.put("packages", pkg.id, pkg.model_dump(), {"work_id": pkg.work_id})
    return pkg


def resumable_tasks() -> List[str]:
    """可恢复任务清单：等待用户选择的任务不在其中，重启后不得被自动推进或代选。"""
    out = []
    for t in st.all_rows("tasks"):
        if t["state"] not in ("queued", "running", "waiting_provider"):
            continue
        if t.get("pending_selection_ids"):
            continue
        out.append(t["id"])
    return out


def recover_pending() -> int:
    """重启后识别未完成任务并重新调度（NFR-04）。

    没有运行中的事件循环时（离线工具、测试、启动前检查）只返回 0 并保留清单，
    不抛异常、也不声称已恢复——检查点不能代替账本。
    """
    ids = resumable_tasks()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return 0
    for tid in ids:
        schedule(tid)
    return len(ids)
