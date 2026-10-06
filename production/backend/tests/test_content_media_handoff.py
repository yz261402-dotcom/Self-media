import asyncio

from app import engine, storage as st


def test_media_stage_does_not_reenter_content_agents(monkeypatch):
    wid, tid = st.new_id('w'), st.new_id('t')
    st.put('works', wid, {'id': wid, 'brief': {}, 'current_task_id': tid})
    st.put('tasks', tid, {'id': tid, 'work_id': wid, 'stage': 'avatar_variant',
                        'state': 'running', 'mode': 'production'}, {'work_id': wid})
    from app.agents import supervisor

    async def forbidden(_):
        raise AssertionError('media stage entered content agent graph')

    monkeypatch.setattr(supervisor, 'advance', forbidden)
    monkeypatch.setattr(engine, 'ORCHESTRATOR', 'agents')
    asyncio.run(engine._advance(tid))
    task = st.get('tasks', tid)
    assert task['state'] == 'waiting_selection'
    assert task['stage'] == 'tts'
    assert task['pending_selection_ids'] == ['sel_audio']


def test_selected_storyboard_never_enters_audio_handoff(monkeypatch):
    wid, tid = st.new_id('w'), st.new_id('t')
    st.put('works', wid, {'id': wid, 'brief': {'content_route': 'short_video_storyboard'},
                         'script_version_id': 's', 'current_task_id': tid})
    st.put('tasks', tid, {'id': tid, 'work_id': wid, 'stage': 'avatar_variant',
                        'state': 'running', 'mode': 'production'}, {'work_id': wid})
    monkeypatch.setattr(engine, 'ORCHESTRATOR', 'agents')
    asyncio.run(engine._advance(tid))
    task = st.get('tasks', tid)
    assert task['state'] == 'waiting_review'
    assert task['stage'] == 'content_review'
    assert 'sel_audio' not in task.get('pending_selection_ids', [])
