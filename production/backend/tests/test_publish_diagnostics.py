"""发布诊断失败必须可读且脱敏；真实浏览器仅使用本地空页面。"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.publishing import adapters, api, service


@pytest.fixture
def diagnostic_client(monkeypatch):
    adapter = adapters.PlaywrightDouyinAdapter()
    monkeypatch.setattr(service, 'get_adapter', lambda platform: adapter)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def browser_host(monkeypatch):
    host = adapters._BrowserHost()
    monkeypatch.setattr(adapters, '_host', host)

    def open_local_page():
        from playwright.sync_api import sync_playwright
        host.runtime = sync_playwright().start()
        browser = host.runtime.chromium.launch(channel='chrome', headless=True)
        host.context = browser.new_context()
        host.context.route('**/*', lambda route: route.abort())
        page = host.context.new_page()
        page.set_content('<p>本地诊断</p><input placeholder="作品标题" value="private-title">'
                         '<input type="hidden" value="private-token"><button>发布</button>')
        host.driver = adapters.DouyinPage(page, timeout_ms=150)
        host.driver.phase = 'uploading_video'

    host.call(open_local_page)
    try:
        yield host
    finally:
        host.call(host.close)


def error_host(monkeypatch, error):
    def inspect_controls():
        raise error

    host = SimpleNamespace(busy=False, last_error=None, opened=lambda: True,
                           driver=SimpleNamespace(phase='uploading_video', inspect_controls=inspect_controls),
                           call=lambda fn: fn())
    monkeypatch.setattr(adapters, '_host', host)
    return host


def test_unreadable_page_diagnostics_return_a_controlled_response(browser_host, diagnostic_client):
    browser_host.call(lambda: browser_host.driver.page.evaluate('document.body.remove()'))

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'unreadable'
    assert result['phase'] == 'uploading_video'
    assert result['last_error'] == {'type': 'TimeoutError', 'code': 'DOUYIN_BROWSER_ERROR'}


@pytest.mark.parametrize('error', [RuntimeError('cookie=private-cookie token=private-token '
                                            'https://example.invalid/?access_token=private-query'),
                                 RuntimeError()])
def test_exception_body_is_never_returned_or_cached(monkeypatch, diagnostic_client, error):
    host = error_host(monkeypatch, error)

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'unreadable'
    assert result['last_error'] == {'type': 'RuntimeError', 'code': 'DOUYIN_BROWSER_ERROR'}
    assert host.last_error == result['last_error']
    assert 'private-' not in response.text
    assert 'example.invalid' not in response.text


def test_closed_page_keeps_only_safe_previous_error_fields(browser_host, diagnostic_client):
    browser_host.last_error = {'type': 'TimeoutError', 'code': 'DOUYIN_BROWSER_ERROR',
                               'message': 'token=private-token',
                               'url': 'https://example.invalid/?token=private-query'}
    browser_host.call(lambda: browser_host.driver.page.close())

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'closed'
    assert result['last_error'] == {'type': 'TimeoutError', 'code': 'DOUYIN_BROWSER_ERROR'}
    assert 'private-' not in response.text


def test_adapter_error_diagnostics_discard_message_and_details(monkeypatch, diagnostic_client):
    error_host(monkeypatch, adapters.AdapterError('DOUYIN_PREPARATION_LOST',
               'cookie=private-cookie', detail={'token': 'private-token'}))

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    result = response.json()
    assert result['state'] == 'unreadable'
    assert result['last_error'] == {'type': 'AdapterError', 'code': 'DOUYIN_PREPARATION_LOST'}
    assert 'private-' not in response.text


def test_busy_diagnostics_return_without_queuing_browser_work(monkeypatch, diagnostic_client):
    def fail_if_queued(fn):
        pytest.fail('忙碌时诊断不能向浏览器队列提交工作')

    host = SimpleNamespace(busy=True, driver=SimpleNamespace(phase='uploading_video'),
                           last_error=None, call=fail_if_queued)
    monkeypatch.setattr(adapters, '_host', host)

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    assert response.json()['state'] == 'busy'
    assert response.json()['phase'] == 'uploading_video'


def test_normal_diagnostics_keep_controls_without_previous_exception_text(browser_host, diagnostic_client):
    browser_host.last_error = {'type': 'TimeoutError', 'message': 'token=private-token'}

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    result = response.json()
    assert set(result) >= {'url', 'title', 'phase', 'network_issues', 'interface_text',
                           'inputs', 'editors', 'buttons', 'dialogs', 'image_controls'}
    assert result['buttons'] == ['发布']
    assert any(item['placeholder'] == '作品标题' for item in result['inputs'])
    assert result['last_error'] == {'type': 'TimeoutError', 'code': 'DOUYIN_BROWSER_ERROR'}
    assert 'private-' not in response.text


def test_missing_browser_diagnostics_remain_unavailable(monkeypatch, diagnostic_client):
    monkeypatch.setattr(adapters, '_host', None)

    response = diagnostic_client.get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    assert response.json()['state'] == 'unavailable'
