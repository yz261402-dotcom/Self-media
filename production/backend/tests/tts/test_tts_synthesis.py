"""T04 全文合成（AC-TTS-02/03/04 的正文侧）。

覆盖三类只有在"提交媒体之前"拦住才有意义的检查：
- 没采用稿件 / 音色未审批 / 计划未冻结 → 直接拒收；
- 计划冻结后被改动 → 摘要复算不一致，拒收；
- 合成产物必须绑定稿件与计划、落盘名就是资产 id、Mock 不继承任何通过结论。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "tts"))

from app import storage as st  # noqa: E402
from app.tts import audio as au  # noqa: E402
from app.tts import compiler as cp  # noqa: E402
from app.tts import plan as pl  # noqa: E402
from app.tts import synthesis as sy  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsRejected  # noqa: E402
from tts_helpers import (PRESET_VOICE, allow_billable, make_transport,  # noqa: E402
                         ok_body, script_text_of)


def test_explicit_plan_must_belong_to_requested_work(work):
    plan = pl.ensure_plan(work["id"])
    plan["work_id"] = "some_other_work"
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    with pytest.raises(TtsRejected) as err:
        sy.synthesize_full(work_id=work["id"], plan_id=plan["id"], dry_run=True)
    assert err.value.code == "PLAN_WORK_MISMATCH"


def test_identical_full_synthesis_reuses_saved_bytes(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=1800))
    first = sy.synthesize_full(work_id=work["id"], transport=transport)
    second = sy.synthesize_full(work_id=work["id"], transport=transport)
    assert second["audio"]["id"] == first["audio"]["id"]
    assert second["cached"] is True
    assert len(calls) == 1


def test_explicit_new_full_synthesis_creates_new_attempt(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=1800))
    first = sy.synthesize_full(work_id=work["id"], transport=transport)
    second = sy.synthesize_full(work_id=work["id"], transport=transport, force_new=True)
    assert second["audio"]["id"] != first["audio"]["id"]
    assert len(calls) == 2


def test_explicit_new_plan_can_synthesize_unchanged_text(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=1800))
    first = sy.synthesize_full(work_id=work["id"], transport=transport)
    second = sy.synthesize_full(work_id=work["id"], transport=transport, force_new_plan=True)
    assert second["compiled"]["plan_id"] != first["compiled"]["plan_id"]
    assert len(calls) == 2


def _checks_by_code(checks) -> dict:
    return {c["code"]: c for c in checks}


# ---------- 前置条件 ----------

def test_requires_selected_script(work):
    w = st.get("works", work["id"])
    w["script_version_id"] = None
    st.put("works", work["id"], w)
    with pytest.raises(TtsRejected) as e:
        sy.synthesize_full(work_id=work["id"], dry_run=True)
    assert e.value.code == "SELECTION_REQUIRED"


def test_requires_approved_voice(work):
    vc.register_voice(voice_id="voice_unapproved_v1", name="待审音色",
                      provider="minimax", model=vc.MINIMAX_TTS_MODEL,
                      remote_voice_id="Chinese (Mandarin)_Pending",
                      sample_text="同一短句。", source_record_id="src_x")
    w = st.get("works", work["id"])
    w["voice_asset_id"] = "voice_unapproved_v1"
    st.put("works", work["id"], w)
    with pytest.raises(TtsRejected) as e:
        sy.synthesize_full(work_id=work["id"], dry_run=True)
    assert e.value.code == "VOICE_NOT_APPROVED"


def test_plan_modified_after_freeze_is_rejected(work):
    plan = pl.ensure_plan(work["id"])
    plan["segments"][0]["emotion"] = "excited"
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})
    with pytest.raises(TtsRejected) as e:
        sy.synthesize_full(work_id=work["id"], dry_run=True, plan_id=plan["id"])
    assert e.value.code == "PLAN_DIGEST_MISMATCH"


def test_unknown_work_rejected():
    with pytest.raises(TtsRejected) as e:
        sy.synthesize_full(work_id="w_missing", dry_run=True)
    assert e.value.code == "WORK_NOT_FOUND"


# ---------- 占位产物：不得继承任何通过结论 ----------

def test_dry_run_registers_mock_full_audio_that_cannot_be_confirmed(work):
    out = sy.synthesize_full(work_id=work["id"], dry_run=True)
    assert out["is_mock"] is True
    audio = out["audio"]
    assert audio["kind"] == "full" and audio["is_mock"] is True
    assert audio["script_version_id"] == work["script_version_id"]
    assert audio["performance_plan_id"]
    assert "不得确认" in out["note"]
    codes = [f["code"] for f in out["hard_failures"]]
    # 占位产物在多个维度上都"未通过"，其中 Mock 单列一条：
    # 它不能沿用任何"检查通过"的印象
    assert "AUDIO_IS_MOCK" in codes
    assert "AUDIO_DECODE" in codes and "DURATION" in codes
    assert audio["storage_uri"] == "", "占位产物没有音频字节，不得写空文件冒充已落盘"


def test_dry_run_checks_are_honest_about_what_was_not_run(work):
    out = sy.synthesize_full(work_id=work["id"], dry_run=True)
    checks = _checks_by_code(out["checks"])
    assert checks["VOICE_SCOPE"]["outcome"] == "pass"
    assert checks["SCRIPT_BINDING"]["outcome"] == "pass"
    assert checks["COMPLETENESS"]["outcome"] == "pass"
    # 没发生的事就写 not_run，不写 pass
    assert checks["AUDIO_DECODE"]["outcome"] == "not_run"
    assert checks["DURATION"]["outcome"] == "not_run"
    assert checks["ALIGNMENT_PROVENANCE"]["outcome"] == "not_run"
    assert checks["ALIGNMENT_PROVENANCE"]["hard_gate"] is False
    # 术语读法只能人工听检：本项目没有音频识别能力，绝不自动判通过
    assert checks["PRONUNCIATION"]["outcome"] == "not_run"
    assert checks["PRONUNCIATION"]["hard_gate"] is False


def test_unsupported_controls_surface_in_voice_expression_check(work):
    text = "这一段想表达兴奋。"
    _set_script_text(work, text, [{"segment_id": "seg_1", "text": text,
                                   "emotion": "excited", "pace": "normal",
                                   "emphasis": [], "motion": "neutral"}])
    out = sy.synthesize_full(work_id=work["id"], dry_run=True)
    assert _checks_by_code(out["checks"])["VOICE_EXPRESSION"]["outcome"] == "review"
    assert out["compiled"]["controls"]["unsupported"]


def _set_script_text(work, text: str, segments) -> None:
    from app.agents import contracts as ac

    batch = st.get("script_batches", work["id"])
    for c in batch["candidates"]:
        if c["id"] == work["script_version_id"]:
            c["text"] = text
    st.put("script_batches", work["id"], batch)
    plan = {"id": st.new_id("plan"), "work_id": work["id"],
            "script_version_id": work["script_version_id"],
            "script_digest": ac.digest(text), "identity_id": work["identity_id"],
            "voice_asset_id": PRESET_VOICE, "segments": segments,
            "target_duration_ms": 60000}
    plan["plan_digest"] = cp.build_plan_digest(plan)
    st.put("performance_plans", plan["id"], plan, {"work_id": work["id"]})


# ---------- 真实请求（transport 注入，不联网不付费） ----------

def test_real_synthesis_sends_script_text_verbatim(monkeypatch, work):
    allow_billable(monkeypatch)
    script_text = script_text_of(work)
    transport, calls = make_transport(body=ok_body(duration_ms=1800))
    out = sy.synthesize_full(work_id=work["id"], transport=transport)
    assert len(calls) == 1
    sent = calls[0]["json"]
    assert sent["text"] == script_text, "送去合成的必须是稿件原文，逐字符相同"
    assert sent["voice_setting"]["voice_id"] == vc.PRESET_REMOTE_VOICE_ID
    assert sent["audio_setting"]["format"] == "wav"
    assert out["duration_ms"] == 1800
    assert out["timing_source"] is None
    assert out["hard_failures"] == []


def test_real_synthesis_stores_audio_under_asset_id(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=600))
    out = sy.synthesize_full(work_id=work["id"], transport=transport)
    audio = out["audio"]
    assert audio["storage_uri"] == f"audio/{work['id']}/{audio['id']}.wav"
    disk = os.path.join(ROOT, "runtime", "audio", work["id"], f"{audio['id']}.wav")
    assert os.path.isfile(disk)
    with open(disk, "rb") as f:
        assert f.read()[:4] == b"RIFF"
    assert au.get_audio(audio["id"])["storage_uri"] == audio["storage_uri"]


def test_real_synthesis_records_lineage(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=600))
    out = sy.synthesize_full(work_id=work["id"], transport=transport)
    rows = [r for r in st.all_rows("lineage") if r.get("artifact_id") == out["audio"]["id"]]
    assert rows, "音频必须登记血缘，否则无法追溯它念的是哪一版稿"
    assert out["compiled"]["plan_id"] in rows[0]["depends_on"]


def test_real_synthesis_with_verified_words_registers_timing_source(monkeypatch, work):
    allow_billable(monkeypatch)
    words = [{"segment_id": "seg_1", "start_char": 0, "end_char": 5,
              "start_ms": 0, "end_ms": 400}]
    transport, _ = make_transport(body=ok_body(duration_ms=1000, words=words))
    out = sy.synthesize_full(work_id=work["id"], transport=transport)
    assert out["timing_source"] == "provider"
    assert _checks_by_code(out["checks"])["ALIGNMENT_PROVENANCE"]["outcome"] == "pass"
    assert len(au.get_audio(out["audio"]["id"])["words"]) == 1


def test_real_synthesis_without_words_says_alignment_not_done(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=1000))
    out = sy.synthesize_full(work_id=work["id"], transport=transport)
    check = _checks_by_code(out["checks"])["ALIGNMENT_PROVENANCE"]
    assert check["outcome"] == "not_run"
    assert "不得宣称字词对齐已完成" in check["message"]


def test_provider_failure_leaves_no_audio_asset(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, calls = make_transport(status_code=500, message="upstream boom")
    from app.tts.errors import TtsBlocked

    with pytest.raises(TtsBlocked):
        sy.synthesize_full(work_id=work["id"], transport=transport)
    assert len(calls) == 1
    assert au.list_audio(work["id"]) == [], "失败不得留下任何音频资产"


def test_without_budget_full_synthesis_sends_nothing(work):
    from app.tts.errors import TtsBlocked

    transport, calls = make_transport(body=ok_body())
    with pytest.raises(TtsBlocked) as e:
        sy.synthesize_full(work_id=work["id"], transport=transport)
    assert e.value.code == "BUDGET_NOT_AUTHORIZED"
    assert calls == []


def test_force_new_plan_creates_new_version(monkeypatch, work):
    first = pl.ensure_plan(work["id"])
    second = pl.ensure_plan(work["id"], force_new=True)
    assert second["id"] != first["id"]
    assert len(pl.plans_for_work(work["id"])) == 2
    assert pl.is_usable(second)
