"""T04 音色试听（AC-TTS-01 的试听侧）。

要点：
- **同一短句**：跨音色比较的前提是"只有声音不同"，换句必须显式重置；
- **缓存与计费分开报**：命中缓存的试听不产生新费用，参数变了不命中；
- **Mock 不入缓存**：否则"没真做过"会被报成"已有样音"；
- **试听片不是正文音频**：它的门禁失败来自 kind 本身，不可被确认。
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
from app.tts import preview as pv  # noqa: E402
from app.tts import provider as prov  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsRejected  # noqa: E402
from tts_helpers import PRESET_VOICE, allow_billable, make_transport, ok_body  # noqa: E402


def _voice_without_sample_text(voice_id: str = "voice_no_sample_v1") -> dict:
    """构造一条"来源与作用域齐全但没有基准短句"的旧记录。

    正常登记路径会挡住缺短句的音色（VOICE_SAMPLE_TEXT_MISSING），
    因此这里直接写库，模拟历史遗留行——判定的健壮性不能只依赖登记入口。
    """
    row = dict(vc.get_voice(PRESET_VOICE))
    row.update({"id": voice_id, "name": "无短句音色", "sample_text": "",
                "remote_voice_id": "Chinese (Mandarin)_NoSample", "scope_key": ""})
    row["scope_key"] = vc.scope_key(row["provider"], row["model"], row["region"],
                                    row["remote_voice_id"])
    st.put("voices", voice_id, row)
    return row


# ---------- 同一短句约束 ----------

def test_same_work_is_pinned_to_one_sentence(work, voice):
    first = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                              sample_text="第一句试听文本。", dry_run=True)
    assert first["cached"] is False
    assert pv.audition_sentence(work["id"]) == "第一句试听文本。"
    with pytest.raises(TtsRejected) as e:
        pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                          sample_text="换了另一句。", dry_run=True)
    assert e.value.code == "PREVIEW_SAMPLE_TEXT_MISMATCH"


def test_pinned_sentence_is_reused_when_not_specified(work, voice):
    pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                      sample_text="固定短句。", dry_run=True)
    # 不指定短句时沿用已绑定的那一句，而不是悄悄回落到音色自带短句
    resolved = pv.resolve_audition_sentence(voice, work["id"])
    assert resolved == "固定短句。"


def test_falls_back_to_voice_baseline_sentence(work, voice):
    resolved = pv.resolve_audition_sentence(voice, work["id"])
    assert resolved == voice["sample_text"]
    assert pv.audition_sentence(work["id"]) == voice["sample_text"]


def test_explicit_reset_allows_changing_sentence(work, voice):
    pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                      sample_text="旧短句。", dry_run=True)
    pv.reset_audition_sentence(work["id"], "新短句。", reason="原短句含生僻多音字")
    assert pv.audition_sentence(work["id"]) == "新短句。"
    out = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                            sample_text="新短句。", dry_run=True)
    assert out["note"] == pv.PREVIEW_NOTE
    # 重置也解绑：不再传短句则重新按音色基准选定
    pv.reset_audition_sentence(work["id"])
    assert pv.audition_sentence(work["id"]) is None


def test_reset_requires_work():
    with pytest.raises(TtsRejected) as e:
        pv.reset_audition_sentence("")
    assert e.value.code == "WORK_NOT_FOUND"


def test_missing_sample_text_is_rejected(work):
    _voice_without_sample_text()
    with pytest.raises(TtsRejected) as e:
        pv.create_preview(work_id=work["id"], voice_id="voice_no_sample_v1",
                          dry_run=True)
    assert e.value.code == "VOICE_SAMPLE_TEXT_MISSING"


def test_source_record_is_required(work, voice):
    row = dict(voice)
    row["source_record_id"] = ""
    vc.register_voice(voice_id="voice_no_source_v1", name="无来源",
                      provider="minimax", model=vc.MINIMAX_TTS_MODEL,
                      remote_voice_id="Chinese (Mandarin)_NS", sample_text="同一短句。",
                      source_record_id="src_tmp")
    legacy = dict(st.get("voices", "voice_no_source_v1"))
    legacy["source_record_id"] = ""
    st.put("voices", "voice_no_source_v1", legacy)
    with pytest.raises(TtsRejected) as e:
        pv.create_preview(work_id=work["id"], voice_id="voice_no_source_v1",
                          dry_run=True)
    assert e.value.code == "VOICE_SOURCE_MISSING"


# ---------- 缓存与计费 ----------
#
# 缓存键 = 作用域四元组 + 合成参数 + 文本摘要，**不含 work_id**：
# 同一音色同一句的输出在任何作品里都该复用。因此下面每个用例都用自己的一句试听文本，
# 否则用例之间会互相命中缓存，"第一次合成"就变成"命中缓存"，断言随之失去意义。

SENT_CACHE = "缓存命中用例专用短句。"
SENT_FORCE = "强制重合成用例专用短句。"
SENT_NO_WORK = "无作品上下文用例专用短句。"
SENT_NOT_FINAL = "试听不可确认用例专用短句。"
SENT_STORE = "试听落盘用例专用短句。"
SENT_LIST = "试听列表用例专用短句。"
SENT_PARAMS = "试听参数用例专用短句。"
SENT_MOCK = "Mock不入缓存用例专用短句。"


def test_cache_hit_does_not_recharge(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=700))
    first = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                              sample_text=SENT_CACHE, transport=transport)
    assert first["cached"] is False and len(calls) == 1
    assert first["cost_reservation_id"], "新合成是可计费动作，必须有费用登记"
    assert first["audio"]["kind"] == "preview"

    second = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                               sample_text=SENT_CACHE, transport=transport)
    assert second["cached"] is True
    assert second["cost_reservation_id"] is None
    assert "不产生新的计费合成" in second["message"]
    assert len(calls) == 1, "命中缓存不得再发一次请求"


def test_force_new_bypasses_cache_and_is_billable(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=700))
    pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                      sample_text=SENT_FORCE, transport=transport)
    forced = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                               sample_text=SENT_FORCE, transport=transport,
                               force_new=True)
    assert forced["cached"] is False and forced["cost_reservation_id"]
    assert len(calls) == 2


def test_mock_preview_is_never_cached(work, voice):
    out = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                            sample_text=SENT_MOCK, dry_run=True)
    assert out["audio"]["is_mock"] is True
    assert out["cost_reservation_id"] is None
    assert "占位试听" in out["message"]
    assert pv.find_cached(voice, SENT_MOCK) is None
    again = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                              sample_text=SENT_MOCK, dry_run=True)
    assert again["cached"] is False


def test_cache_key_changes_when_profile_changes(voice):
    base = pv.preview_cache_key(voice, "同一短句。")
    changed = dict(voice, synthesis_profile=dict(voice["synthesis_profile"], speed=0.9))
    assert pv.preview_cache_key(changed, "同一短句。") != base
    with pytest.raises(TtsRejected) as e:
        pv.preview_cache_key(dict(voice, synthesis_profile={"unknown_key": 1}),
                             "同一短句。")
    assert e.value.code == "VOICE_PROFILE_KEY_UNKNOWN"


def test_cache_key_ignores_work_but_not_profile(voice):
    """缓存跨作品复用是对的，但参数一变就必须不命中。"""
    a = pv.preview_cache_key(voice, "跨作品短句。")
    b = pv.preview_cache_key(dict(voice, id="voice_other"), "跨作品短句。")
    assert a == b, "同一音色同一句在不同作品间应可复用试听"
    c = pv.preview_cache_key(dict(voice, synthesis_profile=dict(
        voice["synthesis_profile"], vol=0.8)), "跨作品短句。")
    assert c != a


def test_preview_without_work_uses_voice_baseline(monkeypatch, voice):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=500))
    out = pv.create_preview(work_id=None, voice_id=PRESET_VOICE,
                            sample_text=SENT_NO_WORK, transport=transport)
    assert out["audio"]["work_id"] == f"preview-{PRESET_VOICE}"
    assert out["audio"]["kind"] == au.KIND_PREVIEW


# ---------- 试听片不是正文音频 ----------

def test_preview_audio_cannot_be_confirmed_as_final(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=900))
    out = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                            sample_text=SENT_NOT_FINAL, transport=transport)
    audio_id = out["audio"]["id"]
    assert out["audio"]["script_version_id"] is None
    with pytest.raises(TtsRejected) as e:
        gt.confirm_final_audio(work["id"], audio_id, actor_id="user_1",
                               audition_ack=True)
    assert e.value.code == "AUDIO_PREVIEW_NOT_FINAL"
    assert gt.audio_gate(audio_id)["ok"] is False


def test_preview_audio_is_stored_under_asset_id(monkeypatch, work):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=400))
    out = pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                            sample_text=SENT_STORE, transport=transport)
    audio = out["audio"]
    assert audio["storage_uri"] == f"audio/{work['id']}/{audio['id']}.wav"
    assert os.path.isfile(os.path.join(ROOT, "runtime", "audio", work["id"],
                                       f"{audio['id']}.wav"))


def test_list_previews_filters_by_work(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=400))
    pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                      sample_text=SENT_LIST, transport=transport)
    rows = pv.list_previews(work["id"])
    assert len(rows) == 1 and rows[0]["work_id"] == work["id"]
    assert pv.list_previews("w_other") == []


# ---------- 基准样例 ----------

def test_baseline_sample_points_at_registered_evidence(voice):
    sample = pv.baseline_sample(PRESET_VOICE)
    assert sample["available"] is True
    assert sample["audio_asset_id"] == voice["baseline_audio_asset_id"]
    assert sample["file_hash"] == voice["baseline_audio_hash"]
    assert "基准合成证据" in sample["source"]
    assert sample["sample_text"] == voice["sample_text"]


def test_text_digest_is_short_and_stable():
    d = pv.text_digest("同一短句。")
    assert d == pv.text_digest("同一短句。") and d.startswith("sha256:")
    assert d != pv.text_digest("另一句。")


def test_provider_params_sent_for_preview_are_voice_profile(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=400))
    pv.create_preview(work_id=work["id"], voice_id=PRESET_VOICE,
                      sample_text=SENT_PARAMS, transport=transport)
    setting = calls[0]["json"]["voice_setting"]
    profile = voice["synthesis_profile"]
    assert setting["speed"] == profile["speed"]
    assert setting["emotion"] == profile["emotion"]
    assert calls[0]["json"]["text"] == SENT_PARAMS
    assert prov.SECRET_ENV  # 密钥只经环境注入，不写进请求体
