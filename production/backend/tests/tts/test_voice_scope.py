"""T04 音色作用域与受控登记（AC-TTS-01）。

重点是三类"看起来能用、实际不成立"的情形：
- 只给一个 `remote_voice_id` 就要求复用（四元组缺三项）；
- 换一个模型名继续用同一个 voice ID；
- 预设音色还没补基准合成证据，却因为 `approval_state="approved"` 被当成已验收。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.avatar import licensing  # noqa: E402
from app.tts import preset as tts_preset  # noqa: E402
from app.tts import samples as tts_samples  # noqa: E402
from app.tts import sources as tts_sources  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsBlocked, TtsRejected  # noqa: E402

HASH = "sha256:" + "b" * 64


def _source(subject: str) -> dict:
    return tts_sources.register_source(
        subject=subject, source_type="preset_voice_selection",
        provider="minimax", model=vc.MINIMAX_TTS_MODEL, version="官方预置音色")


def _register(voice_id: str, **kwargs) -> dict:
    base = dict(name="候选音色", provider="minimax", model=vc.MINIMAX_TTS_MODEL,
                remote_voice_id="Chinese (Mandarin)_Candidate", sample_text="同一短句。",
                source_record_id=_source(voice_id)["id"])
    base.update(kwargs)
    return vc.register_voice(voice_id=voice_id, **base)


# ---------- 作用域四元组 ----------

def test_incomplete_scope_rejected():
    """四元组缺项必须在登记时就被拦下，且报错要点名缺了哪一项。"""
    with pytest.raises(TtsRejected) as e:
        _register("voice_incomplete", region="")
    assert e.value.code == "VOICE_SCOPE_INCOMPLETE"
    assert e.value.detail["missing"] == ["region"]


def test_scope_complete_check_flags_legacy_row():
    """历史行可能缺 region（早期只登记了名称与 voice_id）：读出来必须能发现。"""
    from app import storage as st

    st.put("voices", "voice_legacy", {"id": "voice_legacy", "name": "历史音色",
                                      "provider": "minimax", "model": vc.MINIMAX_TTS_MODEL,
                                      "remote_voice_id": "Chinese (Mandarin)_Legacy",
                                      "approval_state": "approved", "is_preset": False,
                                      "sample_text": "短句。"})
    with pytest.raises(TtsRejected) as e:
        vc.assert_scope_complete(vc.get_voice("voice_legacy"))
    assert e.value.code == "VOICE_SCOPE_INCOMPLETE"


def test_scope_declared_by_voice_id_only_rejected():
    """只给 remote_voice_id 等于"仅凭字符串 ID 复用"，缺其余三项即拒收。"""
    v = _register("voice_partial")
    with pytest.raises(TtsRejected) as e:
        vc.require_scope_match(v, remote_voice_id="Chinese (Mandarin)_Candidate")
    assert e.value.code == "VOICE_SCOPE_PARTIAL"
    assert "provider" in e.value.detail["supplied"] or \
        "provider" not in e.value.detail["supplied"]


def test_cross_scope_reuse_rejected():
    """provider/model/region 任一不符即跨作用域复用。"""
    v = _register("voice_cross")
    for field, bad in (("provider", "other-tts"), ("model", "speech-2.8-turbo"),
                       ("region", "global")):
        args = dict(provider="minimax", model=vc.MINIMAX_TTS_MODEL, region="cn",
                    remote_voice_id="Chinese (Mandarin)_Candidate")
        args[field] = bad
        with pytest.raises(TtsRejected) as e:
            vc.require_scope_match(v, **args)
        assert e.value.code in ("VOICE_SCOPE_MISMATCH",), field


def test_same_scope_passes():
    v = _register("voice_same")
    sc = vc.require_scope_match(v, provider="minimax", model=vc.MINIMAX_TTS_MODEL,
                                region="cn",
                                remote_voice_id="Chinese (Mandarin)_Candidate")
    assert sc["complete"] is True


def test_model_outside_whitelist_rejected():
    """"不改为 Turbo"：型号是作用域的一部分，换型号即静默降级。"""
    with pytest.raises(TtsRejected) as e:
        vc.assert_model_allowed("minimax", "speech-2.8-turbo")
    assert e.value.code == "VOICE_MODEL_NOT_ALLOWED"


def test_unverified_region_rejected():
    with pytest.raises(TtsRejected) as e:
        vc.assert_region_verified("minimax", "intl")
    assert e.value.code == "VOICE_SCOPE_UNVERIFIED"


def test_unknown_provider_rejected():
    with pytest.raises(TtsRejected) as e:
        vc.assert_model_allowed("some-tts", "v1")
    assert e.value.code == "VOICE_PROVIDER_UNSUPPORTED"


# ---------- 登记门禁 ----------

def test_registration_requires_source_record():
    with pytest.raises(TtsRejected) as e:
        vc.register_voice(voice_id="voice_nosource", name="无来源", provider="minimax",
                          model=vc.MINIMAX_TTS_MODEL,
                          remote_voice_id="Chinese (Mandarin)_X", sample_text="短句。")
    assert e.value.code == "VOICE_SOURCE_MISSING"


def test_registration_requires_sample_text():
    with pytest.raises(TtsRejected) as e:
        _register("voice_nosample", sample_text="")
    assert e.value.code == "VOICE_SAMPLE_TEXT_MISSING"


def test_preset_requires_license():
    with pytest.raises(TtsRejected) as e:
        _register("voice_preset_nolicense", is_preset=True)
    assert e.value.code == "VOICE_LICENSE_MISSING"


def test_unknown_profile_key_rejected():
    """参数不透传：未知键必须拒绝，而不是原样发给供应商。"""
    with pytest.raises(TtsRejected) as e:
        vc.normalize_profile({"speed": 1.0, "ssml": "<speak>hi</speak>"})
    assert e.value.code == "VOICE_PROFILE_KEY_UNKNOWN"


def test_declared_unsupported_control_rejected():
    """whisper 官方声明不支持，不得因通用枚举里有就启用。"""
    with pytest.raises(TtsRejected) as e:
        vc.normalize_profile({"whisper": True})
    assert e.value.code == "VOICE_CONTROL_UNSUPPORTED"


# ---------- 版本与冻结 ----------

def test_approved_voice_cannot_be_overwritten():
    v = _register("voice_frozen", approval_state="approved")
    assert v["approval_state"] == "approved"
    with pytest.raises(TtsRejected) as e:
        _register("voice_frozen", name="想覆盖")
    assert e.value.code == "VOICE_FROZEN"


def test_new_version_keeps_old_row_and_bumps_version():
    v1 = _register("voice_ver", approval_state="approved")
    v2 = _register("voice_ver2", supersedes_id=v1["id"])
    assert v2["version"] == 2
    assert v2["approval_state"] == "pending"
    # 旧行原样保留：旧作品继续引用旧版本
    old = vc.get_voice(v1["id"])
    assert old["approval_state"] == "approved"
    assert old["version"] == 1


# ---------- 预设音色 3 的基准证据 ----------

def test_preset_scope_is_complete_and_matches_decision_record():
    status = tts_preset.preset_status()
    assert status["exists"] is True
    sc = status["scope"]
    assert sc["complete"] is True
    assert sc["provider"] == "minimax"
    assert sc["model"] == "speech-2.8-hd"
    assert sc["region"] == "cn"
    assert sc["remote_voice_id"] == "Chinese (Mandarin)_Warm_Bestie"


def test_preset_baseline_audio_hash_matches_decision_record():
    """基准证据不是文字转述：哈希与容器参数都从真实文件实测。"""
    status = tts_preset.preset_status()
    base = status["baseline"]
    assert base, "预设音色必须有本项目基准合成证据"
    assert base["file_hash"] == tts_preset.PRESET_WAV_SHA256
    assert base["duration_ms"] == tts_preset.PRESET_DURATION_MS
    assert base["sample_rate"] == 32000
    assert base["channels"] == 1


def test_preset_blocked_only_by_unverified_license():
    """预设已选 ≠ 已获准生产：阻塞项必须精确到"权属未核验"，而不是笼统失败。"""
    status = tts_preset.preset_status()
    assert status["production_ready"] is False
    assert len(status["production_blockers"]) == 1
    assert "权属未核验" in status["production_blockers"][0]


def test_baseline_hash_tampered_rejected(tmp_path):
    from tests.tts.conftest import make_wav

    path = os.path.join(tmp_path, "x.wav")
    with open(path, "wb") as f:
        f.write(make_wav(duration_ms=100))
    with pytest.raises(TtsRejected) as e:
        tts_samples.register_sample(voice_id="v", audio_path=path, sample_text="短句。",
                                    source="测试", declared_hash=HASH)
    assert e.value.code == "VOICE_BASELINE_HASH_TAMPERED"


def test_baseline_without_declared_hash_is_accepted_and_measured(tmp_path):
    """首次登记没有可比的哈希时，以实测哈希为准并登记容器解出的规格。"""
    from tests.tts.conftest import make_wav

    path = os.path.join(tmp_path, "y.wav")
    with open(path, "wb") as f:
        f.write(make_wav(duration_ms=2500, sample_rate=16000, channels=2))
    rec = tts_samples.register_sample(voice_id="v2", audio_path=path, sample_text="短句。",
                                      source="测试")
    assert rec["duration_ms"] == 2500
    assert rec["sample_rate"] == 16000
    assert rec["channels"] == 2


def test_baseline_undecodable_rejected(tmp_path):
    """解不出时长的音频不能登记：否则"基准验证"只是有个文件在那儿。"""
    path = os.path.join(tmp_path, "z.wav")
    with open(path, "wb") as f:
        f.write(b"not a wav at all")
    with pytest.raises(TtsRejected) as e:
        tts_samples.register_sample(voice_id="v3", audio_path=path, sample_text="短句。",
                                    source="测试")
    assert e.value.code == "VOICE_BASELINE_UNDECODABLE"


def test_candidate_voice_is_not_production_ready():
    """候选音色：未审批、缺来源/权属/基准证据，必须逐条列出缺口。"""
    v = _register("voice_candidate")
    ok, reasons = vc.voice_ready(v["id"])
    assert ok is False
    joined = "；".join(reasons)
    assert "未审批" in joined
    assert "缺少权属记录" in joined
    assert "基准合成证据" in joined


def test_voice_scope_view_reports_completeness(voice):
    sc = vc.voice_scope(voice)
    assert sc["complete"] is True
    assert sc["scope_key"].startswith("minimax|speech-2.8-hd|cn|")


def test_license_record_for_preset_is_honest_about_scope():
    """权属只记录事实边界，不代替用户做法律结论。"""
    rec = next((r for r in licensing.list_records()
                if r.get("subject") == vc.PRESET_VOICE_ID), None)
    assert rec is not None
    assert rec["scope"] == "unverified"
    assert "未取得书面法律意见" in rec["note"]
    assert "升级路径" in rec["note"]


def test_test_material_cannot_be_reviewed_as_usable():
    """测试素材始终为模拟：不许被核验成可用于生产。"""
    rec = tts_sources.register_source(subject="素材X", source_type="project_test_material")
    with pytest.raises(TtsRejected) as e:
        tts_sources.mark_license_review(rec["id"], "reviewed", reviewer="user")
    assert e.value.code == "SOURCE_TEST_MATERIAL_NOT_APPROVABLE"
    ok, reasons = tts_sources.source_ready(rec["id"])
    assert ok is False
    assert any("测试素材" in r for r in reasons)


def test_source_requires_evidence_for_its_type():
    """project_generated 缺 provider_run_id 即来源不清。"""
    with pytest.raises(TtsRejected) as e:
        tts_sources.register_source(subject="v", source_type="project_generated",
                                    provider="minimax", model="speech-2.8-hd")
    assert e.value.code == "SOURCE_EVIDENCE_MISSING"


def test_require_voice_ready_blocks_with_structured_reasons():
    v = _register("voice_blocked")
    with pytest.raises(TtsBlocked) as e:
        vc.require_voice_ready(v["id"])
    assert e.value.code == "VOICE_NOT_PRODUCTION_READY"
    assert e.value.detail["reasons"]
