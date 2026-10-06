"""T06 表演计划：分段必须是稿件的一个划分，计划随稿件变化而失效。

这里锁住 T04 已确立的不变量：各段按序拼接后与稿件**逐字符相同**，
一旦破坏，"保留原文"与字词对齐就都失去可核对的基准。
"""
from __future__ import annotations

import pytest

from app import storage as st
from app.tts import plan as tts_plan
from app.tts.errors import TtsRejected

WORK = "w_plan"
SCRIPT = "s_plan"


def _setup(text: str, work: str = WORK, script: str = SCRIPT) -> None:
    st.put("script_batches", work,
           {"id": "b1", "work_id": work,
            "candidates": [{"id": script, "angle": "生活类比", "title": "标题",
                            "text": text, "duration_estimate_ms": 1000,
                            "quality_report": {"checks": [], "overall": "pass",
                                               "review_required": False},
                            "claims": []}]},
           {"work_id": work})
    st.put("works", work,
           {"id": work, "name": "测试作品", "script_version_id": script,
            "brief": {"topic": "RAG", "target_duration_ms": 60000}},
           {"id": work})


def _set_work(work: str, **fields) -> dict:
    row = st.get("works", work)
    row.update(fields)
    st.put("works", work, row, {"id": work})
    return row


@pytest.mark.parametrize("text", [
    "第一句。\n第二句。\n第三句。",
    "只有一段。",
    "甲。\n\n\n乙。",          # 连续空行
    "\n前导空行的稿件。\n第二段。",
    "结尾带换行的段落。\n",
])
def test_segments_join_equals_script_exactly(text):
    """划分性质：拼接后逐字符等于原稿（换行留在前一段末尾）。"""
    segs = tts_plan.derive_segments(text)
    if not text.strip():
        assert segs == []
        return
    assert "".join(s["text"] for s in segs) == text


def test_created_plan_is_usable_and_bound_to_script():
    _setup("第一句。\n第二句。")
    plan = tts_plan.create_plan(WORK, SCRIPT)
    assert plan["script_version_id"] == SCRIPT
    assert tts_plan.is_usable(plan) is True


def test_plan_digest_follows_script_change():
    _setup("甲。\n乙。")
    first = tts_plan.create_plan(WORK, SCRIPT)
    batch = st.get("script_batches", WORK)
    batch["candidates"][0]["text"] = "甲。\n乙。\n丙。"
    st.put("script_batches", WORK, batch, {"work_id": WORK})
    second = tts_plan.create_plan(WORK, SCRIPT)
    assert first["script_digest"] != second["script_digest"], "稿件变了计划摘要必须变"


def test_tampered_plan_is_not_usable():
    _setup("甲。\n乙。")
    plan = tts_plan.create_plan(WORK, SCRIPT)
    plan["segments"][0]["text"] = "被改坏的文字"
    assert tts_plan.is_usable(plan) is False, "摘要不符即不可用，不得放行"


def test_unchanged_choice_reuses_plan():
    """没换声也没换人时必须复用：否则每合成一次就新建一份计划，旧计划白留。"""
    work, script = "w_plan_stable", "s_plan_stable"
    _setup("甲。\n乙。", work, script)
    _set_work(work, voice_asset_id="voice_a", identity_id="id_a")
    first = tts_plan.ensure_plan(work)
    second = tts_plan.ensure_plan(work)
    assert second["id"] == first["id"]


def test_voice_change_derives_new_plan():
    """换声后必须重新派生计划。

    旧计划结构上是完整的（`is_usable()` 为真），但它绑的是上一个音色。
    复用它会一路走到提交前才被 `assert_voice_binding` 挡下，
    表现为"刚选完音色，合成却报错"——报错点离原因太远。
    """
    work, script = "w_plan_voice", "s_plan_voice"
    _setup("甲。\n乙。", work, script)
    _set_work(work, voice_asset_id="voice_a")
    first = tts_plan.ensure_plan(work)
    assert first["voice_asset_id"] == "voice_a"
    assert tts_plan.is_usable(first) is True
    assert tts_plan.binds_current_choice(first, st.get("works", work)) is True

    _set_work(work, voice_asset_id="voice_b")
    assert tts_plan.binds_current_choice(
        first, st.get("works", work)) is False, "换了音色，旧计划绑定即过期"

    second = tts_plan.ensure_plan(work)
    assert second["id"] != first["id"], "换了音色不得复用旧计划"
    assert second["voice_asset_id"] == "voice_b"
    assert second["revision_of"] == first["id"], "新计划须以 revision_of 指回旧计划"
    # 旧计划保留：换声是新增版本，不是抹掉历史
    assert st.get("performance_plans", first["id"]) is not None


def test_identity_change_derives_new_plan():
    """换人同理：计划同时绑定人物，人物变了也必须重新派生。"""
    work, script = "w_plan_identity", "s_plan_identity"
    _setup("甲。\n乙。", work, script)
    _set_work(work, identity_id="id_a")
    first = tts_plan.ensure_plan(work)
    assert first["identity_id"] == "id_a"

    _set_work(work, identity_id="id_b")
    second = tts_plan.ensure_plan(work)
    assert second["id"] != first["id"], "换了人物不得复用旧计划"
    assert second["identity_id"] == "id_b"


def test_missing_script_rejected():
    with pytest.raises(TtsRejected) as caught:
        tts_plan.create_plan("no_work", "no_script")
    assert caught.value.code == "STALE_SELECTION"


def test_empty_script_rejected():
    _setup("   ")
    with pytest.raises(TtsRejected) as caught:
        tts_plan.create_plan(WORK, SCRIPT)
    assert caught.value.code == "PLAN_SCRIPT_EMPTY"
