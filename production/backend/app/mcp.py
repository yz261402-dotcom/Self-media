"""受限 MCP 工具通道（T02）。

对应 05 实施计划 T02 第三项：
- 实现外部/跨进程能力的 submit / query / register 受限工具；
  Provider→Worker 保持受控执行通道；持久意图、幂等键和预算先于副作用。
- 本机纯函数普通工具使用同一权限和业务门禁，
  不得包装外部副作用来绕过台账。

红线：
- 未登记的能力一律不可用；登记表是白名单，不是文档。
- 声明为 local（纯函数）的能力若试图走 submit，视为绕过台账，直接拒绝。
- 每次调用都要带角色、命名空间与幂等键，写入审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import jobs, storage as st
from .contracts import now_iso

CapabilityKind = str  # external | local


class McpDenied(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 能力登记表（白名单）。未在此登记的能力不存在，未实测的能力标 unverified。
REGISTRY: Dict[str, Dict[str, Any]] = {
    "script_candidates": {"kind": "local", "billable": False, "heavy": False, "verified": True},
    "performance_plan": {"kind": "local", "billable": False, "heavy": False, "verified": True},
    "quality_report": {"kind": "local", "billable": False, "heavy": False, "verified": True},
    "tts": {"kind": "external", "billable": True, "heavy": False, "verified": False,
            "provider": "minimax", "note": "未实测；额度 0 时提交即 blocked"},
    "motion": {"kind": "external", "billable": True, "heavy": True, "verified": False,
               "provider": "m2_worker"},
    "lipsync": {"kind": "external", "billable": True, "heavy": True, "verified": False,
                "provider": "m2_worker"},
    "avatar_video": {"kind": "external", "billable": True, "heavy": True, "verified": False,
                     "provider": "m2_worker",
                     "note": "M2 本地路线；单 GPU 重型作业，排队互斥"},
    "avatar_video_cloud": {"kind": "external", "billable": True, "heavy": False,
                           "verified": False, "provider": "volcengine",
                           "model": "omnihuman-1.5",
                           "note": "OmniHuman 云任务：不占本地 GPU，可与本地重型作业并行"},
    "short_video_cloud": {"kind": "external", "billable": True, "heavy": False,
                          "verified": False, "provider": "dashscope_wan",
                          "model": "wan3.0-video",
                          "note": "Wan 云端短视频；完整原音频与口型同步须独立验收"},
}

# 角色 → 可用能力。总控不把全部权限继承给角色。
PERMISSIONS: Dict[str, List[str]] = {
    "supervisor": ["script_candidates", "performance_plan", "quality_report", "tts", "motion",
                   "lipsync", "avatar_video", "avatar_video_cloud", "short_video_cloud"],
    "content": ["script_candidates", "performance_plan"],
    "quality": ["quality_report"],
}


def register(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """登记能力。只允许在进程内显式登记，登记不等于可用（verified 另判）。"""
    if not name or not spec.get("kind"):
        raise McpDenied("VALIDATION_FAILED", "能力登记需要 name 与 kind")
    if spec["kind"] not in ("external", "local"):
        raise McpDenied("VALIDATION_FAILED", "kind 只能是 external 或 local")
    REGISTRY[name] = dict(spec)
    return {"name": name, "registered": True, "verified": bool(spec.get("verified"))}


def capabilities_for(role: str) -> List[str]:
    return list(PERMISSIONS.get(role, []))


def _require_permission(role: str, capability: str) -> Dict[str, Any]:
    spec = REGISTRY.get(capability)
    if spec is None:
        raise McpDenied("CAPABILITY_NOT_REGISTERED", f"能力 {capability} 未在受控登记表中")
    if capability not in PERMISSIONS.get(role, []):
        raise McpDenied("PERMISSION_DENIED", f"角色 {role} 未被授予能力 {capability}")
    return spec


def _audit(c, entry: Dict[str, Any]) -> None:
    st.insert_row(c, "agent_tool_audit", entry["id"], entry, {"work_item_id": entry.get("work_item_id")})


def _require_user_confirmation(capability: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> None:
    routes = {"tts": "TTS_MINIMAX", "motion": "M2", "lipsync": "M2",
              "avatar_video": "M2", "avatar_video_cloud": "OMNIHUMAN_1_5",
              "short_video_cloud": "WAN_3_0"}
    confirmation = st.get("cost_confirmations", args.get("confirmation_id")) if args.get("confirmation_id") else None
    if (not confirmation or confirmation.get("work_id") != ctx.get("work_id")
            or confirmation.get("route") != routes.get(capability)
            or not str(confirmation.get("actor_id") or "").startswith("user")
            or not args.get("request_digest")
            or confirmation.get("request_digest") != args["request_digest"]):
        raise McpDenied("COST_CONFIRMATION_REQUIRED", "付费能力须先由用户确认当前作品、路线及调用清单")


def submit(*, role: str, capability: str, args: Dict[str, Any], ctx: Dict[str, Any],
           idempotency_key: str, billable: Optional[bool] = None,
           amount_fen: int = 0) -> Dict[str, Any]:
    """外部能力提交：先落持久意图与幂等键，再谈副作用。"""
    spec = _require_permission(role, capability)
    if spec["kind"] != "external":
        raise McpDenied("NOT_EXTERNAL",
                        f"{capability} 是本机能力，不得经 submit；外部副作用必须走台账")
    work_id = ctx.get("work_id")
    if not work_id:
        raise McpDenied("VALIDATION_FAILED", "提交外部作业必须绑定 work_id")
    if spec.get("billable") or spec.get("heavy"):
        _require_user_confirmation(capability, args, ctx)
    namespace = ctx.get("namespace") or f"work:{work_id}:{role}"
    media_tools = {"tts": ("submit_tts", "TTS_MINIMAX"), "motion": ("submit_motion", "M2"),
                   "lipsync": ("submit_lipsync", "M2"), "avatar_video": ("submit_avatar_video", "M2"),
                   "avatar_video_cloud": ("submit_avatar_video", "OMNIHUMAN_1_5"),
                   "short_video_cloud": ("submit_avatar_video", "WAN_3_0")}
    if capability in media_tools:
        from .agents import tools as agent_tools
        tool, route = media_tools[capability]
        bound = dict(args, work_id=work_id, route=route)
        scoped = dict(ctx, work_item_id=f"{ctx.get('work_item_id', '')}:{jobs.args_digest(idempotency_key)}")
        try:
            result = agent_tools.invoke(role, tool, bound, scoped)
        except agent_tools.ToolDenied as exc:
            raise McpDenied(exc.code, exc.message) from None
        receipt = result["result"]
        return {"id": receipt.get("job_id"), "state": receipt.get("state"), "capability": capability,
                "result": receipt, "replayed": result.get("replayed", False),
                "mcp": {"role": role, "capability": capability, "namespace": namespace}}
    is_billable = bool(spec["billable"] or billable)
    try:
        job = jobs.submit(
            work_id=work_id, task_id=ctx.get("task_id"), capability=capability,
            provider=spec.get("provider", "unknown"), model=spec.get("model", capability),
            args=args, idempotency_key=idempotency_key, billable=is_billable,
            amount_fen=amount_fen, namespace=namespace,
        )
    except jobs.JobRejected as exc:
        raise McpDenied(exc.code, exc.message)

    with st.transaction() as c:
        _audit(c, {
            "id": st.new_id("aud"), "work_item_id": ctx.get("work_item_id", ""),
            "role": role, "tool": f"mcp.submit:{capability}",
            "args_digest": jobs.args_digest(args), "idempotency_key": idempotency_key,
            "outcome": "ok" if job["state"] != "blocked" else "denied",
            "message": job.get("block_detail", f"作业 {job['id']} 状态 {job['state']}"),
            "replayed": bool(job.get("replayed")), "created_at": now_iso(),
        })
    return dict(job, mcp={"role": role, "capability": capability, "namespace": namespace})


def query(job_id: str, role: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """只读查询作业状态。unknown 时附带对账要求，不替调用方猜结果。"""
    job = jobs.get_job(job_id)
    if job is None:
        raise McpDenied("JOB_NOT_FOUND", f"作业不存在：{job_id}")
    spec = REGISTRY.get(job["capability"], {})
    if job["capability"] not in PERMISSIONS.get(role, []) and role != "supervisor":
        raise McpDenied("PERMISSION_DENIED", f"角色 {role} 无权查询 {job['capability']} 作业")
    out = dict(job)
    out["verified"] = bool(spec.get("verified"))
    if job["state"] == "unknown":
        out["reconciliation"] = jobs.reconcile(job_id)
    return out


def call_local(*, role: str, capability: str, args: Dict[str, Any],
               ctx: Dict[str, Any], fn) -> Dict[str, Any]:
    """本机纯函数能力：与 submit 走同一权限与审计门禁。

    fn 必须是纯函数：不允许内部发起网络、落外部作业或改他人命名空间。
    """
    spec = _require_permission(role, capability)
    if spec["kind"] != "local":
        raise McpDenied("NOT_LOCAL", f"{capability} 是外部能力，必须经 submit 走台账")
    result = fn(args, ctx)
    with st.transaction() as c:
        _audit(c, {
            "id": st.new_id("aud"), "work_item_id": ctx.get("work_item_id", ""),
            "role": role, "tool": f"mcp.local:{capability}",
            "args_digest": jobs.args_digest(args), "idempotency_key": ctx.get("idempotency_key", ""),
            "outcome": "ok", "message": "本机纯函数执行", "replayed": False, "created_at": now_iso(),
        })
    return {"capability": capability, "role": role, "result": result, "verified": bool(spec.get("verified"))}


def audit_for_work_item(work_item_id: str) -> List[dict]:
    return st.all_rows("agent_tool_audit", "work_item_id=?", [work_item_id])
