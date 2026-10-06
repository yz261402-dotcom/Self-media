"""口播阶段只认真实作业/原片，不把 Agent 工具回执当成成片。"""
import asyncio
import hashlib

import pytest

from app import delivery, engine, storage as st
from app.agents import supervisor
from app.contracts import Task


def seed(tmp_path, monkeypatch):
    from app import postprocess_worker
    from app.digital_human import gates
    wid, tid = st.new_id('w_head'), st.new_id('task')
    work = {'id': wid, 'current_task_id': tid, 'identity_id': 'person', 'variant_id': 'variant',
            'final_audio_id': 'audio', 'route': 'M2', 'motion_intent': {'motion_ref_id': 'motion'}}
    task = Task(id=tid, work_id=wid, mode='production', route='M2', route_confirmed=True,
                state='running', stage='talking_head').model_dump()
    st.put('works', wid, work)
    st.put('tasks', tid, task, {'work_id': wid})
    st.put('audios', 'audio', {'id': 'audio', 'work_id': wid, 'confirmed': True}, {'work_id': wid})
    monkeypatch.setattr(postprocess_worker, '_runtime_path', lambda uri: str(tmp_path / uri))
    monkeypatch.setattr(gates, 'video_gate', lambda _: {'ok': True})
    return work, task


def video(work, tmp_path, **extra):
    path = tmp_path / 'original.mp4'
    path.write_bytes(b'local media bytes')
    item = {'id': st.new_id('video'), 'work_id': work['id'], 'identity_id': work['identity_id'],
            'variant_id': work['variant_id'], 'audio_asset_id': work['final_audio_id'],
            'route': 'M2', 'kind': 'original', 'motion_ref_id': 'motion', 'is_mock': False,
            'approval_state': 'pending', 'confirmed': False, 'storage_uri': path.name,
            'file_hash': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), **extra}
    st.put('videos', item['id'], item, {'work_id': work['id']})
    return item


def active_run(work, task):
    row = {'id': st.new_id('run'), 'work_id': work['id'], 'task_id': task['id'], 'route': 'M2',
           'capability': 'lipsync', 'state': 'waiting_provider', 'is_mock': False,
           'provider_params': {'input_snapshot': {'work_id': work['id'], 'identity_id': 'person',
               'variant_id': 'variant', 'audio_asset_id': 'audio', 'motion_ref_id': 'motion'}}}
    st.put('provider_runs', row['id'], row, {'work_id': work['id']})
    return row


@pytest.mark.parametrize('has_run,expected', [(False, 'waiting_selection'), (True, 'waiting_provider')])
def test_production_waits_without_original_and_never_calls_placeholder_tools(tmp_path, monkeypatch, has_run, expected):
    work, task = seed(tmp_path, monkeypatch)
    if has_run:
        active_run(work, task)
    async def forbidden(*args):
        pytest.fail('Unready production talking_head must not run placeholder tools or postprocess')
    monkeypatch.setattr(supervisor, 'run_stage', forbidden)
    asyncio.run(supervisor.advance(task['id']))
    result = st.get('tasks', task['id'])
    assert result['stage'] == 'talking_head'
    assert result['state'] == expected
    assert next(s for s in result['stage_rows'] if s['stage'] == 'talking_head')['state'] != 'done'


def test_unconfirmed_original_waits_for_user_without_entering_postprocess(tmp_path, monkeypatch):
    work, task = seed(tmp_path, monkeypatch)
    original = video(work, tmp_path)
    asyncio.run(supervisor.advance(task['id']))
    result = st.get('tasks', task['id'])
    assert (result['stage'], result['state']) == ('talking_head', 'waiting_review')
    assert st.get('videos', original['id'])['confirmed'] is False
    assert not st.all_rows('media_jobs', 'work_id=?', [work['id']])


def test_only_effectively_confirmed_current_original_can_advance(tmp_path, monkeypatch):
    work, task = seed(tmp_path, monkeypatch)
    original = video(work, tmp_path, confirmed=True, approval_state='approved')
    work['video_asset_id'] = original['id']
    st.put('works', work['id'], work)
    stages = []
    async def run_stage(task, work, stage):
        stages.append(stage)
        return {'failures': [{'state': 'blocked', 'error': {'code': 'TEST_STOP', 'message': 'No processing'}}]}
    monkeypatch.setattr(supervisor, 'run_stage', run_stage)
    asyncio.run(supervisor.advance(task['id']))
    assert stages == ['postprocess']


@pytest.mark.parametrize('extra', [{'is_mock': True}, {'audio_asset_id': 'old'}, {'approval_state': 'revoked'}])
def test_mock_stale_or_revoked_original_is_not_a_real_ready_candidate(tmp_path, monkeypatch, extra):
    work, task = seed(tmp_path, monkeypatch)
    video(work, tmp_path, **extra)
    result = engine.talking_head_wait(task, work)
    assert result['state'] == 'waiting_selection'


def test_historical_media_not_ready_projection_preserves_database_and_events(tmp_path, monkeypatch):
    work, task = seed(tmp_path, monkeypatch)
    task.update(stage='postprocess', state='blocked', block_reason='MEDIA_NOT_READY')
    st.put('tasks', task['id'], task, {'work_id': work['id']})
    video(work, tmp_path)
    before = st.get('tasks', task['id'])
    result = engine.get_task(task['id'])
    assert (result['stage'], result['state']) == ('talking_head', 'waiting_review')
    assert next(s for s in result['stage_rows'] if s['stage'] == 'talking_head')['state'] == 'active'
    assert delivery.status(work['id'])['task']['stage'] == 'talking_head'
    assert st.get('tasks', task['id']) == before
    assert st.events_since(task['id']) == []


def test_cancelled_task_is_never_projected_as_active(tmp_path, monkeypatch):
    work, task = seed(tmp_path, monkeypatch)
    task.update(stage='postprocess', state='cancelled', block_reason='MEDIA_NOT_READY')
    st.put('tasks', task['id'], task, {'work_id': work['id']})
    video(work, tmp_path)
    assert engine.get_task(task['id'])['state'] == 'cancelled'


def test_confirmed_original_can_start_postprocess_even_if_old_task_waited_for_provider(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    work, task = seed(tmp_path, monkeypatch)
    task['state'] = 'waiting_provider'
    st.put('tasks', task['id'], task, {'work_id': work['id']})
    original = video(work, tmp_path, confirmed=True, approval_state='approved')
    work['video_asset_id'] = original['id']
    st.put('works', work['id'], work)
    monkeypatch.setattr(engine, 'schedule', lambda _: None)
    response = TestClient(app).post(f"/api/v1/works/{work['id']}/postprocess")
    assert response.status_code == 200, response.text
