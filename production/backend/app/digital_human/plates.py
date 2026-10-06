"""M2 动态底片（LivePortrait 阶段）的生成与审核。

底片是 M2 路线的中间产物：**有人物的动作，但还没有最终音频的口型**。
它的两条硬约束来自 AC-TH-01 与 AC-TH-05：

1. 人物身份必须保持——底片源图是已定版原图或作品绑定的已审变体，动作参考只提供运动，
   不提供脸。来源哈希必须进作业参数，否则缓存复用与版本错配无从判定。
2. 时长必须与已确认最终音频核对。底片比音频短就必然要"加速或截字"，
   这是明确禁止的做法；底片比音频长则是可裁的多余量，
   但它属于编辑决定（T07 的 provider_lead_ms），不能被默认丢掉，
   也不能靠改字幕把偏移掩盖过去。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import jobs, storage as st
from ..avatar import registry
from ..avatar.registry import AssetRejected
from ..contracts import now_iso
from ..tts import audio as tts_audio
from . import providers as pv
from . import videos as vd
from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .plate_source import resolve_plate_source

PLATE_REVIEW_TABLE = "plate_reviews"
PLATE_DURATION_TOLERANCE_MS = 40


def _deps(work_id: str) -> Dict[str, Any]:
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    if work.get("deleted_at"):
        raise TalkingHeadRejected("WORK_DELETED", "作品已删除，不能继续制作")
    identity_id = work.get("identity_id")
    if not identity_id:
        raise TalkingHeadRejected("IDENTITY_REQUIRED", "未选定人物，不得生成底片")
    return work


def create_plate(work_id: str, *, variant_id: Optional[str] = None,
                 output_seconds: Optional[float] = None,
                 dry_run: Optional[bool] = None, transport=None,
                 task_id: Optional[str] = None) -> Dict[str, Any]:
    """生成动态底片。依赖不满足一律拒绝，不做近似兜底。"""
    from .. import config

    work = _deps(work_id)
    identity_id = work["identity_id"]
    source = resolve_plate_source(work, variant_id=variant_id)
    variant_id = source["variant_id"]
    source_asset = registry.get_asset(source["asset_id"]) or {}

    # 2) 动作意图必须映射到已获准参考（T03 已实现同一份规则）
    motion_ref = _resolve_motion(work)
    motion_asset = registry.get_asset(motion_ref.get("asset_id", "")) or {}

    # 3) 时长以已确认最终音频为准：底片要覆盖整条音轨
    audio = _confirmed_audio(work_id)
    audio_ms = int(audio.get("duration_ms") or 0)
    seconds = float(output_seconds) if output_seconds else audio_ms / 1000.0
    if seconds <= 0:
        raise TalkingHeadRejected("LP_OUTPUT_SECONDS_INVALID", "底片时长必须为正")

    request = pv.build_liveportrait_request(
        source_image_url=source_asset.get("storage_uri", ""),
        motion_ref_url=motion_asset.get("storage_uri", ""),
        output_seconds=seconds,
        source_hash=source_asset.get("file_hash", ""),
        motion_hash=motion_asset.get("file_hash", ""),
    )

    is_mock = config.resolve_mode() == "mock" if dry_run is None else bool(dry_run)

    if is_mock:
        # 占位底片不登记为可用资产：register_video 会拒收无哈希、无视频轨的产物，
        # 而这正是 Mock 的真实状态——它没有字节、没有视频轨。
        # 与其把 asset 表写脏再让门禁去挡，不如让它在登记这一步就进不来。
        return {
            "registered": False, "is_mock": True, "work_id": work_id,
            "kind": vd.KIND_PLATE, "route": "M2", "provider": "m2_worker",
            "model": "LivePortrait", "request": request,
            "duration_ms": int(round(seconds * 1000)),
            "audio_asset_id": audio.get("id"),
            "motion_ref_id": motion_ref.get("id"), "variant_id": variant_id,
            "source": source,
            "note": "占位底片：dry_run 下不产生真实字节，不登记为可用资产，不继承任何通过结论",
        }

    if not transport:
        raise TalkingHeadBlocked("M2_TRANSPORT_UNAVAILABLE",
                                "M2 推理在算力云隔离环境执行；未提供通道时不得假装已执行")
    base_key = (f"motion:{work_id}:{source['asset_id']}:{motion_ref.get('id')}:"
                f"{source_asset.get('file_hash','')[:12]}:"
                f"{motion_asset.get('file_hash','')[:12]}:{seconds}:{audio['id']}")
    job = jobs.submit(
        work_id=work_id, capability="motion", provider="m2_worker", model="LivePortrait",
        args=request["args"],
        # 已对账取消的死尝试不占死键位：否则一次传输抖动就会永久锁死这组输入，
        # 且没有任何输入可以改动来解锁（见 jobs.plate_idempotency_key）。
        idempotency_key=jobs.plate_idempotency_key(base_key),
        task_id=task_id, billable=True, amount_fen=0,
        note="LivePortrait 动作迁移（单 GPU 重型作业）",
    )
    if job["state"] == "blocked":
        raise TalkingHeadBlocked(job.get("block_code", "JOB_BLOCKED"),
                                job.get("block_detail", "底片作业被阻断"))
    if job.get("replayed"):
        prior_video = vd.get_video((job.get("result") or {}).get("video_id"))
        if job["state"] == "succeeded" and prior_video:
            return {"registered": True, "is_mock": False, "video": prior_video,
                    "job_id": job["id"], "audio_asset_id": audio["id"], "request": request,
                    "source": source, "replayed": True, "note": "复用同一输入已生成的底片，未重复提交"}
        raise TalkingHeadBlocked("PLATE_JOB_ALREADY_SUBMITTED",
                                 "同一底片任务已提交；需核对既有任务结果，不能重复创建 GPU 作业",
                                 {"job_id": job["id"], "state": job["state"]})
    worker_id = f"plate:{job['id']}"
    jobs.acquire_lease(job["id"], worker_id, ttl_seconds=max(3600, int(seconds * 60)))
    try:
        resp = transport(request)
        parsed = pv.parse_m2_response(resp)
    except Exception:
        jobs.mark_unknown(job["id"], worker_id, reason="底片提交未取得可核对结果，禁止自动重提")
        raise
    if parsed["state"] != "succeeded":
        detail = {"job_id": job["id"], "state": parsed["state"],
                  "remote_job_id": parsed.get("remote_job_id")}
        if parsed["state"] == "running":
            jobs.mark_unknown(job["id"], worker_id, reason=f"远端底片仍在运行：{parsed.get('remote_job_id') or '未返回ID'}")
            # 还在生成 ≠ 失败。写成失败会诱导用户重提，而重提会在 GPU 上多排一份队。
            raise TalkingHeadBlocked(
                "PLATE_JOB_STILL_RUNNING",
                "底片仍在远端生成中：这不是失败，稍后查询同一作业即可；"
                "重复提交会在 GPU 上多排一份相同的队", detail)
        if parsed["state"] == "failed":
            jobs.fail(job["id"], worker_id, {"code": "PLATE_JOB_NOT_COMPLETED", "remote_job_id": parsed.get("remote_job_id")})
        else:
            jobs.mark_unknown(job["id"], worker_id, reason="远端底片结果未确定")
        raise TalkingHeadBlocked(
            "PLATE_JOB_NOT_COMPLETED",
            f"底片作业未完成（{parsed['state']}）：{parsed.get('reason','')}；"
            "不改判成功、不自动回退其他路线", detail,
        )
    try:
        downloaded = pv.download_and_verify(parsed["output_url"], transport)
    except Exception:
        jobs.mark_unknown(job["id"], worker_id, reason="远端已返回结果但下载或校验未完成，须对账")
        raise
    probe = downloaded["probe"]
    video = vd.register_video(
        work_id=work_id, kind=vd.KIND_PLATE, route="M2", provider="m2_worker",
        model="LivePortrait", duration_ms=int(probe.get("duration_ms") or int(seconds * 1000)),
        file_hash=probe["file_hash"], is_mock=False, identity_id=identity_id,
        variant_id=variant_id, motion_ref_id=motion_ref.get("id"),
        source_asset_id=source["asset_id"], source_asset_hash=source["asset_hash"],
        audio_asset_id=audio["id"],
        provider_run_id=job["id"], has_video_track=bool(probe.get("has_video_track")),
        has_audio_track=bool(probe.get("has_audio_track")),
        width=probe.get("width"), height=probe.get("height"),
        checks=[], note="动态底片；须与最终音频核对时长并经人工审核后才能对口型",
    )
    video["storage_uri"] = vd.store_video_bytes(work_id, video["id"], downloaded["data"])
    st.put(vd.VIDEO_TABLE, video["id"], video, {"work_id": work_id})
    jobs.complete(job["id"], worker_id, {"video_id": video["id"], "file_hash": video["file_hash"],
                                        "remote_job_id": parsed.get("remote_job_id")})
    return {"registered": True, "is_mock": False, "video": video, "job_id": job["id"],
            "audio_asset_id": audio.get("id"), "request": request, "source": source,
            "note": "底片已登记，等待时长核对与身份审核"}


def record_plate_review(plate_id: str, *, reviewer: str, outcome: str,
                        audio_duration_ms: Optional[int] = None,
                        identity_digest: str = "",
                        checks: Optional[List[Dict[str, Any]]] = None,
                        note: str = "") -> Dict[str, Any]:
    """底片审核记录。通过只能由用户给出：本项目未接入视觉识别能力。"""
    video = vd.get_video(plate_id)
    if not video:
        raise TalkingHeadRejected("VIDEO_NOT_FOUND", f"视频不存在：{plate_id}")
    if video.get("kind") != vd.KIND_PLATE:
        raise TalkingHeadRejected("NOT_A_PLATE", "该产物不是动态底片，不能按底片审核")
    if outcome not in ("pass", "fail", "review", "not_run"):
        raise TalkingHeadRejected("PLATE_REVIEW_OUTCOME_INVALID", f"审核结论非法：{outcome}")
    if outcome == "pass":
        if not reviewer.startswith("user"):
            raise TalkingHeadRejected(
                "PLATE_REVIEW_ACTOR_INVALID",
                "底片审核通过只能由用户目检给出：本项目不做自动身份判定，"
                "也不把『看起来像』写成结论",
                {"reviewer": reviewer},
            )
        reasons = [f["message"] for f in vd.hard_failures(video)]
        if reasons:
            raise TalkingHeadRejected("PLATE_HARD_GATE_FAILED", "；".join(reasons))
        if identity_digest:
            ident = registry.get_identity(video.get("identity_id") or "")
            if not ident or ident.get("identity_digest") != identity_digest:
                raise TalkingHeadRejected(
                    "PLATE_IDENTITY_DRIFT",
                    "底片身份摘要与冻结身份基准不一致，判定为身份漂移，不得用于对口型",
                    {"expected": (ident or {}).get("identity_digest"),
                     "actual": identity_digest},
                )
    duration_info: Dict[str, Any] = {}
    if audio_duration_ms:
        duration_info = check_plate_duration(video, int(audio_duration_ms), strict=True)

    rec = {
        "id": st.new_id("prev"), "video_id": plate_id, "work_id": video.get("work_id"),
        "reviewer": reviewer, "outcome": outcome,
        "video_hash": video.get("file_hash", ""), "video_version": video.get("duration_ms"),
        "identity_digest": identity_digest, "audio_duration_ms": audio_duration_ms,
        "checks": checks if checks is not None else video.get("checks", []),
        "duration": duration_info, "note": note, "reviewed_at": now_iso(),
    }
    st.put(PLATE_REVIEW_TABLE, rec["id"], rec, {"video_id": plate_id})
    video["approval_state"] = {"pass": "approved", "fail": "rejected"}.get(outcome, "pending")
    if outcome == "pass":
        video["excess_ms"] = duration_info.get("excess_ms", 0)
    st.put(vd.VIDEO_TABLE, plate_id, video, {"work_id": video.get("work_id")})
    return rec


def check_plate_duration(plate: Dict[str, Any], audio_duration_ms: int,
                         *, strict: bool = False,
                         tolerance_ms: int = PLATE_DURATION_TOLERANCE_MS) -> Dict[str, Any]:
    """底片与最终音频的时长核对。短了必然要加速/截字，属明确禁止。"""
    plate_ms = int(plate.get("duration_ms") or 0)
    diff = plate_ms - int(audio_duration_ms)
    if plate_ms + tolerance_ms < int(audio_duration_ms):
        raise TalkingHeadRejected(
            "PLATE_TOO_SHORT",
            f"底片 {plate_ms}ms 短于最终音频 {audio_duration_ms}ms：覆盖不足只能靠加速或截字，"
            "两者都禁止；须重做底片而不是修音频",
            {"plate_ms": plate_ms, "audio_ms": audio_duration_ms},
        )
    excess = max(0, diff)
    return {
        "plate_ms": plate_ms, "audio_ms": int(audio_duration_ms),
        "delta_ms": diff, "excess_ms": excess,
        "tolerated": abs(diff) <= tolerance_ms,
        "note": ("底片与音频等长" if excess == 0 else
                 f"底片比音频多 {excess}ms：属可裁多余量，作为编辑决定记录"
                 "（provider_lead_ms），不得靠改字幕掩盖偏移"),
    }


def current_plate_review(plate_id: str) -> Optional[Dict[str, Any]]:
    rows = st.all_rows(PLATE_REVIEW_TABLE, "video_id=?", [plate_id])
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("reviewed_at", ""))[-1]


def _resolve_motion(work: Dict[str, Any]) -> Dict[str, Any]:
    from ..avatar import motion as avatar_motion

    intent = work.get("motion_intent") or {}
    if not intent:
        raise TalkingHeadRejected("MOTION_REQUIRED", "M2 必须先从获准动作模板中选择动作")
    try:
        resolved = avatar_motion.resolve_motion_intent(work["identity_id"], intent, route="M2")
    except AssetRejected as error:
        raise TalkingHeadRejected(error.code, error.message, error.detail) from error
    return st.get("motion_references", resolved["motion_ref_id"]) or {}


def _confirmed_audio(work_id: str) -> Dict[str, Any]:
    audio = tts_audio.final_audio(work_id)
    if not audio:
        raise TalkingHeadBlocked("FINAL_AUDIO_REQUIRED", "尚无已确认最终音频：底片时长没有可核对的目标")
    if audio.get("is_mock"):
        raise TalkingHeadRejected("FINAL_AUDIO_IS_MOCK", "占位音频不得驱动底片生成")
    if not audio.get("file_hash"):
        raise TalkingHeadRejected("FINAL_AUDIO_NO_HASH", "最终音频缺少文件哈希")
    if int(audio.get("duration_ms") or 0) <= 0:
        raise TalkingHeadRejected("FINAL_AUDIO_DURATION_INVALID", "最终音频时长必须为正")
    return audio


def preparation(work_id: str, *, variant_id: Optional[str] = None) -> Dict[str, Any]:
    """只校验生成底片的输入，不依赖尚未生成的底片或下游字幕计划。"""
    source = None
    blockers = []
    audio = None
    try:
        work = _deps(work_id)
        source = resolve_plate_source(work, variant_id=variant_id)
        _resolve_motion(work)
        audio = _confirmed_audio(work_id)
    except (TalkingHeadBlocked, TalkingHeadRejected) as error:
        blockers.append({"code": error.code, "message": error.message,
                         "kind": "blocked" if isinstance(error, TalkingHeadBlocked) else "rejected"})
    return {"ok": not blockers, "blockers": blockers, "source": source,
            "audio_asset_id": audio.get("id") if audio else None,
            "audio_duration_ms": audio.get("duration_ms") if audio else None}


def approved_plate_for(work_id: str) -> Optional[Dict[str, Any]]:
    """本作品已审且仍有效的底片。缓存复用必须校验输入哈希。"""
    work = st.get("works", work_id) or {}
    motion_ref_id = (work.get("motion_intent") or {}).get("motion_ref_id")
    if not motion_ref_id:
        return None
    try:
        source = resolve_plate_source(work)
        _resolve_motion(work)
        audio = _confirmed_audio(work_id)
    except (TalkingHeadRejected, TalkingHeadBlocked):
        return None
    rows = [v for v in vd.list_videos(work_id, vd.KIND_PLATE)
            if v.get("approval_state") == "approved"
            and v.get("motion_ref_id") == motion_ref_id
            and v.get("identity_id") == source["identity_id"]
            and v.get("variant_id") == source["variant_id"]
            and v.get("source_asset_id") == source["asset_id"]
            and v.get("source_asset_hash") == source["asset_hash"]
            and v.get("audio_asset_id") == audio["id"]]
    for candidate in sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True):
        review = current_plate_review(candidate["id"])
        if not review or review.get("outcome") != "pass" or vd.hard_failures(candidate):
            continue
        if review.get("video_hash") != candidate.get("file_hash"):
            continue
        try:
            check_plate_duration(candidate, audio["duration_ms"], strict=True)
        except TalkingHeadRejected:
            continue
        return candidate
    return None


def plate_status(work_id: str, *, variant_id: Optional[str] = None) -> Dict[str, Any]:
    plates = vd.list_videos(work_id, vd.KIND_PLATE)
    approved = approved_plate_for(work_id)
    return {
        "work_id": work_id,
        "plate_count": len(plates),
        "approved_plate_id": approved.get("id") if approved else None,
        "approved": bool(approved),
        "preparation": preparation(work_id, variant_id=variant_id),
        "plates": [{"id": p["id"], "duration_ms": p.get("duration_ms"),
                    "approval_state": p.get("approval_state"), "is_mock": p.get("is_mock"),
                    "file_hash": p.get("file_hash", "")} for p in plates],
    }
