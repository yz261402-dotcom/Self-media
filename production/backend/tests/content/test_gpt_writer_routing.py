"""GPT 写作与审核仍使用既有任务和账本，不把渠道参考价当实际费用。"""
import json

import pytest

from app.content import tasks
from app.content.errors import ContentBlocked, ContentRejected
from app.content.providers import text
from tests.content.test_text_providers import env, parent, Stub  # noqa: F401


@pytest.fixture(autouse=True)
def gpt_settings(monkeypatch, env):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_PROVIDER", "openai")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_EXECUTION_MODE", "http_api")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_MAX_OUTPUT", "16384")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_WRITER_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("AVATAR_STUDIO_OPENAI_API_KEY", "fake-gpt-key")
    monkeypatch.setenv("AVATAR_STUDIO_OPENAI_BASE_URL", "https://relay.example/v1")


def reply(**updates):
    return {"id": "chat_gpt_test", "model": "gpt-6.1-sol", "choices": [{"message": {
        "content": '{"candidates":[]}', **updates}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 900,
            "completion_tokens_details": {"reasoning_tokens": 400},
            "prompt_tokens_details": {"cached_tokens": 300}}}


def test_gpt_writer_uses_exact_model_and_http_budget_lease_once():
    job = parent()
    requests = []
    def transport(url, headers, raw, timeout):
        requests.append((url,headers,json.loads(raw),timeout))
        call=tasks.jobs_calls(job["id"])[0]
        assert call["lease"]["expires_at"]-call["lease"]["acquired_at"] >= 660
        return 200,json.dumps(reply()).encode()
    result=text.call_text("writer",parent=job,step="write",system="s",user="u",transport=transport)
    assert len(requests)==1 and requests[0][0]=="https://relay.example/v1/chat/completions"
    body=requests[0][2]
    assert body["model"]=="gpt-6.1-sol" and body["max_completion_tokens"]==16384
    assert body["response_format"]=={"type":"json_object"} and "max_tokens" not in body
    assert result["usage"]["output_tokens"]==900
    call=tasks.jobs_calls(job["id"])[0]
    assert call["state"]=="succeeded" and call["settlement_state"]=="held" and call["actual_cost_fen"] is None
    assert call["price_snapshot"]["price_verified"] is False
    text.call_text("writer",parent=job,step="write",system="s",user="u",transport=transport)
    assert len(requests)==1
    assert text.profile("repair")["provider"]=="openai"
    assert text.profile("planner")["provider"]=="openai" and text.profile("planner")["model"]=="gpt-6-astra"
    assert text.profile("reviewer")["provider"]=="openai" and text.profile("reviewer")["model"]=="gpt-6-astra"


@pytest.mark.parametrize("role,model", [
    ("writer", "gpt-6.1-sol"), ("repair", "gpt-6.1-sol"),
    ("planner", "gpt-6-astra"), ("reviewer", "gpt-6-astra"),
])
def test_gpt_roles_send_exact_model_with_shared_url_and_key(role, model):
    job = parent()
    requests = []

    def transport(url, headers, raw, timeout):
        requests.append((url, headers, json.loads(raw)))
        return 200, json.dumps(dict(reply(), model=model)).encode()

    result = text.call_text(role, parent=job, step=role, system="JSON", user="test", transport=transport)
    assert len(requests) == 1
    url, headers, body = requests[0]
    assert url == "https://relay.example/v1/chat/completions"
    assert headers["authorization"] == "Bearer fake-gpt-key"
    assert body["model"] == model and result["model"] == model
    call = tasks.jobs_calls(job["id"])[0]
    assert call["provider"] == "openai" and call["model"] == model
    assert call["price_snapshot"]["model"] == model
    assert call["price_snapshot"]["price_verified"] is False
    assert call["actual_cost_fen"] is None


def test_gpt_refusal_keeps_known_response_and_usage():
    job=parent()
    with pytest.raises(ContentRejected) as exc:
        text.call_text("writer",parent=job,step="write",system="s",user="u",
                       transport=Stub(body=reply(content=None,refusal="private provider text")))
    assert exc.value.code=="MODEL_REFUSED" and "private" not in exc.value.message
    call=tasks.jobs_calls(job["id"])[0]
    assert call["state"]=="succeeded" and call["usage"]["output_tokens"]==900


def test_gpt_credential_is_separate_from_image_credential(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_OPENAI_API_KEY")
    monkeypatch.setenv("AVATAR_STUDIO_GPTIMAGE_API_KEY","fake-image-key")
    job=parent()
    with pytest.raises(ContentBlocked) as exc:
        text.call_text("writer",parent=job,step="write",system="s",user="u",transport=Stub(body=reply()))
    assert exc.value.code=="PROVIDER_NOT_CONFIGURED" and tasks.jobs_calls(job["id"])==[]
