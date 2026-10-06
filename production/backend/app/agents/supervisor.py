"""总控编排器（supervisor）。

职责：把内容生成拆成显式工作项（AgentWorkItem），委派给写作与审核角色，
遇到用户选择门即停住等待显式决定，重启后从业务库恢复未完成工作项继续推进。

边界：
- 质量通过不等于用户采用：选择门只认业务库里用户写入的 SelectionDecision。
- 依赖版本不符（稿件重生成、音频失效）时下游工作项标记 superseded 并重建。
- 重型 GPU 作业串行：复用 engine 的单飞行锁，逻辑并行不等于 GPU 并行。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from . import contracts as ac, results as res
from .models import build_model, new_run_id
from .framework import execution_stop_reason, require_active_execution
from .roles import ROLES, require_skill_refs
from .tools import invoke

MAX_ATTEMPTS = int(os.environ.get("AVATAR_STUDIO_MAX_ATTEMPTS", "2"))
PARALLEL_LIMIT = int(os.environ.get("AVATAR_STUDIO_AGENT_PARALLEL", "3"))

# 阶段 → (角色, 目标, 依赖的阶段内序号, 需要的 Skill)
def _s(tool: str, **args) -> Dict[str, Any]:
    return {"tool": tool, "args": args}


def _needs_visual(work: dict) -> bool:
    from ..content import visual
    return visual.reference_needs_visual(work) is not None


STAGE_PLAN: Dict[str, List[Dict[str, Any]]] = {
    "script": [
        # 视觉准备只由总控执行，且仅在分镜路线的参考既无转写也无 ready 画面分析时才存在；写作 Agent 不碰它
        {"key": "visual", "role": "supervisor", "objective": "准备参考画面分析（费用须已由用户确认，视觉结果就绪后才写分镜）",
         "skills": [], "when": _needs_visual,
         "inputs": ["work_id", "reference_snapshot_id", "media_asset_id", "visual_confirmation_id"],
         "script": [_s("analyze_reference_visual", work_id="{work_id}", reference_snapshot_id="{reference_snapshot_id}",
                       media_asset_id="{media_asset_id}", confirmation_id="{visual_confirmation_id}")]},
        {"key": "script", "role": "content", "objective": "整理参考并按所选内容路线生成三个不同角度的候选", "skills": ["S01", "S02"],
         "inputs": ["work_id", "brief"],
         "script": [_s("register_script_candidates", work_id="{work_id}", topic="{topic}")]},
    ],
    "content_review": [
        {"key": "review", "role": "quality", "objective": "逐稿执行事实/查重/口语检查并登记结论", "skills": ["S03"],
         "inputs": ["work_id", "batch_digest"],
         "script": [_s("search_content_history", query="{topic}"),
                    _s("submit_quality_report", work_id="{work_id}", subject_id="__all__")]},
    ],
}

# 选择门：阶段结束后必须由用户显式决定
SELECTION_GATES = {
    "content_review": "sel_script",
}


def _wi_id(task_id: str, stage: str, key: str) -> str:
    return f"wi_{task_id}_{stage}_{key}"


def _dep_id(task_id: str, stage: str, dep: str) -> str:
    """依赖可写 `key`（同阶段）或 `stage.key`（跨阶段）。"""
    if "." in dep:
        s, k = dep.split(".", 1)
        return _wi_id(task_id, s, k)
    return _wi_id(task_id, stage, dep)


def _ctx(task: dict, work: dict) -> Dict[str, Any]:
    from ..content import visual
    topic = (work.get("brief") or {}).get("topic", "")
    return {
        **visual.preparation_inputs(work),
        "work_id": task["work_id"],
        "task_id": task["id"],
        "topic": topic,
        "brief": work.get("brief") or {},
        "batch_digest": ac.digest(st.get("script_batches", task["work_id"]) or {}),
        "topic_digest": ac.digest(topic),
        "identity_id": work.get("identity_id") or "",
        "voice_asset_id": work.get("voice_asset_id") or "",
        "script_id": work.get("script_version_id") or "",
        "route": task.get("route") or work.get("route") or "M2",
        "plan_id": _latest_plan_id(task["work_id"]),
    }


def _spec_of(stage: str, key: str) -> Dict[str, Any]:
    for s in STAGE_PLAN.get(stage, []):
        if s["key"] == key:
            return s
    return {}


def _key_of(item_id: str, task_id: str, stage: str) -> str:
    """工作项 ID 形如 wi_{task_id}_{stage}_{key}，key 本身可含下划线。"""
    prefix = f"wi_{task_id}_{stage}_"
    return item_id[len(prefix):] if item_id.startswith(prefix) else item_id.rsplit("_", 1)[-1]


def _latest_plan_id(work_id: str) -> str:
    rows = st.all_rows("performance_plans", "work_id=?", [work_id])
    return rows[-1]["id"] if rows else ""


def build_work_items(task: dict, work: dict) -> List[ac.AgentWorkItem]:
    """生成整期工作图并落库（幂等：已存在且未过期则复用）。"""
    ctx = _ctx(task, work)
    items: List[ac.AgentWorkItem] = []
    for stage, specs in STAGE_PLAN.items():
        visual_on = stage == "script" and _needs_visual(work)
        for spec in specs:
            when = spec.get("when")
            if when is not None and not when(work):
                continue
            wid = _wi_id(task["id"], stage, spec["key"])
            depends = list(spec.get("depends", [])) + (["visual"] if visual_on and spec["key"] == "script" else [])
            # 摘要只绑定该工作项真正依赖的输入：上游项不因下游版本变化而失效
            bound = spec.get("inputs", ["work_id", "topic"])
            input_digest = ac.digest({"stage": stage, "key": spec["key"],
                                      **{k: ctx[k] for k in bound}})
            existing = st.get("agent_work_items", wid)
            dep_ids = [_dep_id(task["id"], stage, d) for d in depends]
            if existing and existing.get("input_digest") == input_digest:
                if existing.get("depends_on_ids") != dep_ids:  # 视觉前置出现/消失时同步依赖，避免永久等待
                    existing["depends_on_ids"] = dep_ids
                    st.put("agent_work_items", wid, existing, {"task_id": task["id"]})
                items.append(ac.AgentWorkItem(**existing))
                continue
            item = ac.AgentWorkItem(
                id=wid,
                task_id=task["id"],
                stage=stage,
                role=spec["role"],
                objective=spec["objective"],
                depends_on_ids=dep_ids,
                input_refs=[{"work_id": task["work_id"], "script_version_id": ctx["script_id"] or None}],
                input_digest=input_digest,
                skill_refs=require_skill_refs(spec["skills"]) if spec["skills"] else [],
                namespace=f"{task['mode']}/{task['id']}/{wid}",
                mode=task.get("mode", "mock"),
            )
            st.put("agent_work_items", item.id, item.model_dump(), {"task_id": task["id"]})
            items.append(item)
    return items


def _save(item: ac.AgentWorkItem) -> ac.AgentWorkItem:
    item.updated_at = now_iso()
    st.put("agent_work_items", item.id, item.model_dump(), {"task_id": item.task_id})
    return item


def reset_stage(task_id: str, stage: str) -> int:
    """把一个已结束的阶段重新排队，供"用户显式要求重跑"使用（如「重新生成三稿」）。

    只重置本阶段工作项的状态：旧尝试的 `AgentResult` 仍留在结果表里作为证据，
    `attempt` 继续累加——"重跑过几次、每次结果如何"都可查。不清空任何产物台账，
    也不动其它阶段（重跑写稿不该把已完成的审核/配音一起推倒）。
    """
    reset = 0
    for row in st.all_rows("agent_work_items", "task_id=?", [task_id]):
        if row.get("stage") != stage:
            continue
        if row.get("state") in ("succeeded", "failed", "blocked"):
            row["state"] = "queued"
            _save(ac.AgentWorkItem(**row))
            reset += 1
    return reset


def _emit(task_id: str, type_: str, message: str, stage: Optional[str] = None, state: Optional[str] = None) -> None:
    from .. import engine

    engine.emit(task_id, type_, message, stage, state)


async def _run_work_item(task: dict, work: dict, item: ac.AgentWorkItem,
                         script: Optional[List[Dict[str, Any]]] = None) -> ac.AgentResult:
    """执行单个工作项：模型决策 → 受控工具 → 结果校验 → 落库。"""
    ctx = _ctx(task, work)
    ctx["work_item_id"] = item.id
    ctx["role"] = item.role
    # 幂等键按"这一次尝试"计算（见 tools.invoke）：把 attempt 带进上下文，
    # 否则用户要求重跑时参数没变 → 键没变 → 被当成幂等重放，按钮只会返回上次的结果。
    ctx["attempt"] = item.attempt
    # 还要带上本工作项的输入摘要：`attempt` 只在**同一份输入**下才连续。
    # 输入变了（例如给作品补了参考资料）时 `build_work_items` 会按新摘要重建工作项，
    # attempt 从 0 起算 → 键与上一轮撞上 → 被当成重放，**新的输入根本没有被执行**。
    # 曾因此让"补资料后重新出稿"返回旧批次、工作项反而判失败。
    ctx["input_digest"] = item.input_digest
    model_run_id = new_run_id(item.role)

    tool_calls: List[Dict[str, Any]] = []
    output_refs: List[Dict[str, Any]] = []
    error: Optional[Dict[str, Any]] = None
    state = "succeeded"
    visual_gate: Dict[str, Any] = {}

    def consume(call: Dict[str, Any], runtime: str) -> None:
        tool_calls.append({"tool": call["tool"], "outcome": call["outcome"],
                           "replayed": call.get("replayed", False), "runtime": runtime,
                           **({"code": call["code"]} if call.get("code") else {})})
        r = call.get("result") or {}
        if call.get("tool") == "analyze_reference_visual" and call.get("outcome") == "ok":
            visual_gate.update(r)
        for k in ("batch_id", "plan_id", "quality_decision_id", "package_id", "id", "audio_id", "video_id",
                  "memory_candidate_id", "inspection_id", "job_id"):
            if r.get(k):
                output_refs.append({"kind": k, "id": r[k]})
        for k, v in r.items():
            if k.endswith("_ids") and isinstance(v, list):
                output_refs.extend({"kind": k[:-4], "id": x} for x in v)

    try:
        require_active_execution(ctx)
        runtime = os.environ.get("AVATAR_STUDIO_AGENT_RUNTIME", "deepagents").strip().lower()
        if runtime not in ("deepagents", "legacy"):
            from .tools import ToolDenied
            raise ToolDenied("AGENT_RUNTIME_UNSUPPORTED", "未知执行框架，请配置 deepagents 或 legacy")
        model = build_model(item.role, ctx, script=script)
        model_run_id = model.run_id
        ctx["model_run_id"] = model_run_id
        if runtime == "deepagents":
            from .framework import execute_work_item
            execution = await asyncio.to_thread(execute_work_item, item.role, ctx, script, model=model)
            for call in execution["calls"]:
                consume(call, runtime)
            error = execution.get("error")
        else:
            if not callable(getattr(model, "decide", None)):
                from .tools import ToolDenied
                raise ToolDenied("AGENT_RUNTIME_UNSUPPORTED", "真实聊天模型须使用 deepagents 执行框架")
            while True:
                decision = await asyncio.to_thread(model.decide)
                if decision is None:
                    break
                try:
                    def invoke_active():
                        require_active_execution(ctx)
                        return invoke(item.role, decision["tool"], decision["args"], ctx)
                    out = await asyncio.to_thread(invoke_active)
                except Exception as exc:
                    code = getattr(exc, "code", "TOOL_ERROR")
                    error = {"code": code, "message": getattr(exc, "message", "工具执行失败")}
                    consume({"tool": decision["tool"], "outcome": "denied", "code": code}, runtime)
                    break
                consume({"tool": decision["tool"], "outcome": "ok", **out}, runtime)
        if visual_gate and not error:
            error = _visual_gate_error(visual_gate)
        if error:
            state = "blocked" if error["code"] in BLOCKING_CODES else "failed"
    except Exception as exc:  # 编排异常不静默
        state = "failed"
        error = {"code": getattr(exc, "code", "AGENT_ERROR"),
                 "message": getattr(exc, "message", f"执行框架失败（{type(exc).__name__}），未回退其他执行器")}

    # 最后一个工具运行中也可能取消。保留已受理作业的引用和审计，不重试、不抹去。
    stop = execution_stop_reason(ctx)
    if stop:
        state, error = "cancelled", stop

    result = res.make_result(item, state, output_refs, tool_calls,
                             model_run_id=model_run_id, error=error)
    try:
        res.validate_result(result, item)
    except res.ResultRejected as rej:
        result.state = "failed"
        result.error = {"code": rej.code, "message": rej.message}
        result.result_digest = ac.digest({"work_item": item.id, "state": "failed"})
    st.put("agent_results", result.id, result.model_dump(), {"work_item_id": item.id})
    item.state = result.state
    item.last_error = result.error
    _save(item)
    return result


BLOCKING_CODES = ("DEPENDENCY_NOT_MET", "SELECTION_REQUIRED", "BUDGET_NOT_AUTHORIZED", "ROUTE_NOT_SELECTED",
                  "VISUAL_USER_ACTION_REQUIRED", "VISUAL_PENDING")


def _visual_gate_error(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把视觉工具结果映射成阶段门禁：等用户 / 等同一个任务 / 等用户选择新快照。视觉未就绪不启动写作。"""
    if result.get("requires_user_action"):
        return {"code": "VISUAL_USER_ACTION_REQUIRED",
                "message": (result.get("message") or "画面分析需要用户先上传视频并确认费用，或核对未知结果")}
    if result.get("visual_state") == "pending":
        return {"code": "VISUAL_PENDING", "message": f"画面分析任务 {result.get('job_id')} 进行中，等待同一个任务完成"}
    if result.get("visual_state") == "ready":
        return {"code": "SELECTION_REQUIRED",
                "message": "画面分析已完成：请把新快照设为主参考后再生成分镜（不会替用户选择）"}
    return {"code": "VISUAL_USER_ACTION_REQUIRED", "message": "画面分析未成功，请检查后重试"}


def _deps_done(item: ac.AgentWorkItem, states: Dict[str, str]) -> bool:
    return all(states.get(d) == "succeeded" for d in item.depends_on_ids)


async def run_stage(task: dict, work: dict, stage: str) -> Dict[str, Any]:
    """执行一个阶段内的全部工作项。

    同阶段内无依赖关系的工作项并行执行；有依赖的分批推进（先跑完依赖，再跑后继）。
    """
    from .. import postprocess_worker

    stopped = execution_stop_reason({"task_id": task["id"], "work_id": task["work_id"]})
    if stopped:
        return {"executed": 0, "states": {}, "failures": [{"state": "cancelled", "error": stopped}]}

    if stage in postprocess_worker.STAGES:
        record = await asyncio.to_thread(postprocess_worker.run_stage, task, work, stage)
        return {"executed": 1, "states": {record["id"]: record["state"]},
                "failures": [{"work_item_id": record["id"], "state": record["state"],
                              "error": record["error"]}] if record["state"] != "succeeded" else []}
    if not STAGE_PLAN.get(stage):
        return {"executed": 0, "states": {}, "failures": [
            {"state": "blocked", "error": {"code": "STAGE_NOT_REGISTERED",
                                             "message": f"阶段 {stage} 没有已注册执行器"}}]}

    sem = asyncio.Semaphore(PARALLEL_LIMIT)
    executed: List[ac.AgentResult] = []
    states: Dict[str, str] = {}

    async def one(item: ac.AgentWorkItem):
        async with sem:
            item.state = "running"
            item.attempt += 1
            _save(item)
            _emit(task["id"], "work_item.started", f"[{item.role}] {item.objective}", stage, task["state"])
            key = _key_of(item.id, task["id"], stage)
            return await _run_work_item(task, work, item, script=_spec_of(stage, key).get("script"))

    while True:
        stopped = execution_stop_reason({"task_id": task["id"], "work_id": task["work_id"]})
        if stopped:
            return {"executed": len(executed), "states": {r.work_item_id: r.state for r in executed},
                    "failures": [{"state": "cancelled", "error": stopped}]}
        all_items = build_work_items(task, work)
        items = [i for i in all_items if i.stage == stage]
        states = {i.id: i.state for i in all_items}
        pending = [i for i in items if i.state in ("queued", "retry_wait") and _deps_done(i, states)]
        if not pending:
            break
        round_results = await asyncio.gather(*[one(i) for i in pending])
        executed += round_results
        if all(r.state != "succeeded" for r in round_results):
            break  # 本轮全失败，避免死循环

    # 恢复时必须检查持久工作图，不能只检查本轮新执行的结果。
    failures = [{"work_item_id": i.id, "state": i.state,
                 "error": i.last_error or {"code": "DEPENDENCY_NOT_MET",
                                           "message": "阶段仍有未完成的工作项"}}
                for i in items if i.state != "succeeded"]
    return {
        "executed": len(executed),
        "states": {i.id: i.state for i in items},
        "failures": failures,
    }


async def advance(task_id: str) -> None:
    """推进任务：按阶段顺序执行，遇到选择门或阻断即停。"""
    from .. import engine

    task = engine.get_task(task_id)
    work = st.get("works", task["work_id"]) or {}
    if execution_stop_reason({"task_id": task_id, "work_id": task["work_id"]}):
        return

    # 老工作图保留为证据，不把已移除的角色/研究节点静默跳过并继承成功。
    valid_ids = {_wi_id(task_id, stage, spec["key"])
                 for stage, specs in STAGE_PLAN.items() for spec in specs}
    old_items = st.all_rows("agent_work_items", "task_id=?", [task_id])
    if any(i.get("role") not in ROLES or i["id"] not in valid_ids for i in old_items):
        task["block_reason"] = "LEGACY_AGENT_GRAPH"
        engine.set_state(task, "blocked", task["stage"],
                         ("gate.blocked", "旧 Agent 工作图不兼容 v2.4，请新建执行；历史结果保留"))
        return

    from .. import postprocess_worker

    if task["stage"] in postprocess_worker.STAGES:
        # Compatibility entry for durable tasks; no Agent/model is involved.
        stage = task["stage"]
        engine.set_state(task, "running", stage, ("stage.started", f"进入 Worker 阶段 {stage}"))
        summary = await run_stage(task, work, stage)
        task = engine.get_task(task_id)
        if execution_stop_reason({"task_id": task_id, "work_id": task["work_id"]}):
            return
        if summary.get("failures"):
            failure = summary["failures"][0]
            error = failure.get("error") or {}
            state = "blocked" if failure.get("state") == "blocked" else "failed"
            task["block_reason"] = error.get("code") if state == "blocked" else None
            task["last_error"] = error if state == "failed" else None
            engine.set_state(task, state, stage, ("gate.blocked", error.get("message") or "Worker 执行未完成"))
            return
        if stage == "publish_package":
            engine.set_state(task, "succeeded", stage, ("package.ready", "发布就绪包已生成"))
        return

    order = ["script", "content_review"]
    if task["stage"] not in order and task["stage"] != "topic":
        return  # 选稿后的媒体制作与发布由业务 API/Worker 推进。
    start = order.index(task["stage"]) if task["stage"] in order else 0

    for stage in order[start:]:
        task = engine.get_task(task_id)
        work = st.get("works", task["work_id"]) or {}
        if execution_stop_reason({"task_id": task_id, "work_id": task["work_id"]}):
            return

        engine.set_state(task, "running", stage, ("stage.started", f"进入阶段 {stage}"))
        summary = await run_stage(task, work, stage)
        # await 期间用户可以取消/切换任务；必须先刷新，禁止旧快照覆写新状态。
        task = engine.get_task(task_id)
        work = st.get("works", task["work_id"]) or {}
        if execution_stop_reason({"task_id": task_id, "work_id": task["work_id"]}):
            return
        if summary.get("failures"):
            f = summary["failures"][0]
            err = f.get("error") or {}
            if f.get("state") == "blocked" or err.get("code") in BLOCKING_CODES:
                engine.set_state(task, "blocked", stage,
                                 ("gate.blocked", f"{err.get('code')}: {err.get('message')}"))
                task["block_reason"] = err.get("code")
                engine._save_task(task)
                return
            engine.set_state(task, "failed", stage, ("task.failed", f"{err.get('code')}: {err.get('message')}"))
            task["last_error"] = err
            engine._save_task(task)
            return

        gate = SELECTION_GATES.get(stage)
        if gate:
            task = engine.get_task(task_id)
            ids = list(task.get("pending_selection_ids") or [])
            if gate not in ids:
                ids.append(gate)
            task["pending_selection_ids"] = ids
            engine.set_state(task, "waiting_selection", stage,
                             ("selection.requested", f"等待用户决定：{gate}"))
            engine._save_task(task)
            return

def plan_snapshot(task_id: str) -> Dict[str, Any]:
    """供检查与界面展示：当前工作图状态。"""
    rows = st.all_rows("agent_work_items", "task_id=?", [task_id])
    return {
        "count": len(rows),
        "by_state": {s: sum(1 for r in rows if r.get("state") == s)
                     for s in ("queued", "running", "succeeded", "failed", "blocked")},
        "items": [{"id": r["id"], "role": r["role"], "stage": r["stage"], "state": r["state"],
                   "objective": r["objective"]} for r in rows],
    }
