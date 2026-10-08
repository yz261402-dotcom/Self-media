"""所有内容步骤不受应用内预算限制，搜索与导入仍准确记账。"""
import json
import threading

import pytest

from app import jobs, storage as st
from app.content import costs, references, tasks
from app.content.providers import redfox


@pytest.fixture(autouse=True)
def isolated_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()
    for key, value in {
        "CONTENT_BUDGET_PER_FLOW_FEN": "300", "CONTENT_BUDGET_DAILY_FEN": "600",
        "BUDGET_PER_TASK_FEN": "300", "BUDGET_DAILY_FEN": "8000", "BUDGET_MAX_SUBMISSIONS": "100",
        "REDFOX_API_KEY": "test-key", "REDFOX_BASE_URL": "https://redfox.hk", "REDFOX_POLL_INTERVAL": "0",
        "REDFOX_RESERVE_FEN_SEARCH": "50", "REDFOX_RESERVE_FEN_DETAIL": "50",
        "REDFOX_RESERVE_FEN_TRANSCRIPT": "100",
    }.items():
        monkeypatch.setenv("AVATAR_STUDIO_" + key, value)
    for step in ("SEARCH", "DETAIL", "TRANSCRIPT"):
        monkeypatch.delenv("AVATAR_STUDIO_REDFOX_PRICE_FEN_" + step, raising=False)
    st.put("works", "work", {"id": "work", "name": "t", "brief": {}, "version": 1, "content_revision": 0})
    yield
    st.conn().close()


def seed_spend(amount):
    st.put("provider_jobs", "prior", {
        "id": "prior", "namespace": costs.NAMESPACE, "state": "succeeded", "billable": True,
        "parent_content_job_id": "prior-parent", "created_at": jobs._iso(),
        "settlement_state": "settled", "actual_cost_fen": amount,
    })


def submit(kind="import", reserve=100, key="call"):
    parent = tasks.submit_job("work", kind, "request", {})
    return jobs.submit_content_call(
        parent_id=parent["id"], parent_kind=kind, work_id="work", provider="redfox", model="m",
        capability="transcript", idempotency_key=key, request_digest="digest", reserve_fen=reserve,
    )


@pytest.mark.parametrize("content_daily,global_daily", [(600, 8000), (8000, 600), (0, 0)])
def test_search_then_import_finishes_past_daily_amount_limits_and_keeps_ledger(monkeypatch, content_daily, global_daily):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", str(content_daily))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", str(global_daily))
    seed_spend(552)
    calls = []
    item = {"videoId": "77", "content": "参考简介", "videoType": "0",
            "opusUrl": "https://www.iesdouyin.com/share/video/77"}
    reservations = {
        redfox.PATH_SEARCH: (602, 50),
        redfox.PATH_DETAIL: (652, 100),
        redfox.PATH_TRANSCRIPT_SUBMIT: (752, 200),
        redfox.PATH_TRANSCRIPT_RESULT: (752, 200),
    }

    def transport(url, headers, body, timeout):
        path = url.replace("https://redfox.hk", "")
        calls.append(path)
        with st.transaction() as c:
            total, reserved = reservations[path]
            ledger_totals = costs.totals(c, day=jobs._iso()[:10])
            assert ledger_totals["total_fen"] == total
            assert ledger_totals["reserved_fen"] == reserved
            assert costs.global_totals(c, day=jobs._iso()[:10])["spent_fen"] == total
        data = {
            redfox.PATH_SEARCH: {"total": 1, "list": [item]},
            redfox.PATH_DETAIL: item,
            redfox.PATH_TRANSCRIPT_SUBMIT: {"taskId": "T-77"},
            redfox.PATH_TRANSCRIPT_RESULT: {"status": "succeeded", "text": "参考口播正文"},
        }[path]
        return 200, json.dumps({"code": 2000, "msg": "成功", "data": data}).encode()

    search = tasks.submit_job("work", "search", "search-request", {"query": "AI"})
    tasks.set_progress(search["id"], state="running")
    references.handle_search(search, transport=transport)
    found = tasks.get_job(search["id"])
    assert found["state"] == "succeeded"
    selected_id = found["result_refs"]["items"][0]["platform_work_id"]
    search_ledger = tasks.jobs_calls(search["id"])
    assert len(search_ledger) == 1 and search_ledger[0]["reserved_fen"] == 50
    assert search_ledger[0]["settlement_state"] == "held"

    parent = tasks.submit_job("work", "import", "request", {
        "search_job_id": search["id"], "platform_work_id": selected_id,
    })
    tasks.set_progress(parent["id"], state="running")
    references.handle_import(parent, transport=transport)
    result = tasks.get_job(parent["id"])
    assert result["state"] == "succeeded"
    assert result["result_refs"]["transcript_state"] == "ready"
    ledger = tasks.jobs_calls(parent["id"])
    assert len(ledger) == 2
    assert sum(call["reserved_fen"] for call in ledger) == 150
    transcript_call = next(call for call in ledger if call["step"] == "transcript_submit")
    assert transcript_call["settlement_state"] == "held" and transcript_call["actual_cost_fen"] is None
    again = tasks.submit_job("work", "import", "another-request", {"platform_work_id": "77"})
    references.handle_import(again, transport=transport)
    assert tasks.get_job(again["id"])["result_refs"]["reused"] is True
    assert calls == [redfox.PATH_SEARCH, redfox.PATH_DETAIL, redfox.PATH_TRANSCRIPT_SUBMIT,
                     redfox.PATH_TRANSCRIPT_RESULT]


@pytest.mark.parametrize("kind", tasks.ALL_KINDS)
def test_all_content_kinds_continue_past_daily_and_submission_limits(monkeypatch, kind):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_DAILY_FEN", "600")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", "600")
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_TEXT_CALLS", "0")
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", "1")
    seed_spend(552)
    result = submit(kind)
    assert result["state"] == "queued"
    assert result["reserved_fen"] == 100
    with st.transaction() as c:
        assert costs.totals(c, day=jobs._iso()[:10])["total_fen"] == 652


@pytest.mark.parametrize("setting,value", [
    ("CONTENT_BUDGET_PER_FLOW_FEN", "0"),
    ("CONTENT_BUDGET_PER_FLOW_FEN", "99"),
    ("BUDGET_PER_TASK_FEN", "99"),
    ("BUDGET_MAX_SUBMISSIONS", "0"),
    ("BUDGET_MAX_SUBMISSIONS", "1"),
])
def test_import_ignores_zero_or_insufficient_budget_settings(monkeypatch, setting, value):
    monkeypatch.setenv("AVATAR_STUDIO_" + setting, value)
    seed_spend(552)
    result = submit()
    assert result["state"] == "queued" and result["reserved_fen"] == 100
    with st.transaction() as c:
        assert costs.totals(c, day=jobs._iso()[:10])["total_fen"] == 652


def test_import_replayed_reservation_is_not_counted_twice():
    seed_spend(552)
    first = submit()
    assert first["state"] == "queued"
    again = submit()
    assert again["replayed"] is True and again["id"] == first["id"]
    with st.transaction() as c:
        assert costs.totals(c, day=jobs._iso()[:10])["total_fen"] == 652
