"""审核未知结果的人工恢复：只复审已有稿，旧调用与预留保持不变。"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import jobs, storage as st
from app.content import repository as repo, service, tasks
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)


@pytest.fixture
def client():
    return TestClient(app)


def unknown_review():
    wid = make_work()
    transport = Script()
    transport.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, transport)
    transport.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    transport.q["reviewer"].append(TimeoutError("review test timeout"))
    parent = run_job("generation", wid, {}, service.handle_generation, transport)
    call = next(c for c in jobs.content_calls(parent["id"]) if c["step"] == "review")
    batch = st.get("script_batches", wid)
    assert parent["state"] == call["state"] == "unknown"
    return wid, transport, parent, call, batch


def body(wid, batch, **extra):
    return {"request_id": uuid4().hex, "batch_id": batch["id"],
            "expected_content_revision": st.get("works", wid)["content_revision"], **extra}


def endpoint(wid):
    return f"/api/v1/works/{wid}/content"


def error(response):
    data = response.json()
    return data.get("error") or data["detail"]["error"]


def test_state_exposes_current_unknown_review_without_sending(client):
    wid, transport, parent, call, batch = unknown_review()
    counts = dict(transport.calls)
    state = client.get(endpoint(wid) + "/state").json()
    assert state["review_retry"] == {"call_id": call["id"], "job_id": parent["id"],
                                     "batch_id": batch["id"], "reserved_fen": call["reserved_fen"],
                                     "has_remote_handle": False}
    receipt = next(j for j in state["recent_jobs"] if j["job_id"] == parent["id"])
    assert receipt["batch_id"] == batch["id"]
    assert transport.calls == counts
    assert jobs.get_job(call["id"])["settlement_state"] == "held"


def test_unknown_review_requires_matching_fee_ack_before_new_intent(client):
    wid, _, _, call, batch = unknown_review()
    initial = len(tasks.list_jobs(wid))
    for extra in ({}, {"retry_unknown_call_id": call["id"]}, {"retry_paid_ack": True}):
        response = client.post(endpoint(wid) + "/reviews", json=body(wid, batch, **extra))
        assert response.status_code == 422
        assert error(response)["code"] == "REVIEW_RETRY_CONFIRMATION_REQUIRED"
    assert len(tasks.list_jobs(wid)) == initial


def test_confirmed_retry_only_reviews_and_replays_same_intent(client):
    wid, transport, _, call, batch = unknown_review()
    old_call = jobs.get_job(call["id"])
    payload = body(wid, batch, retry_unknown_call_id=call["id"], retry_paid_ack=True)
    response = client.post(endpoint(wid) + "/reviews", json=payload)
    assert response.status_code == 202, response.text
    retry = tasks.get_job(response.json()["job_id"])
    assert retry["payload"] == {"batch_id": batch["id"], "retry_unknown_call_id": call["id"], "retry_paid_ack": True}
    replay = client.post(endpoint(wid) + "/reviews", json=payload)
    assert replay.json()["job_id"] == retry["id"] and replay.json()["replayed"]
    before_write = transport.calls["writer"]
    transport.q["reviewer"].append(review_reply())
    tasks.set_progress(retry["id"], state="running")
    service.handle_review(retry, transport=transport)
    assert tasks.get_job(retry["id"])["state"] == "succeeded"
    assert transport.calls["writer"] == before_write
    assert len(jobs.content_calls(retry["id"])) == 1
    assert jobs.content_calls(retry["id"])[0]["step"] == "review"
    assert jobs.get_job(call["id"]) == old_call
    assert client.get(endpoint(wid) + "/state").json()["review_retry"] is None
    # 已完成后相同 RID 仍回放；旧调用不因后续成功而被覆盖或释放。
    assert client.post(endpoint(wid) + "/reviews", json=payload).json()["job_id"] == retry["id"]
    changed = client.post(endpoint(wid) + "/reviews", json={**payload, "retry_unknown_call_id": "wrong-call"})
    assert changed.status_code == 409 and error(changed)["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("mutate", ["wrong_work", "wrong_batch", "wrong_step", "remote_handle", "stale_revision"])
def test_retry_rejects_unrelated_or_queryable_targets(client, mutate):
    wid, _, _, call, batch = unknown_review()
    if mutate == "wrong_work":
        other, _, _, foreign, _ = unknown_review()
        assert other != wid
        target = foreign["id"]
    elif mutate == "wrong_batch":
        target = call["id"]
        batch = dict(batch, id="batch_not_current")
    elif mutate == "wrong_step":
        target = next(c["id"] for c in st.all_rows("provider_jobs", "work_id=?", [wid]) if c["step"] == "write")
    elif mutate == "remote_handle":
        target = call["id"]
        st.put("provider_jobs", target, dict(jobs.get_job(target), remote_task_id="remote-review"), {"work_id": wid})
    else:
        target = call["id"]
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, brief=dict(st.get("works", wid)["brief"], topic="changed"))
    count = len(tasks.list_jobs(wid))
    response = client.post(endpoint(wid) + "/reviews", json=body(wid, batch, retry_unknown_call_id=target, retry_paid_ack=True))
    assert response.status_code == 409, response.text
    assert error(response)["code"] == "REVIEW_RETRY_TARGET_INVALID"
    assert len(tasks.list_jobs(wid)) == count
    assert jobs.get_job(call["id"])["state"] == "unknown"


def test_queryable_unknown_review_cannot_be_silently_resubmitted(client):
    wid, _, _, call, batch = unknown_review()
    st.put("provider_jobs", call["id"], dict(jobs.get_job(call["id"]), remote_task_id="remote-review"), {"work_id": wid})
    state = client.get(endpoint(wid) + "/state").json()
    assert state["review_retry"] is None
    response = client.post(endpoint(wid) + "/reviews", json=body(wid, batch))
    assert response.status_code == 409 and error(response)["code"] == "REVIEW_RESULT_PENDING"


def test_review_after_failed_known_result_needs_no_unknown_retry_ack(client):
    wid, transport, _, call, batch = unknown_review()
    response = client.post(endpoint(wid) + "/reviews", json=body(wid, batch, retry_unknown_call_id=call["id"], retry_paid_ack=True))
    retry = tasks.get_job(response.json()["job_id"])
    transport.q["reviewer"].append(review_reply(fail=(1,)))
    tasks.set_progress(retry["id"], state="running")
    service.handle_review(retry, transport=transport)
    assert tasks.get_job(retry["id"])["state"] == "partial"
    assert client.get(endpoint(wid) + "/state").json()["review_retry"] is None
    response = client.post(endpoint(wid) + "/reviews", json=body(wid, batch))
    assert response.status_code == 202


def test_edit_after_timeout_does_not_bind_old_unknown_review_to_new_batch(client):
    wid, _, parent, call, batch = unknown_review()
    original = batch["candidates"][0]
    service.revise_candidate(wid, original["id"], original["text"] + "这是修改后的个人观察。")
    edited = st.get("script_batches", wid)
    assert edited["id"] != batch["id"]
    state = client.get(endpoint(wid) + "/state").json()
    assert state["review_retry"] is None
    stale = client.post(endpoint(wid) + "/reviews", json=body(
        wid, edited, retry_unknown_call_id=call["id"], retry_paid_ack=True))
    assert stale.status_code == 409 and error(stale)["code"] == "REVIEW_RETRY_TARGET_INVALID"
    normal = client.post(endpoint(wid) + "/reviews", json=body(wid, edited))
    assert normal.status_code == 202
    assert jobs.get_job(call["id"])["state"] == "unknown"
