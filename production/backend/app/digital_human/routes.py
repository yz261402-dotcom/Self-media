"""口播路线声明、能力视图与提交前的前置门禁（T05 第一项）。

三条不变量：

1. **能力声明与实测分栏。** `declared` 是供应商/历史核验给出的规格，`verified`
   是本项目在当前账户、当前人物上真实测过的东西。二者混在一起，就会把
   "文档写着支持"当成"我们验过"，而 AC-TH-01 明确要求 L1 真实任务结果。
2. **声音不由视频模型决定。** 所有路线都消费 T04 已确认的最终音频；
   视频模型只被音频驱动。因此"没有已确认音频"不是警告，是硬拒。
3. **纯文生视频不能冒充口播。** 只给一段文字描述让模型自由生成视频，
   产出的人物既不是作品绑定的人物、声音也不是已确认音色——这与
   "固定人物 + 已确认音频驱动"是两件事，必须拒绝而不是"效果也差不多"。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..avatar import motion as avatar_motion
from ..avatar import registry as avatar_registry
from ..tts import audio as tts_audio
from ..tts import compiler as tts_compiler
from ..tts import voices as tts_voices
from .errors import TalkingHeadBlocked, TalkingHeadRejected

ROUTE_M2 = "M2"
ROUTE_OMNIHUMAN = "OMNIHUMAN_1_5"
ROUTE_WAN = "WAN_3_0"
ROUTES = (ROUTE_M2, ROUTE_OMNIHUMAN, ROUTE_WAN)

# 成本档位。界面要让用户一眼看懂"便宜的那条少了什么"，因此档位是路线声明的一部分，
# 不是前端文案：换文案不该改变"哪条需要费用确认"这个事实。
COST_TIER_LOW = "low"
COST_TIER_HIGH = "high"

# 费用提示版本。提示内容或费率口径变化就升版，用户必须重新确认——
# 否则会出现"用户确认的是旧价、系统按新价提交"。
COST_NOTICE_VERSION = "2026-09-24.1"

# 调用能力名。M2 在本地 GPU Worker 上跑（重型、互斥）；OmniHuman 是云任务，
# 不与本地作业争 GPU，因此用独立能力名进白名单，而不是复用 avatar_video。
CAP_AVATAR_VIDEO = "avatar_video"          # M2 本地重推理
CAP_AVATAR_VIDEO_CLOUD = "avatar_video_cloud"  # OmniHuman 云端
CAP_SHORT_VIDEO_CLOUD = "short_video_cloud"  # Wan 云端，独立于数字人口型能力
CAP_MOTION = "motion"
CAP_LIPSYNC = "lipsync"

# OmniHuman 当期规格（2026-09-21 接口核验）。严格小于 60 秒是硬上限，
# "建议不超过 15 秒"是建议值——实施前需按当前账户复核，本模块只做声明。
OMNIHUMAN_LIMIT = {
    "audio_max_seconds_exclusive": 60,
    "audio_recommended_seconds": 15,
    "prompt_max_chars": 300,
    "resolutions": ["720p", "1080p"],
    "async": True,
    "idempotent_submit": False,
    "supports_cancel": None,  # 未核验：不写 True 也不写 False
    "req_key": "jimeng_realman_avatar_picture_omni_v15",
    "endpoint": "https://visual.volcengineapi.com",
    "region": "cn-north-1",
    "service": "cv",
    "action_submit": "CVSubmitTask",
    "action_query": "CVGetResult",
    "api_version": "2022-08-31",
    # 云端并发上限：**按账号而定**。2026-09-28 用户实测本账号（免费试用）为 1 ——
    # 一次只能有一个生成任务在跑。把整条音轨的分段一口气提交出去，第 2 段必然
    # 撞 429；而 429 在 `providers._classify_http` 里被判成"额度不足、不得重试"，
    # 于是整个 7 段计划在第 2 段就被整体打断，后面几段连作业都没建。
    # 所以上限必须显式声明，由提交方按上限串行推进（见 omni_driver）。
    "max_concurrency": 1,
}

# 官方规格只算声明，不代表当前账户权限、人物一致性或口型已经实测。
WAN_LIMIT = {
    "audio_min_seconds": 1,
    "audio_max_seconds": 15,
    "generated_min_seconds": 2,
    "generated_max_seconds": 30,
    "default_duration_seconds": 5,
    "resolutions": ["480p", "720p", "1080p"],
    "default_resolution": "720p",
    "aspect_ratio": "9:16",
    "audio_mode": "native_audio",
    "generation_mode": "image_prompt_native_audio",
    "prompt_required": True,
    "prompt_max_chars": 20000,
    "async": True,
    "idempotent_submit": False,
    "supports_cancel": None,
    "segmentation_supported": False,
}


def assert_wan_duration_allowed(duration_seconds: int) -> int:
    if type(duration_seconds) is not int or not 2 <= duration_seconds <= 30:
        raise TalkingHeadRejected("WAN_DURATION_INVALID", "Wan 生成时长必须为 2–30 秒整数。")
    return duration_seconds


def wan_audio_duration_blocker(audio_duration_ms: int) -> Optional[Dict[str, Any]]:
    """Wan 保留完整原音频，只接受 1–15 秒单任务，不切分或截短。"""
    duration_ms = int(audio_duration_ms or 0)
    minimum_ms = int(WAN_LIMIT["audio_min_seconds"] * 1000)
    maximum_ms = int(WAN_LIMIT["audio_max_seconds"] * 1000)
    if minimum_ms <= duration_ms <= maximum_ms:
        return None
    return {
        "code": "WAN_AUDIO_TOO_SHORT" if duration_ms < minimum_ms else "WAN_AUDIO_TOO_LONG",
        "message": "Wan 3.0 当前仅支持 1–15 秒完整原音频（含边界）；"
                   "第一版不分段、不加速、不截字，请调整稿件并重新确认音频。",
        "kind": "rejected",
        "detail": {"audio_duration_ms": duration_ms, "minimum_ms": minimum_ms,
                   "maximum_ms": maximum_ms},
    }


def assert_wan_audio_duration_allowed(audio_duration_ms: int) -> Dict[str, Any]:
    blocker = wan_audio_duration_blocker(audio_duration_ms)
    if blocker:
        raise TalkingHeadRejected(blocker["code"], blocker["message"], blocker["detail"])
    return {"audio_duration_ms": int(audio_duration_ms or 0),
            "minimum_ms": int(WAN_LIMIT["audio_min_seconds"] * 1000),
            "maximum_ms": int(WAN_LIMIT["audio_max_seconds"] * 1000)}


def wan_generation_seconds(audio_duration_ms: int) -> int:
    """提交与估价共享整数生成秒数，保证原音轨完整覆盖。"""
    allowed = assert_wan_audio_duration_allowed(audio_duration_ms)
    return max(int(WAN_LIMIT["generated_min_seconds"]),
               (allowed["audio_duration_ms"] + 999) // 1000)

# 并发上限的环境变量覆盖：换到并发更高的账号时不必改代码。
OMNI_CONCURRENCY_ENV = "AVATAR_STUDIO_OMNIHUMAN_MAX_CONCURRENCY"


def omnihuman_audio_duration_blocker(audio_duration_ms: int) -> Optional[Dict[str, Any]]:
    """返回 OmniHuman 长音频门禁；60 秒及以上不再自动分段。"""
    duration_ms = int(audio_duration_ms or 0)
    limit_ms = int(OMNIHUMAN_LIMIT["audio_max_seconds_exclusive"] * 1000)
    if duration_ms < limit_ms:
        return None
    return {
        "code": "OMNIHUMAN_AUDIO_TOO_LONG",
        "message": (
            "OmniHuman 当前仅支持严格小于 60 秒的单段音频；"
            "长音频不再自动分段，请缩短音频后再提交；禁止加速或截字。"
        ),
        "kind": "rejected",
        "detail": {"audio_duration_ms": duration_ms, "limit_ms": limit_ms},
    }


def assert_omnihuman_audio_duration_allowed(audio_duration_ms: int) -> Dict[str, Any]:
    """提交前统一拦截 OmniHuman 的长音频；M2 不调用此门禁。"""
    blocker = omnihuman_audio_duration_blocker(audio_duration_ms)
    if blocker:
        raise TalkingHeadRejected(blocker["code"], blocker["message"], blocker["detail"])
    return {"audio_duration_ms": int(audio_duration_ms or 0),
            "limit_ms": int(OMNIHUMAN_LIMIT["audio_max_seconds_exclusive"] * 1000)}


def omnihuman_max_concurrency() -> int:
    """生效的 OmniHuman 并发上限（同时可有多少段在飞）。

    取不到或非法时回落到声明值，且**只接受 >= 1**：并发 0 会让所有分段
    永远发不出去，那不是"更保守"，是死锁。
    """
    raw = (os.environ.get(OMNI_CONCURRENCY_ENV) or "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed >= 1:
            return parsed
    return max(1, int(OMNIHUMAN_LIMIT.get("max_concurrency") or 1))

# 本项目实测（`verified`）——空字典就是"什么都没实测过"，不允许用声明填满它。
OMNIHUMAN_VERIFIED: Dict[str, Any] = {}

ROUTE_SPEC: Dict[str, Dict[str, Any]] = {
    ROUTE_WAN: {
        "route": ROUTE_WAN,
        "label": "Wan 3.0 · 创意短视频",
        "provider": "dashscope_wan",
        "model": "wan3.0-video",
        "capability": CAP_SHORT_VIDEO_CLOUD,
        "consumes": ["上传参考图或已审人物图", "画面与声音提示词（必填）", "2–30 秒生成时长"],
        "requires_motion_plate": False,
        "stage_count": 1,
        "heavy": False,
        "billable": True,
        "cost_tier": COST_TIER_HIGH,
        "cost_tier_label": "云端计费 · 创意短视频",
        "cost_notice_required": True,
        "cost_summary": "百炼北京原价：480P ¥0.30、720P ¥0.60、1080P ¥1.20/生成秒；"
                        "按所选 2–30 整数生成秒计价，实际账单未核验",
        "body_motion": {
            "supported": True,
            "scope": "图片与提示词生成画面及声音；内容、人物一致性和声音需观看审核",
            "verified": False,
            "verified_note": "仅能力声明；当前账户权限、人物一致性与口型尚未实测",
        },
        "declared": WAN_LIMIT,
        "verified": {},
        "evidence": [],
        "open_questions": ["当前账户 Wan 3.0 邀测与模型权限", "人物一致性、口型和动作指令遵循",
                           "实际账单与失败扣费规则"],
    },
    ROUTE_OMNIHUMAN: {
        "route": ROUTE_OMNIHUMAN,
        "label": "OmniHuman 1.5 · 图＋音频驱动",
        "provider": "volcengine",
        "model": "omnihuman-1.5",
        "capability": CAP_AVATAR_VIDEO_CLOUD,
        "consumes": ["已审固定人物/变体图", "已确认最终 TTS", "可编辑表演 prompt（可选）"],
        "requires_motion_plate": False,
        "stage_count": 1,
        "heavy": False,  # 云端任务：不与本地 GPU 作业争用资源
        "billable": True,
        "cost_tier": COST_TIER_HIGH,
        "cost_tier_label": "高成本 · 含肢体动作",
        "cost_notice_required": True,
        "cost_summary": "按生成秒计费：¥1.00/生成秒（报价，取整/最低消费/失败扣费均未核验）",
        # 肢体动作：服务商声明该路线由「图＋音频」驱动生成含肢体动作的人物视频；
        # 本项目跑通过真实任务，但未逐项验收动作质量与指令遵循程度，所以 verified=False。
        "body_motion": {
            "supported": True,
            "scope": "画面含头肩以外的肢体动作，由模型自由生成，不是逐帧指定",
            "verified": False,
            "verified_note": "声明口径：服务商文档如此描述；本项目真实任务尚未逐项验收"
                             "动作自然度与手势指令遵循程度（见 open_questions）",
        },
        "declared": OMNIHUMAN_LIMIT,
        "verified": OMNIHUMAN_VERIFIED,
        "evidence": [
            "TH-COMP-003（2026-09 历史短片）：9.48 秒原片，704×1248/25fps，",
            "输入音轨 9.36 秒；24kHz 单声道对齐偏移 0ms、重叠区 Pearson 0.99996223。",
            "该样本不是当前人物 B，也不证明长片、身体动作或任意手势达标。",
        ],
        "open_questions": [
            "当前账户的音频时长上限与分辨率权限",
            "计费取整/最低粒度/失败扣费",
            "身体动作与手势的指令遵循程度",
        ],
    },
    ROUTE_M2: {
        "route": ROUTE_M2,
        "label": "M2 · LivePortrait → MuseTalk 1.5",
        "provider": "m2_worker",
        "model": "LivePortrait+MuseTalk1.5",
        "capability": CAP_AVATAR_VIDEO,
        "consumes": ["已定版人物原图或已审变体图", "已获准动作参考", "已确认最终 TTS"],
        "requires_motion_plate": True,
        "stage_count": 2,
        "heavy": True,  # 本地单 GPU 重推理：排队互斥
        "billable": True,
        "cost_tier": COST_TIER_LOW,
        "cost_tier_label": "低成本 · 头肩口型",
        "cost_notice_required": False,
        "cost_summary": "无云单价：占用本机 GPU 卡时（EXP-001 观测约 ¥0.10/15.2 秒，实验值不是生产单价）",
        "body_motion": {
            "supported": False,
            "scope": "沿用已审动态底片的动作，路线本身不新增肢体动作与手势",
            "verified": False,
            "verified_note": "动作来自已获准动作参考与底片；不承诺任意手势",
        },
        "declared": {
            "stage_1": "LivePortrait 动作迁移（人物图 + 动作参考 → 动态底片）",
            "stage_2": "MuseTalk 1.5 口型（底片 + 最终音频 → 原片）",
            "async": False,
        },
        "verified": {
            "exp_001": {
                "output_seconds": 15.20,
                "liveportrait_seconds": 53.505,
                "musetalk_seconds": 181.672,
                "gpu_cost_yuan_estimate": 0.103217,
                "scope": "EXP-001 隔离环境；非账单、非生产单价、非约 60 秒能力证据",
            },
            "mw_001": {
                "date": "2026-09-24",
                "motion_job_id": "bd9bf651889c4e29",
                "motion_seconds": 126.7,
                "plate_sha256": "b55f723dea1cf4d2e32a608cd15bb795b350c645125b8697698090494ec7325c",
                "lipsync_job_id": "2ab8b5433b5c4c45",
                "lipsync_seconds": 217.9,
                "output_seconds": 23.187,
                "output_sha256": "648f97125f7d26fd9dd41938c85346cdd0f92767e1698fd4c4476f709f240879",
                "audio_offset_samples": 0,
                "audio_corr": 0.9995,
                "scope": ("经 HTTP worker + SSH 转发在算力云 RTX 4080 实跑两阶段；"
                          "提交走 worker_client.transport，未经 HTTP 端点登记台账，"
                          "故不代表端到端生产链路已验收。身份/口型质量仍待人工验收。"),
                "evidence": "experiments/M2-Worker链路/MW-001/",
            },
        },
        "evidence": [
            "EXP-001：15.20 秒 M2 原片，两阶段共 235.177 秒 GPU 时间。",
            "MW-001：经 HTTP worker 实跑两阶段出片 23.187 秒，音轨偏移 0 样本、相关 0.9995。",
            "历史约 9.13 秒阿里片仅作实验动作参考，未被批准为长期母版。",
        ],
        "open_questions": ["约 60 秒循环接缝", "每天 5 条产能", "动作母版定版"],
    },
}

# 所有路线的共同前置：任何一条缺了都不能提交
_COMMON_REQUIREMENTS = ("identity", "variant", "script", "plan", "audio")


def route_spec(route: str) -> Dict[str, Any]:
    spec = ROUTE_SPEC.get(route)
    if spec is None:
        raise TalkingHeadRejected(
            "ROUTE_UNSUPPORTED",
            f"路线 {route} 不在当前可调度能力内；Grok 等历史路线只读保留，"
            "不接受新建任务，也不映射为 OmniHuman 或恢复提交",
            {"route": route, "supported": list(ROUTES)},
        )
    return spec


def route_view(route: str) -> Dict[str, Any]:
    """对外能力视图：声明/实测/未决问题三块分开，便于前端如实展示。"""
    spec = route_spec(route)
    declared = dict(spec["declared"])
    if route == ROUTE_WAN:
        declared["user_prompt_max_chars"] = declared["prompt_max_chars"]
        # Audio duration limits apply only to persisted historical reference-audio jobs.
        declared.pop("audio_min_seconds", None)
        declared.pop("audio_max_seconds", None)
    return {
        "route": spec["route"],
        "label": spec["label"],
        "provider": spec["provider"],
        "model": spec["model"],
        "capability": spec["capability"],
        "consumes": spec["consumes"],
        "requires_motion_plate": spec["requires_motion_plate"],
        "stage_count": spec["stage_count"],
        "heavy": spec["heavy"],
        "cost_tier": spec["cost_tier"],
        "cost_tier_label": spec["cost_tier_label"],
        "cost_notice_required": spec["cost_notice_required"],
        "cost_summary": spec["cost_summary"],
        "body_motion": spec["body_motion"],
        "declared": declared,
        "verified": spec["verified"],
        "evidence": spec["evidence"],
        "open_questions": spec["open_questions"],
        "verified_state": "unverified" if not spec["verified"] else "partially_verified",
    }


def routes_view() -> List[Dict[str, Any]]:
    return [route_view(r) for r in ROUTES]


def assert_route_selected(task: Optional[Dict[str, Any]], route: Optional[str] = None) -> str:
    """路线必须来自一次真实人工选择，不能用默认值当授权。"""
    if not task:
        raise TalkingHeadBlocked("TASK_REQUIRED", "口播提交必须绑定任务：路线确认属于任务级决定")
    chosen = route or task.get("route")
    if not chosen:
        raise TalkingHeadRejected("ROUTE_NOT_SELECTED", "未选择口播路线，不得提交任何生成任务")
    if not task.get("route_confirmed"):
        raise TalkingHeadRejected(
            "ROUTE_NOT_CONFIRMED",
            "路线尚未经用户确认与预算核对；默认路线不是付费授权",
            {"route": chosen},
        )
    if not task.get("route_selection_id"):
        raise TalkingHeadRejected(
            "ROUTE_SELECTION_UNTRACEABLE",
            "路线缺少可追溯的人工选择记录（SelectionDecision），不得据此提交",
            {"route": chosen},
        )
    if chosen != task.get("route") and route:
        raise TalkingHeadRejected(
            "ROUTE_MISMATCH",
            f"提交路线 {route} 与任务已确认路线 {task.get('route')} 不一致；切换路线须重新确认",
            {"requested": route, "confirmed": task.get("route")},
        )
    # 高成本路线：选择时必须有"已看过并确认费用提示"的记录。
    # 放在路线一致性之后——不一致时先说不一致，否则会给错的动作指引。
    if route_spec(chosen).get("cost_notice_required"):
        _assert_cost_notice_acknowledged(task, chosen)
    return chosen


def _assert_cost_notice_acknowledged(task: Dict[str, Any], route: str) -> None:
    """高成本路线的费用提示确认。

    选择路线本身不产生费用，但它是这条付费链路的第一道人工授权：
    用户必须先看到"这条要花多少、哪些风险没核验"，再决定要不要走它。
    确认记录挂在作品上（路线是作品级决定，且 Task 契约是冻结的、不能加字段）。
    """
    work = st.get("works", task.get("work_id") or "") or {}
    ack = work.get("cost_notice_ack") or {}
    if ack.get("route") != route or ack.get("version") != COST_NOTICE_VERSION:
        raise TalkingHeadBlocked(
            "COST_NOTICE_NOT_ACKNOWLEDGED",
            f"{route} 是高成本路线：必须先向用户展示费用提示（费率、估价口径、未核验风险）"
            "并取得确认，才能选择该路线；未确认不得进入提交环节。"
            "这与提交前的「调用清单与金额确认」是两件事：前者决定走不走这条路，"
            "后者决定这次具体花多少。",
            {"route": route, "notice_version": COST_NOTICE_VERSION,
             "ack": ack or None},
        )


def reject_text_to_video(payload: Optional[Dict[str, Any]]) -> None:
    """纯文生视频负例：只有文本描述、没有固定人物图与已确认音频时直接拒收。"""
    p = payload or {}
    has_image = bool(p.get("image_asset_id") or p.get("variant_id") or p.get("identity_id"))
    has_audio = bool(p.get("audio_asset_id"))
    if not has_image and not has_audio:
        raise TalkingHeadRejected(
            "TEXT_TO_VIDEO_NOT_SUPPORTED",
            "本路线是「固定人物图＋已确认音频」的音频驱动口播，不是文生视频；"
            "仅凭文本描述生成的视频不代表作品绑定的人物与音色",
            {"payload_keys": sorted(p.keys())},
        )


def _final_audio(work_id: str) -> Optional[Dict[str, Any]]:
    audio = tts_audio.final_audio(work_id)
    if not audio:
        return None
    return audio


def preflight(work_id: str, *, task: Optional[Dict[str, Any]] = None,
              route: Optional[str] = None,
              require_plate: Optional[bool] = None) -> Dict[str, Any]:
    """结构化前置判定。返回 blockers 而不是抛错，便于前端逐项展示。"""
    work = st.get("works", work_id) or {}
    # 路线解析回退链：显式入参 → 任务已确认路线 → 作品当前路线。
    # 不回退到空串：空串会让 route_spec 抛 ROUTE_UNSUPPORTED，
    # 把「还没选路线」误报成「这条路线不支持」，而预检的契约是报告阻断项、不抛错。
    chosen = (route or (task or {}).get("route") or work.get("route") or "").strip()
    spec = route_spec(chosen)
    blockers: List[Dict[str, str]] = []

    def block(code: str, message: str, kind: str = "rejected") -> None:
        blockers.append({"code": code, "message": message, "kind": kind})

    if not task:
        block("TASK_REQUIRED", "口播提交必须绑定任务", "blocked")
    else:
        try:
            assert_route_selected(task, chosen)
        except (TalkingHeadRejected, TalkingHeadBlocked) as e:
            block(e.code, e.message, "blocked" if isinstance(e, TalkingHeadBlocked) else "rejected")

    if chosen == ROUTE_WAN:
        from .wan_inputs import resolve_source
        source = None
        try:
            source = resolve_source(work)
        except (TalkingHeadRejected, TalkingHeadBlocked) as error:
            block(error.code, error.message, "blocked" if isinstance(error, TalkingHeadBlocked) else "rejected")
        return {"work_id": work_id, "route": chosen, "ok": not blockers, "blockers": blockers,
                "rejected": [b for b in blockers if b["kind"] == "rejected"],
                "identity_id": (source or {}).get("identity_id"), "variant_id": (source or {}).get("variant_id"),
                "source": source, "script_version_id": None, "performance_plan_id": None,
                "audio_asset_id": None, "audio_duration_ms": None, "plate_id": None,
                "motion_intent": _motion_intent_view(work_id, None, spec),
                "note": "图片与提示词生成原生有声短片；不需要稿件、配音、音色或表演计划。"}

    # 人物：必须是已定版身份 + 已审变体/原图
    identity_id = work.get("identity_id")
    if not identity_id:
        block("IDENTITY_REQUIRED", "尚未选定已定版人物")
    else:
        ident = avatar_registry.get_identity(identity_id)
        if not ident:
            block("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
        else:
            if ident.get("approval_state") != "approved" or not ident.get("frozen"):
                block("IDENTITY_NOT_APPROVED", "人物未定版或未冻结，不得用于口播")
            if not ident.get("identity_digest"):
                block("IDENTITY_BASE_MISSING", "缺少身份基准摘要，无法核对身份保持")

    variant_id = work.get("variant_id")
    source = None
    if identity_id:
        from .plate_source import resolve_plate_source

        try:
            source = resolve_plate_source(work)
        except (TalkingHeadRejected, TalkingHeadBlocked) as error:
            block(error.code, error.message,
                  "blocked" if isinstance(error, TalkingHeadBlocked) else "rejected")

    # 已确认最终音频：所有路线的唯一音轨来源
    audio = _final_audio(work_id)
    if not audio:
        block("FINAL_AUDIO_REQUIRED", "尚无经用户试听确认的最终音频，不得提交口播")
    else:
        if audio.get("is_mock"):
            block("FINAL_AUDIO_IS_MOCK", "占位音频不得驱动口播")
        if not audio.get("file_hash"):
            block("FINAL_AUDIO_NO_HASH", "最终音频缺少文件哈希，音轨保持无法核对")
        if chosen == ROUTE_OMNIHUMAN:
            duration_blocker = omnihuman_audio_duration_blocker(
                int(audio.get("duration_ms") or 0))
            if duration_blocker:
                blockers.append({"code": duration_blocker["code"],
                                 "message": duration_blocker["message"],
                                 "kind": duration_blocker["kind"]})
        elif chosen == ROUTE_WAN:
            duration_blocker = wan_audio_duration_blocker(int(audio.get("duration_ms") or 0))
            if duration_blocker:
                blockers.append({"code": duration_blocker["code"],
                                 "message": duration_blocker["message"],
                                 "kind": duration_blocker["kind"]})

    # 稿件与表演计划：分段与表达的唯一来源
    script_id = work.get("script_version_id")
    if not script_id:
        block("SCRIPT_REQUIRED", "尚未采用稿件")
    plan_id = work.get("performance_plan_id")
    plan = None
    if not plan_id:
        block("PLAN_REQUIRED", "缺少冻结的 PerformancePlan，分段与表达无来源")
    else:
        try:
            plan = tts_compiler.load_frozen_plan(plan_id)
        except Exception as e:  # 计划过期/摘要不符/被取代
            block(getattr(e, "code", "PLAN_NOT_USABLE"), getattr(e, "message", str(e)))
    if plan and audio:
        # 版本错配：计划与音频必须描述同一份稿件
        if plan.get("script_version_id") != audio.get("script_version_id"):
            block("VERSION_MISMATCH",
                  "冻结计划与最终音频指向不同稿件版本，不得混用",
                  "rejected")
        if plan.get("voice_asset_id") and audio.get("voice_asset_id") \
                and plan["voice_asset_id"] != audio["voice_asset_id"]:
            block("VOICE_MISMATCH", "计划绑定音色与最终音频音色不一致", "rejected")

    ok, reasons = tts_voices.voice_ready(work.get("voice_asset_id") or "")
    if not ok and audio:
        block("VOICE_NOT_PRODUCTION_READY", "；".join(reasons), "blocked")

    # M2 追加：已获准动态底片
    if require_plate is None:
        require_plate = spec["requires_motion_plate"]
    plate = None
    if require_plate:
        from . import plates as pl

        plate = pl.approved_plate_for(work_id)
        if not plate:
            block("PLATE_NOT_APPROVED", "M2 需要已审动态底片：未审底片不得提交对口型", "blocked")
        elif audio and plate.get("audio_asset_id") != audio.get("id"):
            block("PLATE_AUDIO_MISMATCH",
                  "动态底片绑定的音频与本次最终音频不一致，版本错配", "rejected")

    rejected = [b for b in blockers if b["kind"] == "rejected"]
    return {
        "work_id": work_id,
        "route": spec["route"],
        "ok": not blockers,
        "blockers": blockers,
        "rejected": rejected,
        "identity_id": identity_id,
        "variant_id": variant_id,
        "source": source,
        "script_version_id": script_id,
        "performance_plan_id": plan_id,
        "audio_asset_id": audio.get("id") if audio else None,
        "audio_duration_ms": audio.get("duration_ms") if audio else None,
        "plate_id": plate.get("id") if plate else None,
        "motion_intent": _motion_intent_view(work_id, identity_id, spec),
        "note": "前置不通过不得提交；rejected 类需要改输入，blocked 类需要等外部条件",
    }


def _motion_intent_view(work_id: str, identity_id: Optional[str],
                        spec: Dict[str, Any]) -> Dict[str, Any]:
    """M2 的动作意图必须映射到已获准参考；云路线用自由描述，不做质量承诺。"""
    intent = (st.get("works", work_id) or {}).get("motion_intent") or {}
    if not identity_id:
        return {"state": "blocked", "reason": "未选定人物"}
    if spec["requires_motion_plate"]:
        if not intent:
            return {"state": "missing", "reason": "M2 必须从获准动作模板中选择"}
        try:
            resolved = avatar_motion.resolve_motion_intent(identity_id, intent, route=ROUTE_M2)
        except Exception as e:
            return {"state": "rejected", "reason": getattr(e, "message", str(e)),
                    "code": getattr(e, "code", "MOTION_REJECTED")}
        return {"state": "resolved", **resolved}
    free_text = (intent or {}).get("free_text", "")
    return {
        "state": "free_text" if free_text else "unspecified",
        "free_text": free_text,
        "reason": f"表演描述交由 {spec['label']} 执行；不承诺任意动作，需实测指令遵循程度",
    }


def require_preflight(work_id: str, **kwargs: Any) -> Dict[str, Any]:
    """强门禁版本：任一前置不通过即抛错，阻断后续提交。"""
    out = preflight(work_id, **kwargs)
    if out["ok"]:
        return out
    blocked = [b for b in out["blockers"] if b["kind"] == "blocked"]
    if blocked:
        raise TalkingHeadBlocked(blocked[0]["code"],
                                "；".join(b["message"] for b in out["blockers"]), out)
    raise TalkingHeadRejected(out["blockers"][0]["code"],
                             "；".join(b["message"] for b in out["blockers"]), out)
