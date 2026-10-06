"""T04 表达编译（AC-TTS-02、AC-AG-03）。

覆盖实施计划点名的负例：表达范围越界、标签误读、缺字/重字、不支持情绪，
以及"计划冻结后被改动"和"版本错配"这两类只有在提交媒体前拦住才有意义的问题。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.agents import contracts as ac  # noqa: E402
from app.contracts import now_iso  # noqa: E402
from app.tts import compiler as cp  # noqa: E402
from app.tts import plan as pl  # noqa: E402
from app.tts.errors import TtsRejected  # noqa: E402

PRESET_VOICE = "voice_minimax_warm_bestie_v1"
T1 = "2026-01-01T00:00:00+00:00"
T2 = "2026-01-02T00:00:00+00:00"


def _seg(seg_id: str, text: str, emotion: str = "calm", pace: str = "normal",
         emphasis=None, motion: str = "neutral") -> dict:
    return {"segment_id": seg_id, "text": text, "emotion": emotion, "pace": pace,
            "emphasis": emphasis or [], "motion": motion}


def _set_script_text(work, text: str) -> str:
    """把作品的采用稿正文改成 text。

    编译器的正文来源是**稿件**而不是计划：所以"计划里写什么"必须先在稿件里成立，
    否则测出来的永远只是 PLAN_SCRIPT_TEXT_MISMATCH，掩盖了真正想验证的分支。
    """
    batch = st.get("script_batches", work["id"])
    for c in batch["candidates"]:
        if c["id"] == work["script_version_id"]:
            c["text"] = text
    st.put("script_batches", work["id"], batch)
    return text


def _frozen(work, script_text: str, segments=None, *, voice_asset_id=PRESET_VOICE,
            identity_id=None, created_at=T1, digest=None) -> dict:
    _set_script_text(work, script_text)
    w = st.get("works", work["id"])
    plan = {"id": st.new_id("plan"), "work_id": work["id"],
            "script_version_id": w["script_version_id"],
            "script_digest": ac.digest(script_text),
            "identity_id": w.get("identity_id") if identity_id is None else identity_id,
            "voice_asset_id": voice_asset_id,
            "segments": segments if segments is not None else pl.derive_segments(script_text),
            "target_duration_ms": 60000, "created_at": created_at}
    plan["plan_digest"] = digest if digest is not None else cp.build_plan_digest(plan)
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    return plan


# ---------- 单一分段来源与逐字一致 ----------

def test_spoken_text_equals_script_text_exactly(work, script_text, plan):
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["spoken_text"] == script_text
    assert out["source_text"] == script_text
    assert "派生视图" in out["note"]


def test_segment_offsets_are_contiguous_and_cover_text(work, script_text, plan):
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    cursor = 0
    for s in out["segments"]:
        assert s["char_start"] == cursor
        assert s["char_end"] - s["char_start"] == len(s["text"])
        cursor = s["char_end"]
    assert cursor == len(script_text)


def test_stale_nested_vector_fixture(work, script_text):
    """多分段的偏移必须累计（第二段起点等于第一段长度），不能每段都从 0 起。"""
    segs = [_seg("seg_1", "第一段内容。"), _seg("seg_2", "第二段内容。"),
            _seg("seg_3", "第三段内容。")]
    plan = _frozen(work, "第一段内容。第二段内容。第三段内容。", segs)
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert [s["char_start"] for s in out["segments"]] == [0, 6, 12]


# ---------- 计划冻结与版本一致性 ----------

def test_plan_without_digest_rejected(work, script_text):
    plan = _frozen(work, script_text)
    plan["plan_digest"] = ""
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_NOT_FROZEN"


def test_plan_modified_after_freeze_rejected(work, script_text):
    plan = _frozen(work, script_text)
    plan["segments"][0]["text"] = "被改过的正文"
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_DIGEST_MISMATCH"


def test_stale_plan_rejected(work, script_text):
    old = _frozen(work, script_text, created_at=T1)
    _frozen(work, script_text, created_at=T2)
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(old["id"], PRESET_VOICE)
    assert e.value.code == "STALE_PLAN"


def test_plan_bound_to_other_voice_rejected(work, script_text):
    plan = _frozen(work, script_text, voice_asset_id="voice_someone_else")
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "VOICE_SCOPE_MISMATCH"


def test_request_voice_must_be_work_voice(work, script_text):
    w = dict(work)
    w["voice_asset_id"] = "voice_other"
    st.put("works", work["id"], w)
    plan = _frozen(work, script_text, voice_asset_id=PRESET_VOICE)
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "VOICE_SCOPE_MISMATCH"


def test_identity_mismatch_rejected(work, script_text):
    plan = _frozen(work, script_text, identity_id="av_someone_else")
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_IDENTITY_MISMATCH"


def test_plan_script_digest_mismatch_rejected(work, script_text):
    plan = _frozen(work, script_text)
    plan["script_digest"] = ac.digest("另一版正文")
    plan["plan_digest"] = cp.build_plan_digest(plan)
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_SCRIPT_DIGEST_MISMATCH"


# ---------- 缺字 / 重字 ----------

def test_missing_character_rejected_with_diagnosis(work):
    text = "检索质量决定回答质量。"
    plan = _frozen(work, text, [_seg("seg_1", "检索质量决定回答质。")])
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_SCRIPT_TEXT_MISMATCH"
    assert "量" in "".join(e.value.detail["missing"])


def test_duplicated_character_rejected_with_diagnosis(work):
    text = "检索质量决定回答质量。"
    plan = _frozen(work, text, [_seg("seg_1", "检索质量决定回答质量量。")])
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_SCRIPT_TEXT_MISMATCH"
    assert "量" in "".join(e.value.detail["extra"])


def test_duplicate_segment_id_rejected(work):
    text = "第一段。第二段。"
    plan = _frozen(work, text, [_seg("seg_1", "第一段。"), _seg("seg_1", "第二段。")])
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code == "PLAN_SEGMENT_ID_DUPLICATED"


def test_empty_segments_rejected(work, script_text):
    plan = _frozen(work, script_text, [])
    with pytest.raises(TtsRejected) as e:
        cp.compile_speech(plan["id"], PRESET_VOICE)
    assert e.value.code in ("PLAN_SEGMENTS_EMPTY", "PLAN_SCRIPT_TEXT_MISMATCH")


# ---------- 情绪：支持 / 不支持 / 降级留证 ----------

def test_calm_is_supported_exactly(work):
    text = "这一段平静陈述。"
    plan = _frozen(work, text, [_seg("seg_1", text, emotion="calm")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["segments"][0]["emotion_applied"] == "calm"
    # 断言只针对情绪这一维：语速 normal 同样会产出"exact"条目（见
    # test_pace_normal_is_exact_slow_fast_are_approximate），把它算进来
    # 会让"情绪是否精确支持"这个断言被无关维度污染。
    assert [c["applied_as"] for c in out["controls"]["supported"]
            if c["control"] == "emotion"] == ["exact"]
    assert out["controls"]["unsupported"] == []


def test_segment_emotion_is_reported_unapplied_by_global_adapter(work):
    """单次全局请求不假装已应用分段情绪；全局控制是实际请求的依据。"""
    text = "这一段想表达兴奋。"
    plan = _frozen(work, text, [_seg("seg_1", text, emotion="excited")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["segments"][0]["emotion_applied"] == "calm"
    unsupported = [c for c in out["controls"]["unsupported"] if c["control"] == "emotion"]
    assert len(unsupported) == 1
    assert unsupported[0]["requested"] == "excited"
    assert unsupported[0]["applied"] == "calm"
    assert unsupported[0]["applied_as"] == "not_applied"
    assert out["control_summary"]["unsupported"] >= 1


def test_segment_emotion_difference_reports_global_request_limit(work):
    text = "这一段想表达开心。"
    plan = _frozen(work, text, [_seg("seg_1", text, emotion="happy")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    entry = [c for c in out["controls"]["unsupported"] if c["control"] == "emotion"][0]
    assert entry["resolved_to"] == "happy"
    assert entry["applied"] == "calm"
    assert "全局" in entry["reason"]


def test_emotion_outside_provider_enum_reported(work):
    text = "这一段情绪词很奇怪。"
    plan = _frozen(work, text, [_seg("seg_1", text, emotion="薛定谔的平静")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    entry = [c for c in out["controls"]["unsupported"] if c["control"] == "emotion"][0]
    assert "不在供应商情绪枚举内" in entry["reason"]


# ---------- 语速：exact / approximate / 降级 ----------

def test_segment_pace_changes_are_not_claimed_applied(work):
    text = "第一段。第二段。第三段。"
    plan = _frozen(work, text, [_seg("seg_1", "第一段。", pace="normal"),
                                _seg("seg_2", "第二段。", pace="slow"),
                                _seg("seg_3", "第三段。", pace="fast")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    paces = {c["segment_id"]: c for c in out["controls"]["supported"]
             + out["controls"]["unsupported"] if c["control"] == "pace"}
    assert paces["seg_1"]["applied_as"] == "exact"
    assert paces["seg_2"]["applied_as"] == "not_applied"
    assert paces["seg_3"]["applied_as"] == "not_applied"
    assert out["segments"][1]["speed"] == out['provider_params']['speed']
    assert out["segments"][2]["speed"] == out['provider_params']['speed']


def test_unknown_pace_falls_back_to_baseline_and_is_recorded(work):
    text = "这一段语速档位写错了。"
    plan = _frozen(work, text, [_seg("seg_1", text, pace="turbo")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    entry = [c for c in out["controls"]["unsupported"] if c["control"] == "pace"][0]
    assert entry["applied"] == 1.0
    assert out["segments"][0]["speed"] == 1.0


# ---------- 重音 / 停顿 / motion：不支持就说不支持 ----------

def test_emphasis_reported_unsupported_and_never_injected(work):
    """重音没有内联能力时明示不支持；不得用 SSML 绕过，也不得改动正文。"""
    text = "检索质量决定回答质量。"
    plan = _frozen(work, text, [_seg("seg_1", text, emphasis=["检索"])])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    entry = [c for c in out["controls"]["unsupported"] if c["control"] == "emphasis"][0]
    assert entry["requested"] == "检索"
    assert entry["applied"] is None
    assert "SSML" in entry["reason"]
    assert out["spoken_text"] == text          # 正文一字未动
    ann = [a for a in out["annotations"] if a["type"] == "emphasis"][0]
    assert (ann["start_char"], ann["end_char"]) == (0, 2)


def test_pause_annotation_is_zero_length_and_boundary_scoped(work):
    text = "第一段。第二段。"
    plan = _frozen(work, text, [_seg("seg_1", "第一段。"), _seg("seg_2", "第二段。")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    pauses = [a for a in out["annotations"] if a["type"] == "pause"]
    assert len(pauses) == 1
    assert pauses[0]["start_char"] == pauses[0]["end_char"] == 4
    assert any(c["control"] == "pause" for c in out["controls"]["unsupported"])


def test_motion_is_delegated_not_claimed_as_tts_capability(work):
    text = "这一段带抬手动作。"
    plan = _frozen(work, text, [_seg("seg_1", text, motion="raise_hand")])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    entry = [c for c in out["controls"]["approximate"] if c["control"] == "motion"][0]
    assert entry["owner"] == "talking_head"
    assert entry["applied_as"] == "delegated"
    assert "不伪称由文本精确生成动作" in entry["reason"]


# ---------- 标签误读 ----------

def test_tag_syntax_reported_but_text_not_rewritten(work):
    text = "第一句。[pause:0.5] 第二句。"
    plan = _frozen(work, text, [_seg("seg_1", text)])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["suspicious_tags"], "疑似标记语法必须被报告"
    assert out["spoken_text"] == text, "只报告不改写：静默改写作者正文更危险"
    assert any("已报告未改写" in w for w in out["warnings"])


def test_scaffold_labels_reported(work, script_text, plan):
    """策划稿的段落脚手架词（"开头：""解释："）会被逐字念出来，必须报出来。"""
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    kinds = {h["kind"] for h in out["suspicious_tags"]}
    assert "段落脚手架标签" in kinds
    assert out["spoken_text"] == script_text


def test_clean_text_has_no_tag_findings(work):
    text = "检索质量决定最终回答质量。"
    plan = _frozen(work, text, [_seg("seg_1", text)])
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["suspicious_tags"] == []
    assert out["warnings"] == []


# ---------- 能力边界声明 ----------

def test_official_text_limit_is_marked_unverified(work, script_text, plan):
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    assert out["provider_text_limit"]["official_limit_verified"] is False
    assert "尚未核验" in out["provider_text_limit"]["note"]


def test_provider_params_carry_four_part_scope_and_baseline_values(work, script_text, plan):
    out = cp.compile_speech(plan["id"], PRESET_VOICE)
    params = out["provider_params"]
    assert params["model"] == "speech-2.8-hd"
    assert params["voice_id"] == "Chinese (Mandarin)_Warm_Bestie"
    assert params["speed"] == 1.0 and params["vol"] == 1.0 and params["pitch"] == 0
    assert params["emotion"] == "calm" and params["language_boost"] == "Chinese"
    assert out["voice_scope"]["complete"] is True
    assert now_iso  # 契约工具仍可用
