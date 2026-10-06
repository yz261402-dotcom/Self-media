"""收费提交只能发生一次；丢响应必须留账后停止，而不是鼓励再次付费。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app import jobs, storage as st
from app.digital_human import providers as pv, submission as sub
from app.digital_human.errors import TalkingHeadBlocked
from dh_helpers import allow_billable, authorize_cost, volc_submit_ok


def _ready(work, monkeypatch):
    monkeypatch.setenv(pv.SECRET_ENV_AK, "fixture-ak")
    monkeypatch.setenv(pv.SECRET_ENV_SK, "fixture-sk")
    allow_billable(monkeypatch)
    authorize_cost(work["id"])
    return st.get("tasks", work["current_task_id"])


def test_repeated_submit_returns_original_remote_job_without_second_post(work, monkeypatch):
    task = _ready(work, monkeypatch)
    calls = []

    def transport(request):
        calls.append(request)
        return volc_submit_ok("original-remote")

    first = sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    second = sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert len(calls) == 1
    assert second["runs"][0]["id"] == first["runs"][0]["id"]
    assert second["runs"][0]["remote_job_id"] == "original-remote"
    assert len(sub.runs_for_work(work["id"])) == 1


def test_concurrent_submit_has_one_external_owner(work, monkeypatch):
    task = _ready(work, monkeypatch)
    entered, release = Event(), Event()
    calls = []

    def transport(request):
        calls.append(request)
        entered.set()
        assert release.wait(8)
        return volc_submit_ok("concurrent-remote")

    def submit():
        return sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        assert entered.wait(8)
        second = pool.submit(submit)
        try:
            replayed = second.result(timeout=6)
        finally:
            release.set()
        submitted = first.result(timeout=8)
    assert len(calls) == 1
    assert replayed["runs"][0]["id"] == submitted["runs"][0]["id"]


def test_separate_segments_never_share_run_claim(work):
    args = dict(work_id=work["id"], route="OMNIHUMAN_1_5", provider="volcengine",
                model="omnihuman-1.5", capability="avatar_video_cloud",
                request_digest="same-call-list", reuse_existing=True)
    first = sub.start_run(**args, idempotency_key="segment-0")
    second = sub.start_run(**args, idempotency_key="segment-1")
    replayed = sub.start_run(**args, idempotency_key="segment-0")
    assert first["id"] != second["id"]
    assert replayed["id"] == first["id"]
    assert replayed["replayed"] is True


@pytest.mark.parametrize("failure", [TimeoutError("remote result unavailable"),
                                      ValueError("malformed response")])
def test_submit_exception_records_unknown_and_prevents_automatic_resubmit(work, monkeypatch, failure):
    task = _ready(work, monkeypatch)
    calls = []

    def transport(request):
        calls.append(request)
        raise failure

    with pytest.raises(TalkingHeadBlocked) as raised:
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert raised.value.code == "OMNIHUMAN_SUBMISSION_UNKNOWN"
    run = sub.runs_for_work(work["id"])[-1]
    assert run["state"] == "unknown"
    assert jobs.get_job(run["job_id"])["state"] == "unknown"
    with pytest.raises(TalkingHeadBlocked, match="先.*对账"):
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert len(calls) == 1


def test_malformed_success_response_stays_unknown_in_persistent_ledger(work, monkeypatch):
    task = _ready(work, monkeypatch)
    with pytest.raises(TalkingHeadBlocked) as raised:
        sub.submit_omnihuman(work["id"], task=task, dry_run=False,
                             transport=lambda request: {"status_code": 200,
                                                        "json": {"data": "invalid schema"}})
    assert raised.value.code == "OMNIHUMAN_SUBMISSION_UNKNOWN"
    run = sub.runs_for_work(work["id"])[-1]
    assert run["state"] == "unknown"
    assert jobs.get_job(run["job_id"])["state"] == "unknown"


@pytest.mark.parametrize("status,code", [(401, "OMNIHUMAN_AUTH_FAILED"),
                                          (429, "OMNIHUMAN_QUOTA_EXHAUSTED")])
def test_explicit_http_rejection_is_persisted_and_never_silently_resubmitted(work, monkeypatch, status, code):
    task = _ready(work, monkeypatch)
    calls = []

    def transport(request):
        calls.append(request)
        return {"status_code": status, "json": {}}

    with pytest.raises(TalkingHeadBlocked) as raised:
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert raised.value.code == code
    run = sub.runs_for_work(work["id"])[-1]
    assert run["state"] == "failed"
    assert run["last_error"]["code"] == code
    with pytest.raises(TalkingHeadBlocked) as retry:
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert retry.value.code == "RESUBMIT_REQUIRES_AUTHORIZATION"
    assert len(calls) == 1


def test_orphan_existing_job_stops_without_another_external_submit(work, monkeypatch):
    task = _ready(work, monkeypatch)
    listing = sub.current_call_list(work["id"], route="OMNIHUMAN_1_5")
    jobs.submit(work_id=work["id"], capability="avatar_video_cloud", provider="volcengine",
                model="omnihuman-1.5", args={"legacy": True},
                idempotency_key=f"oh:{work['id']}:{listing['digest']}:oh_seg_1")
    calls = []

    def transport(request):
        calls.append(request)
        return volc_submit_ok("must-not-submit")

    with pytest.raises(TalkingHeadBlocked) as raised:
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    assert raised.value.code == "RESUBMIT_REQUIRES_RECONCILIATION"
    assert calls == []
    assert sub.runs_for_work(work["id"])[-1]["state"] == "unknown"


@pytest.mark.parametrize("initial_result", ["failed", "unknown"])
def test_explicit_replacement_requires_new_cost_confirmation_without_creating_records(
        work, monkeypatch, initial_result):
    task = _ready(work, monkeypatch)
    calls = []

    def transport(request):
        calls.append(request)
        if initial_result == "unknown":
            raise TimeoutError("fixture response lost")
        return {"status_code": 401, "json": {}}

    with pytest.raises(TalkingHeadBlocked):
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    before_runs = sub.runs_for_work(work["id"])
    before_jobs = st.all_rows("provider_jobs", "work_id=?", [work["id"]])
    assert before_runs[0]["state"] == initial_result
    with pytest.raises(TalkingHeadBlocked) as raised:
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False,
                             replacement_authorized=True)
    assert raised.value.code == "RESUBMIT_REQUIRES_AUTHORIZATION"
    assert len(calls) == 1
    assert sub.runs_for_work(work["id"]) == before_runs
    assert st.all_rows("provider_jobs", "work_id=?", [work["id"]]) == before_jobs


@pytest.mark.parametrize("initial_result", ["failed", "unknown"])
def test_reconfirmed_explicit_replacement_submits_once_and_preserves_original_ledger(
        work, monkeypatch, initial_result):
    task = _ready(work, monkeypatch)
    calls = []

    def transport(request):
        calls.append(request)
        if len(calls) == 1:
            if initial_result == "unknown":
                raise TimeoutError("fixture response lost")
            return {"status_code": 401, "json": {}}
        return volc_submit_ok("replacement-remote")

    with pytest.raises(TalkingHeadBlocked):
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    original = sub.runs_for_work(work["id"])[0]
    original_job = jobs.get_job(original["job_id"])
    confirmation = authorize_cost(work["id"])
    assert confirmation["id"] != original["cost_reservation_id"]
    # 重新确认本身不能自动重做，仍须用户明确要求这次新尝试。
    with pytest.raises(TalkingHeadBlocked):
        sub.submit_omnihuman(work["id"], task=task, transport=transport, dry_run=False)
    replacement = sub.submit_omnihuman(work["id"], task=task, transport=transport,
                                       dry_run=False, replacement_authorized=True)
    repeated = sub.submit_omnihuman(work["id"], task=task, transport=transport,
                                    dry_run=False, replacement_authorized=True)
    assert replacement["submitted"] is True
    retry_run = replacement["runs"][0]
    assert retry_run["remote_job_id"] == "replacement-remote"
    assert retry_run["id"] != original["id"]
    assert retry_run["cost_reservation_id"] == confirmation["id"]
    assert repeated["runs"][0]["id"] == retry_run["id"]
    assert len(calls) == 2, "旧请求一次、新授权一次；重复点击不得再次 POST"
    assert len(sub.runs_for_work(work["id"])) == 2
    assert sub.get_run(original["id"]) == original
    assert jobs.get_job(original["job_id"]) == original_job
