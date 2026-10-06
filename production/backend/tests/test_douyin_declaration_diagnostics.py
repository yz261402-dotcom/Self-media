"""声明异常仅输出固定分类、静态操作和代码位置，不输出异常原文。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import Error

from app.publishing import adapters, api, service


@pytest.mark.parametrize(('failure', 'reason'), [
    ('strict mode violation: resolved to two elements', 'strict_mode'),
    ('invalid selector', 'invalid_selector'),
    ('The XPath expression could not be evaluated', 'xpath'),
    ('Unexpected token while parsing css selector', 'parser'),
    ('Element is not attached to the DOM', 'not_attached'),
    ('Timeout 30000ms exceeded', 'timeout'),
    ('Timeout 30000ms exceeded; label intercepts pointer events', 'pointer_intercepted'),
    ('Timeout 30000ms exceeded; call log: locator("xpath=ancestor::*")', 'timeout'),
    ('Element is not attached to the DOM; locator("xpath=ancestor::*")', 'not_attached'),
    ('Unexpected token while parsing selector; locator("xpath=ancestor::*")', 'parser'),
    ('unknown failure; call log: locator("xpath=ancestor::*")', 'other'),
    ('unknown browser failure', 'other'),
])
def test_real_exception_path_is_classified_and_survives_page_diagnostics(monkeypatch, failure, reason):
    class UnreadableHeading:
        def filter(self, **kwargs):
            return self

        def count(self):
            raise Error('Locator.count: ' + failure + '\n'
                        'token=private-token cookie=private-cookie https://example.invalid/?token=private-query')

    page = SimpleNamespace(on=lambda *args: None, get_by_text=lambda *args, **kwargs: UnreadableHeading())
    driver = adapters.DouyinPage(page)
    host = SimpleNamespace(busy=False, last_error=None, call=lambda fn: fn(), opened=lambda: False,
                           driver=SimpleNamespace(phase='setting_declaration'))
    monkeypatch.setattr(adapters, '_host', host)
    adapter = adapters.PlaywrightDouyinAdapter()
    monkeypatch.setattr(service, 'get_adapter', lambda platform: adapter)

    with pytest.raises(adapters.AdapterError) as caught:
        adapter._perform(driver._set_ai_label)

    detail = caught.value.detail
    assert detail['operation'] == 'locate'
    assert detail['cause_type'] == 'Error'
    assert detail['reason'] == reason
    assert detail['api_method'] == 'Locator.count'
    source = Path(adapters.__file__).read_text(encoding='utf-8').splitlines()
    assert source[detail['source_line'] - 1].strip() == 'if heading.count() > 1:'
    assert 'private-' not in str(detail)

    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app, raise_server_exceptions=False).get('/api/v1/publish/douyin/page')

    assert response.status_code == 200
    observation = response.json()
    assert observation['state'] == 'closed'
    assert observation['last_error'] == {'type': 'AdapterError', 'code': 'DOUYIN_AI_LABEL_FAILED', **detail}
    assert 'private-' not in response.text
    assert 'example.invalid' not in response.text


def test_diagnostics_allowlist_rejects_untrusted_field_values(monkeypatch):
    host = SimpleNamespace(busy=True, driver=SimpleNamespace(phase='setting_declaration'),
                           last_error={'type': 'Error', 'code': 'DOUYIN_AI_LABEL_FAILED',
                                       'operation': 'select token=private-token',
                                       'cause_type': 'Error\nprivate-token',
                                       'source_line': 'https://example.invalid/?token=private-query',
                                       'api_method': 'Locator.private_token', 'reason': 'private-token',
                                       'message': 'private-token', 'url': 'private-query'})
    monkeypatch.setattr(adapters, '_host', host)

    result = adapters.PlaywrightDouyinAdapter().inspect()

    assert result['last_error'] == {'type': 'Error', 'code': 'DOUYIN_AI_LABEL_FAILED'}
    assert 'private' not in str(result)
