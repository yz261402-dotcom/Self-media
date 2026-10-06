"""Supervisor media access backed by existing synthesis/submission services."""
from __future__ import annotations

import math
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import config, storage as st
from ..contracts import now_iso
from .contracts import digest

router = APIRouter()
TTS_ROUTE = "TTS_MINIMAX"


def _deny(code: str, message: str):
    from .tools import ToolDenied
    raise ToolDenied(code, message)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        if getattr(exc, "code", None) and getattr(exc, "message", None):
            _deny(exc.code, exc.message)
        raise


def tts_quote(work_id: str) -> Dict[str, Any]:
    from ..tts import compiler, plan, provider, voices

    work = st.get("works", work_id)
    if not work:
        _deny("WORK_NOT_FOUND", "作品不存在")
    if work.get("deleted_at"):
        _deny("WORK_DELETED", "作品已删除，不能继续制作")
    frozen = _call(plan.ensure_plan, work_id)
    voice = voices.get_voice(work.get("voice_asset_id"))
    if not voice:
        _deny("VOICE_NOT_FOUND", "请先选择音色")
    compiled = _call(compiler.compile_speech, frozen["id"], voice["id"])
    provider_digest = provider.request_digest(compiled["voice_scope"]["model"],
                                               compiled["voice_scope"]["scope_key"],
                                               compiled["provider_params"], compiled["spoken_text"])
    listing = {"route": TTS_ROUTE, "work_id": work_id, "plan_id": frozen["id"],
               "script_version_id": compiled["script_version_id"], "script_digest": compiled["script_digest"],
               "voice_asset_id": voice["id"], "provider_request_digest": provider_digest,
               "tts_controls_digest": compiled["tts_controls_digest"],
               "tts_controls_revision": compiled["tts_controls_revision"],
               "characters": len(compiled["spoken_text"]), "model": compiled["voice_scope"]["model"]}
    listing["digest"] = digest(listing)
    rate = os.environ.get("AVATAR_STUDIO_MINIMAX_PRICE_FEN_PER_10000_CHARS", "")
    estimated = math.ceil(listing["characters"] * int(rate) / 10000) if rate.isdigit() else None
    activation = 0
    if voice.get("clone_metadata"):
        from ..tts import cloning
        activation = _call(cloning.synthesis_charge, voice, compiled["spoken_text"])
        estimated = max(estimated or 0, activation)
    return {"work_id": work_id, "route": TTS_ROUTE, "current_call_list": listing,
            "request_digest": listing["digest"], "estimated_fen": estimated,
            "price_verified": False, "clone_activation_included_fen": activation,
            "budget": config.budget_policy().to_dict(),
            "note": "实际费用待供应商账单确认；未配置单价时须由用户给出预留上限"}


def confirm_tts(work_id: str, *, request_digest: str, ceiling_fen: int, actor_id: str) -> Dict[str, Any]:
    if not actor_id.startswith("user"):
        _deny("COST_ACTOR_INVALID", "费用确认必须由用户给出")
    quote = tts_quote(work_id)
    if quote["request_digest"] != request_digest:
        _deny("COST_LIST_CHANGED", "音频调用清单已变化，请重新确认")
    if ceiling_fen <= 0 or ceiling_fen < (quote["estimated_fen"] or 0):
        _deny("COST_AMOUNT_INVALID", "预留上限必须为正且覆盖估价")
    row = {"id": st.new_id("cost"), "work_id": work_id, "route": TTS_ROUTE,
           "actor_id": actor_id, "confirmed_at": now_iso(), "request_digest": request_digest,
           "call_list": quote["current_call_list"], "estimated_fen": quote["estimated_fen"],
           "ceiling_fen": ceiling_fen, "budget_snapshot": quote["budget"], "authorizes_payment": False}
    st.put("cost_confirmations", row["id"], row, {"work_id": work_id})
    return row


def require_confirmation(args: Dict[str, Any], ctx: Dict[str, Any], route: str) -> Dict[str, Any]:
    work_id = args.get("work_id")
    if not work_id or ctx.get("work_id") != work_id:
        _deny("WORK_SCOPE_DENIED", "媒体工具只能处理当前作品")
    work = st.get("works", work_id) or {}
    current_task = st.get("tasks", work.get("current_task_id")) if work.get("current_task_id") else None
    if current_task and current_task.get("state") == "cancelled":
        _deny("TASK_CANCELLED", "作品当前任务已取消，停止媒体提交")
    if ctx.get("task_id") and current_task and ctx["task_id"] != current_task["id"]:
        _deny("TASK_SUPERSEDED", "作品已切换执行，停止旧任务媒体提交")
    row = st.get("cost_confirmations", args.get("confirmation_id")) if args.get("confirmation_id") else None
    if (not row or row.get("work_id") != work_id or row.get("route") != route
            or not str(row.get("actor_id") or "").startswith("user")
            or not args.get("request_digest") or row.get("request_digest") != args["request_digest"]):
        _deny("COST_CONFIRMATION_REQUIRED", "请先由用户确认当前作品、路线及调用清单")
    if route == TTS_ROUTE:
        quote = tts_quote(work_id)
        if quote["request_digest"] != row["request_digest"]:
            _deny("COST_LIST_CHANGED", "稿件、音色或语气参数已变化，请重新确认")
        if quote["budget"] != row.get("budget_snapshot"):
            _deny("BUDGET_POLICY_CHANGED", "预算策略已变化，请重新确认")
        if not config.budget_policy().allows_billable(int(row.get("ceiling_fen") or 0)):
            _deny("BUDGET_NOT_AUTHORIZED", "当前预算不足以覆盖用户确认的音频预留上限")
    else:
        from ..digital_human import cost, submission
        listing = _call(submission.current_call_list, work_id, route=route,
                        prompt=args.get("prompt", ""), resolution=args.get("resolution") or ("720p" if route == "WAN_3_0" else "1080p"),
                        duration_seconds=args.get("duration_seconds", 5))
        if row["request_digest"] != listing["digest"]:
            _deny("COST_LIST_CHANGED", "视频调用清单已变化，请重新确认")
        from ..tts import audio as tts_audio
        final_audio = tts_audio.final_audio(work_id) or {}
        estimate = _call(cost.estimate, route, duration_ms=int(final_audio.get("duration_ms") or 0),
                         resolution=args.get("resolution") or ("720p" if route == "WAN_3_0" else "1080p"),
                         segment_count=int(listing.get("segment_count") or 1),
                         duration_seconds=args.get("duration_seconds", 5) if route == "WAN_3_0" else None)
        estimated = int(estimate["estimated_fen"])
        if estimated > int(row.get("ceiling_fen") or 0):
            _deny("COST_CEILING_EXCEEDED", "本次估价超过用户确认的上限")
        _call(cost.assert_submission_allowed, work_id, route=route, request_digest=listing["digest"],
              estimated_fen=estimated, call_list=listing)
    return row


def synthesize(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    from ..tts import synthesis
    approval = require_confirmation(args, ctx, TTS_ROUTE)
    output = _call(synthesis.synthesize_full, work_id=args["work_id"],
                   transport=ctx.get("tts_transport"), task_id=ctx.get("task_id"),
                   amount_fen=int(approval["ceiling_fen"]), dry_run=False,
                   plan_id=approval["call_list"]["plan_id"])
    audio = output.get("audio") or {}
    if output.get("is_mock") or audio.get("is_mock") or not audio.get("id") or not audio.get("file_hash"):
        _deny("MEDIA_RESULT_UNCONFIRMED", "合成未返回真实音频资产，不登记完成")
    return {"audio_id": audio["id"], "job_id": audio.get("provider_run_id"), "state": "succeeded",
            "duration_ms": audio["duration_ms"], "is_mock": False, "requires_audition": True,
            "cached": bool(output.get("cached"))}


def submit_video(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    from ..digital_human import providers, submission
    route = args.get("route")
    if route not in ("M2", "OMNIHUMAN_1_5", "WAN_3_0"):
        _deny("ROUTE_NOT_SELECTED", "请选择视频制作路线")
    require_confirmation(args, ctx, route)
    work = st.get("works", args["work_id"]) or {}
    task = st.get("tasks", ctx.get("task_id") or work.get("current_task_id")) or None
    options = {"task": task, "dry_run": False,
               "replacement_authorized": False}
    if route == "M2":
        from ..digital_human import worker_client
        options["transport"] = ctx.get("media_transport") or worker_client.make_transport()
        result = _call(submission.submit_m2, args["work_id"], **options)
    elif route == "OMNIHUMAN_1_5":
        options.update(prompt=args.get("prompt", ""), resolution=args.get("resolution") or "1080p",
                       transport=ctx.get("media_transport") or providers.urllib_transport)
        result = _call(submission.submit_omnihuman, args["work_id"], **options)
    else:
        from ..digital_human import wan, wan_provider
        options.update(prompt=args.get("prompt", ""), resolution=args.get("resolution") or "720p",
                       duration_seconds=args.get("duration_seconds", 5),
                       transport=ctx.get("media_transport") or wan_provider.urllib_transport)
        result = _call(wan.submit_wan, args["work_id"], **options)
    runs = result.get("runs") or []
    if (result.get("is_mock") or not runs
            or any(r.get("state") not in ("submitted", "waiting_provider", "succeeded") for r in runs)):
        _deny("MEDIA_RESULT_UNCONFIRMED", "供应商作业结果尚未确认，请查询原作业")
    return {"job_id": runs[0]["id"], "run_ids": [r["id"] for r in runs], "state": runs[0]["state"],
            "submitted": bool(result.get("submitted")), "is_mock": False,
            "replayed": bool(result.get("replayed"))}


class TtsConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_digest: str
    ceiling_fen: int = Field(gt=0)


def _api(fn, *args, **kwargs):
    from .tools import ToolDenied
    try:
        return fn(*args, **kwargs)
    except ToolDenied as exc:
        raise HTTPException(409, detail={"error": {"code": exc.code, "message": exc.message}}) from None


@router.post("/api/v1/works/{work_id}/agent-media/tts/quote")
def quote_tts_api(work_id: str):
    return _api(tts_quote, work_id)


@router.post("/api/v1/works/{work_id}/agent-media/tts/confirmations")
def confirm_tts_api(work_id: str, body: TtsConfirmation):
    return _api(confirm_tts, work_id, request_digest=body.request_digest,
                ceiling_fen=body.ceiling_fen, actor_id="user")
