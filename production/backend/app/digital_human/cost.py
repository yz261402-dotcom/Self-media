"""口播费用：估价、价格快照与费用确认（AC-UI-05）。

一条容易被含糊过去的边界，这里单独强调：

> **用户给出的费率是报价，不是付费授权。**

所以本模块把三件事拆开：价格快照（费率从哪来）、费用确认（这次要花多少、
清单是什么）、预算策略（到底允不允许付）。前两者齐全也不等于可以扣费；
`budget_policy().allows_billable()` 仍然是否决点，默认额度 0 时提交必然被拦。

计价口径也是分开记的：M2 是本地 GPU 按实际使用计入，没有"单价"可写；
OmniHuman 与 Wan 按各自生成秒和费率计价；实际账单与失败扣费尚未核验。
"""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Dict, List, Optional

from .. import config, storage as st
from ..contracts import now_iso
from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .routes import (COST_NOTICE_VERSION, ROUTE_M2, ROUTE_OMNIHUMAN, ROUTE_WAN,
                     ROUTES, route_spec, wan_generation_seconds, assert_wan_duration_allowed)

PRICE_TABLE = "price_snapshots"
CONFIRM_TABLE = "cost_confirmations"

# 用户已确认的 OmniHuman 费率：1 元/生成秒 = 100 分/秒。
# billing_verified=False：这是报价，不是账单核验结论。
OMNIHUMAN_RATE_FEN_PER_SECOND = 100
WAN_RATE_FEN_PER_SECOND = {"480p": 30, "720p": 60, "1080p": 120}
OMNIHUMAN_PRICE_NOTES = {
    "rounding": "计费取整粒度未核验（按秒向上取整只是保守估计，不是账单结论）",
    "minimum": "最低消费未核验",
    "failure": "失败扣费规则未核验：失败可能仍计费，因此不得盲目重提",
}
# 分段重复生成的额外风险：段数越多，计费边界越多
ROUNDING_MARGIN = 1.2


def register_price_snapshot(route: str = ROUTE_OMNIHUMAN) -> Dict[str, Any]:
    spec = route_spec(route)
    if route == ROUTE_OMNIHUMAN:
        snap = {
            "id": st.new_id("price"), "route": route, "provider": spec["provider"],
            "unit": "fen_per_generated_second", "unit_fen": OMNIHUMAN_RATE_FEN_PER_SECOND,
            "price_source": "user_confirmed_rate", "billing_verified": False,
            "rounding_note": OMNIHUMAN_PRICE_NOTES["rounding"],
            "minimum_note": OMNIHUMAN_PRICE_NOTES["minimum"],
            "failure_billing_note": OMNIHUMAN_PRICE_NOTES["failure"],
            "evidence": "用户在 T05 确认 OmniHuman 1 元/生成秒；仅含视频生成费，"
                        "TTS/文本图片/存储后期/返工另计",
            "created_at": now_iso(),
        }
    elif route == ROUTE_WAN:
        snap = {
            "id": st.new_id("price"), "route": route, "provider": spec["provider"],
            "model": spec["model"], "unit": "fen_per_generated_second", "unit_fen": None,
            "generation_mode": "image_prompt_native_audio",
            "unit_fen_by_resolution": dict(WAN_RATE_FEN_PER_SECOND),
            "price_source": "official_bailian_beijing_list_price", "billing_verified": False,
            "rounding_note": "按用户所选整数生成秒估算，声音由模型生成",
            "minimum_note": "单次生成支持 2–30 秒整数，默认 5 秒",
            "failure_billing_note": "实际账单与失败扣费待核验；未知任务不得自动重提",
            "evidence": "2026-09-29 核验百炼北京 Wan 3.0 原价；480P/720P/1080P 分别为"
                        " 0.30/0.60/1.20 元每生成秒，仅含视频生成费",
            "source_url": "https://help.aliyun.com/zh/model-studio/wan3-0-video",
            "created_at": now_iso(),
        }
    elif route == ROUTE_M2:
        snap = {
            "id": st.new_id("price"), "route": route, "provider": spec["provider"],
            "unit": "no_unit_price", "unit_fen": 0,
            "price_source": "unverified", "billing_verified": False,
            "rounding_note": "本地 GPU 无单价：按实际使用时长/卡时计入",
            "minimum_note": "不适用于本地作业",
            "failure_billing_note": "失败作业不产生云费用，但已占用的 GPU 时间仍计入成本",
            "evidence": "EXP-001 235.177 秒 GPU 时间对应约 0.103217 元，"
                        "该数字是实验结果不是生产单价",
            "created_at": now_iso(),
        }
    st.put(PRICE_TABLE, snap["id"], snap, {"route": route})
    return snap


def current_price(route: str) -> Dict[str, Any]:
    rows = st.all_rows(PRICE_TABLE, "route=?", [route])
    if route == ROUTE_WAN:
        rows = [row for row in rows if row.get("generation_mode") == "image_prompt_native_audio"]
    if rows:
        return sorted(rows, key=lambda r: r.get("created_at", ""))[-1]
    return register_price_snapshot(route)


def estimate(route: str = ROUTE_OMNIHUMAN, *, duration_ms: Optional[int] = None,
             segment_count: int = 1, per_day: int = 1, days: int = 30,
             resolution: str = "1080p", duration_seconds: Optional[int] = None) -> Dict[str, Any]:
    """按当前价格快照估价。分段会引入取整风险，因此在结果里如实标注区间。"""
    spec = route_spec(route)
    audio_seconds = max(0.0, (duration_ms if duration_ms is not None else 60000) / 1000.0)
    billing_seconds = int(math.ceil(audio_seconds)) if audio_seconds else 0
    if route == ROUTE_M2:
        return {
            "route": route, "label": spec["label"], "resolution": resolution,
            "audio_seconds": round(audio_seconds, 3), "segment_count": segment_count,
            "billing_seconds": None, "unit_fen": 0,
            "estimated_fen": 0, "ceiling_fen": 0, "currency": "CNY",
            "price_snapshot_id": current_price(route)["id"],
            "billing_verified": False, "billable": False,
            "price_basis": "本地 GPU 作业：不按生成秒计价，按实际卡时计入成本",
            "per_day_fen": 0, "monthly_fen": 0,
            "excluded": ["GPU 卡时", "存储", "后期", "返工"],
            "note": "M2 报价不是生产单价；GPU 费用按实际使用单独记录"
                    "（EXP-001 235.177 秒约 0.103217 元，仅为实验观测）",
        }
    if route == ROUTE_WAN:
        if resolution not in WAN_RATE_FEN_PER_SECOND:
            raise TalkingHeadRejected("WAN_RESOLUTION_UNSUPPORTED", "Wan 仅支持 480p、720p、1080p",
                                      {"resolution": resolution})
        if segment_count != 1:
            raise TalkingHeadRejected("WAN_SEGMENTATION_NOT_SUPPORTED", "Wan 每次仅提交一个短片，多镜头需分别确认费用。")
        # Explicit seconds are the current contract; milliseconds remain for historical callers.
        billing_seconds = (assert_wan_duration_allowed(duration_seconds) if duration_seconds is not None else
                           wan_generation_seconds(duration_ms) if duration_ms is not None else 5)
        unit = WAN_RATE_FEN_PER_SECOND[resolution]
        estimated = billing_seconds * unit
        return {
            "route": route, "label": spec["label"], "resolution": resolution,
            "audio_seconds": (float(billing_seconds) if duration_seconds is not None
                              else round(audio_seconds, 3)),
            "duration_seconds": billing_seconds, "segment_count": 1,
            "generated_seconds": billing_seconds, "billing_seconds": billing_seconds,
            "unit_fen": unit, "estimated_fen": estimated, "ceiling_fen": estimated,
            "currency": "CNY", "price_snapshot_id": current_price(route)["id"],
            "billing_verified": False, "billable": True,
            "price_basis": f"百炼北京 Wan 3.0 官方原价：{resolution} 每生成秒 {unit} 分；仅视频生成费",
            "per_day_fen": estimated * per_day, "monthly_fen": estimated * per_day * days,
            "per_day": per_day, "days": days,
            "excluded": ["文本/图片", "存储与后期", "返工重做", "失败可能仍计费"],
            "note": "生成与计费采用同一整数秒，至少 2 秒；实际账单与失败扣费未核验。"
                    "估价不等于付费授权，提交前仍须费用确认与预算授权。",
        }
    unit = OMNIHUMAN_RATE_FEN_PER_SECOND
    estimated = billing_seconds * unit
    # 分段取整未核验：段数越多越可能在每段被向上取整，用保守上界避免低估
    ceiling = int(math.ceil(estimated * ROUNDING_MARGIN)) if estimated else 0
    return {
        "route": route, "label": spec["label"], "resolution": resolution,
        "audio_seconds": round(audio_seconds, 3), "segment_count": segment_count,
        "billing_seconds": billing_seconds, "unit_fen": unit,
        "estimated_fen": estimated, "ceiling_fen": ceiling, "currency": "CNY",
        "price_snapshot_id": current_price(route)["id"],
        "billing_verified": False, "billable": True,
        "price_basis": "用户已确认 1 元/生成秒（100 分/秒）；仅视频生成费",
        "per_day_fen": estimated * per_day, "monthly_fen": estimated * per_day * days,
        "per_day": per_day, "days": days,
        "excluded": ["TTS", "文本/图片", "存储与后期", "返工重做", "失败可能仍计费"],
        "note": "估价不等于付费授权：提交前须有费用确认与适用预算；"
                "取整/最低消费/失败扣费待账单核验，实际费用可能高于估价",
    }


def scenario_table() -> List[Dict[str, Any]]:
    """用户确认的三个参照点：60 秒 / 5 条每日 / 30 天。"""
    return [
        {"scenario": "单条 60 秒", **estimate(ROUTE_OMNIHUMAN, duration_ms=60000)},
        {"scenario": "每天 5 条 60 秒", **estimate(ROUTE_OMNIHUMAN, duration_ms=60000, per_day=5)},
        {"scenario": "30 天每天 5 条", **estimate(ROUTE_OMNIHUMAN, duration_ms=60000,
                                                 per_day=5, days=30)},
    ]


def cost_notice(route: str, *, duration_ms: Optional[int] = None,
                segment_count: int = 1, resolution: str = "1080p",
                duration_seconds: Optional[int] = None) -> Dict[str, Any]:
    """选路线时展示给用户的费用提示。

    两条规定：
    1. **没有计费时长就不给金额。** 最终音频没就绪时"生成秒"不存在，
       任何数字都是编造；此时 `estimated_fen=None` 并把原因写清楚。
    2. **未核验的事就写未核验。** 取整、最低消费、失败是否扣费都还是问号；
       写进提示是让用户知道自己在承担什么，不是免责声明。
    """
    spec = route_spec(route)
    notice: Dict[str, Any] = {
        "route": route,
        "cost_tier": spec["cost_tier"],
        "cost_tier_label": spec["cost_tier_label"],
        "notice_version": COST_NOTICE_VERSION,
        "requires_acknowledgement": bool(spec.get("cost_notice_required")),
        "unit_text": spec["cost_summary"],
        "body_motion": spec["body_motion"],
        "estimated_fen": None,
        "ceiling_fen": None,
        "amount_known": False,
        "amount_pending_reason": "",
        "risks": [
            "效果不满意仍可能收费",
            "重生成/返工会再次收费",
            "取整粒度、最低消费、失败是否扣费均未核验，实际费用可能高于估价",
        ],
        "excluded": ["TTS 合成", "文本与图片", "存储与后期", "返工重做"],
    }
    if route == ROUTE_M2:
        notice["amount_pending_reason"] = (
            "M2 不按生成秒计价：成本以本机 GPU 卡时计入，没有云单价可给；"
            "EXP-001 观测约 0.103217 元/15.2 秒（实验值，不是生产单价）"
        )
        notice["risks"] = [
            "不产生云费用，但占用本机 GPU 卡时，单卡串行排队",
            "失败不产生云费用，已占用的 GPU 时间仍计入成本",
            "实验观测值不是生产单价，实际成本以运行记录为准",
        ]
        notice["excluded"] = ["GPU 卡时", "存储", "后期", "返工"]
        notice["note"] = "低成本路线：不按生成秒计费，也不需要费用提示确认即可选择。"
        return notice

    if route == ROUTE_WAN:
        duration_seconds = 5 if duration_seconds is None else duration_seconds
        notice["excluded"] = ["文本与图片", "存储与后期", "返工重做"]
        notice["risks"] = ["效果不满意仍可能收费", "重生成/返工会再次收费",
                           "按所选整数生成秒计费；实际账单与失败扣费待核验"]

    if route != ROUTE_WAN and not duration_ms:
        notice["amount_pending_reason"] = (
            "最终音频未就绪，还没有「生成秒」：此时不显示金额，"
            "等音频确认后再按同一费率给出估价"
        )
        notice["note"] = ("高成本路线：选择前须确认本提示；提交前还须再确认一次"
                          "具体的调用清单与金额。")
        return notice

    try:
        est = estimate(route, duration_ms=duration_ms, segment_count=segment_count,
                       resolution=resolution, duration_seconds=duration_seconds)
    except TalkingHeadRejected as error:
        if route != ROUTE_WAN:
            raise
        notice["amount_pending_reason"] = error.message
        notice["blocker"] = {"code": error.code, "message": error.message}
        return notice
    notice.update({
        "estimated_fen": est["estimated_fen"], "ceiling_fen": est["ceiling_fen"],
        "amount_known": True,
        "audio_seconds": est["audio_seconds"], "segment_count": est["segment_count"],
        "billing_seconds": est["billing_seconds"], "unit_fen": est["unit_fen"],
        "duration_seconds": est.get("duration_seconds"),
        "price_basis": est["price_basis"],
        "note": "高成本路线：选择前须确认本提示；提交前还须再确认一次具体的调用清单与金额。",
    })
    return notice


def confirm_cost(work_id: str, *, route: str, request_digest: str,
                 estimated_fen: int, ceiling_fen: int, segment_count: int = 1,
                 generated_seconds: float = 0.0, resolution: str = "",
                 actor_id: str = "", accepted_risks: Optional[List[str]] = None,
                 task_id: Optional[str] = None,
                 call_list: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记录一次费用确认。**本函数的成功不代表可以扣费**，只代表清单与金额被确认过。"""
    spec = route_spec(route)
    if not actor_id or not actor_id.startswith("user"):
        raise TalkingHeadRejected("COST_ACTOR_INVALID",
                                 "费用确认必须由用户给出，系统不代确认付费范围",
                                 {"actor_id": actor_id})
    if not request_digest:
        raise TalkingHeadRejected("COST_REQUEST_DIGEST_REQUIRED",
                                 "费用确认必须绑定具体调用清单摘要：否则金额变化后无法察觉")
    if call_list is not None and (call_list.get("digest") != request_digest
                                  or call_list.get("route") != route):
        raise TalkingHeadRejected("COST_SNAPSHOT_MISMATCH", "调用清单快照与费用确认摘要不一致")
    if estimated_fen < 0 or ceiling_fen < estimated_fen:
        raise TalkingHeadRejected("COST_AMOUNT_INVALID",
                                 "估价与上限不合法：上限必须不小于估价")
    policy = config.budget_policy()
    snap = current_price(route)
    rec = {
        "id": st.new_id("cost"), "work_id": work_id, "task_id": task_id,
        "route": route, "provider": spec["provider"], "model": spec["model"],
        "request_digest": request_digest, "price_snapshot_id": snap["id"],
        "call_list": deepcopy(call_list),
        "estimated_fen": int(estimated_fen), "ceiling_fen": int(ceiling_fen),
        "segment_count": int(segment_count),
        "generated_seconds": round(float(generated_seconds), 3),
        "resolution": resolution,
        "budget_policy_id": "default",
        "budget_snapshot": policy.to_dict(),
        "max_billable_submissions": policy.max_billable_submissions,
        "accepted_risks": accepted_risks or [
            "效果不满意仍可能收费",
            "重生成可能再次收费",
            "取整/最低消费/失败扣费尚未核验",
        ],
        "actor_id": actor_id, "confirmed_at": now_iso(),
        # 本版本不按时间过期：确认失效的条件是**清单或预算发生变化**，不是钟表。
        # 之前这里写的是一个算出来的时间戳而没有任何地方校验它，
        # 那等于给用户一个"有效期"的假承诺——用户会以为过期后必须重新确认，
        # 而系统其实永远不会因此拦下任何提交。因此显式留空，并把真实规则写出来。
        "valid_until": None,
        "expiry_policy": "invalidate_on_change",
        "expiry_note": "清单摘要、段数、分辨率或预算策略变化即失效；不按时间自动过期",
        "authorizes_payment": False,
        "note": "费用确认绑定调用清单与预算；报价本身不是付费授权，"
                "实际扣费仍由预算策略与台账决定",
    }
    st.put(CONFIRM_TABLE, rec["id"], rec, {"work_id": work_id})
    return rec


def current_confirmation(work_id: str, route: Optional[str] = None) -> Optional[Dict[str, Any]]:
    rows = st.all_rows(CONFIRM_TABLE, "work_id=?", [work_id])
    if route:
        rows = [r for r in rows if r.get("route") == route]
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("confirmed_at", ""))[-1]


def changed_fields(confirmation: Dict[str, Any], listing: Dict[str, Any]) -> List[str]:
    """摘要才是门禁依据；快照只用于解释变化，不凭金额相同替用户续授权。"""
    if confirmation.get("request_digest") == listing.get("digest"):
        return []
    previous = confirmation.get("call_list") or {}
    if not previous:
        return ["调用清单"]
    labels = {"route": "生成路线", "audio_id": "配音版本", "audio_hash": "配音文件",
              "identity_id": "人物身份", "source_asset_id": "人物原图", "source_asset_hash": "人物图文件",
              "variant_id": "人物画面", "motion_ref_id": "动作参考", "plate_id": "动作底片",
              "prompt": "动作描述", "resolution": "分辨率", "segment_count": "分段数量",
              "duration_seconds": "生成时长", "generation_mode": "生成模式", "sound_mode": "声音模式"}
    return [label for key, label in labels.items() if previous.get(key) != listing.get(key)] or ["调用清单"]


def assert_submission_allowed(work_id: str, *, route: str, request_digest: str,
                              estimated_fen: int,
                              call_list: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """提交前的费用门禁。三类否决严格分开，便于前端给出不同动作。"""
    conf = current_confirmation(work_id, route)
    if not conf:
        if route == ROUTE_M2:
            # M2 没有云单价，但它占用本机 GPU 卡时——说成"计费路线"与
            # 自己的价格快照（unit=no_unit_price、billable=False）自相矛盾，
            # 用户会去找一个并不存在的单价。要确认的是清单与预算边界。
            raise TalkingHeadBlocked(
                "COST_CONFIRMATION_REQUIRED",
                "M2 没有云单价，但会占用本机 GPU 卡时：提交前须先确认调用清单"
                "（段数、预计卡时）与预算边界（估价 0 元不等于零成本）",
                {"route": route, "pricing": "no_unit_price"},
            )
        raise TalkingHeadBlocked(
            "COST_CONFIRMATION_REQUIRED",
            f"{route} 是计费路线：必须先展示调用清单与估价并经用户确认",
            {"route": route},
        )
    if conf.get("request_digest") != request_digest:
        changes = changed_fields(conf, call_list or {"digest": request_digest}) if call_list else ["调用清单"]
        raise TalkingHeadRejected(
            "COST_LIST_CHANGED",
            "已确认后发生变化：" + "、".join(changes)
            + "。请先点击「确认调用清单与金额」，再生成口播；本次尚未提交到生成服务。",
            {"confirmed": conf.get("request_digest"), "current": request_digest,
             "changed_fields": changes},
        )
    if int(estimated_fen) > int(conf.get("ceiling_fen") or 0):
        raise TalkingHeadBlocked(
            "COST_CEILING_EXCEEDED",
            f"估价 {estimated_fen} 分超过已确认上限 {conf.get('ceiling_fen')} 分："
            "超额须停止提交并重新确认，而不是自动放宽",
            {"estimated_fen": estimated_fen, "ceiling_fen": conf.get("ceiling_fen")},
        )
    policy = config.budget_policy()
    if not policy.allows_billable(int(estimated_fen)):
        if route == ROUTE_M2:
            raise TalkingHeadBlocked(
                "BUDGET_NOT_AUTHORIZED",
                f"M2 未获预算授权（per_task={policy.per_task_limit_fen}、"
                f"max_submissions={policy.max_billable_submissions}）："
                "本地 GPU 作业不产生云费用，但会占用卡时；默认 0 预算即不提交 GPU 作业",
                {"budget": policy.to_dict(), "route": route},
            )
        raise TalkingHeadBlocked(
            "BUDGET_NOT_AUTHORIZED",
            f"预算未授权（per_task={policy.per_task_limit_fen}、"
            f"max_submissions={policy.max_billable_submissions}）：报价确认不等于付费授权",
            {"budget": policy.to_dict(), "estimated_fen": estimated_fen},
        )
    return {"ok": True, "confirmation_id": conf["id"], "estimated_fen": estimated_fen,
            "ceiling_fen": conf.get("ceiling_fen"), "budget": policy.to_dict()}


def cost_summary(work_id: str, *, route: Optional[str] = None,
                 duration_ms: Optional[int] = None, prompt: str = "",
                 resolution: str = "1080p", duration_seconds: int = 5) -> Dict[str, Any]:
    conf = current_confirmation(work_id, route)
    policy = config.budget_policy()
    listing = None
    blocker = None
    if route:
        from .submission import current_call_list

        try:
            listing = current_call_list(work_id, route=route, prompt=prompt, resolution=resolution,
                                        duration_seconds=duration_seconds)
        except TalkingHeadBlocked as error:
            # 音频/分段计划未就绪，费用页面仍须可打开，不能显示旧确认有效。
            if route == ROUTE_WAN:
                blocker = {"code": error.code, "message": error.message}
        except TalkingHeadRejected as error:
            if route != ROUTE_WAN:
                raise
            blocker = {"code": error.code, "message": error.message}
    valid = bool(conf and listing and conf.get("request_digest") == listing["digest"])
    est = None
    if route and (duration_ms or route == ROUTE_WAN):
        try:
            est = estimate(route, duration_ms=duration_ms, resolution=resolution,
                           segment_count=(listing or {}).get("segment_count", 1),
                           duration_seconds=duration_seconds if route == ROUTE_WAN else None)
        except TalkingHeadRejected as error:
            if route != ROUTE_WAN:
                raise
            blocker = {"code": error.code, "message": error.message}
            valid = False
    return {
        "work_id": work_id, "route": route,
        "confirmation": conf, "estimate": est,
        "current_call_list": listing, "confirmation_valid": valid,
        "changed_fields": changed_fields(conf, listing) if conf and listing else [],
        "budget": policy.to_dict(),
        "price_snapshots": {r: current_price(r) for r in ROUTES},
        "blocker": blocker,
        "billable_allowed": policy.allows_billable(est["estimated_fen"]) if est else False,
        "note": "估价、预留与实际费用分开记录；用户报价不是本轮付费授权",
    }
