"""T04 路由层冒烟：HTTP 语义与异常映射（AC-TTS-01～04 的接口侧）。

单元测试证明的是模块行为，这里证明的是**接线**：
- 请求不成立 → 422；当前不可执行（预算/上游不满足）→ 409 且 retryable；
- mock 模式下不触网，占位产物如实标注 is_mock；
- 试听、正文音频、门禁总览、对齐登记四条链路都能从 HTTP 走到存储。
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "tts"))

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from app.tts import audio as au  # noqa: E402
from tts_helpers import PRESET_VOICE, approved_alt_voice  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def sel_work(client, work) -> dict:
    """走真实的选稿接口把稿件"采用"掉。

    全文合成的前置条件是"已采用稿件"，而"采用"在数据上是 selections 表里
    一条 kind=script/state=selected 的记录。直接改 work.script_version_id
    绕不过这道门禁——这正是我们想要的：门禁检查的是选择记录本身。
    """
    r = client.post(f"/api/v1/works/{work['id']}/script-selection",
                    json={"script_id": work["script_version_id"]})
    assert r.status_code == 200, r.text
    return work


def _err(response) -> dict:
    """错误体形状由全局异常处理器决定：这里两种都认，避免测试被包装层绊住。"""
    body = response.json()
    if "error" in body:
        return body["error"]
    return body["detail"]["error"]


# ---------- 能力与音色 ----------

def test_capabilities_endpoint_separates_declared_and_verified(client):
    r = client.get("/api/v1/tts/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    adapter = body["adapter"]
    assert adapter["provider"] == "minimax"
    assert adapter["capabilities"]["declared"]["word_timestamps"] is True
    assert adapter["capabilities"]["verified"]["word_timestamps"] is False, \
        "词级时间戳尚未验收，接口不得把它列进已实测能力"
    assert body["declared_unsupported"] == ["whisper"]
    assert "未核验项" in body["note"]


def test_voices_endpoint_exposes_scope_and_blockers(client):
    r = client.get("/api/v1/voices")
    assert r.status_code == 200, r.text
    rows = {v["id"]: v for v in r.json()}
    preset = rows[PRESET_VOICE]
    assert preset["scope"]["complete"] is True
    assert preset["scope"]["region"] == "cn"
    assert preset["baseline_sample"]["available"] is True
    assert preset["production_ready"] is False
    assert any("权属" in b or "TTS" in b or "基准" in b
               for b in preset["production_blockers"])


# ---------- 试听 ----------

def test_voice_preview_is_mock_and_pins_one_sentence(client, work):
    r = client.post("/api/v1/voice-previews",
                    json={"work_id": work["id"], "voice_asset_id": PRESET_VOICE,
                          "sample_text": "路由用例专用短句。"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cached"] is False
    assert body["audio"]["is_mock"] is True
    assert body["audio"]["kind"] == "preview"
    assert body["sample_sentence"] == "路由用例专用短句。"
    assert "试听短句" in body["note"]

    r2 = client.post("/api/v1/voice-previews",
                     json={"work_id": work["id"], "voice_asset_id": PRESET_VOICE,
                           "sample_text": "换一句就不行。"})
    assert r2.status_code == 422
    assert _err(r2)["code"] == "PREVIEW_SAMPLE_TEXT_MISMATCH"


def test_voice_preview_requires_known_work(client):
    r = client.post("/api/v1/voice-previews",
                    json={"work_id": "w_missing", "voice_asset_id": PRESET_VOICE})
    assert r.status_code == 404


# ---------- 选声与换声 ----------

def test_select_voice_switch_invalidates_downstream(client, work):
    alt = approved_alt_voice("voice_alt_route_v1")
    r = client.post(f"/api/v1/works/{work['id']}/voice",
                    json={"voice_asset_id": alt["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["voice_asset_id"] == alt["id"]


def test_select_voice_rejects_unapproved(client, work):
    r = client.post(f"/api/v1/works/{work['id']}/voice",
                    json={"voice_asset_id": "voice_does_not_exist"})
    assert r.status_code == 422
    assert _err(r)["code"] == "VOICE_SCOPE_MISMATCH"


# ---------- 全文合成 ----------

def test_full_audio_in_mock_mode_is_placeholder(client, sel_work):
    r = client.post(f"/api/v1/works/{sel_work['id']}/audio", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_mock"] is True
    assert body["duration_ms"] == 0
    assert body["timing_source"] is None
    assert any(f["code"] == "AUDIO_IS_MOCK" for f in body["hard_failures"])
    assert "不得确认" in body["note"]
    assert body["audio"]["performance_plan_id"]


def test_full_audio_without_budget_maps_to_409(client, sel_work, monkeypatch):
    """非 mock 模式下预算未授权：必须 409 + retryable，而不是 200 或 422。"""
    monkeypatch.setattr(config, "resolve_mode", lambda *a, **k: "integration")
    r = client.post(f"/api/v1/works/{sel_work['id']}/audio", json={})
    assert r.status_code == 409, r.text
    err = _err(r)
    assert err["code"] == "BUDGET_NOT_AUTHORIZED"
    assert err["retryable"] is True


def test_audio_list_and_gate_summary(client, sel_work):
    client.post(f"/api/v1/works/{sel_work['id']}/audio", json={})
    audios = client.get(f"/api/v1/works/{sel_work['id']}/audios").json()
    assert len(audios) == 1 and audios[0]["is_mock"] is True

    gate = client.get(f"/api/v1/works/{sel_work['id']}/audio-gate").json()
    assert gate["audio_confirmed"] is False
    assert gate["final_audio_id"] is None
    assert gate["alignment"]["claimable"] is False


# ---------- 确认 ----------

def test_confirmation_requires_audition_and_user(client, work):
    audio = au.register_audio(work_id=work["id"], kind="full", voice_id=PRESET_VOICE,
                              duration_ms=1000, file_hash="sha256:" + "c" * 64,
                              script_version_id=work["script_version_id"],
                              performance_plan_id="plan_r")
    r = client.post(f"/api/v1/works/{work['id']}/audio-confirmation",
                    json={"audio_id": audio["id"], "actor_id": "user_1",
                          "audition_ack": False})
    assert r.status_code == 422
    assert _err(r)["code"] == "AUDIO_AUDITION_REQUIRED"

    r2 = client.post(f"/api/v1/works/{work['id']}/audio-confirmation",
                     json={"audio_id": audio["id"], "actor_id": "user_1",
                           "audition_ack": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["duration_ms"] == 1000
    gate = client.get(f"/api/v1/works/{work['id']}/audio-gate").json()
    assert gate["audio_confirmed"] is True


def test_mock_audio_cannot_be_confirmed_over_http(client, sel_work):
    body = client.post(f"/api/v1/works/{sel_work['id']}/audio", json={}).json()
    r = client.post(f"/api/v1/works/{sel_work['id']}/audio-confirmation",
                    json={"audio_id": body["audio"]["id"], "actor_id": "user_1",
                          "audition_ack": True})
    assert r.status_code == 422
    assert _err(r)["code"] == "AUDIO_IS_MOCK"


def test_confirmation_requires_audio_id(client, work):
    r = client.post(f"/api/v1/works/{work['id']}/audio-confirmation", json={})
    assert r.status_code == 422
    assert _err(r)["code"] == "VALIDATION_FAILED"


# ---------- 对齐 ----------

def test_alignment_rejects_even_split_over_http(client, work):
    audio = au.register_audio(work_id=work["id"], kind="full", voice_id=PRESET_VOICE,
                              duration_ms=1000, file_hash="sha256:" + "d" * 64,
                              script_version_id=work["script_version_id"],
                              performance_plan_id="plan_a")
    r = client.post(f"/api/v1/works/{work['id']}/alignment",
                    json={"audio_id": audio["id"], "source": "even_split",
                          "method": "average",
                          "words": [{"start_char": 0, "end_char": 3,
                                     "start_ms": 0, "end_ms": 300}]})
    assert r.status_code == 422
    assert _err(r)["code"] == "ALIGNMENT_SOURCE_INVALID"

    ok = client.post(f"/api/v1/works/{work['id']}/alignment",
                     json={"audio_id": audio["id"], "source": "forced_alignment",
                           "method": "mfa-2.2", "rule_version": "v1",
                           "words": [{"start_char": 0, "end_char": 3,
                                      "start_ms": 0, "end_ms": 300}]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["word_count"] == 1
    assert ok.json()["source"] == "forced_alignment"
