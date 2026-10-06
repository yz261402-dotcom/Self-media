"""Wan 付费任务按持久台账原子计算当日预留，未知任务也占预算。"""
import itertools
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import jobs, storage as st


@pytest.fixture
def budget(monkeypatch):
    # Isolate this test's ledger day from all other provider fixtures.
    day = next(_days)
    monkeypatch.setattr(jobs, '_iso', lambda ts=None: f'2040-01-{day:02d}T00:00:00+00:00')
    monkeypatch.setenv('AVATAR_STUDIO_BUDGET_PER_TASK_FEN', '500')
    monkeypatch.setenv('AVATAR_STUDIO_BUDGET_DAILY_FEN', '600')
    monkeypatch.setenv('AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS', '2')


_days = itertools.count(1)


def submit(key=None, provider='dashscope_wan', amount=400):
    return jobs.submit(work_id=st.new_id('w'), capability='short_video_cloud',
                       provider=provider, model='wan3.0-video', args={},
                       idempotency_key=key or st.new_id('wan'), billable=True, amount_fen=amount)


def test_wan_second_reservation_cannot_exceed_daily_budget(budget):
    assert submit()['state'] == 'queued'
    second = submit()
    assert second['state'] == 'blocked'
    assert second['block_code'] == 'BUDGET_NOT_AUTHORIZED'


def test_wan_replay_does_not_reserve_again(budget):
    key = st.new_id('same')
    first = submit(key, amount=300)
    assert submit(key, amount=300)['id'] == first['id']
    assert submit(amount=300)['state'] == 'queued'
    assert submit(amount=1)['state'] == 'blocked'


def test_wan_counts_unknown_and_other_provider_reservations(budget):
    prior = submit(provider='volcengine')
    prior.update(state='unknown', settlement_required=True)
    st.put('provider_jobs', prior['id'], prior, {'work_id': prior['work_id']})
    assert submit()['state'] == 'blocked'


def test_wan_concurrent_reservations_are_atomic(budget):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(r['state'] for r in results) == ['blocked', 'queued']


@pytest.mark.parametrize('provider', ['volcengine', 'm2_worker', 'minimax'])
def test_other_routes_cannot_spend_wan_reserved_budget(budget, provider):
    assert submit()['state'] == 'queued'
    assert submit(provider=provider)['state'] == 'blocked'
