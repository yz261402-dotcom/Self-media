"""受控工具层（M01–M04；代码后期不暴露为 Agent 工具）。

红线（10 文档 §6.3）：
- 工具不接受任意文件系统路径或任意 shell；路径类参数一律拒绝 `..`、绝对路径与盘符。
- 权限按角色授予，角色不能自填 allowlist；越权、未知工具名一律拒绝并记录审计。
- 不伪造证据：检索/检测未接入真实能力时返回空结果与 not_run，不返回编造的指标或来源。
- 默认额度 0：任何 billable 提交在授权前被 BUDGET_NOT_AUTHORIZED 阻断。
- 幂等：同一工作项 + 工具 + 参数摘要的重放返回原结果，不重复产生副作用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional

from .. import storage as st
from ..contracts import ApiError, QualityCheck, now_iso
from . import contracts as ac
from .roles import ROLES, role_tools

MODE = os.environ.get("AVATAR_STUDIO_MODE", "mock")

_ABS_OR_TRAVERSAL = re.compile(r"(\.\.[/\\])|^([a-zA-Z]:)?[/\\\\]|^~")


class ToolDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _check_path(value: str) -> None:
    if _ABS_OR_TRAVERSAL.search(value or ""):
        raise ToolDenied("PATH_NOT_ALLOWED", f"路径参数不允许绝对地址或上级目录：{value!r}")


def _require(args: Dict[str, Any], fields: List[str]) -> None:
    for f in fields:
        v = args.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ToolDenied("VALIDATION_FAILED", f"缺少参数 {f}")
        if isinstance(v, str):
            _check_path(v)


def _media_job(work_id: str, kind: str, provider: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """登记媒体作业。Mock/未授权模式下不向外提交，state 记为 not_submitted。"""
    job = {
        "id": st.new_id("job"),
        "work_id": work_id,
        "kind": kind,
        "provider": provider,
        "model": model,
        "state": "not_submitted",
        "simulated": True,
        "remote_job_id": None,
        "payload": payload,
        "created_at": now_iso(),
        "note": "MOCK：未提交真实 Provider，不产生费用与外部副作用",
    }
    st.put("media_jobs", job["id"], job, {"work_id": work_id})
    return job


def _billable_guard(kind: str) -> None:
    """默认额度 0：未授权的可计费提交一律阻断。"""
    limit = int(os.environ.get("AVATAR_STUDIO_BUDGET_FEN", "0"))
    if limit <= 0:
        raise ToolDenied(
            "BUDGET_NOT_AUTHORIZED",
            f"{kind} 属于可计费作业，当前每日/单次额度为 0，未取得授权前不提交",
        )


def _require_media_confirmation(args: Dict[str, Any], ctx: Dict[str, Any], route: str) -> None:
    from .media import require_confirmation
    require_confirmation(args, ctx, route)


def t_search_douyin(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Route Redfox searches through the existing billed content-job ledger."""
    _require(args, ["work_id", "query"])
    if args["work_id"] != ctx.get("work_id"):
        raise ToolDenied("WORK_SCOPE_DENIED", "检索只能绑定当前作品")
    request_id = args.get("request_id")
    if not request_id:
        raise ToolDenied("VALIDATION_FAILED", "检索需要幂等 request_id")
    from ..content import references, tasks
    from ..content.errors import ContentError

    try:
        payload = references.normalize_search_payload(args)
        job = tasks.submit_job(args["work_id"], "search", request_id, payload)
    except ContentError as exc:
        raise ToolDenied(exc.code, exc.message) from None
    tasks.get_scheduler().start()
    return {"job_id": job["id"], "state": job["state"], "replayed": bool(job.get("replayed"))}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def t_search_content_history(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["query"])
    q = args["query"].strip()
    hits = []
    for w in st.all_rows("works"):
        topic = (w.get("brief") or {}).get("topic", "")
        if q and q in topic:
            hits.append({"work_id": w["id"], "topic": topic, "name": w.get("name", "")})
    for b in st.all_rows("script_batches"):
        for c in b.get("candidates", []):
            if q and (q in c.get("title", "") or q in c.get("text", "")):
                hits.append({"work_id": b.get("work_id"), "candidate_id": c.get("id"), "title": c.get("title", "")})
    return {"hits": hits, "count": len(hits), "note": "业务库全文子串匹配，非向量检索"}


def t_get_presenter_profile(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["identity_id"])
    ident = st.get("identities", args["identity_id"])
    if not ident:
        raise ToolDenied("ASSET_NOT_FOUND", "人物不存在")
    if ident.get("deleted_at"):
        raise ToolDenied("IDENTITY_DELETED", "人物已删除，不能继续使用")
    # 固定人设只读：不返回可写字段
    return {
        "id": ident["id"], "name": ident.get("name"), "persona": ident.get("persona"),
        "hero_asset_id": ident.get("hero_asset_id"), "identity_digest": ident.get("identity_digest"),
        "approval_state": ident.get("approval_state"), "readonly": True,
    }


def t_list_approved_assets(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    kind = args.get("kind") or "voice"
    table = {"voice": "voices", "identity": "identities"}.get(kind)
    if not table:
        raise ToolDenied("VALIDATION_FAILED", f"未知资产类型 {kind}")
    rows = [r for r in st.all_rows(table) if not r.get("deleted_at") and not r.get("discarded_at")]
    return {"assets": [r for r in rows if r.get("approval_state") == "approved"], "count": len(rows)}


def t_register_script_candidates(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """写候选稿：唯一实现在 content.service（与 API 同一入口），本工具只提交/取回持久父任务并等待。

    凭据不可用时：mock 模式回落到如实标注 `source_kind=mock` 的模拟稿；非 mock 直接阻断，绝不静默造假稿。
    同一输入的重复调用（Agent attempt 变化、页面重复点击、超时重试）取回原任务，不会再收一次费。
    """
    _require(args, ["work_id", "topic"])
    from ..content import service as content_service
    from ..content import tasks as content_tasks
    from ..content.errors import ContentBlocked, ContentError
    from ..content.providers import text as text_providers

    work_id = args["work_id"]
    brief = (st.get("works", work_id) or {}).get("brief") or {}
    materials = brief.get("materials") or args.get("materials") or []
    execution_context = dict(ctx, work_id=ctx.get("work_id") or work_id)
    from .framework import require_active_execution

    try:
        # Reserve the content task under the same lock that protects human takeover.
        with st.transaction() as connection:
            require_active_execution(execution_context)
            for role in ("writer", "repair", "reviewer"):
                prof = text_providers.profile(role)
                if not os.environ.get(prof["key_env"]):
                    raise ContentBlocked(f"未配置 {prof['key_env']}，不能生成稿件；不返回伪装成模型输出的内容",
                                         "PROVIDER_NOT_CONFIGURED", retryable=False)
            prompt = content_service.ensure_prompt(work_id, _connection=connection)
            payload = {"prompt_id": prompt["id"], "topic": args["topic"]}
            request_id = content_service.legacy_request_id(work_id, "generation", payload)
            job = content_tasks.submit_job(work_id, "generation", request_id, payload, _connection=connection)
        if job["state"] not in content_tasks.TERMINAL_STATES:
            job = content_tasks.run_inline(job["id"], wait_seconds=content_service.LEGACY_WAIT_SECONDS)
    except ContentBlocked as exc:
        if MODE != "mock":
            raise ToolDenied(exc.code, exc.message)
        from ..content import selection as content_selection
        from ..mockdata import make_candidates

        cands = [c.model_dump() for c in make_candidates(args["topic"])]
        batch = {"id": st.new_id("batch"), "work_id": work_id, "candidates": cands,
                 "shortfall_note": "模拟三稿（未取得写作/审稿模型凭据），不冒充模型产出", "source_kind": "mock",
                 "repair_note": None, "repair_history": [], "materials_count": len(materials)}
        with st.transaction() as c:
            require_active_execution(execution_context)
            content_selection.on_batch_replaced(c, work_id)
            st.insert_row(c, "script_batches", work_id, batch)
        return {"batch_id": batch["id"], "candidates": [c["id"] for c in cands], "angles": [c["angle"] for c in cands],
                "source_kind": "mock", "shortfall_note": batch["shortfall_note"], "materials_count": len(materials),
                "repair_note": None, "note": "模拟三稿，已标注 mock，不冒充模型产出"}
    except ContentError as exc:
        raise ToolDenied(exc.code, exc.message)

    if job["state"] in ("succeeded", "partial"):
        batch = st.get("script_batches", work_id) or {}
        return {"batch_id": batch.get("id"), "candidates": [c["id"] for c in batch.get("candidates", [])],
                "angles": [c["angle"] for c in batch.get("candidates", [])], "source_kind": "llm",
                "shortfall_note": batch.get("shortfall_note"), "materials_count": len(materials),
                "repair_note": batch.get("repair_note"), "job_id": job["id"],
                "note": "真实模型生成，独立审核已随任务持久化"}
    err = job.get("error") or {}
    if job["state"] in content_tasks.ACTIVE_STATES:
        raise ToolDenied("CONTENT_JOB_PENDING", f"内容任务 {job['id']} 仍在进行，请用 /content/jobs/{job['id']} 查询；不会重复生成")
    raise ToolDenied(err.get("code") or "CONTENT_JOB_FAILED",
                     (err.get("message") or f"内容任务未成功（{job['state']}）") + f"（任务 {job['id']}）")


def t_analyze_reference_visual(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """提交或读取视觉分析。只有主控可调用；与页面按钮走同一个 content.visual 服务和同一份台账。

    不接受 paid_ack / 金额：付费授权只认页面创建的用户费用确认记录（confirmation_id）。
    没有有效授权时不发任何请求，返回 requires_user_action 交回主控工作项等待用户。
    """
    _require(args, ["work_id"])
    extra = sorted(set(args) - {"work_id", "reference_snapshot_id", "media_asset_id", "confirmation_id"})
    if extra:
        raise ToolDenied("VALIDATION_FAILED", f"参数不被接受：{'、'.join(extra)}（费用授权只能来自用户确认记录）")
    if args["work_id"] != ctx.get("work_id"):
        raise ToolDenied("WORK_SCOPE_DENIED", "视觉分析只能绑定当前作品")
    from ..content import tasks, visual
    from ..content.errors import ContentError

    def user_action(code: str, message: str) -> Dict[str, Any]:
        return {"state": "requires_user_action", "job_id": None, "reference_id": args.get("reference_snapshot_id") or "",
                "visual_state": "none", "requires_user_action": True, "code": code, "message": message}

    if not (args.get("reference_snapshot_id") and args.get("media_asset_id")):
        return user_action("REFERENCE_MEDIA_UNAVAILABLE", "没有可分析的参考视频：请先由用户上传原视频并确认费用")
    for key in ("reference_snapshot_id", "media_asset_id", "confirmation_id"):
        _check_path(str(args.get(key) or ""))
    try:
        job = visual.submit_visual_analysis(
            args["work_id"], args["reference_snapshot_id"], args["media_asset_id"],
            confirmation_id=str(args.get("confirmation_id") or ""))
    except ContentError as exc:
        if exc.code in ("VISUAL_COST_CONFIRMATION_REQUIRED", "COST_LIST_CHANGED", "REFERENCE_MEDIA_UNAVAILABLE"):
            return user_action(exc.code, exc.message)
        raise ToolDenied(exc.code, exc.message) from None
    tasks.get_scheduler().start()
    state = job["state"]
    refs = job.get("result_refs") or {}
    visual_state = {"succeeded": "ready", "unknown": "unknown"}.get(
        state, "pending" if state in tasks.ACTIVE_STATES else "failed")
    return {"job_id": job["id"], "state": state, "reference_id": refs.get("reference_id") or args["reference_snapshot_id"],
            "visual_state": visual_state, "requires_user_action": state in ("unknown", "blocked", "failed"),
            "replayed": bool(job.get("replayed")),
            **({"code": (job.get("error") or {}).get("code")} if job.get("error") else {})}


def t_compile_performance_plan(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """由已审稿件生成 PerformancePlan（意图层），不预造精确时间戳。

    分段构造只有一份实现（`tts.plan.create_plan`）：角色工具与 API 路径共用它，
    否则会出现"计划里的分段"和"合成时的分段"两套边界，对齐时必然对不上。
    """
    _require(args, ["work_id", "script_version_id"])
    from ..tts import plan as tts_plan

    row = tts_plan.create_plan(args["work_id"], args["script_version_id"])
    return {"plan_id": row["id"], "plan_digest": row["plan_digest"],
            "segments": len(row.get("segments", [])),
            "note": "意图层计划；实际分段时间须来自最终音频与校验后的对齐"}


def t_submit_quality_report(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """登记审核结论。硬门禁由业务层按规则复算，不采信 Agent 自报的放行。

    subject_id 为空或 `__all__` 时逐稿审核该作品当前批次的全部候选。
    """
    _require(args, ["work_id"])
    reported = args.get("checks") or []
    batch = st.get("script_batches", args["work_id"]) or {}
    subject = args.get("subject_id")
    targets = ([c for c in batch.get("candidates", []) if c["id"] == subject]
               if subject and subject != "__all__" else list(batch.get("candidates", [])))
    if not targets:
        raise ToolDenied("STALE_SELECTION", "被审对象不存在")

    from ..content import repository as content_repo

    is_v2 = content_repo.get_batch(batch.get("id") or "") is not None
    batch_modified = False
    ids, hard_all = [], []
    for cand in targets:
        checks = [QualityCheck(**c) for c in cand.get("quality_report", {}).get("checks", [])]
        if reported:
            # Agent 试图放宽：不允许改写已有检查项结论
            for rc in reported:
                code = str(rc.get("code", ""))
                if any(c.code == code for c in checks):
                    raise ToolDenied("QUALITY_REPORT_TAMPERING", f"不允许改写已有检查项结论：{code}")
        if batch.get("source_kind") != "mock" and not is_v2:
            # 升级前写入的批次没有不可变快照：仍按资料复算，绝不信任已保存的 pass
            from ..content.generate import revalidate_revision

            brief = (st.get("works", args["work_id"]) or {}).get("brief") or {}
            report, claims = revalidate_revision(
                work_id=args["work_id"], text=cand.get("text", ""),
                title=cand.get("title", ""), topic=brief.get("topic", ""),
                claims=cand.get("claims") or [], materials=brief.get("materials") or [],
                target_duration_ms=int(brief.get("target_duration_ms", 0)),
            )
            cand["quality_report"] = report.model_dump()
            cand["claims"] = [claim.model_dump() for claim in claims]
            checks = report.checks
            batch_modified = True
        # v2 批次的稿件版本是不可变快照，规则检查与独立审核已在出稿任务里持久化：
        # 这里只复用，不重算、不改写、不再调用模型收费。
        hard_fail = [c for c in checks if c.hard_gate and c.outcome != "pass"]
        recommendation = "fail" if hard_fail else cand.get("quality_report", {}).get("overall", "not_run")
        d = ac.QualityDecision(
            id=st.new_id("qd"), work_item_id=ctx.get("work_item_id", ""), subject_id=cand["id"],
            subject_digest=ac.digest(cand["text"]), recommendation=recommendation,
            report_ids=[cand["id"]],
            issues=[{"code": c.code, "reason": c.message, "rework_target": "content"} for c in hard_fail],
            model_run_id=ctx.get("model_run_id"),
        )
        d.decision_digest = ac.digest({"subject": d.subject_digest, "recommendation": recommendation})
        st.put("quality_decisions", d.id, d.model_dump(), {"work_id": args["work_id"]})
        ids.append(d.id)
        hard_all += [c.code for c in hard_fail]
    if batch_modified:
        # 只有真的改了候选才写回：读—改—写整条批次会把并发重新生成的新批次覆盖掉（丢失更新）
        st.put("script_batches", args["work_id"], batch)
    return {"quality_decision_id": ids[0], "quality_decision_ids": ids, "reviewed": len(ids),
            "hard_gate_failures": hard_all,
            "note": "结论不含批准令牌；用户采用是独立决定"}


def t_compile_speech_expression(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """表达编译：与 API 路径共用 `tts.compiler`，不另写一套降级规则。

    降级与否、支持/近似/不支持都必须来自同一份编译结果，否则
    "Agent 看到的控制能力"和"实际提交的参数"会不一致。
    """
    _require(args, ["work_id", "performance_plan_id"])
    from ..tts import compiler as tts_compiler

    w = st.get("works", args["work_id"]) or {}
    voice_id = args.get("voice_asset_id") or w.get("voice_asset_id")
    if not voice_id:
        raise ToolDenied("SELECTION_REQUIRED", "未选定音色，无法编译表达")
    try:
        compiled = tts_compiler.compile_speech(args["performance_plan_id"], voice_id)
    except Exception as e:
        raise ToolDenied(getattr(e, "code", "TTS_COMPILE_FAILED"), str(e))
    return {
        "plan_id": compiled["plan_id"], "plan_digest": compiled["plan_digest"],
        "segment_count": len(compiled["segments"]),
        "provider_params": compiled["provider_params"],
        "control_summary": compiled["control_summary"],
        "supported": compiled["controls"]["supported"],
        "approximate": compiled["controls"]["approximate"],
        "unsupported": compiled["controls"]["unsupported"],
        "suspicious_tags": compiled["suspicious_tags"],
        "note": "派生视图：分段与表达的唯一来源是冻结计划，不支持项已在调用前报告",
    }


def t_submit_tts(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Submit full synthesis only after a persisted, current user confirmation."""
    _require(args, ["work_id"])
    from .media import synthesize
    return synthesize(args, ctx)


def t_submit_motion(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["work_id", "route"])
    if args["route"] == "WAN_3_0":
        return {"skipped": True, "note": "Wan 使用图片和分镜提示词，不创建 LivePortrait 作业"}
    if args["route"] not in ("M2", "OMNIHUMAN_1_5"):
        raise ToolDenied("VALIDATION_FAILED", "路线取值非法")
    if args["route"] != "M2":
        return {"skipped": True, "note": "OmniHuman 不生成 LivePortrait 底片"}
    return t_prepare_motion_assets(args, ctx)


def t_submit_lipsync(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["work_id", "route"])
    if args["route"] in ("OMNIHUMAN_1_5", "WAN_3_0"):
        return {"skipped": True, "note": "云视频路线不创建 MuseTalk 嘴型作业"}
    if args["route"] != "M2":
        raise ToolDenied("VALIDATION_FAILED", "路线取值非法")
    from .media import submit_video
    return submit_video(args, ctx)


def t_submit_avatar_video(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Submit through the same service and cost gates as the workbench."""
    _require(args, ["work_id", "route"])
    from .media import submit_video
    return submit_video(args, ctx)


def t_inspect_audio(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """检测未接入时登记一条 not_run 检测记录：有产物引用，但不产生编造指标。"""
    _require(args, ["work_id"])
    job = _media_job(args["work_id"], "inspection_audio", "local", "detector-mock", {"target": "audio"})
    return {"inspection_id": job["id"], "outcome": "not_run", "metrics": {},
            "note": "检测能力未接入；not_run 不能当作 pass"}


def t_inspect_video(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["work_id"])
    job = _media_job(args["work_id"], "inspection_video", "local", "detector-mock", {"target": "video"})
    return {"inspection_id": job["id"], "outcome": "not_run", "metrics": {},
            "note": "检测能力未接入；not_run 不能当作 pass"}


def t_prepare_motion_assets(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Create an M2 plate through the guarded business service."""
    _require(args, ["work_id", "route"])
    if args["route"] != "M2":
        return {"skipped": True, "note": "所选云端路线不生成动作底片"}
    _require_media_confirmation(args, ctx, "M2")
    from ..digital_human import plates, worker_client
    from .media import _call
    result = _call(plates.create_plate, args["work_id"], dry_run=False, task_id=ctx.get("task_id"),
                   transport=ctx.get("media_transport") or worker_client.make_transport())
    video = result.get("video") or {}
    if result.get("is_mock") or not result.get("registered") or not video.get("id"):
        raise ToolDenied("MEDIA_RESULT_UNCONFIRMED", "底片未返回真实视频资产，不登记完成")
    return {"job_id": result.get("job_id"), "video_id": video["id"], "state": "succeeded", "requires_review": True}


def t_get_media_job(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["job_id"])
    job = (st.get("provider_jobs", args["job_id"]) or st.get("provider_runs", args["job_id"])
           or st.get("media_jobs", args["job_id"]))
    if not job:
        raise ToolDenied("JOB_NOT_FOUND", "作业不存在")
    if job.get("work_id") != ctx.get("work_id"):
        raise ToolDenied("WORK_SCOPE_DENIED", "无权查询其他作品的作业")
    return job


def t_get_media_result(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    job = t_get_media_job(args, ctx)
    assets = [row["id"] for table in ("audios", "videos")
              for row in st.all_rows(table, "work_id=?", [ctx["work_id"]])
              if row.get("provider_run_id") == job["id"] or row["id"] in job.get("result_asset_ids", [])]
    return {"job_id": job["id"], "state": job["state"], "asset_ids": assets,
            "result": job.get("result"), "is_mock": bool(job.get("is_mock") or job.get("simulated"))}


def t_cancel_media_job(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["job_id"])
    job = st.get("media_jobs", args["job_id"])
    if not job:
        raise ToolDenied("VALIDATION_FAILED", "作业不存在")
    if job["state"] == "not_submitted":
        job["state"] = "cancelled"
        st.put("media_jobs", job["id"], job, {"work_id": job.get("work_id")})
        return {"job_id": job["id"], "state": "cancelled", "note": "未向外提交，无费用可退"}
    return {"job_id": job["id"], "state": job["state"],
            "note": "已受理的外部作业需对账核实，不伪报取消"}


def t_propose_memory(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """只创建候选：审核合并前不参与跨任务检索。"""
    _require(args, ["namespace", "content_digest"])
    if args.get("state") not in (None, "proposed"):
        raise ToolDenied("MEMORY_WRITE_DENIED", "Agent 只能提交 proposed 候选，审核合并由业务服务执行")
    c = ac.MemoryCandidate(
        id=st.new_id("mem"), task_id=ctx.get("task_id", ""), work_item_id=ctx.get("work_item_id", ""),
        scope=args.get("scope", "role"), namespace=args["namespace"], content_digest=args["content_digest"],
        evidence_refs=args.get("evidence_refs") or [], applicability=args.get("applicability") or {},
    )
    st.put("memory_candidates", c.id, c.model_dump(), {"work_id": ctx.get("work_id", "")})
    return {"memory_candidate_id": c.id, "state": "proposed"}


def t_plan_work_items(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["task_id"])
    return {"task_id": args["task_id"], "note": "工作图由编排器生成并经业务校验登记，Agent 不能自填依赖"}


def t_get_progress(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["task_id"])
    task = st.get("tasks", args["task_id"])
    if not task:
        raise ToolDenied("VALIDATION_FAILED", "任务不存在")
    return {"stage": task.get("stage"), "state": task.get("state"),
            "pending_selection_ids": task.get("pending_selection_ids", [])}


def t_read_report(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    _require(args, ["report_id"])
    d = st.get("quality_decisions", args["report_id"])
    return d or {"note": "无该报告"}


TOOLS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {
    "search_douyin": t_search_douyin,
    "search_content_history": t_search_content_history,
    "get_presenter_profile": t_get_presenter_profile,
    "list_approved_assets": t_list_approved_assets,
    "register_script_candidates": t_register_script_candidates,
    "compile_performance_plan": t_compile_performance_plan,
    "submit_quality_report": t_submit_quality_report,
    "compile_speech_expression": t_compile_speech_expression,
    "submit_tts": t_submit_tts,
    "submit_motion": t_submit_motion,
    "submit_lipsync": t_submit_lipsync,
    "submit_avatar_video": t_submit_avatar_video,
    "prepare_motion_assets": t_prepare_motion_assets,
    "get_media_job": t_get_media_job,
    "get_media_result": t_get_media_result,
    "analyze_reference_visual": t_analyze_reference_visual,
    "cancel_media_job": t_cancel_media_job,
    "propose_memory": t_propose_memory,
    "plan_work_items": t_plan_work_items,
    "get_progress": t_get_progress,
    "read_report": t_read_report,
}

# 会写入业务账本的受控工具（用于幂等重放）
SIDE_EFFECT_TOOLS = {
    "search_douyin",
    "register_script_candidates", "compile_performance_plan", "submit_quality_report",
    "compile_speech_expression", "submit_tts", "submit_motion", "submit_lipsync",
    "submit_avatar_video", "prepare_motion_assets", "analyze_reference_visual",
    "propose_memory",
}


def args_digest(args: Dict[str, Any]) -> str:
    return ac.digest(args)


# 成功调用的 `message` 里存的是**完整结果载荷**，它同时是幂等重放的取回来源。
# 所以这部分不能按日志长度截断：写稿结果带上自修复留痕后超过 300 字，就会被截成
# 非法 JSON → 重放时反序列化失败 → 退化成空结果 → 产物引用全丢 → 整个工作项被判
# `RESULT_INCOMPLETE` 失败（**真实发生过**）。日志可读性由 `outcome`/`replayed` 与界面
# 渲染负责，不靠牺牲"可回放"来换。
_RESULT_PAYLOAD_CAP = None
_LOG_MESSAGE_CAP = 300


def invoke(role: str, tool: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """唯一工具入口：权限 → 存在性 → 参数 → 幂等 → 执行 → 审计。

    幂等的边界是**同一份输入的同一次尝试**：键里带输入摘要与 `attempt`。
    同一尝试内参数相同的重复调用仍然只执行一次（保护付费与外部副作用），但
    ①用户显式要求重跑（attempt 递增）、②工作项输入改变（例如补了参考资料）都必须
    真的重执行——否则"重新生成"永远只返回上次的结果，"补资料再出稿"根本不会执行。
    """
    work_item_id = ctx.get("work_item_id", "")
    ad = args_digest(args)
    attempt = ctx.get("attempt")
    inp = ctx.get("input_digest")
    idem = (f"{work_item_id}:{tool}:{ad}"
            + (f":i{inp}" if inp else "")
            + (f":a{attempt}" if attempt else ""))

    def audit(outcome: str, message: str, replayed: bool = False,
              cap: Optional[int] = _LOG_MESSAGE_CAP) -> None:
        if role not in ROLES:
            # 拒绝审计也要保存旧/未知角色，不能套用当前 AgentRole 导致拒绝路径崩溃。
            # 原始调用者只存在审计表，不作为 AgentWorkItem 或可执行角色注册。
            record = {
                "id": st.new_id("audit"), "work_item_id": work_item_id,
                "role": role, "tool": tool, "args_digest": ad,
                "idempotency_key": idem, "outcome": "denied", "message": message[:300],
                "replayed": False, "created_at": now_iso(),
            }
            st.put("agent_tool_audit", record["id"], record, {"work_item_id": work_item_id})
            return
        rec = ac.ToolCallRecord(
            id=st.new_id("audit"), work_item_id=work_item_id, role=role, tool=tool,
            args_digest=ad, idempotency_key=idem, outcome=outcome, message=message[:cap],
            replayed=replayed,
        )
        st.put("agent_tool_audit", rec.id, rec.model_dump(), {"work_item_id": work_item_id})

    spec = ROLES.get(role)
    if spec is None:
        audit("denied", "未知角色")
        raise ToolDenied("ROLE_UNKNOWN", f"未知角色 {role}")
    if not isinstance(tool, str) or not re.fullmatch(r"[a-z_]{2,40}", tool):
        audit("denied", "非法工具名")
        raise ToolDenied("TOOL_NOT_ALLOWED", f"非法工具名：{tool!r}")
    if tool not in spec.tools:
        audit("denied", "角色无权调用")
        raise ToolDenied("TOOL_NOT_ALLOWED", f"角色 {role} 无权调用 {tool}（越权工具调用已拒绝）")
    fn = TOOLS.get(tool)
    if fn is None:
        audit("denied", "工具未实现")
        raise ToolDenied("TOOL_NOT_ALLOWED", f"工具未实现：{tool}")

    if tool in SIDE_EFFECT_TOOLS:
        for r in st.all_rows("agent_tool_audit", "work_item_id=?", [work_item_id]):
            if (r.get("idempotency_key") == idem and r.get("outcome") == "ok"
                    and not r.get("replayed")):
                audit("ok", "幂等重放，未重复执行", replayed=True)
                try:
                    prev = json.loads(r.get("message") or "")
                    if not isinstance(prev, dict):
                        raise ValueError("原结果载荷不是 JSON 对象")
                except (TypeError, ValueError):
                    # 静默返回 `{}` 会让重放"看起来成功、却拿不到任何产物引用"，
                    # 调用方只能收到一句"结果不完整"，根因被藏起来。宁可明确报错。
                    audit("error", "幂等重放命中，但原结果载荷无法取回")
                    raise ToolDenied(
                        "REPLAY_PAYLOAD_MISSING",
                        "幂等重放命中，但无法取回原结果载荷；不接受空结果冒充已完成")
                return {"replayed": True, "idempotency_key": idem, "result": prev}

    try:
        result = fn(dict(args), dict(ctx))
    except ToolDenied:
        audit("denied", "业务门禁拒绝")
        raise
    except Exception as exc:
        message = f"工具执行失败（{type(exc).__name__}），未取得可用结果"
        audit("error", message)
        raise ToolDenied("TOOL_ERROR", message) from None

    audit("ok", json.dumps(result, ensure_ascii=False), cap=_RESULT_PAYLOAD_CAP)
    return {"replayed": False, "idempotency_key": idem, "result": result}


def tool_names() -> List[str]:
    return sorted(TOOLS.keys())
