"""Claude relay 的 OpenAI 兼容 Chat Completions 协议。"""
import json

from app.content.providers import claude_compat


def test_build_request_matches_relay_compatible_shape():
    url, headers, raw = claude_compat.build_request(
        base_url="https://relay.example", api_key="sk-test", model="claude-sonnet-5-5",
        system="只输出 JSON", user="你好", max_tokens=6144,
    )
    body = json.loads(raw)
    assert url == "https://relay.example/v1/chat/completions"
    assert headers == {"content-type": "application/json", "authorization": "Bearer sk-test"}
    assert body == {
        "model": "claude-sonnet-5-5",
        "messages": [{"role": "system", "content": "只输出 JSON"}, {"role": "user", "content": "你好"}],
        "max_tokens": 6144,
        "stream": False,
    }


def test_parse_response_normalizes_openai_compatible_reply():
    raw = json.dumps({
        "id": "chatcmpl_claude", "model": "claude-sonnet-5-5",
        "choices": [{"message": {"content": "{\"ok\":true}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }).encode()
    result = claude_compat.parse_response(raw)
    assert result["text"] == '{"ok":true}'
    assert result["model"] == "claude-sonnet-5-5"
    assert result["usage"]["input_tokens"] == 12 and result["usage"]["output_tokens"] == 4
