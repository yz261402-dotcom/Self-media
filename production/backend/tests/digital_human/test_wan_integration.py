"""Wan 三路分派的离线集成回归：不访问网络或真实数据库。"""
import pytest

from app import main, mcp, engine, storage as st
from app.digital_human import routes, submission
from app.digital_human.errors import TalkingHeadRejected
from app.agents import tools as agent_tools
from dh_helpers import prepare_work


def test_confirm_route_never_defaults_to_m2():
    work = prepare_work(audio_ms=10000)
    with pytest.raises(main.HTTPException) as exc:
        main.confirm_route(work['id'], {})
    assert exc.value.detail['error']['code'] == 'ROUTE_NOT_SELECTED'


def test_wan_capability_registered_without_local_gpu_or_verified_claim():
    cap = mcp.REGISTRY.get('short_video_cloud')
    assert cap is not None
    assert cap['provider'] == 'dashscope_wan'
    assert not cap['heavy'] and not cap['verified']
    assert 'short_video_cloud' in mcp.capabilities_for('supervisor')


def test_unknown_provider_is_not_polled_through_volcengine(monkeypatch):
    monkeypatch.setattr(submission, 'get_run', lambda _: {
        'id': 'unsupported', 'state': 'waiting_provider', 'provider': 'unknown',
        'route': 'WAN_3_0', 'remote_job_id': 'r1'})
    monkeypatch.setattr(submission, '_credentials', lambda: pytest.fail('must not load Volc keys'))
    with pytest.raises(TalkingHeadRejected) as exc:
        submission.poll_run('unsupported', transport=lambda _: pytest.fail('network not allowed'))
    assert exc.value.code == 'PROVIDER_UNSUPPORTED'


def test_wan_agent_motion_and_lipsync_do_not_submit_m2(monkeypatch):
    monkeypatch.setattr(agent_tools, '_media_job', lambda *a, **kw: pytest.fail('Wan cannot create M2 job'))
    args = {'work_id': 'w1', 'route': 'WAN_3_0'}
    assert agent_tools.t_submit_motion(args, {})['skipped']
    assert agent_tools.t_submit_lipsync(args, {})['skipped']


def test_wan_wait_state_recognizes_existing_remote_run(monkeypatch):
    work = {'id': 'w1', 'route': 'WAN_3_0', 'current_task_id': 't1'}
    task = {'id': 't1'}
    monkeypatch.setattr(st, 'all_rows', lambda table, *a: [] if table == 'videos' else [{
        'id': 'pr1', 'route': 'WAN_3_0', 'capability': 'short_video_cloud',
        'provider': 'dashscope_wan', 'state': 'waiting_provider', 'task_id': 't1'}])
    result = engine.talking_head_wait(task, work)
    assert result['code'] == 'ORIGINAL_PENDING'


def test_capabilities_displays_wan_and_no_retired_omni_segmentation():
    result = main.capabilities().model_dump()
    by_route = {r['route']: r for r in result['routes']}
    assert 'WAN_3_0' in by_route
    assert '分段' not in by_route['OMNIHUMAN_1_5']['note']


def test_wan_api_submission_reaches_wan_only(monkeypatch):
    from app.digital_human import wan, wan_provider
    work = prepare_work(audio_ms=10000)
    monkeypatch.setattr(routes, 'assert_route_selected', lambda *a: 'WAN_3_0')
    called = []
    sentinel_transport = object()
    monkeypatch.setattr(wan, 'submit_wan', lambda work_id, **kw:
                        called.append((work_id, kw)) or {'submitted': False})
    monkeypatch.setattr(wan_provider, 'urllib_transport', sentinel_transport)
    monkeypatch.setattr(submission, 'submit_m2', lambda *a, **kw: pytest.fail('M2 fallback'))
    monkeypatch.setattr(submission, 'submit_omnihuman', lambda *a, **kw: pytest.fail('Omni fallback'))
    main.talking_head_submit(work['id'], {'prompt': '自然说话，镜头缓慢推进', 'dry_run': True})
    assert called[0][1]['resolution'] == '720p'
    assert called[0][1]['transport'] is sentinel_transport


def test_wan_estimate_honors_resolution_and_explicit_native_duration():
    work = prepare_work(audio_ms=10200)
    estimate = main.cost_estimate({'work_id': work['id'], 'route': 'WAN_3_0', 'resolution': '480p',
                                   'duration_seconds': 30})
    assert estimate.estimated_fen == 900


def test_wan_estimate_without_audio_uses_native_default():
    estimate = main.cost_estimate({'route': 'WAN_3_0'})
    assert estimate.estimated_fen == 300


def test_wan_route_notice_matches_default_720p():
    work = prepare_work(audio_ms=10000)
    view = main.talking_head_routes(work['id'])
    wan = next(r for r in view['routes'] if r['route'] == 'WAN_3_0')
    assert wan['cost_notice']['estimated_fen'] == 300


def test_wan_default_cost_view_matches_submit_resolution():
    work = prepare_work(route='WAN_3_0', audio_ms=10000)
    view = main.talking_head_cost(work['id'], prompt='自然讲述')
    assert view['current_call_list']['resolution'] == '720p'
    assert view['estimate']['estimated_fen'] == 300


def test_wan_declares_actual_user_prompt_capacity():
    declared = routes.route_view('WAN_3_0')['declared']
    assert declared['user_prompt_max_chars'] == 20000


def test_wan_reconciliation_shares_poll_lock(monkeypatch):
    from contextlib import contextmanager
    from app.digital_human import wan
    locked = []
    @contextmanager
    def lock(run_id):
        locked.append(run_id)
        try:
            yield
        finally:
            locked.pop()
    def settle(run_id, outcome, evidence):
        assert locked == [run_id], 'settle must not race with Wan result retrieval'
        return {'state': outcome}
    monkeypatch.setattr(submission, 'get_run', lambda _: {'provider': 'dashscope_wan'})
    monkeypatch.setattr(wan, '_poll_lock', lock)
    monkeypatch.setattr(submission, 'settle_unknown', settle)
    assert main.talking_head_settle('r1', {'outcome': 'failed', 'evidence': {'checked': True}})['state'] == 'failed'
