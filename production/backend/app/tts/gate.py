"""音频门禁、最终音频确认与下游失效（AC-TTS-03/04、AC-AG-06 的音频段）。

三条不可绕过的约束：

1. **试听片不是正文音频**。`kind` 就是权限：`preview` 无法被确认，
   哪怕它的检查项全绿。
2. **确认是一次人工动作，不是自动放行**。必须显式给出
   `actor_id="user"` 与 `audition_ack=True`。本项目未接入音频检测能力，
   所以"术语读法正确"这件事只能由人听过之后承担；
   门禁不会假装检测过，也不会因为检查项里没有硬失败就自动确认。
3. **失效不是删除**。换声或改正文时，下游产物被标记失效但保留原文件、
   摘要与费用；旧音频的"已确认"被撤销而不是抹掉。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from . import audio as au
from . import provider as pv
from .errors import TtsRejected

AUDIO_REVIEW_TABLE = "audio_reviews"

# 对齐来源白名单：均分时长不在其中，因为均分不是对齐
VALID_ALIGNMENT_SOURCES = ("provider", "forced_alignment", "asr_alignment")


# ---------- 门禁 ----------

def audio_gate(audio_id: str) -> Dict[str, Any]:
    """结构化判定，不抛异常，便于前端逐项展示原因。"""
    audio = au.get_audio(audio_id)
    if not audio:
        return {"ok": False, "outcome": "fail", "audio_id": audio_id,
                "reasons": [f"音频不存在：{audio_id}"]}
    reasons: List[str] = []
    if audio.get("kind") != au.KIND_FULL:
        reasons.append("试听片不能作为最终音频：其文本是试听短句而不是正文")
    for c in audio.get("checks", []):
        if c.get("hard_gate") and c.get("outcome") != "pass":
            reasons.append(f"{c.get('code')}：{c.get('message', '')}")
    reasons.extend(au.is_confirmable(audio)[1] if audio.get("kind") == au.KIND_FULL else [])
    reasons = list(dict.fromkeys(reasons))
    return {"ok": not reasons, "outcome": "pass" if not reasons else "fail",
            "audio_id": audio_id, "kind": audio.get("kind"),
            "is_mock": audio.get("is_mock"),
            "duration_ms": audio.get("duration_ms"),
            "timing_source": audio.get("timing_source"),
            "reasons": reasons}


def require_confirmable(audio_id: str) -> Dict[str, Any]:
    d = audio_gate(audio_id)
    if not d["ok"]:
        raise TtsRejected("AUDIO_HARD_GATE_FAILED", "；".join(d["reasons"]), d)
    return d


def record_audio_review(audio_id: str, reviewer: str, outcome: str,
                        checks: Optional[List[Dict[str, Any]]] = None,
                        note: str = "", connection=None) -> Dict[str, Any]:
    """人工听检记录。reviewer 必须是用户以外的自动结论不得写 pass。"""
    if outcome not in ("pass", "fail", "review", "not_run"):
        raise TtsRejected("AUDIO_REVIEW_OUTCOME_INVALID", f"听检结论非法：{outcome}")
    audio = au.get_audio(audio_id)
    if not audio:
        raise TtsRejected("AUDIO_NOT_FOUND", f"音频不存在：{audio_id}")
    if outcome == "pass" and not reviewer.startswith("user"):
        raise TtsRejected("AUDIO_REVIEW_ACTOR_INVALID",
                          "听检通过只能由用户给出：本项目无音频识别能力，"
                          "系统不产生自动的听检通过结论",
                          {"reviewer": reviewer})
    rec = {
        "id": st.new_id("arv"), "audio_id": audio_id,
        "work_id": audio.get("work_id"),
        "reviewer": reviewer, "outcome": outcome,
        "audio_hash": audio.get("file_hash", ""),
        "duration_ms": audio.get("duration_ms"),
        "checks": checks if checks is not None else audio.get("checks", []),
        "note": note, "reviewed_at": now_iso(),
    }
    if connection is not None:
        st.insert_row(connection, AUDIO_REVIEW_TABLE, rec["id"], rec, {"audio_id": audio_id})
    else:
        st.put(AUDIO_REVIEW_TABLE, rec["id"], rec, {"audio_id": audio_id})
    return rec


def current_audio_review(audio_id: str) -> Optional[Dict[str, Any]]:
    rows = st.all_rows(AUDIO_REVIEW_TABLE, "audio_id=?", [audio_id])
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("reviewed_at", ""))[-1]


# ---------- 确认 ----------

def confirm_final_audio(work_id: str, audio_id: str, *, actor_id: str = "",
                        audition_ack: bool = False, note: str = "") -> Dict[str, Any]:
    with st.transaction() as connection:
        return _confirm_final_audio(work_id, audio_id, actor_id=actor_id,
                                    audition_ack=audition_ack, note=note, connection=connection)


def _confirm_final_audio(work_id: str, audio_id: str, *, actor_id: str,
                         audition_ack: bool, note: str, connection) -> Dict[str, Any]:
    """确认为本次作品的最终音频。确认后时长即为本作品确定时长。"""
    audio = au.get_audio(audio_id)
    if not audio:
        raise TtsRejected("AUDIO_NOT_FOUND", f"音频不存在：{audio_id}")
    if audio.get("work_id") != work_id:
        raise TtsRejected("AUDIO_WORK_MISMATCH",
                          "该音频不属于此作品，不能作为它的最终音频",
                          {"audio_work_id": audio.get("work_id"), "work_id": work_id})
    if audio.get("kind") != au.KIND_FULL:
        raise TtsRejected("AUDIO_PREVIEW_NOT_FINAL",
                          "试听片不能被当作最终音频：它是短句试听，正文并没有被合成",
                          {"audio_id": audio_id, "kind": audio.get("kind")})
    if audio.get("is_mock"):
        raise TtsRejected("AUDIO_IS_MOCK",
                          "Mock 音频不得被确认为最终音频，也不得进入下游口播",
                          {"audio_id": audio_id})
    if not audition_ack:
        raise TtsRejected("AUDIO_AUDITION_REQUIRED",
                          "必须由用户试听后确认：本项目未接入音频检测能力，"
                          "术语读法、音色与完整性只能由人听过之后承担")
    if not actor_id or not actor_id.startswith("user"):
        raise TtsRejected("AUDIO_ACTOR_INVALID",
                          "确认动作的主体必须是用户（选择不能由系统代做）",
                          {"actor_id": actor_id})

    require_confirmable(audio_id)

    # 同一作品只保留一条"已确认"：旧音频保留记录但撤销确认状态
    revoked: List[str] = []
    for row in au.list_audio(work_id, au.KIND_FULL):
        if row.get("confirmed") and row["id"] != audio_id:
            row["confirmed"] = False
            row["revoked_at"] = now_iso()
            row["revoked_reason"] = "被后续确认的音频取代"
            st.insert_row(connection, "audios", row["id"], row, {"work_id": work_id})
            revoked.append(row["id"])

    audio["confirmed"] = True
    audio["approved"] = True
    audio["confirmed_at"] = now_iso()
    audio["confirmed_by"] = actor_id
    audio["frozen_at"] = now_iso()
    st.insert_row(connection, "audios", audio_id, audio, {"work_id": work_id})

    review = record_audio_review(audio_id, actor_id, "pass",
                                 note=note or "用户试听确认；作为本作品最终音频", connection=connection)

    work = st.get("works", work_id) or {}
    work["final_audio_id"] = audio_id
    work["final_audio_duration_ms"] = audio["duration_ms"]
    # 下游必须沿用这条音频的生成计划，不能漏绑或取另一份最新计划。
    work["performance_plan_id"] = audio.get("performance_plan_id")
    st.insert_row(connection, "works", work_id, work)
    return {
        "ok": True, "audio": audio, "audio_review_id": review["id"],
        "revoked_previous": revoked,
        "duration_ms": audio["duration_ms"],
        "message": f"最终音频已冻结，确定时长 {audio['duration_ms']}ms；"
                   "下游口播与字幕以此为唯一音轨",
    }


# ---------- 对齐 ----------

def register_alignment(audio_id: str, *, source: str, method: str,
                       words: List[Dict[str, Any]], rule_version: str = "",
                       note: str = "") -> Dict[str, Any]:
    """登记对齐产物。均分时长永远不能走进来。"""
    if source not in VALID_ALIGNMENT_SOURCES:
        raise TtsRejected(
            "ALIGNMENT_SOURCE_INVALID",
            f"对齐来源 {source} 不合法：只有 provider/forced_alignment/asr_alignment "
            "可登记；均分时长或未验证逐句时长不算对齐",
            {"source": source, "allowed": list(VALID_ALIGNMENT_SOURCES)},
        )
    if not method:
        raise TtsRejected("ALIGNMENT_METHOD_MISSING", "登记对齐必须说明所用方法")
    audio = au.get_audio(audio_id)
    if not audio:
        raise TtsRejected("AUDIO_NOT_FOUND", f"音频不存在：{audio_id}")
    ok, reasons = pv.validate_word_timings(
        words, duration_ms=int(audio.get("duration_ms") or 0))
    if not ok:
        raise TtsRejected("ALIGNMENT_WORDS_INVALID",
                          "对齐结果未通过确定性校验，已拒绝登记：" + "；".join(reasons),
                          {"reasons": reasons})

    rec = {
        "id": st.new_id("align"), "audio_asset_id": audio_id,
        "audio_digest": audio.get("file_hash", ""), "source": source,
        "method": method, "rule_version": rule_version, "words": words,
        "note": note or "对齐产物版本；改变时间轴时另行登记新版本", "created_at": now_iso(),
    }
    st.put("alignment_records", rec["id"], rec, {"audio_id": audio_id})
    # 音频上的对齐来源随之更新：由"未对齐"变为可追溯的具体来源
    audio["timing_source"] = source
    audio["alignment_id"] = rec["id"]
    st.put("audios", audio_id, audio, {"work_id": audio.get("work_id")})
    return rec


def alignment_status(work_id: str) -> Dict[str, Any]:
    final = au.final_audio(work_id)
    if not final:
        return {"work_id": work_id, "aligned": False, "word_count": 0,
                "reason": "尚无已确认的最终音频，字幕对齐不启动",
                "claimable": False}
    rows = st.all_rows("alignment_records", "audio_id=?", [final["id"]])
    if not rows or final.get("timing_source") not in VALID_ALIGNMENT_SOURCES:
        return {"work_id": work_id, "audio_id": final["id"], "aligned": False,
                "word_count": 0, "claimable": False,
                "reason": "无可用字词时间戳：不得宣称字词对齐已完成；"
                          "由后期对齐后另行登记对齐产物版本"}
    latest = sorted(rows, key=lambda r: r.get("created_at", ""))[-1]
    return {"work_id": work_id, "audio_id": final["id"], "aligned": True,
            "word_count": len(latest.get("words", [])),
            "source": latest.get("source"), "method": latest.get("method"),
            "rule_version": latest.get("rule_version"),
            "alignment_id": latest.get("id"), "claimable": True}


# ---------- 下游失效 ----------

def _invalidate_inputs(work: dict, reason: str, change_type: str, connection) -> Dict[str, Any]:
    from .. import lineage
    work_id = work["id"]
    affected = lineage.invalidate(work_id=work_id, change_type=change_type,
                                  actor="user", connection=connection)
    revoked = []
    for row in au.list_audio(work_id):
        if row.get("confirmed"):
            revoked.append(row["id"])
        row.update(confirmed=False, approved=False, invalidated=True,
                   invalidated_at=now_iso(), invalidated_reason=reason,
                   revoked_at=now_iso(), revoked_reason=reason)
        st.insert_row(connection, "audios", row["id"], row, {"work_id": work_id})
    native_video_ids = set()
    for row in st.all_rows("videos", "work_id=?", [work_id]):
        if row.get("kind") == "original" and row.get("generation_mode") == "image_prompt_native_audio":
            native_video_ids.add(row.get("id"))
            continue
        if row.get("kind") == "original":
            row.update(confirmed=False, approval_state="revoked", revoked_at=now_iso(), revoked_reason=reason)
            row["checks"] = [c for c in row.get("checks", []) if c.get("code") != "TTS_INPUT_CHANGED"]
            row["checks"].append({"code": "TTS_INPUT_CHANGED", "hard_gate": True,
                                  "outcome": "fail", "message": reason + "；须使用新音轨重新生成口播"})
            st.insert_row(connection, "videos", row["id"], row, {"work_id": work_id})
    for key in ("final_audio_id", "final_audio_duration_ms", "performance_plan_id"):
        work[key] = None
    if work.get("video_asset_id") not in native_video_ids:
        work["video_asset_id"] = None
    work["tts_controls_revision"] = int(work.get("tts_controls_revision") or 0) + 1
    work["version"] = int(work.get("version") or 1) + 1
    st.insert_row(connection, "works", work_id, work)
    task = st.get("tasks", work.get("current_task_id") or "")
    if task and work.get("route") != "WAN_3_0" and task.get("stage") in ("tts", "talking_head", "postprocess", "video_review", "publish_package"):
        task.update(stage="tts", state="waiting_selection")
        task["pending_selection_ids"] = list(dict.fromkeys([*task.get("pending_selection_ids", []), "sel_audio"]))
        st.insert_row(connection, "tasks", task["id"], task, {"work_id": work_id})
    return {"ok": True, "revoked_audio": revoked, "invalidated": affected["invalidated"],
            "note": "参数已变更，旧音频、字幕与成片失效；历史文件和费用记录保留"}


def set_work_controls(work_id: str, controls: Dict[str, Any]) -> Dict[str, Any]:
    from . import voices as vc
    normalized = vc.normalize_controls(controls)
    with st.transaction() as connection:
        work = st.get("works", work_id)
        if not work:
            raise TtsRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
        voice = vc.get_voice(work.get("voice_asset_id"))
        before = vc.effective_profile(work, voice) if voice else work.get("tts_controls", {})
        work["tts_controls"] = normalized
        after = vc.effective_profile(work, voice) if voice else normalized
        if before != after:
            _invalidate_inputs(work, "作品语音参数已变化", "tts_controls", connection)
        else:
            st.insert_row(connection, "works", work_id, work)
        return work


def on_voice_change(work_id: str, new_voice_id: str) -> Dict[str, Any]:
    voice = st.get("voices", new_voice_id)
    if not voice or voice.get("approval_state") != "approved":
        raise TtsRejected("VOICE_NOT_APPROVED", "未审批的音色不能用于生产")
    with st.transaction() as connection:
        work = st.get("works", work_id)
        if not work:
            raise TtsRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
        previous = work.get("voice_asset_id")
        if previous == new_voice_id:
            return {"ok": True, "voice_asset_id": new_voice_id, "revoked_audio": [], "invalidated": []}
        work["voice_asset_id"] = new_voice_id
        result = _invalidate_inputs(work, f"音色由 {previous} 变更为 {new_voice_id}", "voice", connection)
        return {**result, "previous_voice_asset_id": previous, "voice_asset_id": new_voice_id}


def on_script_change(work_id: str) -> Dict[str, Any]:
    with st.transaction() as connection:
        work = st.get("works", work_id)
        if not work:
            raise TtsRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
        return _invalidate_inputs(work, "正文变更", "script_text", connection)


def gate_summary(work_id: str) -> Dict[str, Any]:
    """作品的语音环节总览，供前端与验收脚本直接读取。"""
    work = st.get("works", work_id) or {}
    audios = au.list_audio(work_id)
    final = au.final_audio(work_id)
    return {
        "work_id": work_id,
        "voice_asset_id": work.get("voice_asset_id"),
        "full_audio_count": len([a for a in audios if a.get("kind") == au.KIND_FULL]),
        "preview_count": len([a for a in audios if a.get("kind") == au.KIND_PREVIEW]),
        "final_audio_id": final.get("id") if final else None,
        "final_audio_duration_ms": final.get("duration_ms") if final else None,
        "audio_confirmed": bool(final),
        "alignment": alignment_status(work_id),
    }
