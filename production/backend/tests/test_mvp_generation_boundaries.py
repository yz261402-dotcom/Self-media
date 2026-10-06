"""离线 Provider 合同回归；不会提交模型或媒体请求。"""
from __future__ import annotations

import io
import json
import struct
import urllib.error
import wave

import pytest

from app import storage as st
from app.agents import models, tools
from app.avatar import person_image as pi
from app.content import generate as gen, llm
from app.content.errors import ContentBlocked, ContentRejected
from app.tts import provider as tts
from app.tts.errors import TtsBlocked, TtsRejected


SECRET = "test-private-provider-token-do-not-expose"


def _response(body):
    return io.BytesIO(json.dumps(body).encode())


@pytest.mark.parametrize("body", [{"choices": []}, [], {"choices": [None]}, {"choices": [{"message": {"content": "[]"}}]}])
def test_deepseek_malformed_envelope_is_domain_error(monkeypatch, body):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", SECRET)
    with pytest.raises(ContentRejected) as err:
        llm.deepseek_json("system", "user", transport=lambda *_: _response(body))
    assert err.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("http", [True, False])
def test_deepseek_failure_does_not_publish_provider_body(monkeypatch, http):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", SECRET)

    def fail(*_):
        if http:
            raise urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(SECRET.encode()))
        raise RuntimeError(SECRET)

    with pytest.raises(ContentBlocked) as err:
        llm.deepseek_json("system", "user", transport=fail)
    assert SECRET not in json.dumps(err.value.to_dict())


def test_injected_deepseek_transport_cannot_create_real_evidence(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", SECRET)
    records = []
    monkeypatch.setattr(llm, "record_provider_evidence", lambda *args: records.append(args))
    result = llm.deepseek_json("system", "user", transport=lambda *_: _response({"choices": [{"message": {"content": '{"ok": true}'}}]}))
    assert result == {"ok": True}
    assert records == []


def test_agent_model_uses_same_provider_configuration(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_MODEL", "configured-text-model")
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_BASE_URL", "https://provider.invalid/v1/")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = models.DeepSeekModel("content")
    assert model.api_key == SECRET
    assert model.model_ref()["name"] == "configured-text-model"
    assert model.base_url == "https://provider.invalid/v1"


def test_unknown_model_backend_cannot_silently_run_mock():
    with pytest.raises(models.CapabilityNotConfigured):
        models.build_model("content", backend="deepseak")


@pytest.mark.parametrize("base", ["https://provider.invalid", "https://provider.invalid/v1", "https://provider.invalid/v1/"])
def test_image_base_url_joins_api_version_once(monkeypatch, base):
    import httpx
    urls = []

    class Client:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def post(self, url, **kwargs):
            urls.append(url)
            return httpx.Response(200, json={"data": []})

    monkeypatch.setenv(pi.KEY_ENV, SECRET)
    monkeypatch.setenv(pi.BASE_ENV, base)
    monkeypatch.setattr(httpx, "Client", Client)
    pi.httpx_transport(pi.build_request(prompt="新人物"))
    assert urls == ["https://provider.invalid/v1/images/generations"]


@pytest.mark.parametrize("body", [[], {"data": [None]}, {"data": [{"b64_json": "not-base64!"}]}, {"data": [{"b64_json": "bm90LWFuLWltYWdl"}]}])
def test_image_malformed_output_is_domain_error(body):
    with pytest.raises(pi.PersonImageBlocked):
        pi.generate(prompt="新人物", transport=lambda *_, **__: {"http_status": 200, "body": body})


def test_image_http_failure_does_not_publish_provider_body():
    with pytest.raises(pi.PersonImageBlocked) as err:
        pi.generate(prompt="新人物", transport=lambda *_, **__: {"http_status": 401, "body": {"error": {"message": SECRET}}})
    assert SECRET not in str(err.value)


@pytest.mark.parametrize("response", [
    {"error": "network_error", "message": SECRET},
    {"status_code": 200, "body": {"base_resp": {"status_code": 1004, "status_msg": SECRET}}},
])
def test_tts_failure_does_not_publish_provider_body(response):
    with pytest.raises(TtsBlocked) as err:
        tts.parse_response(response, requested={"format": "wav"})
    assert SECRET not in json.dumps(err.value.to_dict())


@pytest.mark.parametrize("body", [["bad"], {"data": ["bad"]}, {"base_resp": ["bad"]}])
def test_tts_malformed_envelope_is_domain_error(body):
    with pytest.raises(TtsBlocked) as err:
        tts.parse_response({"status_code": 200, "body": body}, requested={"format": "wav"})
    assert err.value.code == "TTS_PROVIDER_RESPONSE_INVALID"


def test_wav_header_with_incomplete_fmt_chunk_is_rejected():
    corrupt = b"RIFF" + struct.pack("<I", 40) + b"WAVE" + b"JUNK" + struct.pack("<I", 20) + bytes(20) + b"fmt " + struct.pack("<I", 16) + b"\x01"
    with pytest.raises(TtsRejected) as err:
        tts.parse_response({"status_code": 200, "body": {"data": {"audio": corrupt.hex()}}}, requested={"format": "wav"})
    assert err.value.code == "TTS_AUDIO_CORRUPT"


def test_tool_replay_preserves_large_result(monkeypatch):
    payload = {"batch_id": "batch_large", "note": "质量说明" * 4000}
    calls = []
    def execute(*args):
        calls.append(True)
        return payload
    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", execute)
    ctx = {"work_item_id": st.new_id("wi_large")}
    first = tools.invoke("content", "register_script_candidates", {"work_id": "work_large"}, ctx)
    second = tools.invoke("content", "register_script_candidates", {"work_id": "work_large"}, ctx)
    assert first["result"] == second["result"] == payload
    assert calls == [True]


def test_tool_unexpected_exception_is_sanitized_in_audit(monkeypatch):
    def fail(*args):
        raise RuntimeError(SECRET)
    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", fail)
    wid = st.new_id("wi_safe")
    with pytest.raises(tools.ToolDenied) as err:
        tools.invoke("content", "register_script_candidates", {"work_id": "work_safe"}, {"work_item_id": wid})
    assert SECRET not in str(err.value)
    assert SECRET not in json.dumps(st.all_rows("agent_tool_audit", "work_item_id=?", [wid]))


def test_quality_recomputes_claim_evidence_instead_of_trusting_saved_pass():
    wid, cid = st.new_id("w_quality"), st.new_id("script")
    text = "研究表明这个办法能节约百分之九十的时间。"
    st.put("works", wid, {"id": wid, "brief": {"topic": "效率", "materials": []}})
    st.put("script_batches", wid, {"id": st.new_id("batch"), "work_id": wid, "source_kind": "llm", "candidates": [{"id": cid, "text": text, "title": "效率", "claims": [{"id": "claim", "text": text, "verdict": "supported"}], "quality_report": {"overall": "pass", "checks": []}}]})
    result = tools.t_submit_quality_report({"work_id": wid}, {"work_item_id": "quality"})
    assert "FACT_EVIDENCE" in result["hard_gate_failures"]
    candidate = st.get("script_batches", wid)["candidates"][0]
    assert candidate["claims"][0]["verdict"] == "unverified"


def test_revision_cannot_keep_supported_verdict_after_materials_removed():
    text = "这个工具售价九十九元。"
    report, claims = gen.revalidate_revision(work_id="recheck", text=text, title="价格", topic="价格", claims=[{"id": "c", "text": text, "verdict": "supported"}], materials=[])
    assert claims[0].verdict == "unverified"
    assert any(c.code == "FACT_EVIDENCE" and c.outcome != "pass" for c in report.checks)


def _raw_candidates():
    return [
        {"angle": "练习", "title": "先试试看", "text": "你愿意试试看吗？我喜欢先用纸笔写下想法，再慢慢修改。", "claims": []},
        {"angle": "阅读", "title": "随手读书", "text": "想读点什么呢？我习惯在窗边翻开一本书，享受自己的片刻宁静。", "claims": []},
        {"angle": "散步", "title": "走进公园", "text": "出去走走好吗？沿着小路看看树叶，或许心情也会轻松一些。", "claims": []},
        {"angle": "厨房", "title": "做顿饭吧", "text": "今晚想吃什么？给自己煮一碗面，热气腾腾的感觉真好。", "claims": []},
    ]


def test_generation_delivers_at_most_three_candidates(monkeypatch):
    monkeypatch.setattr(gen, "deepseek_json", lambda *_, **__: {"candidates": _raw_candidates()})
    out = gen.generate_candidates(work_id="cap_three", topic="生活观察", max_repair_rounds=0)
    assert out["produced"] == 3


def test_repaired_candidates_are_checked_again_for_duplicate_text(monkeypatch):
    calls = []
    def reply(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return {"candidates": _raw_candidates()[:3]}
        return {"text": "想歇一歇吗？我喜欢靠在窗边，慢慢享受片刻安静。", "claims": []}
    monkeypatch.setattr(gen, "deepseek_json", reply)
    # 明确 60 秒目标才能触发短稿的时长修复；自由时长默认不再强行扩写。
    out = gen.generate_candidates(work_id="repair_dupes", topic="生活观察", target_duration_ms=60000, max_repair_rounds=1)
    assert out["produced"] == 1
    assert out["shortfall_note"]


@pytest.mark.parametrize("field,value", [("text", None), ("text", {"text": "正文"}), ("claims", "断言"), ("tags", {"bad": "shape"})])
def test_invalid_candidate_fields_do_not_become_script_text(monkeypatch, field, value):
    candidate = _raw_candidates()[0]
    candidate[field] = value
    monkeypatch.setattr(gen, "deepseek_json", lambda *_, **__: {"candidates": [candidate]})
    out = gen.generate_candidates(work_id="invalid_candidate", topic="生活观察", max_repair_rounds=0)
    assert out["produced"] == 0
    assert out["shortfall_note"]


@pytest.mark.parametrize("text", [None, {"text": "invalid shape"}])
def test_repair_cannot_turn_nontext_output_into_a_script(monkeypatch, text):
    candidate = gen._as_script_version(_raw_candidates()[0], work_id="repair_shape", topic="生活观察",
                                       materials=[], target_duration_ms=60000)
    monkeypatch.setattr(gen, "deepseek_json", lambda *_, **__: {"text": text, "claims": []})
    with pytest.raises(ContentRejected) as err:
        gen.repair_one(version=candidate, work_id="repair_shape")
    assert err.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("sample_width", [1, 2, 3, 4])
def test_wav_duration_uses_real_block_alignment(sample_width):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setframerate(32000)
        f.setsampwidth(sample_width)
        f.writeframes(bytes(32000 * sample_width))
    assert tts.parse_wav(buf.getvalue())["duration_ms"] == 1000
