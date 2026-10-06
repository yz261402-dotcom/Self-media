"""目标时长可省略；不得把自由时长回填成 60 秒或限制为 45–90 秒。"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.contracts import WorkBrief
from app.content.generate import build_generation_messages
from app.content.review import review_candidate


@pytest.mark.parametrize('duration', [None, 0, 500, 15000, 60000, 90001, 600000, 3600000])
def test_create_work_retains_any_nonnegative_duration(duration):
    payload = {'topic': '自由时长测试'}
    if duration is not None:
        payload['target_duration_ms'] = duration
    response = TestClient(app).post('/api/v1/works', json=payload)
    assert response.status_code == 200
    assert response.json()['brief']['target_duration_ms'] == (duration or 0)


@pytest.mark.parametrize('duration', [-1, 'abc', True, 1.2, 'Infinity'])
def test_invalid_duration_has_a_readable_validation_error(duration):
    response = TestClient(app, raise_server_exceptions=False).post(
        '/api/v1/works', json={'topic': '非法时长', 'target_duration_ms': duration})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'VALIDATION_FAILED'


def test_unset_duration_is_not_a_sixty_second_script_instruction():
    assert WorkBrief().target_duration_ms == 0
    _, prompt = build_generation_messages(topic='介绍AI', target_duration_ms=0)
    assert '不限定' in prompt
    assert '约 0 秒' not in prompt and '60 秒' not in prompt


def test_explicit_short_and_long_targets_are_preserved_in_prompt():
    for milliseconds, seconds in [(12500, '12.5'), (600000, '600')]:
        _, prompt = build_generation_messages(topic='介绍AI', target_duration_ms=milliseconds)
        assert f'{seconds} 秒' in prompt


def test_unlimited_duration_has_no_duration_fit_rewrite_check():
    report = review_candidate(text='你也喜欢散步吗？我想慢慢聊聊我的感受。',
                              target_duration_ms=0, duration_estimate_ms=5000)
    assert all(check.code != 'DURATION_FIT' for check in report.checks)
