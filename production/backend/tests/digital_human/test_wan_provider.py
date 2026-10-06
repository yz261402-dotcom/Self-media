"""Wan official API contracts; entirely offline, with fixture credentials."""
import importlib
import json

import pytest

from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected


@pytest.fixture
def wan():
    spec = importlib.util.find_spec("app.digital_human.wan_provider")
    assert spec is not None, "Wan adapter has not been implemented"
    return importlib.import_module("app.digital_human.wan_provider")


def request(wan, **changes):
    args = dict(image_url="https://media.example/image.png", audio_url="https://media.example/voice.wav",
                prompt="人物自然讲述", audio_duration_ms=1001, resolution="720p", api_key="fixture-secret",
                base_url="https://workspace123.cn-beijing.maas.aliyuncs.com/api/v1")
    return wan.build_submit_request(**dict(args, **changes))


def test_official_reference_media_request_and_async_auth(wan):
    req = request(wan)
    assert req["url"].endswith("/api/v1/services/aigc/video-generation/video-synthesis")
    assert req["method"] == "POST"
    assert req["headers"]["X-DashScope-Async"] == "enable"
    assert req["headers"]["Authorization"] == "Bearer fixture-secret"
    effective = req["json"]["input"]["prompt"]
    assert "音频1" in effective and "不新增人声" in effective and "人物自然讲述" in effective
    assert req["json"] == {
        "model": "wan3.0-video", "input": {"prompt": effective, "media": [
            {"type": "reference_image", "url": "https://media.example/image.png"},
            {"type": "reference_audio", "url": "https://media.example/voice.wav"}]},
        "parameters": {"resolution": "720P", "ratio": "9:16", "duration": 2, "audio": True}}
    assert json.loads(req["body"]) == req["json"]


@pytest.mark.parametrize("milliseconds,seconds", [(1000, 2), (2001, 3), (14999, 15), (15000, 15)])
def test_generation_seconds_cover_complete_audio(wan, milliseconds, seconds):
    assert request(wan, audio_duration_ms=milliseconds)["json"]["parameters"]["duration"] == seconds


@pytest.mark.parametrize("changes", [{"audio_duration_ms": 999}, {"audio_duration_ms": 15001},
                                     {"prompt": " "}, {"resolution": "4k"},
                                     {"image_url": "file:///x.png"}, {"audio_url": ""}])
def test_invalid_input_fails_before_network(wan, changes):
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)):
        request(wan, **changes)


@pytest.mark.parametrize("url", ["https://third-party.example/api/v1", "http://dashscope.aliyuncs.com/api/v1",
                                  "https://dashscope.aliyuncs.com.evil.example/api/v1",
                                  "https://user:secret@dashscope.aliyuncs.com/api/v1",
                                  "https://dashscope.aliyuncs.com/api/v1?secret=x"])
def test_key_is_never_directed_to_untrusted_base(wan, url):
    with pytest.raises(TalkingHeadBlocked) as raised:
        request(wan, base_url=url)
    assert raised.value.code == "WAN_ENDPOINT_INVALID"
    assert "secret" not in raised.value.message


def test_submit_parser_and_original_task_get(wan):
    result = wan.parse_submit_response({"status_code": 200, "json": {
        "output": {"task_id": "task-original", "task_status": "PENDING"}, "request_id": "request-fixture"}})
    assert result["remote_job_id"] == "task-original"
    assert result["state"] == "submitted"
    req = wan.build_query_request(task_id=result["remote_job_id"], api_key="fixture-secret",
                                 base_url="https://workspace123.cn-beijing.maas.aliyuncs.com/api/v1")
    assert req["method"] == "GET"
    assert req["url"].endswith("/api/v1/tasks/task-original")
    assert not req.get("body")


@pytest.mark.parametrize("response", [{"status_code": 503}, {"status_code": None, "error": "network"},
                                       {"status_code": 200, "json": {"output": "malformed"}},
                                       {"status_code": 200, "body": b"[]"},
                                       {"status_code": 200, "json": {"output": {"task_status": "PENDING"}}}])
def test_submit_without_acceptance_evidence_is_unknown(wan, response):
    assert wan.parse_submit_response(response)["state"] == "unknown"


@pytest.mark.parametrize("status,code", [(401, "WAN_AUTH_FAILED"), (403, "WAN_AUTH_FAILED"),
                                        (429, "WAN_QUOTA_EXHAUSTED"), (400, "WAN_INPUT_REJECTED")])
def test_explicit_rejection_is_distinct_from_uncertain_submit(wan, status, code):
    with pytest.raises((TalkingHeadBlocked, TalkingHeadRejected)) as raised:
        wan.parse_submit_response({"status_code": status, "json": {"message": "fixture-secret"}})
    assert raised.value.code == code
    assert "fixture-secret" not in raised.value.message


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("provider_code", ["Arrearage", "InsufficientBalance", "QuotaExceeded"])
def test_explicit_quota_code_takes_precedence_over_auth_http_status(wan, status, provider_code):
    with pytest.raises(TalkingHeadBlocked) as raised:
        wan.parse_submit_response({"status_code": status, "json": {
            "code": provider_code, "message": "Bearer fixture-secret; account-id-private"}})
    assert raised.value.code == "WAN_QUOTA_EXHAUSTED"
    assert "充值" in raised.value.message and "显式重新生成" in raised.value.message
    safe_error = json.dumps(raised.value.to_dict())
    assert "fixture-secret" not in safe_error and "account-id-private" not in safe_error


@pytest.mark.parametrize("status,provider_code", [(401, "InvalidApiKey"), (403, "AccessDenied"),
                                                  (403, "")])
def test_auth_status_is_not_reclassified_from_provider_message(wan, status, provider_code):
    with pytest.raises(TalkingHeadBlocked) as raised:
        wan.parse_submit_response({"status_code": status, "json": {
            "code": provider_code, "message": "InsufficientBalance quota fixture-secret"}})
    assert raised.value.code == "WAN_AUTH_FAILED"
    assert "fixture-secret" not in json.dumps(raised.value.to_dict())


@pytest.mark.parametrize("remote,state", [("PENDING", "running"), ("RUNNING", "running"),
                                          ("FAILED", "failed"), ("CANCELED", "failed"),
                                          ("UNKNOWN", "unknown")])
def test_poll_status_mapping(wan, remote, state):
    assert wan.parse_query_response({"status_code": 200, "json": {
        "output": {"task_id": "original", "task_status": remote}}})["state"] == state


@pytest.mark.parametrize("provider_code", ["Arrearage", "InsufficientBalance", "QuotaExceeded"])
def test_poll_failed_quota_returns_safe_actionable_error(wan, provider_code):
    result = wan.parse_query_response({"status_code": 200, "json": {"output": {
        "task_id": "original", "task_status": "FAILED", "code": provider_code,
        "message": "Bearer fixture-secret; account-id-private"}}})
    assert result["state"] == "failed"
    assert result.get("error_code") == "WAN_QUOTA_EXHAUSTED"
    assert "充值" in result["reason"] and "显式重新生成" in result["reason"]
    safe_result = json.dumps(result)
    assert "fixture-secret" not in safe_result and "account-id-private" not in safe_result


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("provider_code", ["Arrearage", "InsufficientBalance", "QuotaExceeded"])
def test_poll_channel_quota_error_does_not_fail_original_task(wan, status, provider_code):
    result = wan.parse_query_response({"status_code": status, "json": {
        "code": provider_code, "message": "fixture-secret", "output": {
            "task_id": "original", "task_status": "FAILED", "code": provider_code}}})
    assert result["state"] == "unavailable"
    assert "error_code" not in result
    assert "fixture-secret" not in json.dumps(result)


def test_poll_failure_does_not_infer_quota_from_provider_message(wan):
    result = wan.parse_query_response({"status_code": 200, "json": {"output": {
        "task_id": "original", "task_status": "FAILED", "code": "InvalidParameter",
        "message": "InsufficientBalance quota fixture-secret"}}})
    assert result["state"] == "failed"
    assert result.get("error_code") != "WAN_QUOTA_EXHAUSTED"
    assert "fixture-secret" not in json.dumps(result)


def test_poll_success_requires_download_url_and_keeps_24h_expiry_distinct(wan):
    success = {"status_code": 200, "json": {"output": {"task_status": "SUCCEEDED",
               "video_url": "https://media.example/result.mp4"}}}
    assert wan.parse_query_response(success)["video_url"].endswith("result.mp4")
    assert wan.parse_query_response({"status_code": 404})["state"] == "unknown"
    assert wan.parse_query_response({"status_code": 503})["state"] == "unavailable"


def test_authenticated_transport_refuses_redirect_without_exposing_key(wan, monkeypatch):
    import urllib.error
    import urllib.request
    seen = []
    class Opener:
        def open(self, req, timeout):
            seen.append(req)
            raise urllib.error.HTTPError(req.full_url, 302, "redirect", {"Location": "https://evil.example"}, None)
    def build(*handlers):
        assert any(isinstance(h, urllib.request.HTTPRedirectHandler) and
                   h.redirect_request(None, None, 302, "", {}, "https://evil.example") is None
                   for h in handlers)
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    assert wan.urllib_transport(request(wan))["status_code"] == 302
    assert len(seen) == 1


def test_missing_workspace_is_visible_configuration_blocker(wan, monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_WAN_BASE_URL", raising=False)
    monkeypatch.delenv("AVATAR_STUDIO_WAN_WORKSPACE_ID", raising=False)
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "fixture-secret")
    status = wan.adapter_status()
    assert status["state"] == "not_configured"
    assert status["configured"] is False and status["verified"] is False
    assert "fixture-secret" not in json.dumps(status)
    monkeypatch.setenv("AVATAR_STUDIO_WAN_WORKSPACE_ID", "fixture123")
    assert wan.endpoint() == "https://fixture123.cn-beijing.maas.aliyuncs.com/api/v1"
    assert wan.adapter_status()["configured"] is True
    assert wan.adapter_status()["verified"] is False


def test_prompt_length_uses_official_twenty_thousand_character_boundary(wan):
    assert len(request(wan, prompt="a" * 5001)["json"]["input"]["prompt"]) > 5001
    prefix = wan.AUDIO_INSTRUCTION + "\n画面描述："
    assert len(request(wan, prompt="a" * (20000 - len(prefix)))["json"]["input"]["prompt"]) == 20000
    with pytest.raises(TalkingHeadRejected) as raised:
        request(wan, prompt="a" * (20001 - len(prefix)))
    assert raised.value.code == "WAN_PROMPT_TOO_LONG"
