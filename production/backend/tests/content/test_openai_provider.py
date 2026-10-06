"""GPT writer adapter: protocol-only tests, no network or paid calls."""
import json

import pytest

from app.content.errors import ContentRejected
from app.content.providers import openai


def build(**overrides):
    kwargs = {"base_url": "https://aihubmix.com/v1", "api_key": "sk-only-header",
              "model": "gpt-6.1-sol", "system": "写三个备选", "user": "参考材料",
              "max_tokens": 16384}
    kwargs.update(overrides)
    return openai.build_request(**kwargs)


def response(**overrides):
    data = {"id": "chatcmpl_test", "model": "gpt-6.1-sol", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": '{"scripts":[]}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 60,
                      "prompt_tokens_details": {"cached_tokens": 20},
                      "completion_tokens_details": {"reasoning_tokens": 25}}}
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("base,url", [("", "https://api.openai.com/v1/chat/completions"),
    ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
    ("https://api.openai.com/v1/", "https://api.openai.com/v1/chat/completions"),
    ("https://aihubmix.com/v1", "https://aihubmix.com/v1/chat/completions")])
def test_endpoint_includes_v1_exactly_once(base, url):
    assert openai.endpoint(base) == url


def test_request_uses_exact_model_budget_developer_and_json(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_CONTENT_GPT_REASONING_EFFORT", raising=False)
    url, headers, raw = build()
    body = json.loads(raw)
    assert url == "https://aihubmix.com/v1/chat/completions"
    assert body["model"] == "gpt-6.1-sol" and body["max_completion_tokens"] == 16384
    assert "max_tokens" not in body
    assert body["messages"][0]["role"] == "developer" and "JSON" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "参考材料"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "medium" and body["stream"] is False and body["n"] == 1
    assert "tools" not in body and "temperature" not in body and "top_p" not in body
    assert headers["authorization"] == "Bearer sk-only-header" and "sk-only-header" not in raw.decode()


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_reasoning_effort_configured_or_explicit_override(monkeypatch, effort):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_GPT_REASONING_EFFORT", effort)
    assert json.loads(build()[2])["reasoning_effort"] == effort
    assert json.loads(build(extra={"reasoning_effort": "low"})[2])["reasoning_effort"] == "low"


@pytest.mark.parametrize("extra", [{"temperature": 1}, {"top_p": 0.9}, {"tools": []},
    {"max_tokens": 1}, {"model": "another"}, {"reasoning_effort": "none"},
    {"reasoning_effort": "minimal"}])
def test_unsupported_parameters_are_rejected_before_call(extra):
    with pytest.raises(ContentRejected) as caught:
        build(extra=extra)
    assert caught.value.code == "PARAM_UNSUPPORTED"


def test_invalid_effort_environment_rejected(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_GPT_REASONING_EFFORT", "none")
    with pytest.raises(ContentRejected):
        build()


@pytest.mark.parametrize("budget", [0, -1, True, "100", 1.2])
def test_invalid_output_budget_rejected(budget):
    with pytest.raises(ContentRejected):
        build(max_tokens=budget)


def test_usage_keeps_totals_without_double_counting_reasoning_or_cached_tokens():
    result = openai.parse_response(response())
    assert result["text"] == '{"scripts":[]}' and result["model"] == "gpt-6.1-sol"
    assert result["finish_reason"] == "stop" and result["provider_request_id"] == "chatcmpl_test"
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 60,
                               "cache_read_input_tokens": 20, "cache_creation_input_tokens": None,
                               "reasoning_tokens": 25}


def test_missing_usage_is_not_fabricated_zero():
    assert openai.parse_response(response(usage=None))["usage"] is None


def test_missing_usage_detail_fields_remain_unknown():
    result = openai.parse_response(response(usage={"prompt_tokens": 1, "completion_tokens": 2}))
    assert result["usage"]["cache_read_input_tokens"] is None
    assert result["usage"]["cache_creation_input_tokens"] is None
    assert result["usage"]["reasoning_tokens"] is None


def test_cache_creation_and_read_counts_preserved_separately():
    usage = {"prompt_tokens": 100, "completion_tokens": 60,
             "prompt_tokens_details": {"cached_tokens": 20, "cache_creation_tokens": 30}}
    result = openai.parse_response(response(usage=usage))
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["cache_read_input_tokens"] == 20
    assert result["usage"]["cache_creation_input_tokens"] == 30


def test_standard_cache_write_tokens_preserved():
    usage = {"prompt_tokens": 100, "completion_tokens": 60,
             "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 35}}
    result = openai.parse_response(response(usage=usage))
    assert result["usage"]["input_tokens"] == 100
    assert result["usage"]["cache_read_input_tokens"] == 20
    assert result["usage"]["cache_creation_input_tokens"] == 35


def test_conflicting_cache_write_aliases_rejected():
    usage = {"prompt_tokens": 100, "completion_tokens": 60,
             "prompt_tokens_details": {"cache_write_tokens": 35, "cache_creation_tokens": 30}}
    with pytest.raises(ContentRejected) as caught:
        openai.parse_response(response(usage=usage))
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_truncation_is_preserved_for_existing_call_text_guard():
    choices = [{"message": {"content": '{"scripts":'}, "finish_reason": "length"}]
    assert openai.parse_response(response(choices=choices))["finish_reason"] == "length"


def test_explicit_refusal_preserves_received_usage_without_echoing_provider_text():
    choices = [{"message": {"content": None, "refusal": "secret-message sk-do-not-echo"},
                "finish_reason": "stop"}]
    result = openai.parse_response(response(choices=choices))
    assert result["refusal"] == "模型拒绝了本次内容请求"
    assert result["usage"]["input_tokens"] == 100 and result["provider_request_id"] == "chatcmpl_test"
    assert "secret-message" not in json.dumps(result) and "sk-do-not-echo" not in json.dumps(result)


@pytest.mark.parametrize("raw", [b"broken", b"[]", b"{}", response(choices=[]),
    response(choices=[{"message": {"content": [{"text": "not-chat-text"}]}}]),
    response(usage={"prompt_tokens": True, "completion_tokens": 5})])
def test_malformed_chat_response_rejected(raw):
    with pytest.raises(ContentRejected) as caught:
        openai.parse_response(raw)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"
