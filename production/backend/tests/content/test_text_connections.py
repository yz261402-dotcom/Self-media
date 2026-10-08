"""断连诊断和显式代理策略；只使用本地替身，不请求真实模型。"""
import json
from http.client import RemoteDisconnected
from urllib.error import URLError

import pytest

from app import jobs
from app.content import tasks
from app.content.errors import ContentBlocked
from app.content.providers import text
from app.content_api import receipt
from tests.content._helpers import make_work, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.delenv("AVATAR_STUDIO_CONTENT_HTTP_PROXY_MODE", raising=False)


@pytest.mark.parametrize("exception", [RemoteDisconnected("private sk-do-not-echo"),
    ConnectionResetError("private sk-do-not-echo"),
    URLError(RemoteDisconnected("private sk-do-not-echo"))])
def test_connection_closed_is_distinct_keeps_reserve_and_never_resends(exception):
    parent = tasks.submit_job(make_work(), "generation", "connection-closed", {})
    sent = []

    def transport(*args):
        sent.append(args)
        raise exception

    with pytest.raises(tasks.CallPending) as caught:
        text.call_text("writer", parent=parent, step="write", system="JSON", user="test", transport=transport)
    assert caught.value.code == "PROVIDER_CONNECTION_CLOSED"
    assert "连接" in caught.value.message and "断开" in caught.value.message
    assert "sk-do-not-echo" not in caught.value.message
    call = tasks.jobs_calls(parent["id"])[0]
    assert call["state"] == "unknown" and call["settlement_state"] == "held"
    assert call["actual_cost_fen"] is None and call["attempts"] == 1
    with pytest.raises(tasks.CallPending):
        text.call_text("writer", parent=parent, step="write", system="JSON", user="test", transport=transport)
    assert len(sent) == 1


def test_direct_transport_does_not_discover_windows_or_environment_proxies(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_HTTP_PROXY_MODE", "direct")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:7897")
    requests = []

    def discover_proxies():
        pytest.fail("直连模式不能读取系统代理")

    class Response:
        status = 200
        def read(self): return b'{}'
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def open_request(self, req, timeout):
        requests.append((self, req, timeout))
        return Response()

    monkeypatch.setattr(text.urllib.request, "getproxies", discover_proxies)
    monkeypatch.setattr(text.urllib.request.OpenerDirector, "open", open_request)
    assert text._default_transport("https://relay.invalid/v1/chat/completions",
        {"authorization": "Bearer test-private"}, b'{}', 600) == (200, b'{}')
    opener, request, timeout = requests[0]
    assert not any(isinstance(h, text.urllib.request.ProxyHandler) for h in opener.handlers)
    assert request.get_header("User-agent") == text.DEFAULT_USER_AGENT and timeout == 600


def test_proxy_mode_changes_writer_runtime_identity(monkeypatch):
    system = text.runtime_profile("writer")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_HTTP_PROXY_MODE", "direct")
    direct = text.runtime_profile("writer")
    assert system["profile_id"] != direct["profile_id"]
    assert direct["http_proxy_mode"] == "direct"
    assert "sk-test" not in json.dumps(direct)


def test_invalid_proxy_mode_blocks_before_reserving_or_sending(monkeypatch):
    parent = tasks.submit_job(make_work(), "generation", "invalid-proxy", {})
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_HTTP_PROXY_MODE", "oops")
    with pytest.raises(ContentBlocked) as caught:
        text.call_text("writer", parent=parent, step="write", system="JSON", user="test",
            transport=lambda *args: pytest.fail("非法代理配置不能发送请求"))
    assert caught.value.code == "TEXT_PROXY_MODE_INVALID"
    assert not tasks.jobs_calls(parent["id"])


@pytest.mark.parametrize("reason", ["RemoteDisconnected: 发送后未收到确定响应", "ConnectionResetError: 发送后未收到确定响应"])
def test_receipt_projects_legacy_connection_error_without_changing_records(reason):
    work_id = make_work()
    parent = tasks.submit_job(work_id, "generation", "legacy-disconnect", {})
    call = jobs.submit_content_call(parent_id=parent["id"], parent_kind="generation", work_id=work_id,
        provider="openai", model="gpt-6.1-sol", capability="chat", idempotency_key=parent["id"] + ":write",
        request_digest="digest", reserve_fen=127, step="write", round_no=0)
    jobs.mark_sending(call["id"], "test")
    jobs.mark_content_unknown(call["id"], reason)
    tasks.finish_job(parent["id"], "unknown", error={"code": "CONTENT_CALL_PENDING", "message": "generic", "detail": call["id"]})
    parent_before, call_before = tasks.get_job(parent["id"]), jobs.get_job(call["id"])
    projected = receipt(parent_before, work_id)
    assert projected["error"]["code"] == "PROVIDER_CONNECTION_CLOSED"
    assert projected["calls"][0]["model"] == "gpt-6.1-sol"
    assert tasks.get_job(parent["id"]) == parent_before and jobs.get_job(call["id"]) == call_before


def test_receipt_does_not_infer_disconnect_from_another_parent_or_arbitrary_error():
    work_id = make_work()
    parent = tasks.submit_job(work_id, "search", "other-parent", {})
    current = tasks.submit_job(work_id, "generation", "current", {})
    call = jobs.submit_content_call(parent_id=parent["id"], parent_kind="search", work_id=work_id,
        provider="openai", model="gpt-6.1-sol", capability="chat", idempotency_key=parent["id"] + ":write",
        request_digest="digest", reserve_fen=127, step="write", round_no=0)
    jobs.mark_sending(call["id"], "test")
    jobs.mark_content_unknown(call["id"], "RemoteDisconnected: private sk-do-not-echo")
    tasks.finish_job(current["id"], "unknown", error={"code": "CONTENT_CALL_PENDING", "message": "generic", "detail": call["id"]})
    projected = receipt(tasks.get_job(current["id"]), work_id)
    assert projected["error"]["code"] == "CONTENT_CALL_PENDING"
    assert "sk-do-not-echo" not in json.dumps(projected)
