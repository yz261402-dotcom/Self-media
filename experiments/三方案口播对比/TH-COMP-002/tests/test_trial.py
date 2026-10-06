import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def runner():
    target = ROOT / '补测火山.py'
    assert target.exists(), '补测执行器尚未实现'
    spec = importlib.util.spec_from_file_location('comparison_runner', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prevents_second_submission_after_success(tmp_path):
    m = runner()
    calls = []
    def call(body):
        calls.append(body)
        return {'_http_status': 200, 'code': 10000, 'data': {'task_id': 'task-123'}}
    result = m.submit_once(tmp_path, {'seed': 1}, call)
    assert result['state'] == 'accepted'
    with pytest.raises(RuntimeError, match='second submission'):
        m.submit_once(tmp_path, {'seed': 1}, call)
    assert len(calls) == 1


def test_exception_keeps_submission_reserved(tmp_path):
    m = runner()
    calls = []
    def call(body):
        calls.append(body)
        raise TimeoutError('uncertain whether server accepted')
    with pytest.raises(TimeoutError):
        m.submit_once(tmp_path, {}, call)
    with pytest.raises(RuntimeError, match='second submission'):
        m.submit_once(tmp_path, {}, call)
    assert len(calls) == 1


def test_http_rejection_is_never_success(tmp_path):
    m = runner()
    result = m.submit_once(tmp_path, {}, lambda body: {
        '_http_status': 401, 'code': 10000, 'data': {'task_id': 'misleading'}})
    assert result['state'] == 'unknown'
    assert result['task_id'] is None


def test_changed_source_fails_before_submission(tmp_path):
    m = runner()
    source = tmp_path / 'input.wav'
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='素材摘要'):
        m.verify_source(source, '0' * 64)
