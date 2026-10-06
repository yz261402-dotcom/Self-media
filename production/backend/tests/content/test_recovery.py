"""发送后中断、重启、租约过期：unknown 不重发；远端 taskId 只查询不重提；取消与晚响应。"""
import time
from uuid import uuid4

import pytest

from app import jobs, storage as st
from app.content import costs, tasks


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "10000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "1000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "10000000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "100000")


def parent(kind="generation"):
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {}, "version": 1, "content_revision": 0})
    return tasks.submit_job(wid, kind, "g", {})


def call(p, send, **kw):
    return tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="w", round_no=0,
                          request={"x": 1}, send=send, reserve_fen=10, **kw)


def raise_(exc):
    def f():
        raise exc
    return f


def test_connection_drop_leaves_unknown_and_never_resends():
    p = parent()
    sent = []

    def drop():
        sent.append(1)
        raise ConnectionResetError("reset")

    with pytest.raises(tasks.CallPending):
        call(p, drop)
    calls = tasks.jobs_calls(p["id"])
    assert calls[0]["state"] == "unknown" and calls[0]["settlement_state"] == "held"
    for _ in range(3):
        tasks.recover_on_startup()
        with pytest.raises(tasks.CallPending):
            call(p, drop)
    assert len(sent) == 1


def test_expired_lease_becomes_unknown_not_queued():
    p = parent()
    c = jobs.submit_content_call(parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen",
                                 model="m", capability="chat", idempotency_key="kk", request_digest="d",
                                 reserve_fen=10)
    jobs.mark_sending(c["id"], "w1", ttl_seconds=1)
    time.sleep(1.2)
    changed = jobs.recover_content_calls()
    assert changed and changed[0]["state"] == "unknown"
    with pytest.raises(jobs.JobRejected):
        jobs.mark_sending(c["id"], "w2")
    with pytest.raises(jobs.JobRejected):
        jobs.acquire_lease(c["id"], "w2")


def test_restart_marks_running_parent_with_unconfirmed_call_unknown():
    p = parent()
    tasks.set_progress(p["id"], state="running", stage="write")
    c = jobs.submit_content_call(parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen",
                                 model="m", capability="chat", idempotency_key="k9", request_digest="d",
                                 reserve_fen=10)
    jobs.mark_sending(c["id"], "w1", ttl_seconds=1)
    time.sleep(1.1)
    res = tasks.recover_on_startup()
    assert res["jobs_unknown"] >= 1
    assert tasks.get_job(p["id"])["state"] == "unknown"


def test_restart_requeues_parent_when_no_call_in_flight_and_reuses_result():
    p = parent()
    tasks.set_progress(p["id"], state="running", stage="write")
    sent = []

    def first():
        sent.append(1)
        return tasks.CallOutcome(response={"a": 1}, cost_fen=2)

    def second():
        sent.append(1)
        return tasks.CallOutcome(response={"a": 9})

    call(p, first)
    tasks.recover_on_startup()
    assert tasks.get_job(p["id"])["state"] == "queued"
    again = call(p, second)
    assert again["result"] == {"a": 1} and len(sent) == 1


def test_remote_task_id_recovery_only_queries_same_task():
    p = parent("search")
    submits, queries = [], []

    def submit():
        submits.append(1)
        return tasks.CallOutcome(pending=True, remote_task_id="T-123", provider_request_id="req1")

    def query(tid):
        queries.append(tid)
        return tasks.CallOutcome(response={"text": "ok"}, cost_fen=4)

    with pytest.raises(tasks.CallPending):
        call(p, submit)
    c = tasks.jobs_calls(p["id"])[0]
    assert c["state"] == "waiting_external" and c["remote_task_id"] == "T-123"
    tasks.recover_on_startup()
    with pytest.raises(tasks.CallPending):
        call(p, submit)
    assert len(submits) == 1
    out = tasks.resume_remote(c["id"], query)
    assert queries == ["T-123"] and out["state"] == "succeeded" and out["actual_cost_fen"] == 4


def test_sync_call_without_handle_cannot_pretend_to_recover():
    p = parent()
    with pytest.raises(tasks.CallPending):
        call(p, raise_(TimeoutError("t")))
    c = tasks.jobs_calls(p["id"])[0]

    def query(tid):
        pytest.fail("不应查询")

    with pytest.raises(tasks.CallPending):
        tasks.resume_remote(c["id"], query)


def test_not_sent_releases_reservation():
    p = parent()
    with pytest.raises(tasks.ContentBlocked):
        call(p, raise_(tasks.NotSent("缺凭据", "CREDENTIAL_MISSING")))
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 0


def test_late_response_after_lease_lost_is_archived():
    p = parent()
    c = jobs.submit_content_call(parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen",
                                 model="m", capability="chat", idempotency_key="kL", request_digest="d",
                                 reserve_fen=10)
    jobs.mark_sending(c["id"], "w1")
    jobs.mark_content_unknown(c["id"], "断线")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.settle_content_call(c["id"], "w1", response={"late": 1}, usage=None, actual_cost_fen=5)
    assert e.value.code == "STALE_WRITER"
    job = jobs.get_job(c["id"])
    assert job["state"] == "unknown" and job["late_responses"] and "result" not in job


def test_cancel_before_send_releases_and_cancel_after_send_keeps_settlement():
    p = parent()
    c = jobs.submit_content_call(parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen",
                                 model="m", capability="chat", idempotency_key="kc", request_digest="d",
                                 reserve_fen=10)
    tasks.cancel_job(p["id"])
    assert jobs.get_job(c["id"])["state"] == "cancelled"
    assert jobs.get_job(c["id"])["settlement_state"] == "released"
    p2 = parent()
    c2 = jobs.submit_content_call(parent_id=p2["id"], parent_kind="generation", work_id=p2["work_id"],
                                  provider="qwen", model="m", capability="chat", idempotency_key="kc2",
                                  request_digest="d", reserve_fen=10)
    jobs.mark_sending(c2["id"], "w")
    jobs.cancel_unsent_call(c2["id"])
    assert jobs.get_job(c2["id"])["state"] == "leased"
