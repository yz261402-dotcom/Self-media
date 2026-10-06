"""内容预算不限额；Decimal、并发记账、unknown 持有与 release 语义保留。"""
import threading
from uuid import uuid4

import pytest

from app import config, jobs, storage as st
from app.content import costs, tasks


@pytest.fixture(autouse=True)
def budget(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "100")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", "1000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1000")
    yield
    st.conn().close()


def parent():
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {}, "version": 1, "content_revision": 0})
    return tasks.submit_job(wid, "generation", "g", {})


def daily_used():
    with st.transaction() as c:
        return costs.totals(c, day=jobs._iso()[:10])["total_fen"]


def test_cost_uses_decimal_and_ceils_to_fen():
    snap = costs.price_snapshot(currency="CNY", input_per_million=2, output_per_million=8)
    assert costs.cost_fen(snap, 16000, 2800) == 6
    usd = costs.price_snapshot(currency="USD", input_per_million=2, output_per_million=10,
                               fx_cny_per_unit="7.2", fx_date="2026-09-29")
    assert costs.cost_fen(usd, 8000, 2500) == 30


def test_missing_fx_is_not_guessed():
    usd = costs.price_snapshot(currency="USD", input_per_million=2, output_per_million=10)
    with pytest.raises(costs.CostError):
        costs.cost_fen(usd, 1, 1)


def test_two_concurrent_80fen_reservations_both_recorded_past_limits():
    base = daily_used()
    p = parent()
    outs, barrier = [], threading.Barrier(2)

    def go(key):
        barrier.wait()
        try:
            outs.append(jobs.submit_content_call(
                parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen", model="m",
                capability="chat", idempotency_key=key, request_digest="d",
                reserve_fen=80)["state"])
        except Exception as exc:  # noqa: BLE001
            outs.append(repr(exc))
        finally:
            st.conn().close()

    ts = [threading.Thread(target=go, args=(key,)) for key in ("first", "second")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in ts)
    assert outs == ["queued", "queued"]
    assert daily_used() == base + 160
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"]) == {
            "settled_fen": 0, "reserved_fen": 160, "calls": 2, "total_fen": 160,
        }


@pytest.mark.parametrize("setting", [None, "0"])
def test_default_or_zero_budget_sends_once_and_replays(monkeypatch, setting):
    for name in ("CONTENT_BUDGET_PER_FLOW_FEN", "CONTENT_BUDGET_DAILY_FEN", "CONTENT_MAX_TEXT_CALLS",
                 "BUDGET_PER_TASK_FEN", "BUDGET_DAILY_FEN", "BUDGET_MAX_SUBMISSIONS"):
        if setting is None:
            monkeypatch.delenv("AVATAR_STUDIO_" + name, raising=False)
        else:
            monkeypatch.setenv("AVATAR_STUDIO_" + name, setting)
    p = parent()
    sent = []

    def send():
        sent.append(1)
        return tasks.CallOutcome(response={"text": "已生成"}, cost_fen=7)

    results = [tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="w", round_no=0,
                              request={}, send=send, reserve_fen=100) for _ in range(2)]
    assert results[0]["state"] == "succeeded"
    assert results[0]["id"] == results[1]["id"]
    assert sent == [1]
    assert daily_used() == 7
    # 内容调用独立放行，媒体共享的全局预算策略仍默认禁止计费提交。
    assert config.budget_policy().allows_billable(1) is False


def test_unknown_keeps_reservation_without_blocking_next_call_or_resending():
    p = parent()
    sent = []

    def send():
        sent.append(1)
        raise TimeoutError("read timed out")

    for _ in range(2):
        with pytest.raises(tasks.CallPending):
            tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="w", round_no=0,
                           request={}, send=send, reserve_fen=100)
    assert sent == [1]
    held = tasks.jobs_calls(p["id"])[0]
    assert held["state"] == "unknown" and held["settlement_state"] == "held"
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 100
    r = jobs.submit_content_call(parent_id=p["id"], parent_kind="generation", work_id=p["work_id"], provider="qwen",
                                 model="m", capability="chat", idempotency_key="other", request_digest="d2",
                                 reserve_fen=1)
    assert r["state"] == "queued"
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 101


def test_settled_uses_actual_cost_and_missing_usage_keeps_hold(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "1000000")
    p = parent()
    done = tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="a", round_no=0,
                          request={"a": 1}, reserve_fen=50,
                          send=lambda: tasks.CallOutcome(response={"r": 1}, usage={"input_tokens": 1, "output_tokens": 1}, cost_fen=7))
    assert done["settlement_state"] == "settled" and done["actual_cost_fen"] == 7
    held = tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="b", round_no=0,
                          request={"b": 1}, reserve_fen=20, send=lambda: tasks.CallOutcome(response={"r": 2}))
    assert held["settlement_state"] == "held" and held["actual_cost_fen"] is None
    with st.transaction() as c:
        t = costs.totals(c, parent_id=p["id"])
    assert t["settled_fen"] == 7 and t["reserved_fen"] == 20


def test_provider_refused_releases_reservation_but_not_when_charge_unknown(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", "100000")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "1000000")
    p = parent()

    def refuse():
        raise tasks.ProviderRefused("401", "AUTH", 401)

    def refuse_unknown_charge():
        raise tasks.ProviderRefused("500", "X", 500, charged=None)

    with pytest.raises(tasks.ContentRejected):
        tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="a", round_no=0, request={},
                       reserve_fen=60, send=refuse)
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 0
    with pytest.raises(tasks.ContentRejected):
        tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step="b", round_no=0, request={},
                       reserve_fen=60, send=refuse_unknown_charge)
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["reserved_fen"] == 60


@pytest.mark.parametrize("kind,count", [("prompt", 2), ("generation", 8), ("review", 2)])
def test_content_parent_and_global_call_caps_do_not_block(monkeypatch, kind, count):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_TEXT_CALLS", "1")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1")
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {}, "version": 1, "content_revision": 0})
    p = tasks.submit_job(wid, kind, "p", {})
    sent = []

    def send():
        sent.append(1)
        return tasks.CallOutcome(response={}, cost_fen=1)

    for i in range(count):
        result = tasks.run_call(parent=p, provider="qwen", model="m", capability="chat", step=f"step-{i}",
                                round_no=0, request={"a": i}, reserve_fen=1, send=send)
        assert result["state"] == "succeeded"
    assert len(sent) == count
    with st.transaction() as c:
        assert costs.totals(c, parent_id=p["id"])["calls"] == count


def test_limits_report_unlimited_budget_but_keep_execution_controls(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_TEXT_CALLS", "1")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_CONCURRENCY", "3")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "2")
    assert costs.content_limits().to_dict() == {
        "per_flow_fen": None, "daily_fen": None, "max_text_calls": None,
        "concurrency": 3, "max_repair_rounds": 2,
    }
