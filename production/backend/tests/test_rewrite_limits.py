"""T06 改稿上限：初稿后最多 3 次，超限拒绝，不重置、不换稿绕开。

计数落在 script_batches 行内，跨实例持久；换 Ledger 实例不等于重置。
"""
from __future__ import annotations

import pytest

from app import storage as st
from app.content.errors import ContentRejected
from app.content.rewrite import RewriteLedger


@pytest.fixture
def work_id():
    st.put("script_batches", "w1",
           {"id": "b1", "work_id": "w1", "candidates": [], "rewrite_count": 0},
           {"work_id": "w1"})
    return "w1"


def test_max_three_rewrites_then_rejected(work_id):
    led = RewriteLedger()
    assert led.remaining(work_id) == 3
    for i in range(3):
        led.record(work_id, reason=f"事实依据不足-{i}")
    assert led.count(work_id) == 3
    assert led.can_rewrite(work_id) is False
    with pytest.raises(ContentRejected) as caught:
        led.record(work_id, reason="还想再改一次")
    assert caught.value.code == "REWRITE_LIMIT_EXCEEDED"
    assert led.count(work_id) == 3, "超限后不得递增计数"


def test_count_persists_across_instances(work_id):
    """换实例/换稿都不能重置计数。"""
    RewriteLedger().record(work_id, "第一次")
    assert RewriteLedger().count(work_id) == 1
    RewriteLedger().record(work_id, "第二次")
    assert RewriteLedger().remaining(work_id) == 1


def test_history_records_reason_and_target(work_id):
    RewriteLedger().record(work_id, "缺时效依据", "cand_1")
    history = RewriteLedger().history(work_id)
    assert history[0]["seq"] == 1
    assert history[0]["reason"] == "缺时效依据"
    assert history[0]["candidate_id"] == "cand_1"


def test_record_without_batch_rejected():
    with pytest.raises(ContentRejected) as caught:
        RewriteLedger().record("no_such_work", "x")
    assert caught.value.code == "BATCH_NOT_FOUND"
