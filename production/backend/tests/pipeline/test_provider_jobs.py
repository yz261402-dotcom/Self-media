"""T02：Provider 作业台账、租约竞争、过期回写、unknown 对账、GPU 互斥。"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import jobs, storage as st  # noqa: E402

WORK = "work_jobs_0001"


def _submit(**over):
    base = dict(work_id=WORK, capability="tts", provider="minimax", model="speech-2.8-hd",
                args={"text": "你好"}, idempotency_key=f"idem_{st.new_id('k')}")
    base.update(over)
    return jobs.submit(**base)


# ---------- 持久意图与幂等 ----------

def test_intent_is_persisted_before_any_side_effect():
    job = _submit()
    assert job["state"] in ("queued", "blocked")
    assert jobs.get_job(job["id"]) is not None


def test_replay_returns_original_without_new_job():
    key = f"idem_replay_{st.new_id('k')}"
    first = _submit(idempotency_key=key)
    second = _submit(idempotency_key=key)
    assert second["id"] == first["id"]
    assert second["replayed"] is True
    assert len([j for j in jobs.jobs_for_work(WORK) if j["id"] == first["id"]]) == 1


def test_concurrent_submit_with_same_key_creates_one_job():
    key = f"idem_race_{st.new_id('k')}"
    results = []
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        results.append(_submit(idempotency_key=key))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {r["id"] for r in results}
    assert len(ids) == 1, "同一幂等键的并发提交只能落一个作业"
    assert sum(1 for r in results if r.get("replayed")) == 3


# ---------- 预算先于副作用 ----------

def test_billable_job_is_blocked_when_budget_zero(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", raising=False)
    job = _submit(capability="tts", billable=True, amount_fen=100)
    assert job["state"] == "blocked"
    assert job["block_code"] == "BUDGET_NOT_AUTHORIZED"
    with pytest.raises(jobs.JobRejected) as e:
        jobs.acquire_lease(job["id"], "w1")
    assert e.value.code == "JOB_BLOCKED"


def test_blocked_job_never_gets_a_lease():
    job = _submit(capability="tts", billable=True, amount_fen=100)
    assert job["state"] == "blocked"
    # blocked 不是终态，但已明确不可提交：租约获取应被业务层拦在前面
    assert jobs.get_job(job["id"])["state"] == "blocked"
    assert not jobs.get_job(job["id"]).get("lease")


# ---------- 租约竞争与过期回写 ----------

def test_only_one_worker_holds_the_lease():
    job = _submit(capability="search_local") if False else _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.acquire_lease(job["id"], "w2", ttl_seconds=30)
    assert e.value.code == "LEASE_HELD"


def test_same_worker_reacquiring_is_allowed():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    lease = jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    assert lease["worker_id"] == "w1"


def test_expired_worker_writeback_is_rejected():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=1)
    time.sleep(1.2)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.complete(job["id"], "w1", {"url": "x"})
    assert e.value.code == "LEASE_EXPIRED"


def test_stale_writer_cannot_overwrite_new_holder():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=1)
    time.sleep(1.2)
    jobs.acquire_lease(job["id"], "w2", ttl_seconds=30)  # 抢占
    with pytest.raises(jobs.JobRejected) as e:
        jobs.complete(job["id"], "w1", {"url": "late"})
    assert e.value.code == "STALE_WRITER"
    assert jobs.get_job(job["id"])["state"] == "leased"


def test_new_holder_can_complete():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=1)
    time.sleep(1.2)
    jobs.acquire_lease(job["id"], "w2", ttl_seconds=30)
    done = jobs.complete(job["id"], "w2", {"url": "ok"})
    assert done["state"] == "succeeded"
    assert done["result_digest"].startswith("sha256:")


def test_heartbeat_rejected_after_expiry():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=1)
    time.sleep(1.2)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.heartbeat(job["id"], "w1")
    assert e.value.code == "LEASE_EXPIRED"


def test_heartbeat_from_non_holder_rejected():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.heartbeat(job["id"], "w2")
    assert e.value.code == "LEASE_NOT_HELD"


# ---------- unknown 与对账 ----------

def test_unknown_result_requires_reconciliation():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.mark_unknown(job["id"], "w1", reason="提交后无响应")
    rec = jobs.reconcile(job["id"])
    assert rec["requires_reconciliation"] is True
    assert rec["may_create_replacement"] is False


def test_billable_unknown_may_not_create_replacement():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.mark_unknown(job["id"], "w1")
    rec = jobs.reconcile(job["id"])
    assert rec["may_create_replacement"] is False, "unknown 不得创建替代计费任务"


def test_resolve_unknown_requires_evidence():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.mark_unknown(job["id"], "w1")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.resolve_unknown(job["id"], "succeeded", {})
    assert e.value.code == "EVIDENCE_REQUIRED"


def test_resolve_unknown_with_evidence_settles():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.mark_unknown(job["id"], "w1")
    out = jobs.resolve_unknown(job["id"], "succeeded", {"source": "provider_query", "remote": "r1"})
    assert out["state"] == "succeeded"
    assert out["reconciliation"]["evidence"]["source"] == "provider_query"


# ---------- 取消后仍需结算 ----------

def test_cancel_after_lease_still_requires_settlement():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    out = jobs.cancel(job["id"])
    assert out["state"] == "cancelled"
    assert out["settlement_required"] is True


def test_cancel_before_submit_needs_no_settlement():
    job = _submit()
    out = jobs.cancel(job["id"])
    assert out["settlement_required"] is False


# ---------- GPU 互斥 ----------

def _free_gpu():
    cur = jobs.gpu_holder()
    if cur and cur.get("released_at") is None:
        jobs.release_gpu(cur["job_id"], force=True)


def test_gpu_is_exclusive():
    _free_gpu()
    j1 = _submit(capability="lipsync")
    j2 = _submit(capability="lipsync")
    assert jobs.get_job(j1["id"])["heavy"] is True
    jobs.acquire_gpu(j1["id"], "w1")
    with pytest.raises(jobs.JobRejected) as e:
        jobs.acquire_gpu(j2["id"], "w2")
    assert e.value.code == "GPU_BUSY"


def test_gpu_not_released_while_holder_unverified():
    _free_gpu()
    job = _submit(capability="lipsync")
    jobs.acquire_gpu(job["id"], "w1")
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    with pytest.raises(jobs.JobRejected) as e:
        jobs.release_gpu(job["id"])
    assert e.value.code == "GPU_BUSY_UNVERIFIED"
    _free_gpu()


def test_gpu_released_after_verified_completion():
    _free_gpu()
    job = _submit(capability="lipsync")
    jobs.acquire_gpu(job["id"], "w1")
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    jobs.complete(job["id"], "w1", {"path": "x"})
    assert jobs.gpu_holder()["released_at"] is not None


# ---------- 账本一致性 ----------

def test_ledger_detects_inconsistency():
    job = _submit()
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    raw = jobs.get_job(job["id"])
    raw["lease"] = None
    st.put("provider_jobs", job["id"], raw, {"work_id": WORK})
    rep = jobs.ledger_consistent(WORK)
    assert rep["consistent"] is False
    assert any(p["code"] == "LEASE_MISSING" for p in rep["problems"])
