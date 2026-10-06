"""Qwen 视觉 provider：请求形状、密钥位置、响应解析与时间线校验。全部离线。"""
import base64
import json

import pytest

from app.content.errors import ContentRejected
from app.content.providers import qwen_vision as qv

FRAMES = [{"timestamp_ms": 0, "jpeg_bytes": b"\xff\xd8AAA", "sha256": "sha256:a"},
          {"timestamp_ms": 1500, "jpeg_bytes": b"\xff\xd8BBB", "sha256": "sha256:b"}]
AUDIO = {"speech_presence": "none", "music_presence": "music", "tempo_bpm": 128.4, "beat_times_ms": [468, 937],
         "confidence": 0.74}


def good(**kw):
    d = {"summary": "竖屏单人舞蹈", "speech_presence": "none", "music_presence": "music", "warnings": [],
         "scenes": [{"index": 1, "start_ms": 0, "end_ms": 3000, "shot_type": "full", "camera_motion": "static",
                     "description": "正面舞蹈", "actions": ["抬手"], "on_screen_text": [], "beat_cues_ms": [468],
                     "confidence": 0.8}]}
    d.update(kw)
    return d


def reply(obj, status=200):
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return json.dumps({"id": "req-1", "model": "qwen3-vl-flash", "choices": [{"message": {"content": body},
                                                                            "finish_reply": "stop", "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}).encode()


def test_build_request_has_model_json_format_frames_audio_and_key_only_in_header():
    url, headers, body = qv.build_request(api_key="sk-secret", model="qwen3-vl-flash", system="sys", user="usr",
                                          frames=FRAMES, audio_summary=AUDIO, duration_ms=3000, max_tokens=2048)
    assert url.endswith("/chat/completions") and headers["authorization"] == "Bearer sk-secret"
    assert b"sk-secret" not in body
    data = json.loads(body)
    assert data["model"] == "qwen3-vl-flash" and data["response_format"] == {"type": "json_object"}
    content = data["messages"][1]["content"]
    images = [c for c in content if c["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(FRAMES[0]["jpeg_bytes"]).decode()
    text = "\n".join(c["text"] for c in content if c["type"] == "text")
    assert "128.4" in text and "0 ms" in text and "1500 ms" in text and "3000" in text
    for field in ("summary", "speech_presence", "music_presence", "scenes", "warnings", "start_ms", "end_ms"):
        assert field in text
    assert "JSON" in data["messages"][0]["content"]


def test_build_request_rejects_empty_and_oversized_frame_lists():
    with pytest.raises(ContentRejected):
        qv.build_request(api_key="k", model="m", system="s", user="u", frames=[], audio_summary={}, duration_ms=1000,
                         max_tokens=10)
    with pytest.raises(ContentRejected):
        qv.build_request(api_key="k", model="m", system="s", user="u", frames=FRAMES * 60, audio_summary={},
                         duration_ms=1000, max_tokens=10)


def test_parse_response_returns_normalized_analysis_and_usage():
    out = qv.parse_response(reply(good()), duration_ms=3000)
    assert out["analysis"]["scenes"][0]["end_ms"] == 3000 and out["analysis"]["speech_presence"] == "none"
    assert out["usage"] == {"input_tokens": 1200, "output_tokens": 300}
    assert out["provider_request_id"] == "req-1" and out["model"] == "qwen3-vl-flash"


def test_parse_response_accepts_json_in_code_fence():
    out = qv.parse_response(reply("```json\n" + json.dumps(good()) + "\n```"), duration_ms=3000)
    assert out["analysis"]["summary"] == "竖屏单人舞蹈"


@pytest.mark.parametrize("raw", [
    "not json at all",
    json.dumps([1, 2]),
    good(scenes=[]),
    {k: v for k, v in good().items() if k != "scenes"},
    good(scenes=[{"index": 1, "start_ms": 2000, "end_ms": 1000, "description": "倒序"}]),
    good(scenes=[{"index": 1, "start_ms": 0, "end_ms": 9000, "description": "超出视频"}]),
    good(scenes=[{"index": 1, "start_ms": 0, "end_ms": 2000, "description": "a"},
                 {"index": 2, "start_ms": 1000, "end_ms": 3000, "description": "重叠"}]),
    good(speech_presence="maybe"),
    good(warnings=3),
    good(scenes=[{"start_ms": 0, "end_ms": 3000, "description": "手势", "actions": 3}]),
    good(scenes=[{"start_ms": 0, "end_ms": 3000, "description": "手势", "actions": "抬手"}]),
    good(scenes=[{"start_ms": 0, "end_ms": 3000, "description": "手势", "beat_cues_ms": [float('inf')]}]),
])
def test_invalid_model_output_is_rejected(raw):
    with pytest.raises(ContentRejected) as e:
        qv.parse_response(reply(raw), duration_ms=3000)
    assert e.value.code == "MODEL_OUTPUT_INVALID"


def test_truncated_output_and_bad_envelope_are_rejected():
    truncated = json.dumps({"id": "r", "model": "m", "choices": [{"message": {"content": "{"}, "finish_reason": "length"}]})
    with pytest.raises(ContentRejected) as e:
        qv.parse_response(truncated.encode(), duration_ms=3000)
    assert e.value.code in ("OUTPUT_TRUNCATED", "MODEL_OUTPUT_INVALID")
    with pytest.raises(ContentRejected) as e:
        qv.parse_response(b"<html>", duration_ms=3000)
    assert e.value.code == "MODEL_OUTPUT_INVALID"


def test_analyze_frames_uses_transport_once_and_never_retries():
    calls = []

    def transport(url, headers, body, timeout):
        calls.append(json.loads(body))
        return 200, reply(good())

    out = qv.analyze_frames(model="qwen3-vl-flash", frames=FRAMES, audio_summary=AUDIO, duration_ms=3000,
                            api_key="k", transport=transport)
    assert len(calls) == 1 and out["analysis"]["scenes"]

    def refuse(url, headers, body, timeout):
        return 401, b'{"error":{"code":"invalid_api_key","message":"secret detail"}}'

    from app.content.tasks import ProviderRefused
    with pytest.raises(ProviderRefused) as e:
        qv.analyze_frames(model="m", frames=FRAMES, audio_summary=AUDIO, duration_ms=3000, api_key="k",
                          transport=refuse)
    assert e.value.code == "AUTH_FAILED" and "secret detail" not in e.value.message


def test_default_quality_model_and_output_budget(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", raising=False)
    assert qv.model() == "qwen3-vl-plus"
    _, _, raw = qv.build_request(api_key="k", model=qv.model(), frames=FRAMES,
                                 audio_summary=AUDIO, duration_ms=3000)
    body = json.loads(raw)
    assert body["max_tokens"] >= 8192
    assert body["enable_thinking"] is False


@pytest.mark.parametrize("scenes", [
    [{"start_ms": 1000, "end_ms": 2000, "description": "只看到了中间一秒"}],
    [{"start_ms": 0, "end_ms": 1000, "description": "开头"},
     {"start_ms": 2000, "end_ms": 3000, "description": "结尾"}],
    [{"start_ms": 0, "end_ms": 1000, "description": "遗漏了后半段"}],
    [{"start_ms": 0, "end_ms": 3000, "description": ""}],
])
def test_incomplete_observation_cannot_be_presented_as_complete(scenes):
    with pytest.raises(ContentRejected) as e:
        qv.parse_response(reply(good(scenes=scenes)), duration_ms=3000)
    assert e.value.code == "MODEL_OUTPUT_INVALID"


def length_envelope(text):
    return {"text": text, "finish_reason": "length", "usage": {"input_tokens": 24500, "output_tokens": 8192},
            "model": "qwen3-vl-plus", "provider_request_id": "saved-original-request"}


def ordered_core(**kw):
    obj = good(**kw)
    return {key: obj[key] for key in ("summary", "speech_presence", "music_presence", "scenes") if key in obj}


def core_then_warnings(tail='["采样间距不均", "后一条未完'):
    return json.dumps(ordered_core(), ensure_ascii=False)[:-1] + ', "warnings":' + tail


def test_length_output_with_only_warning_tail_cut_preserves_complete_core_and_warning_items():
    env = length_envelope(core_then_warnings())
    original = json.dumps(env, ensure_ascii=False)
    out = qv.parse_analysis(env, duration_ms=3000)
    assert out["scenes"] == qv.parse_analysis({"text": json.dumps(good())}, duration_ms=3000)["scenes"]
    assert out["summary"] == "竖屏单人舞蹈"
    assert out["warnings"][0] == "采样间距不均"
    assert "后一条未完" not in out["warnings"]
    assert any("末尾注意事项" in w and "缺失" in w for w in out["warnings"])
    assert any("采样" in w and "视频结束" in w for w in out["warnings"])
    assert json.dumps(env, ensure_ascii=False) == original


@pytest.mark.parametrize("tail", ['[', '["完整项"', '["完整项",', '["末项\\', '["末项\\u12',
                                    '["完整项"]', '["完整项"]}'])
def test_length_warning_eof_accepts_only_complete_items_and_valid_prefixes(tail):
    out = qv.parse_analysis(length_envelope(core_then_warnings(tail)), duration_ms=3000)
    assert out["scenes"][-1]["end_ms"] == 3000
    assert "末项" not in out["warnings"]


def test_complete_json_marked_length_is_validated_with_notice_and_does_not_call_transport():
    out = qv.parse_analysis(length_envelope(json.dumps(good(), ensure_ascii=False)), duration_ms=3000)
    assert out["summary"] == "竖屏单人舞蹈"
    assert out["warnings"]


@pytest.mark.parametrize("text", [
    core_then_warnings('["非法\\q'),
    core_then_warnings('["非法\\u1X'),
    core_then_warnings('["含原始\n换行'),
    core_then_warnings('["末尾原始换行\n'),
    core_then_warnings('[123'),
    core_then_warnings('[null'),
    core_then_warnings('[{}'),
    core_then_warnings('["完整项" "缺逗号'),
    core_then_warnings('["完整项"], "extra":'),
    core_then_warnings('["完整项"]} trailing'),
    json.dumps(ordered_core(), ensure_ascii=False)[:-1],
    json.dumps(ordered_core(), ensure_ascii=False).replace('"start_ms": 0', '"start_ms": 0, "start_ms": 0'),
    json.dumps(ordered_core(), ensure_ascii=False)[:-1] + ', "summary":"重复", "warnings":["未完',
    json.dumps(dict(ordered_core(), extra="unexpected"), ensure_ascii=False),
    json.dumps(ordered_core(), ensure_ascii=False).replace('"end_ms": 3000', '"end_ms": NaN'),
])
def test_length_recovery_rejects_malformed_or_nonwarning_truncation(text):
    with pytest.raises(ContentRejected) as e:
        qv.parse_analysis(length_envelope(text), duration_ms=3000)
    assert e.value.code in ("OUTPUT_TRUNCATED", "MODEL_OUTPUT_INVALID")


@pytest.mark.parametrize("obj", [
    {k: v for k, v in ordered_core().items() if k != "speech_presence"},
    {k: v for k, v in ordered_core().items() if k != "music_presence"},
    ordered_core(summary=""),
    ordered_core(summary={"invented": "summary"}),
    ordered_core(scenes=[dict(good()["scenes"][0], start_ms=True)]),
    ordered_core(scenes=[dict(good()["scenes"][0], end_ms=3000.2)]),
    ordered_core(scenes=[dict(good()["scenes"][0], end_ms="3000")]),
    ordered_core(scenes=[dict(good()["scenes"][0], description=["not a description"])]),
    ordered_core(scenes=[dict(good()["scenes"][0], shot_type={"type": "full"})]),
    ordered_core(scenes=[dict(good()["scenes"][0], camera_motion=["static"])]),
    ordered_core(scenes=[dict(good()["scenes"][0], index=True)]),
    ordered_core(scenes=[dict(good()["scenes"][0], index="1")]),
    ordered_core(scenes=[dict(good()["scenes"][0], index=1.5)]),
    ordered_core(scenes=[dict(good()["scenes"][0], index=0)]),
    ordered_core(scenes=[dict(good()["scenes"][0], index=-1)]),
    ordered_core(scenes=[dict(good()["scenes"][0], actions=[{"invented": "action"}])]),
    ordered_core(scenes=[dict(good()["scenes"][0], end_ms=2500)]),
    ordered_core(scenes=[dict(good()["scenes"][0], start_ms=100)]),
    ordered_core(scenes=[dict(good()["scenes"][0], end_ms=1000),
                        dict(good()["scenes"][0], start_ms=1500)]),
])
def test_length_recovery_requires_explicit_complete_core_and_exact_timeline(obj):
    text = json.dumps(obj, ensure_ascii=False)[:-1] + ', "warnings":["末项未完'
    with pytest.raises(ContentRejected) as e:
        qv.parse_analysis(length_envelope(text), duration_ms=3000)
    assert e.value.code in ("OUTPUT_TRUNCATED", "MODEL_OUTPUT_INVALID")


@pytest.mark.parametrize("model,budget", [("qwen3-vl-plus", 32768), ("qwen3-vl-plus-2025-12-19", 32768),
                                         ("qwen3-vl-flash", 8192), ("unknown-model", 8192)])
def test_default_output_budget_matches_model_capacity(model, budget):
    _, _, raw = qv.build_request(api_key="k", model=model, frames=FRAMES, audio_summary=AUDIO, duration_ms=3000)
    assert json.loads(raw)["max_tokens"] == budget


def test_quality_profile_prompt_preserves_action_detail_and_distinguishes_sampling_from_video_end(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", "qwen3-vl-plus")
    assert qv.ANALYSIS_VERSION == "visual-v3"
    assert qv.status()["max_output_tokens"] == 32768
    _, _, raw = qv.build_request(api_key="k", model="qwen3-vl-plus", frames=FRAMES,
                                 audio_summary=AUDIO, duration_ms=3000)
    prompt = json.loads(raw)["messages"][0]["content"]
    assert "紧凑" in prompt and "重复" in prompt
    assert "最后一张采样帧" in prompt and "空白" in prompt
    assert "左右手/腿" in prompt and "完整" in prompt
