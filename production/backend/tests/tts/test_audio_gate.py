"""T04 音频门禁、最终音频确认、对齐登记与下游失效（AC-TTS-03/04、AC-AG-03）。

三组不可绕过的约束各有一批负例：

1. **试听片不是正文音频**：`kind` 是权限不是标签，checks 全绿也不能被确认。
2. **确认是一次人工动作**：缺 `audition_ack`、主体不是用户、音频不属于该作品、
   硬门禁未通过，任一命中即拒；系统不产生自动的"听检通过"。
3. **失效不是删除**：换声/改稿撤销确认但保留文件、哈希与费用记录。
   对齐必须来自白名单来源——均分时长永远进不来。
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
from app.tts import gate as gt  # noqa: E402
from app.tts import sources as src  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsRejected  # noqa: E402
from tts_helpers import PRESET_VOICE, approved_alt_voice, make_wav  # noqa: E402

HASH = "sha256:" + "a" * 64
WORDS = [{"segment_id": "seg_1", "start_char": 0, "end_char": 5,
          "start_ms": 0, "end_ms": 400},
         {"segment_id": "seg_1", "start_char": 5, "end_char": 9,
          "start_ms": 400, "end_ms": 900}]


def _full_audio(work, *, duration_ms: int = 1000, is_mock: bool = False,
                checks=None, kind: str = "full"):
    return au.register_audio(
        work_id=work["id"], kind=kind, voice_id=PRESET_VOICE,
        duration_ms=duration_ms, file_hash="" if is_mock else HASH, is_mock=is_mock,
        script_version_id=work["script_version_id"] if kind == "full" else None,
        performance_plan_id="plan_test" if kind == "full" else None,
        checks=checks or [], note="门禁用例",
    )


# ---------- 登记输入校验 ----------

def test_register_audio_rejects_bad_inputs(work):
    with pytest.raises(TtsRejected) as e:
        au.register_audio(work_id=work["id"], kind="draft", voice_id=PRESET_VOICE,
                          duration_ms=1, file_hash=HASH)
    assert e.value.code == "AUDIO_KIND_INVALID"

    with pytest.raises(TtsRejected) as e:
        au.register_audio(work_id=work["id"], kind="full", voice_id=PRESET_VOICE,
                          duration_ms=1, file_hash=HASH, performance_plan_id="p")
    assert e.value.code == "AUDIO_SCRIPT_UNBOUND"

    with pytest.raises(TtsRejected) as e:
        au.register_audio(work_id=work["id"], kind="full", voice_id=PRESET_VOICE,
                          duration_ms=1, file_hash=HASH,
                          script_version_id=work["script_version_id"])
    assert e.value.code == "AUDIO_PLAN_UNBOUND"

    with pytest.raises(TtsRejected) as e:
        au.register_audio(work_id=work["id"], kind="preview", voice_id=PRESET_VOICE,
                          duration_ms=1)
    assert e.value.code == "AUDIO_HASH_MISSING"

    with pytest.raises(TtsRejected) as e:
        au.register_audio(work_id=work["id"], kind="preview", voice_id=PRESET_VOICE,
                          duration_ms=1, file_hash="a" * 64)
    assert e.value.code == "AUDIO_HASH_INVALID"


# ---------- 门禁：试听片与 Mock 永远不可确认 ----------

def test_preview_with_green_checks_is_still_not_confirmable(work):
    """试听片的门禁失败来自 kind 本身，不来自检查项：全绿也不能成为最终音频。"""
    green = [{"code": "VOICE_SCOPE", "outcome": "pass", "hard_gate": True,
              "message": "ok"}]
    audio = _full_audio(work, kind="preview", checks=green)
    d = gt.audio_gate(audio["id"])
    assert d["ok"] is False
    assert any("试听片" in r for r in d["reasons"])
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1",
                              audition_ack=True)
    assert e.value.code == "AUDIO_PREVIEW_NOT_FINAL"


def test_mock_audio_is_not_confirmable(work):
    audio = _full_audio(work, is_mock=True)
    d = gt.audio_gate(audio["id"])
    assert any("Mock" in r for r in d["reasons"])
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1",
                              audition_ack=True)
    assert e.value.code == "AUDIO_IS_MOCK"


def test_hard_gate_failure_blocks_confirmation_with_reasons(work):
    checks = [{"code": "COMPLETENESS", "outcome": "fail", "hard_gate": True,
               "message": "分段与稿件不一致"}]
    audio = _full_audio(work, checks=checks)
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1",
                               audition_ack=True)
    assert e.value.code == "AUDIO_HARD_GATE_FAILED"
    assert "COMPLETENESS" in e.value.message


def test_unknown_duration_is_not_confirmable(work):
    audio = _full_audio(work, duration_ms=0)
    ok, reasons = au.is_confirmable(audio)
    assert not ok and any("时长" in r for r in reasons)


def test_audio_of_other_work_rejected(work):
    audio = _full_audio(work)
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio("w_other", audio["id"], actor_id="user_1",
                               audition_ack=True)
    assert e.value.code == "AUDIO_WORK_MISMATCH"


def test_confirmation_requires_explicit_audition_and_user(work):
    audio = _full_audio(work)
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1")
    assert e.value.code == "AUDIO_AUDITION_REQUIRED"

    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="system",
                               audition_ack=True)
    assert e.value.code == "AUDIO_ACTOR_INVALID"

    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio["id"], actor_id="",
                               audition_ack=True)
    assert e.value.code == "AUDIO_ACTOR_INVALID"


# ---------- 确认 ----------

def test_confirm_freezes_audio_and_sets_work_duration(work):
    audio = _full_audio(work, duration_ms=15219)
    out = gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_local",
                                 audition_ack=True)
    assert out["duration_ms"] == 15219
    row = au.get_audio(audio["id"])
    assert row["confirmed"] is True and row["approved"] is True
    assert row["confirmed_by"] == "user_local" and row["frozen_at"]
    assert au.final_audio(work["id"])["id"] == audio["id"]
    w = st.get("works", work["id"])
    assert w["final_audio_id"] == audio["id"]
    assert w["final_audio_duration_ms"] == 15219
    assert w.get("performance_plan_id") == audio["performance_plan_id"], "确认音频必须同时绑定生成它的表演计划"
    review = gt.current_audio_review(audio["id"])
    assert review["reviewer"] == "user_local" and review["outcome"] == "pass"
    assert review["audio_hash"] == HASH


def test_new_confirmation_revokes_previous_without_deleting_it(work):
    first = _full_audio(work, duration_ms=1000)
    second = _full_audio(work, duration_ms=1200)
    gt.confirm_final_audio(work["id"], first["id"], actor_id="user_1", audition_ack=True)
    out = gt.confirm_final_audio(work["id"], second["id"], actor_id="user_1",
                                 audition_ack=True)
    assert out["revoked_previous"] == [first["id"]]
    old = au.get_audio(first["id"])
    assert old["confirmed"] is False and old["revoked_at"]
    assert old["file_hash"] == HASH, "失效不是删除：旧文件与哈希保留"
    assert au.final_audio(work["id"])["id"] == second["id"]


def test_review_pass_requires_user_and_valid_outcome(work):
    audio = _full_audio(work)
    with pytest.raises(TtsRejected) as e:
        gt.record_audio_review(audio["id"], "auto-checker", "pass")
    assert e.value.code == "AUDIO_REVIEW_ACTOR_INVALID"

    with pytest.raises(TtsRejected) as e:
        gt.record_audio_review(audio["id"], "user_1", "maybe")
    assert e.value.code == "AUDIO_REVIEW_OUTCOME_INVALID"

    ok = gt.record_audio_review(audio["id"], "user_1", "review", note="有轻微齿音")
    assert ok["outcome"] == "review" and ok["duration_ms"] == 1000


# ---------- 对齐 ----------

def test_alignment_source_whitelist_excludes_even_split(work):
    audio = _full_audio(work)
    for bad in ("even_split", "average", "estimated", "provider_estimate"):
        with pytest.raises(TtsRejected) as e:
            gt.register_alignment(audio["id"], source=bad, method="split",
                                  words=WORDS)
        assert e.value.code == "ALIGNMENT_SOURCE_INVALID"


def test_alignment_requires_method(work):
    audio = _full_audio(work)
    with pytest.raises(TtsRejected) as e:
        gt.register_alignment(audio["id"], source="forced_alignment", method="",
                              words=WORDS)
    assert e.value.code == "ALIGNMENT_METHOD_MISSING"


def test_alignment_words_are_validated(work):
    audio = _full_audio(work)
    bad = [{"start_char": 0, "end_char": 5, "start_ms": 0, "end_ms": 9000}]
    with pytest.raises(TtsRejected) as e:
        gt.register_alignment(audio["id"], source="asr_alignment", method="whisperx",
                              words=bad)
    assert e.value.code == "ALIGNMENT_WORDS_INVALID"
    assert st.all_rows("alignment_records", "audio_id=?", [audio["id"]]) == []


def test_registered_alignment_updates_timing_source(work):
    audio = _full_audio(work)
    rec = gt.register_alignment(audio["id"], source="forced_alignment",
                                method="mfa-2.2", words=WORDS,
                                rule_version="align-rules-1")
    assert rec["audio_digest"] == HASH and rec["rule_version"] == "align-rules-1"
    assert au.get_audio(audio["id"])["timing_source"] == "forced_alignment"
    assert au.get_audio(audio["id"])["alignment_id"] == rec["id"]


def test_alignment_status_does_not_claim_without_final_audio(work):
    audio = _full_audio(work)
    gt.register_alignment(audio["id"], source="forced_alignment", method="mfa",
                          words=WORDS)
    s = gt.alignment_status(work["id"])
    assert s["aligned"] is False and s["claimable"] is False
    assert "尚无已确认的最终音频" in s["reason"]


def test_alignment_status_does_not_claim_without_timestamps(work):
    """已确认但没拿到时间戳时，必须明说"不得宣称对齐完成"，而不是沉默。"""
    audio = _full_audio(work)
    gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1", audition_ack=True)
    s = gt.alignment_status(work["id"])
    assert s["aligned"] is False and s["claimable"] is False
    assert "不得宣称字词对齐已完成" in s["reason"]


def test_alignment_status_claimable_after_confirmation(work):
    audio = _full_audio(work)
    gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1", audition_ack=True)
    gt.register_alignment(audio["id"], source="forced_alignment", method="mfa",
                          words=WORDS)
    s = gt.alignment_status(work["id"])
    assert s["aligned"] is True and s["claimable"] is True
    assert s["word_count"] == 2 and s["source"] == "forced_alignment"


# ---------- 下游失效 ----------

def test_voice_change_revokes_confirmation_and_invalidates_downstream(work):
    audio = _full_audio(work, duration_ms=1000)
    gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1", audition_ack=True)
    alt = approved_alt_voice()
    out = gt.on_voice_change(work["id"], alt["id"])
    assert out["previous_voice_asset_id"] == PRESET_VOICE
    assert out["revoked_audio"] == [audio["id"]]
    assert st.get("works", work["id"])["voice_asset_id"] == alt["id"]
    row = au.get_audio(audio["id"])
    assert row["confirmed"] is False
    assert alt["id"] in row["revoked_reason"] or row["revoked_reason"]
    assert au.final_audio(work["id"]) is None


def test_voice_change_requires_approved_voice(work):
    vc.register_voice(voice_id="voice_pending_v1", name="未审批", provider="minimax",
                      model=vc.MINIMAX_TTS_MODEL, remote_voice_id="Chinese (Mandarin)_P",
                      sample_text="同一短句。",
                      source_record_id=src.register_source(
                          subject="voice_pending_v1",
                          source_type="preset_voice_selection", provider="minimax",
                          model=vc.MINIMAX_TTS_MODEL, version="v1")["id"])
    with pytest.raises(TtsRejected) as e:
        gt.on_voice_change(work["id"], "voice_pending_v1")
    assert e.value.code == "VOICE_NOT_APPROVED"


def test_script_change_revokes_confirmation(work):
    audio = _full_audio(work)
    gt.confirm_final_audio(work["id"], audio["id"], actor_id="user_1", audition_ack=True)
    out = gt.on_script_change(work["id"])
    assert out["revoked_audio"] == [audio["id"]]
    assert au.get_audio(audio["id"])["confirmed"] is False
    assert au.get_audio(audio["id"])["file_hash"] == HASH


# ---------- 总览 ----------

def test_gate_summary_reflects_progress(work):
    empty = gt.gate_summary(work["id"])
    assert empty["full_audio_count"] == 0 and empty["audio_confirmed"] is False
    assert empty["alignment"]["claimable"] is False

    _full_audio(work)
    full = _full_audio(work, duration_ms=2222)
    _full_audio(work, kind="preview")
    gt.confirm_final_audio(work["id"], full["id"], actor_id="user_1", audition_ack=True)
    s = gt.gate_summary(work["id"])
    assert s["full_audio_count"] == 2 and s["preview_count"] == 1
    assert s["audio_confirmed"] is True
    assert s["final_audio_duration_ms"] == 2222
    assert s["voice_asset_id"] == PRESET_VOICE


def test_store_audio_bytes_uses_asset_id(work, tmp_path):
    """落盘文件名就是资产 id：同一作品的多次合成不会互相覆盖。"""
    audio = _full_audio(work)
    uri = au.store_audio_bytes(work["id"], audio["id"], make_wav(duration_ms=10))
    assert uri.endswith(f"{audio['id']}.wav")
    assert not uri.startswith("/") and ":" not in uri
