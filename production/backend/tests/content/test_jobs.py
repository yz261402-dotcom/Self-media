"""内容父任务：幂等、单飞、并发外部调用只发一次、取消与晚响应归档。"""
import threading
import time
from uuid import uuid4

import pytest

from app import jobs, storage as st
from app.content import repository as repo
from app.content import tasks
from app.content.repository import StaleContentWrite


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "1000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "10000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "1000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "10000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1000")


def make_work():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "AI"}, "version": 1, "content_revision": 0})
    return wid


def test_same_request_id_returns_same_job_and_diff_payload_conflicts():
    wid = make_work()
    a = tasks.submit_job(wid, "search", "r1", {"query": "ai"})
    b = tasks.submit_job(wid, "search", "r1", {"query": "ai"})
    assert b["id"] == a["id"] and b["replayed"] is True and a["replayed"] is False
    with pytest.raises(tasks.IdempotencyConflict) as e:
        tasks.submit_job(wid, "search", "r1", {"query": "other"})
    assert e.value.code == "IDEMPOTENCY_CONFLICT"
    rows = st.conn().execute("SELECT COUNT(*) n FROM content_jobs WHERE work_id=?", [wid]).fetchone()["n"]
    assert rows == 1


def test_concurrent_same_request_yields_one_parent():
    wid = make_work()
    ids, errs = [], []

    def go():
        try:
            ids.append(tasks.submit_job(wid, "search", "same", {"query": "ai"})["id"])
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(ids)) == 1


def test_expected_revision_mismatch_is_rejected():
    wid = make_work()
    with pytest.raises(StaleContentWrite):
        tasks.submit_job(wid, "prompt", "r", {}, expected_revision=5)


def test_single_active_mutating_flow_per_work_but_search_allowed():
    wid = make_work()
    tasks.submit_job(wid, "prompt", "p1", {})
    with pytest.raises(tasks.FlowActive):
        tasks.submit_job(wid, "generation", "g1", {})
    tasks.submit_job(wid, "search", "s1", {"query": "x"})
    assert st.get("works", wid)["current_content_job_id"]


def test_concurrent_calls_same_key_send_once():
    wid = make_work()
    parent = tasks.submit_job(wid, "generation", "g", {})
    sent = []
    barrier = threading.Barrier(4)
    results = []

    def send():
        sent.append(1)
        time.sleep(0.05)
        return tasks.CallOutcome(response={"ok": 1}, usage={"input_tokens": 10, "output_tokens": 5}, cost_fen=3)

    def go():
        barrier.wait()
        try:
            results.append(tasks.run_call(parent=parent, provider="qwen", model="m", capability="chat", step="write",
                                          round_no=0, request={"p": 1}, send=send, reserve_fen=10))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    ts = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(sent) == 1
    assert sum(1 for r in results if isinstance(r, dict) and r["state"] == "succeeded") == 1
    later = tasks.run_call(parent=parent, provider="qwen", model="m", capability="chat", step="write",
                           round_no=0, request={"p": 1}, send=send, reserve_fen=10)
    assert later["state"] == "succeeded" and len(sent) == 1


def test_different_request_same_key_conflicts():
    wid = make_work()
    parent = tasks.submit_job(wid, "generation", "g", {})
    key = "k-fixed"
    tasks.run_call(parent=parent, provider="qwen", model="m", capability="chat", step="a", round_no=0,
                   request={"p": 1}, send=lambda: tasks.CallOutcome(response={}, cost_fen=1),
                   reserve_fen=5, idempotency_key=key)
    with pytest.raises(jobs.JobRejected) as e:
        tasks.run_call(parent=parent, provider="qwen", model="m", capability="chat", step="a", round_no=0,
                       request={"p": 2}, send=lambda: tasks.CallOutcome(response={}), reserve_fen=5,
                       idempotency_key=key)
    assert e.value.code == "IDEMPOTENCY_CONFLICT"


def test_stale_job_result_is_archived_not_bound():
    wid = make_work()
    job = tasks.submit_job(wid, "generation", "g", {})
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, brief={"topic": "换了"})
    applied = []
    done = tasks.finish_job(job["id"], "succeeded", result_refs={"batch_id": "b"}, apply=lambda c: applied.append(1))
    assert done["state"] == "superseded" and not applied
    assert done["archived_result"] == {"batch_id": "b"}


def test_a_b_a_revision_still_supersedes_old_job():
    wid = make_work()
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, brief={"topic": "A"})
    job = tasks.submit_job(wid, "generation", "g", {})
    for topic in ("B", "A"):
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, brief={"topic": topic})

    def must_not_apply(c):
        raise AssertionError("不应绑定")

    done = tasks.finish_job(job["id"], "succeeded", result_refs={}, apply=must_not_apply)
    assert done["state"] == "superseded"


def test_cancel_queued_job_and_running_job_result_not_bound():
    wid = make_work()
    job = tasks.submit_job(wid, "prompt", "p", {})
    assert tasks.cancel_job(job["id"])["state"] == "cancelled"
    wid2 = make_work()
    j2 = tasks.submit_job(wid2, "prompt", "p", {})
    tasks.set_progress(j2["id"], state="running", stage="plan")
    tasks.cancel_job(j2["id"])
    out = tasks.finish_job(j2["id"], "succeeded", result_refs={"x": 1})
    assert out["state"] == "cancelled"


def test_scheduler_concurrency_limit_and_handler_result():
    ws = [make_work() for _ in range(5)]
    active, peak, lock = [0], [0], threading.Lock()

    def handler(job):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.15)
        with lock:
            active[0] -= 1
        tasks.finish_job(job["id"], "succeeded", result_refs={})

    tasks.register("search", handler)
    ids = [tasks.submit_job(w, "search", "s", {"q": 1})["id"] for w in ws]
    tasks.Scheduler(concurrency=2).drain(10)
    assert peak[0] <= 2
    assert all(tasks.get_job(i)["state"] == "succeeded" for i in ids)


def test_handler_that_raises_maps_to_terminal_state():
    wid = make_work()

    def boom(job):
        raise tasks.ContentBlocked("no", "X", retryable=False)

    tasks.register("import", boom)
    j = tasks.submit_job(wid, "import", "i", {"a": 1})
    tasks.Scheduler(concurrency=2).drain(5)
    assert tasks.get_job(j["id"])["state"] == "blocked"
