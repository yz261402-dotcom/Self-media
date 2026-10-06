"""克隆音色通过真实 TTS 入口的首次计费/激活状态，网络使用注入响应。"""
from datetime import datetime, timedelta, timezone

import pytest

from app import jobs, storage as st
from app.tts import cloning, provider, voices
from app.tts.errors import TtsBlocked, TtsRejected
from tts_helpers import allow_billable, make_transport, ok_body


def pending_clone(voice):
    vid = st.new_id('clone_lifecycle')
    row = {**voice, 'id': vid, 'remote_voice_id': vid,
           'clone_metadata': {'activation_state': 'pending', 'activated_at': None,
                              'activation_cost_ack': True, 'first_use_job_id': None,
                              'expires_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}}
    st.put('voices', vid, row)
    return row


def test_first_tts_success_reserves_clone_fee_then_activates(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    clone = pending_clone(voice)
    transport, calls = make_transport(body=ok_body())
    result = provider.synthesize(work_id=work['id'], voice=clone, provider_params={},
                                 text='克隆测试。', transport=transport)
    job = jobs.get_job(result['job_id'])
    assert job['amount_fen'] >= 991
    assert jobs.get_job(result['job_id'])['state'] == 'succeeded'
    assert voices.get_voice(clone['id'])['clone_metadata']['activation_state'] == 'active'
    assert cloning.synthesis_charge(clone, '克隆测试。') < 990
    assert len(calls) == 1


@pytest.mark.parametrize('response,expected', [
    ({'status_code': 400, 'body': {'base_resp': {'status_code': 1004}}}, 'pending'),
    ({'status_code': 200, 'body': {'base_resp': {'status_code': 0}, 'data': {'audio': ''}}}, 'unknown'),
    ({'status_code': 200, 'body': {'base_resp': {'status_code': 1001}}}, 'unknown'),
    ({'status_code': 200, 'body': {'base_resp': 'unexpected'}}, 'unknown'),
    ({'status_code': None, 'error': 'not_configured', 'message': '未配置'}, 'pending'),
])
def test_provider_rejection_or_unparseable_audio_updates_activation(monkeypatch, work, voice, response, expected):
    allow_billable(monkeypatch)
    clone = pending_clone(voice)
    with pytest.raises((TtsBlocked, TtsRejected)):
        provider.synthesize(work_id=work['id'], voice=clone, provider_params={},
                            text='克隆失败测试。', transport=lambda _: response)
    meta = voices.get_voice(clone['id'])['clone_metadata']
    assert meta['activation_state'] == expected
    assert not meta['activated_at']
    job = jobs.jobs_for_work(work['id'])[-1]
    assert job['lease'] is None
    if response.get('error') == 'not_configured':
        assert job['settlement_required'] is False
        assert job['amount_fen'] == 0


def test_activation_lost_race_does_not_leave_billable_unsettled_job(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    clone = pending_clone(voice)
    original = jobs.acquire_lease
    def lease(*args, **kwargs):
        result = original(*args, **kwargs)
        cloning.begin_activation(clone['id'], 'another_tts_job')
        return result
    monkeypatch.setattr(jobs, 'acquire_lease', lease)
    transport, calls = make_transport(body=ok_body())
    with pytest.raises(TtsBlocked):
        provider.synthesize(work_id=work['id'], voice=clone, provider_params={},
                            text='并发首次合成。', transport=transport)
    job = jobs.jobs_for_work(work['id'])[-1]
    assert not calls
    assert job['state'] == 'failed' and job['lease'] is None
    assert job['settlement_required'] is False
    assert job['amount_fen'] == 0


def test_mock_tts_does_not_charge_or_activate_clone(monkeypatch, work, voice):
    clone = pending_clone(voice)
    result = provider.synthesize(work_id=work['id'], voice=clone, provider_params={}, text='模拟。', dry_run=True)
    assert result['is_mock']
    assert not jobs.jobs_for_work(work['id'])
    assert voices.get_voice(clone['id'])['clone_metadata']['activation_state'] == 'pending'
