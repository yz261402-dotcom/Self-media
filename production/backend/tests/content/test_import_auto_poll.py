"""Reference import polling resumes from its saved handle without another paid submit."""
from __future__ import annotations

import json
import threading
from uuid import uuid4

import pytest

from app import storage as st
from app.content import references, tasks
from app.content import repository as repo
from app.content.providers import redfox


class Clock:
    def __init__(self):
        self.now = 2_000_000_000.0

    def advance(self, seconds=10):
        self.now += seconds


class Transport:
    def __init__(self, results):
        self.results = list(results)
        self.log = []
        self.before_result = None

    def __call__(self, url, headers, body, timeout):
        path = url.removeprefix("https://redfox.hk")
        payload = json.loads(body)
        self.log.append((path, payload))
        if path == redfox.PATH_DETAIL:
            data = {"videoId": "77", "content": "Reference caption", "videoType": "0",
                    "opusUrl": "https://www.iesdouyin.com/share/video/77"}
        elif path == redfox.PATH_TRANSCRIPT_SUBMIT:
            data = {"taskId": "T-77"}
        elif path == redfox.PATH_TRANSCRIPT_RESULT:
            if self.before_result:
                self.before_result()
            if not self.results:
                pytest.fail("Unexpected extra remote result query")
            data = self.results.pop(0)
        else:
            pytest.fail(f"Unexpected network path: {path}")
        if isinstance(data, Exception):
            raise data
        return 200, json.dumps({"code": 2000, "msg": "ok", "data": data}).encode()

    def count(self, path):
        return sum(p == path for p, _ in self.log)


@pytest.fixture(autouse=True)
def isolated_polling(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_API_KEY", "test-redfox-key")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_BASE_URL", "https://redfox.hk")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_POLL_MAX", "1")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_POLL_INTERVAL", "0")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_AUTO_POLL_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("AVATAR_STUDIO_REDFOX_AUTO_POLL_TIMEOUT_SECONDS", "600")
    monkeypatch.delenv("AVATAR_STUDIO_REDFOX_PRICE_FEN_TRANSCRIPT", raising=False)
    monkeypatch.setattr(tasks, "HANDLERS", {})
    clock = Clock()
    monkeypatch.setattr(tasks.time, "time", lambda: clock.now)

    def no_sleep(_):
        pytest.fail("A reference polling worker must not sleep between queries")

    monkeypatch.setattr(references.time, "sleep", no_sleep)
    yield clock
    st.conn().close()


def make_import(monkeypatch, results):
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "polling", "brief": {},
                          "version": 1, "content_revision": 0})
    job = tasks.submit_job(wid, "import", "import-1", {"platform_work_id": "77"})
    transport = Transport(results)
    monkeypatch.setitem(tasks.HANDLERS, "import",
                        lambda parent: references.handle_import(parent, transport=transport))
    return job, transport


def run_claim(job):
    claimed = tasks._claim(1)
    assert claimed is not None, "The due reference import must be picked up automatically"
    assert claimed["id"] == job["id"]
    tasks.run_job(claimed)
    return tasks.get_job(job["id"])


def transcript_call(job):
    return next(call for call in tasks.jobs_calls(job["id"])
                if call.get("step") == "transcript_submit")


def assert_single_submission(job, transport):
    assert transport.count(redfox.PATH_DETAIL) == 1
    assert transport.count(redfox.PATH_TRANSCRIPT_SUBMIT) == 1
    assert {body["taskId"] for path, body in transport.log
            if path == redfox.PATH_TRANSCRIPT_RESULT} == {"T-77"}
    assert transcript_call(job)["attempts"] == 1


def test_import_automatically_polls_well_beyond_original_limit(monkeypatch, isolated_polling):
    job, transport = make_import(monkeypatch, [{"status": "processing"}] * 12
                                 + [{"status": "succeeded", "text": "Recovered transcript"}])
    for attempt in range(13):
        before_queries = transport.count(redfox.PATH_TRANSCRIPT_RESULT)
        latest = run_claim(job)
        assert transport.count(redfox.PATH_TRANSCRIPT_RESULT) == before_queries + 1
        if attempt < 12:
            assert latest["state"] == "waiting_external"
            assert latest["remote_poll"]["next_at"] > isolated_polling.now
            assert tasks._claim(1) is None
            isolated_polling.advance()
    assert latest["state"] == "succeeded"
    ref = repo.get_reference(latest["result_refs"]["reference_id"])
    assert ref["transcript"]["text"] == "Recovered transcript"
    assert_single_submission(job, transport)


def test_waiting_reference_releases_capacity_for_other_content_jobs(monkeypatch, isolated_polling):
    job, transport = make_import(monkeypatch, [{"status": "processing"},
                                              {"status": "succeeded", "text": "Done"}])
    assert run_claim(job)["state"] == "waiting_external"
    other = tasks.submit_job(job["work_id"], "search", "search-1", {})
    monkeypatch.setitem(tasks.HANDLERS, "search",
                        lambda parent: tasks.finish_job(parent["id"], "succeeded"))
    claimed = tasks._claim(1)
    assert claimed and claimed["id"] == other["id"]
    tasks.run_job(claimed)
    assert tasks._claim(1) is None
    assert transport.count(redfox.PATH_TRANSCRIPT_RESULT) == 1
    isolated_polling.advance()
    assert run_claim(job)["state"] == "succeeded"
    assert_single_submission(job, transport)


def test_restart_recovers_running_import_and_resumes_known_handle(monkeypatch, isolated_polling):
    job, transport = make_import(monkeypatch, [{"status": "processing"},
                                              {"status": "succeeded", "text": "After restart"}])
    assert run_claim(job)["state"] == "waiting_external"
    isolated_polling.advance()
    claimed = tasks._claim(1)
    assert claimed and claimed["id"] == job["id"]
    assert tasks.get_job(job["id"])["state"] == "running"
    tasks.recover_on_startup()
    assert tasks.get_job(job["id"])["state"] in ("queued", "waiting_external")
    assert run_claim(job)["state"] == "succeeded"
    assert_single_submission(job, transport)


@pytest.mark.parametrize("error", [ConnectionResetError("query disconnected"),
                                   TimeoutError("query timed out"),
                                   tasks.ProviderRefused("limited", "RATE_LIMITED", 429, charged=False)])
def test_temporary_result_query_failure_is_delayed_and_retried(monkeypatch, isolated_polling, error):
    job, transport = make_import(monkeypatch, [error,
                                              {"status": "succeeded", "text": "Recovered"}])
    waiting = run_claim(job)
    assert waiting["state"] == "waiting_external"
    assert waiting["remote_poll"].get("last_error")
    assert waiting["remote_poll"]["next_at"] > isolated_polling.now
    assert transcript_call(job)["state"] == "waiting_external"
    assert tasks._claim(1) is None
    isolated_polling.advance(120)
    assert run_claim(job)["state"] == "succeeded"
    assert_single_submission(job, transport)


@pytest.mark.parametrize("legacy_state", ["running", "waiting_external"])
def test_restart_resumes_legacy_waiter_without_poll_metadata(monkeypatch, isolated_polling, legacy_state):
    job, transport = make_import(monkeypatch, [{"status": "processing"},
                                              {"status": "succeeded", "text": "Legacy task recovered"}])
    assert run_claim(job)["state"] == "waiting_external"
    with st.transaction() as connection:
        legacy = tasks._row(connection, job["id"])
        legacy.pop("remote_poll", None)
        legacy["state"] = legacy_state
        tasks._write(connection, legacy)
    isolated_polling.advance(2400)
    tasks.recover_on_startup()
    assert tasks.get_job(job["id"])["state"] in ("queued", "waiting_external")
    assert run_claim(job)["state"] == "succeeded"
    assert_single_submission(job, transport)


def test_poll_timeout_pauses_and_manual_refresh_resumes_original_handle(monkeypatch, isolated_polling):
    job, transport = make_import(monkeypatch, [{"status": "processing"}] * 3
                                 + [{"status": "succeeded", "text": "After manual refresh"}])
    waiting = run_claim(job)
    assert waiting["state"] == "waiting_external"
    isolated_polling.advance(601)
    paused = run_claim(job)
    assert paused["state"] == "unknown"
    assert paused["error"]["code"] == "REFERENCE_POLL_TIMEOUT"
    call = transcript_call(job)
    assert call["remote_task_id"] == "T-77" and call["state"] == "waiting_external"
    assert call["reserved_fen"] > 0 and call["actual_cost_fen"] is None
    assert tasks._claim(1) is None
    tasks.request_import_refresh(job["id"])
    refreshed = tasks.get_job(job["id"])
    assert refreshed["state"] == "queued"
    assert refreshed["remote_poll"]["deadline_at"] > isolated_polling.now
    for _ in range(3):
        done = run_claim(job)
        if done["state"] == "succeeded":
            break
        isolated_polling.advance()
    assert done["state"] == "succeeded"
    assert_single_submission(job, transport)


def test_cancel_waiting_import_stops_queries_without_releasing_paid_call(monkeypatch, isolated_polling):
    job, transport = make_import(monkeypatch, [{"status": "processing"}])
    assert run_claim(job)["state"] == "waiting_external"
    tasks.cancel_job(job["id"])
    isolated_polling.advance()
    claimed = tasks._claim(1)
    if claimed is not None:
        assert claimed["id"] == job["id"]
        tasks.run_job(claimed)
    assert tasks.get_job(job["id"])["state"] == "cancelled"
    assert transport.count(redfox.PATH_TRANSCRIPT_RESULT) == 1
    call = transcript_call(job)
    assert call["state"] == "waiting_external"
    assert call["reserved_fen"] > 0 and call["settlement_state"] != "released"


def test_cancel_during_query_does_not_adopt_late_success(monkeypatch):
    job, transport = make_import(monkeypatch, [{"status": "succeeded", "text": "Late transcript"}])
    transport.before_result = lambda: tasks.cancel_job(job["id"])
    done = run_claim(job)
    assert done["state"] == "cancelled"
    assert done["result_refs"] == {}
    assert st.get("works", job["work_id"]).get("reference_snapshot_id") is None
    assert_single_submission(job, transport)


def test_cancel_during_query_auth_error_finishes_cancelled_not_unknown(monkeypatch):
    job, transport = make_import(monkeypatch, [tasks.ProviderRefused(
        "auth refused", "AUTH_FAILED", 401, charged=False)])
    transport.before_result = lambda: tasks.cancel_job(job["id"])
    done = run_claim(job)
    assert done["state"] == "cancelled"
    assert done["result_refs"] == {}
    call = transcript_call(job)
    assert call["state"] == "waiting_external"
    assert call["reserved_fen"] > 0 and call["settlement_state"] != "released"
    assert_single_submission(job, transport)


def test_refresh_during_running_query_cannot_enqueue_a_second_worker(monkeypatch):
    job, transport = make_import(monkeypatch, [{"status": "succeeded", "text": "Single worker"}])

    def refresh_while_running():
        tasks.request_import_refresh(job["id"])
        assert tasks.get_job(job["id"])["state"] == "running"
        assert tasks._claim(2) is None

    transport.before_result = refresh_while_running
    assert run_claim(job)["state"] == "succeeded"
    assert_single_submission(job, transport)


def test_unconfirmed_submission_without_remote_handle_is_never_requeued(monkeypatch):
    job, transport = make_import(monkeypatch, [])
    original = transport.__call__

    def drop_submission(url, headers, body, timeout):
        if url.endswith(redfox.PATH_TRANSCRIPT_SUBMIT):
            transport.log.append((redfox.PATH_TRANSCRIPT_SUBMIT, json.loads(body)))
            raise ConnectionResetError("submit disconnected before handle returned")
        return original(url, headers, body, timeout)

    monkeypatch.setitem(tasks.HANDLERS, "import",
                        lambda parent: references.handle_import(parent, transport=drop_submission))
    done = run_claim(job)
    assert done["state"] == "unknown"
    assert not transcript_call(job).get("remote_task_id")
    tasks.recover_on_startup()
    try:
        tasks.request_import_refresh(job["id"])
    except tasks.ContentError:
        pass
    assert tasks.get_job(job["id"])["state"] == "unknown"
    assert tasks._claim(1) is None
    assert transport.count(redfox.PATH_TRANSCRIPT_SUBMIT) == 1
    assert transport.count(redfox.PATH_TRANSCRIPT_RESULT) == 0


@pytest.mark.parametrize("price", [None, "37"])
def test_result_query_never_sets_original_submission_price_to_zero(monkeypatch, price):
    if price is not None:
        monkeypatch.setenv("AVATAR_STUDIO_REDFOX_PRICE_FEN_TRANSCRIPT", price)
    job, transport = make_import(monkeypatch, [{"status": "succeeded", "text": "Paid transcript"}])
    assert run_claim(job)["state"] == "succeeded"
    call = transcript_call(job)
    if price is None:
        assert call["actual_cost_fen"] is None
        assert call["settlement_state"] == "held" and call["settlement_required"]
    else:
        assert call["actual_cost_fen"] == int(price)
        assert call["settlement_state"] == "settled"
    assert_single_submission(job, transport)
