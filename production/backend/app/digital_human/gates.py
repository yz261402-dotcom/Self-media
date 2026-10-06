"""口播原片门禁、音轨保持核对与最终视频确认（AC-TH-01/02/03/04/06）。

这个模块要顶住三种"看起来通过了"：

1. **嘴动了就算对口型。** AC-TH-02 明确：像素变了、RMS 高相关都不能单独通过。
   因此对口型必须由人逐帧/慢放抽查给出结论，且每条结论要绑定资产与毫秒区间。
2. **音轨高相关就算没换声。** 音轨保持是独立门禁（核对是否仍是那一条已确认 TTS），
   它**不能**替代嘴型、身份与身体质量；反过来也一样。
3. **"全部通过"但没有任何引用。** 每条 pass 必须绑定对象摘要与定位
   （asset + 毫秒区间 / 报告 id），否则拒收——没有引用的通过就是没有通过。

此外：本项目未接入视频理解能力，所以任何**视觉**结论都不能由系统自动产生。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from ..tts import audio as tts_audio
from . import videos as vd
from .errors import TalkingHeadRejected

VIDEO_REVIEW_TABLE = "video_reviews"
AUDIO_TRACK_TABLE = "audio_track_checks"

# 必须逐项给出的四个维度。缺少任一项就不是"审核过了"，而是"没审"。
DIMENSIONS = ("lip_sync", "identity", "body_motion", "expression")
DIMENSION_LABELS = {
    "lip_sync": "口型/音素同步",
    "identity": "人物身份保持",
    "body_motion": "身体动作与颈肩构图",
    "expression": "眨眼/眉眼/表情自然度",
}
# 音轨偏移容差：超出即需人工判断，不默认归零
AUDIO_OFFSET_TOLERANCE_MS = 50


def _current_review(video_id: str) -> Optional[Dict[str, Any]]:
    rows = st.all_rows(VIDEO_REVIEW_TABLE, "video_id=?", [video_id])
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("reviewed_at", ""))[-1]


#: 门禁三态。`not_run` 与 `fail` 必须分开：把"还没人审"报成"审了没过"，
#: 复核者会以为已经有人给出过否决结论，从而错判该找谁、要不要重做。
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_NOT_RUN = "not_run"


def video_gate(video_id: str) -> Dict[str, Any]:
    """结构化判定，不抛异常，便于前端逐项展示原因。

    返回 `outcome` 三态：
    - `pass`    四维审核通过且音轨保持核对通过；
    - `fail`    有确定性硬失败（占位产物/缺视频轨/无哈希/时长非正），或已审但未过，
                或音轨核对结论为未通过/已失效；
    - `not_run` 尚无人工审核结论，或音轨尚未核对——"没审"不等于"没过"。
    """
    video = vd.get_video(video_id)
    if not video:
        # 视频不存在是确定性失败：没有可审对象，不是"还没审"。
        return {"ok": False, "outcome": OUTCOME_FAIL, "video_id": video_id,
                 "reasons": [f"视频不存在：{video_id}"]}
    if video.get("generation_mode") == "image_prompt_native_audio":
        from .wan_native import native_gate
        return native_gate(video)
    if video.get("generation_mode") == "storyboard_native_audio":
        from ..short_video.approval import video_gate as storyboard_gate
        return storyboard_gate(video)

    reasons: List[str] = [f["message"] for f in vd.hard_failures(video)]
    settled_fail = bool(reasons)
    if video.get("kind") != vd.KIND_ORIGINAL:
        reasons.append("动态底片不是交付原片：它没有最终音频的口型")
        settled_fail = True

    review = _current_review(video_id)
    dims: Dict[str, str] = (review or {}).get("dimensions", {})
    review_state = OUTCOME_NOT_RUN
    if not review:
        reasons.append("缺少人工原片审核结论（口型/身份/身体/表情四项）")
    else:
        review_state = OUTCOME_PASS
        if review.get("video_hash") != video.get("file_hash"):
            review_state = OUTCOME_FAIL
            reasons.append("审核结论对应的文件哈希与当前文件不一致，审核已失效")
        for d in DIMENSIONS:
            if dims.get(d) != "pass":
                review_state = OUTCOME_FAIL
                reasons.append(f"{DIMENSION_LABELS[d]}未通过（{dims.get(d, 'not_run')}）")
        if review.get("outcome") != "pass":
            review_state = OUTCOME_FAIL
            reasons.append(f"审核总评不是通过（{review.get('outcome')}）")

    track = audio_track_status(video_id)
    if not track.get("checked"):
        track_state = OUTCOME_NOT_RUN
        reasons.append("未核对音轨保持：无法确认原片音轨仍是已确认的最终 TTS")
    else:
        # 记录在、但对当前文件已失效（invalidated）同样属于"核对未通过"，
        # 不能因为它的取值不是字面 "fail" 就滑到通过。
        raw_track = track.get("outcome") or OUTCOME_FAIL
        track_state = OUTCOME_PASS if raw_track == OUTCOME_PASS else OUTCOME_FAIL
        if track_state != OUTCOME_PASS:
            reasons.append(f"音轨保持未通过：{track.get('reason', '')}")

    if settled_fail or OUTCOME_FAIL in (review_state, track_state):
        outcome = OUTCOME_FAIL
    elif OUTCOME_NOT_RUN in (review_state, track_state):
        outcome = OUTCOME_NOT_RUN
    else:
        outcome = OUTCOME_PASS

    reasons = list(dict.fromkeys(reasons))
    return {
        "ok": not reasons, "outcome": outcome,
        "review_state": review_state, "audio_track_state": track_state,
        "video_id": video_id, "kind": video.get("kind"), "route": video.get("route"),
        "is_mock": video.get("is_mock"), "duration_ms": video.get("duration_ms"),
        "dimensions": dims, "audio_track": track, "review_id": (review or {}).get("id"),
        "reasons": reasons,
    }


def require_gate(video_id: str) -> Dict[str, Any]:
    d = video_gate(video_id)
    if not d["ok"]:
        raise TalkingHeadRejected("VIDEO_HARD_GATE_FAILED", "；".join(d["reasons"]), d)
    return d


# ---------- 人工原片审核 ----------

def record_video_review(video_id: str, *, reviewer: str, outcome: str,
                        dimensions: Dict[str, str],
                        references: Optional[Dict[str, Any]] = None,
                        issues: Optional[List[Dict[str, Any]]] = None,
                        note: str = "") -> Dict[str, Any]:
    """人工观感审核。通过必须由用户给出，且每条结论都要有引用。"""
    video = vd.get_video(video_id)
    if not video:
        raise TalkingHeadRejected("VIDEO_NOT_FOUND", f"视频不存在：{video_id}")
    if outcome not in ("pass", "fail", "review", "not_run"):
        raise TalkingHeadRejected("VIDEO_REVIEW_OUTCOME_INVALID", f"审核结论非法：{outcome}")
    unknown = [k for k in (dimensions or {}) if k not in DIMENSIONS]
    if unknown:
        raise TalkingHeadRejected(
            "VIDEO_REVIEW_DIMENSION_UNKNOWN",
            f"未定义的审核维度 {unknown}；只接受 {list(DIMENSIONS)}",
            {"unknown": unknown},
        )
    refs = references or {}

    if outcome == "pass":
        if not reviewer.startswith("user"):
            raise TalkingHeadRejected(
                "VIDEO_REVIEW_ACTOR_INVALID",
                "原片审核通过只能由用户给出：本项目未接入视频理解能力，"
                "系统不产生自动的口型/身份/表情结论",
                {"reviewer": reviewer},
            )
        missing = [d for d in DIMENSIONS if dimensions.get(d) != "pass"]
        if missing:
            raise TalkingHeadRejected(
                "VIDEO_REVIEW_INCOMPLETE",
                f"以下维度不是通过：{[(d, dimensions.get(d, 'not_run')) for d in missing]}；"
                "不同维度之间不能互相顶替",
                {"missing": missing},
            )
        no_ref = [d for d in DIMENSIONS if not refs.get(d)]
        if no_ref:
            raise TalkingHeadRejected(
                "REVIEW_WITHOUT_EVIDENCE",
                f"维度 {no_ref} 判为通过但没有绑定证据（资产与毫秒区间/报告 id）："
                "无引用的通过等于没有通过",
                {"missing_references": no_ref},
            )
        for d in DIMENSIONS:
            r = refs.get(d) or {}
            if not (r.get("asset_id") or r.get("report_id") or r.get("reference")):
                raise TalkingHeadRejected(
                    "REVIEW_REFERENCE_INVALID",
                    f"维度 {d} 的证据没有指向任何对象",
                    {"dimension": d, "reference": r},
                )
            if d == "lip_sync" and str(r.get("method", "")).lower() in (
                    "waveform_correlation", "rms_correlation"):
                raise TalkingHeadRejected(
                    "LIPSYNC_PROOF_INVALID",
                    "口型结论不能用波形相关或 RMS 代替：音量包络相近但音素不同的负例"
                    "恰好能被这类指标判成通过",
                    {"method": r.get("method")},
                )
        reasons = [f["message"] for f in vd.hard_failures(video)]
        if reasons:
            raise TalkingHeadRejected("VIDEO_HARD_GATE_FAILED", "；".join(reasons))

    rec = {
        "id": st.new_id("vrev"), "video_id": video_id, "work_id": video.get("work_id"),
        "reviewer": reviewer, "outcome": outcome, "dimensions": dict(dimensions or {}),
        "references": refs, "video_hash": video.get("file_hash", ""),
        "duration_ms": video.get("duration_ms"), "issues": issues or [],
        "rule_version": "t05-1", "note": note, "reviewed_at": now_iso(),
    }
    st.put(VIDEO_REVIEW_TABLE, rec["id"], rec, {"video_id": video_id})
    if outcome == "pass":
        video["approval_state"] = "approved"
        st.put(vd.VIDEO_TABLE, video_id, video, {"work_id": video.get("work_id")})
    elif outcome == "fail":
        video["approval_state"] = "rejected"
        st.put(vd.VIDEO_TABLE, video_id, video, {"work_id": video.get("work_id")})
    return rec


# ---------- 音轨保持 ----------

def record_audio_track_check(video_id: str, *, audio_asset_id: str, method: str,
                             outcome: str, measured_offset_ms: Optional[int] = None,
                             loudness_delta_db: Optional[float] = None,
                             evidence: str = "", note: str = "") -> Dict[str, Any]:
    """核对原片音轨是否仍是那一条已确认最终 TTS。

    必须实测偏移：历史样本的 0ms 不能被推定为本任务也是 0ms。
    """
    video = vd.get_video(video_id)
    if not video:
        raise TalkingHeadRejected("VIDEO_NOT_FOUND", f"视频不存在：{video_id}")
    if not audio_asset_id:
        raise TalkingHeadRejected(
            "AUDIO_TRACK_UNBOUND",
            "音轨保持必须绑定具体音频资产：否则换声无法被发现")
    if outcome == "pass":
        if measured_offset_ms is None:
            raise TalkingHeadRejected(
                "AUDIO_TRACK_OFFSET_MISSING",
                "音轨保持判为通过却没有实测偏移：不得默认 0ms，"
                "历史样本的 0ms 不能推定本任务",
            )
        if abs(int(measured_offset_ms)) > AUDIO_OFFSET_TOLERANCE_MS:
            raise TalkingHeadRejected(
                "AUDIO_TRACK_OFFSET_EXCEEDED",
                f"实测偏移 {measured_offset_ms}ms 超过容差 {AUDIO_OFFSET_TOLERANCE_MS}ms："
                "不得靠改字幕掩盖，须回到原片处理",
                {"measured_offset_ms": measured_offset_ms},
            )
        if not evidence:
            raise TalkingHeadRejected("AUDIO_TRACK_NO_EVIDENCE",
                                     "音轨核对结论必须附证据来源")
    rec = {
        "id": st.new_id("atc"), "video_id": video_id, "work_id": video.get("work_id"),
        "audio_asset_id": audio_asset_id, "method": method, "outcome": outcome,
        "measured_offset_ms": measured_offset_ms, "loudness_delta_db": loudness_delta_db,
        "video_hash": video.get("file_hash", ""), "evidence": evidence,
        "note": note, "checked_at": now_iso(),
    }
    st.put(AUDIO_TRACK_TABLE, rec["id"], rec, {"video_id": video_id})
    return rec


def audio_track_status(video_id: str) -> Dict[str, Any]:
    video = vd.get_video(video_id) or {}
    if video.get("generation_mode") == "image_prompt_native_audio":
        from .wan_native import native_gate
        return native_gate(video)["audio_track"]
    rows = st.all_rows(AUDIO_TRACK_TABLE, "video_id=?", [video_id])
    if not rows:
        return {"checked": False, "outcome": "not_run",
                "reason": "尚无音轨核对记录"}
    rec = sorted(rows, key=lambda r: r.get("checked_at", ""))[-1]
    video = vd.get_video(video_id) or {}
    final = tts_audio.final_audio(video.get("work_id") or "")
    ok = rec.get("outcome") == "pass"
    reason = ""
    if ok and rec.get("video_hash") != video.get("file_hash"):
        ok, reason = False, "音轨核对对应的文件哈希与当前文件不一致"
    if ok and rec.get("audio_asset_id") != video.get("audio_asset_id"):
        ok, reason = False, "音轨核对绑定的音频与视频登记的音频不一致"
    if ok and final and rec.get("audio_asset_id") != final.get("id"):
        ok, reason = False, "音轨核对绑定的音频不是本作品当前已确认的最终音频"
    # 注意：核对记录本身写着 pass，不代表它对**当前这个文件**仍然成立。
    # 一旦绑定对象被替换（换了文件、换了音频），旧结论必须作废，
    # 而不是继续返回 pass——否则"审核记录在、结论却早已失效"会被当成通过。
    return {"checked": True, "outcome": "pass" if ok else "invalidated",
            "recorded_outcome": rec.get("outcome"),
            "reason": reason, "measured_offset_ms": rec.get("measured_offset_ms"),
            "method": rec.get("method"), "audio_asset_id": rec.get("audio_asset_id"),
            "final_audio_id": final.get("id") if final else None,
            "record_id": rec.get("id")}


# ---------- 确认 ----------

def confirm_final_video(work_id: str, video_id: str, *, actor_id: str = "",
                        review_ack: bool = False, note: str = "") -> Dict[str, Any]:
    video = vd.get_video(video_id)
    if not video:
        raise TalkingHeadRejected("VIDEO_NOT_FOUND", f"视频不存在：{video_id}")
    if video.get("work_id") != work_id:
        raise TalkingHeadRejected("VIDEO_WORK_MISMATCH", "该视频不属于此作品")
    if video.get("kind") != vd.KIND_ORIGINAL:
        raise TalkingHeadRejected("PLATE_NOT_FINAL",
                                 "动态底片不能被确认为最终原片：它没有最终音频的口型")
    if video.get("is_mock"):
        raise TalkingHeadRejected("VIDEO_IS_MOCK", "占位产物不得被确认为最终原片")
    if not review_ack:
        raise TalkingHeadRejected(
            "VIDEO_REVIEW_ACK_REQUIRED",
            "必须由用户观看原片后确认：口型、身份、表情与外框穿帮只能由人判断",
        )
    if not actor_id or not actor_id.startswith("user"):
        raise TalkingHeadRejected("VIDEO_ACTOR_INVALID", "确认动作的主体必须是用户")
    require_gate(video_id)

    revoked: List[str] = []
    for row in vd.list_videos(work_id, vd.KIND_ORIGINAL):
        if row.get("confirmed") and row["id"] != video_id:
            vd.revoke(row["id"], "被后续确认的原片取代")
            revoked.append(row["id"])
    video["confirmed"] = True
    video["approval_state"] = "approved"
    video["confirmed_at"] = now_iso()
    video["confirmed_by"] = actor_id
    video["frozen_at"] = now_iso()
    st.put(vd.VIDEO_TABLE, video_id, video, {"work_id": work_id})
    work = st.get("works", work_id) or {}
    work["video_asset_id"] = video_id
    st.put("works", work_id, work)
    return {"ok": True, "video": video, "revoked_previous": revoked,
            "duration_ms": video["duration_ms"],
            "message": f"口播原片已冻结，时长 {video['duration_ms']}ms；"
                       "字幕与后期以此为唯一时间轴来源"}


# ---------- 下游失效 ----------

def on_motion_change(work_id: str, motion_ref_id: str) -> Dict[str, Any]:
    """更换动作仅撤销本作品 M2 下游；音频、视频字节及旧审核记录保留。"""
    from .. import lineage

    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    affected = lineage.invalidate(work_id=work_id, change_type="motion",
                                  source_artifact_id=motion_ref_id, actor="user")
    revoked = []
    for row in vd.list_videos(work_id):
        if row.get("route") == "M2":
            vd.revoke(row["id"], f"动作已改为 {motion_ref_id}，须重新生成并审核")
            revoked.append(row["id"])
    if work.get("video_asset_id") in revoked:
        work["video_asset_id"] = None
        st.put("works", work_id, work)
    return {"invalidated_videos": revoked, "invalidated": affected["invalidated"]}


def on_route_change(work_id: str, new_route: str) -> Dict[str, Any]:
    """切换路线：原片与底片失效，但已确认音频可复用（不重合成声音）。"""
    from .. import lineage

    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    previous = work.get("route")
    affected = lineage.invalidate(work_id=work_id, change_type="motion",
                                  source_artifact_id=new_route, actor="user")
    revoked = []
    for row in vd.list_videos(work_id):
        if row.get("confirmed"):
            revoked.append(row["id"])
        row["approval_state"] = "revoked" if row.get("approval_state") == "approved" \
            else row.get("approval_state")
        row["confirmed"] = False
        row["invalidated_reason"] = f"路线由 {previous} 变更为 {new_route}"
        st.put(vd.VIDEO_TABLE, row["id"], row, {"work_id": work_id})
    work["route"] = new_route
    work["video_asset_id"] = None
    st.put("works", work_id, work)
    return {"ok": True, "previous_route": previous, "route": new_route,
            "invalidated_videos": [r["id"] for r in vd.list_videos(work_id)],
            "revoked_confirmed": revoked,
            "invalidated": affected["invalidated"],
            "note": "切换路线需重新核验人物、能力与预算；已确认最终音频保持不变，不重新合成声音"}


def gate_summary(work_id: str) -> Dict[str, Any]:
    work = st.get("works", work_id) or {}
    original = vd.final_video(work_id)
    plates = vd.list_videos(work_id, vd.KIND_PLATE)
    return {
        "work_id": work_id,
        "route": work.get("route"),
        "route_selection_id": work.get("route_selection_id"),
        "plate_count": len(plates),
        "original_count": len(vd.list_videos(work_id, vd.KIND_ORIGINAL)),
        "final_video_id": original.get("id") if original else None,
        "original_confirmed": bool(original),
        "gate": video_gate(original["id"]) if original else None,
        "note": "无已确认原片时 gate 为 null：不把『尚未审核』写成通过",
    }
