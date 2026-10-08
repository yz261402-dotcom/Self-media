"""content API 契约：写操作 202 + 回执、GET 不触发外部请求、幂等/修订号冲突、状态与能力页无凭据。

调度线程在测试里不启动（handlers 由用例直接调用），这里只验证接线与契约。
"""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import repository as repo
from app.content import service, tasks
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    # 不让后台调度器抢占用例里手动执行的任务
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)


@pytest.fixture()
def client():
    return TestClient(app)


def base(wid):
    return f"/api/v1/works/{wid}/content"


def err(r):
    b = r.json()
    return b.get("error") or b["detail"]["error"]


def rev(wid):
    return st.get("works", wid)["content_revision"]


def test_generation_rejects_route_mismatch_before_submitting_job(client):
    wid = make_work()
    body = {"request_id": "route-mismatch", "prompt_id": "p",
            "content_route": "short_video_storyboard", "expected_content_revision": rev(wid)}
    response = client.post(base(wid) + "/generations", json=body)
    assert response.status_code == 409
    assert err(response)["code"] == "CONTENT_ROUTE_STALE"
    assert not tasks.list_jobs(wid)


def _unknown_write_call(wid, prompt_id="prompt-retry", *, step="write"):
    from app import jobs

    parent = tasks.submit_job(wid, "generation", "old-generation", {"prompt_id": prompt_id},
                              expected_revision=rev(wid))
    tasks.set_progress(parent["id"], state="running")
    call = jobs.submit_content_call(parent_id=parent["id"], parent_kind="generation", work_id=wid,
                                    provider="claude_compat", model="claude-sonnet-5-5", capability="chat",
                                    idempotency_key="old-write-" + uuid4().hex, request_digest="digest",
                                    reserve_fen=127, step=step, round_no=0)
    jobs.mark_sending(call["id"], "test-worker")
    jobs.mark_content_unknown(call["id"], "RemoteDisconnected")
    tasks.finish_job(parent["id"], "unknown", error={"code": "CONTENT_RESULT_UNKNOWN", "message": "unknown"})
    return parent, call


def test_generation_unknown_write_retry_requires_ack_and_binds_new_intent(client):
    wid = make_work()
    old, call = _unknown_write_call(wid)
    body = {"request_id": "new-generation", "prompt_id": "prompt-retry",
            "expected_content_revision": rev(wid), "retry_unknown_call_id": call["id"], "retry_paid_ack": True}
    response = client.post(base(wid) + "/generations", json=body)
    assert response.status_code == 202
    retry = tasks.get_job(response.json()["job_id"])
    assert retry["id"] != old["id"]
    assert retry["payload"]["retry_unknown_call_id"] == call["id"]
    assert retry["payload"]["retry_paid_ack"] is True
    from app import jobs
    jobs_state = jobs.get_job(call["id"])
    assert jobs_state
    assert jobs_state["state"] == "unknown" and jobs_state["attempts"] == 1


def test_generation_unknown_retry_rejects_wrong_stage_and_missing_ack(client):
    wid = make_work()
    _, call = _unknown_write_call(wid, step="review")
    base_body = {"prompt_id": "prompt-retry", "expected_content_revision": rev(wid),
                 "retry_unknown_call_id": call["id"]}
    no_ack = client.post(base(wid) + "/generations", json={**base_body, "request_id": "no-ack"})
    assert no_ack.status_code == 422 and err(no_ack)["code"] == "GENERATION_RETRY_CONFIRMATION_REQUIRED"
    wrong_stage = client.post(base(wid) + "/generations", json={**base_body, "request_id": "wrong-stage",
                                                                       "retry_paid_ack": True})
    assert wrong_stage.status_code == 409 and err(wrong_stage)["code"] == "GENERATION_RETRY_TARGET_INVALID"


def test_writes_return_202_receipt_and_replay_same_job(client):
    wid = make_work()
    body = {"request_id": "r1", "expected_content_revision": rev(wid)}
    a = client.post(base(wid) + "/prompts", json=body)
    assert a.status_code == 202
    j = a.json()
    assert set(j) >= {"job_id", "kind", "state", "stage", "replayed", "poll_url", "result_refs", "error"}
    assert j["kind"] == "prompt" and j["replayed"] is False and j["poll_url"].endswith(j["job_id"])
    b = client.post(base(wid) + "/prompts", json=body)
    assert b.status_code == 202 and b.json()["job_id"] == j["job_id"] and b.json()["replayed"] is True


def test_same_request_id_different_payload_is_idempotency_conflict(client):
    wid = make_work()
    r1 = client.post(base(wid) + "/searches", json={"request_id": "s", "query": "ai"})
    assert r1.status_code == 202
    r2 = client.post(base(wid) + "/searches", json={"request_id": "s", "query": "other"})
    assert r2.status_code == 409 and err(r2)["code"] == "IDEMPOTENCY_CONFLICT"


def test_stale_expected_revision_is_409(client):
    wid = make_work()
    r = client.post(base(wid) + "/prompts", json={"request_id": "x", "expected_content_revision": 99})
    assert r.status_code == 409 and err(r)["code"] == "CONTENT_REVISION_STALE"


def test_unknown_fields_and_missing_request_id_rejected(client):
    wid = make_work()
    assert client.post(base(wid) + "/prompts", json={"request_id": "x", "expected_content_revision": 0, "evil": 1}).status_code == 422
    assert client.post(base(wid) + "/prompts", json={"expected_content_revision": 0}).status_code == 422


def test_unknown_work_is_404(client):
    r = client.post("/api/v1/works/nope/content/prompts", json={"request_id": "x", "expected_content_revision": 0})
    assert r.status_code == 404


def test_search_validation(client):
    wid = make_work()
    assert client.post(base(wid) + "/searches", json={"request_id": "a", "query": " "}).status_code == 422
    assert client.post(base(wid) + "/searches", json={"request_id": "b", "query": "x", "sort": "hot"}).status_code == 422
    assert client.post(base(wid) + "/searches", json={"request_id": "c", "query": "x", "period_days": 3}).status_code == 422


def test_import_requires_id_or_link_and_search_membership(client):
    wid = make_work()
    assert client.post(base(wid) + "/imports", json={"request_id": "i", "expected_content_revision": rev(wid)}).status_code == 422
    r = client.post(base(wid) + "/imports", json={"request_id": "i2", "search_job_id": "cjob_nope", "platform_work_id": "1",
                                                   "expected_content_revision": rev(wid)})
    assert r.status_code == 422
    ok = client.post(base(wid) + "/imports", json={"request_id": "i3", "platform_work_id": "77", "expected_content_revision": rev(wid)})
    assert ok.status_code == 202


def test_second_mutating_flow_is_rejected_while_one_is_active(client):
    wid = make_work()
    assert client.post(base(wid) + "/prompts", json={"request_id": "p1", "expected_content_revision": rev(wid)}).status_code == 202
    r = client.post(base(wid) + "/prompts", json={"request_id": "p2", "expected_content_revision": rev(wid)})
    assert r.status_code == 409 and err(r)["code"] == "CONTENT_FLOW_ACTIVE"
    # 搜索不受限
    assert client.post(base(wid) + "/searches", json={"request_id": "s1", "query": "ai"}).status_code == 202


def test_get_job_and_state_never_call_providers(client, monkeypatch):
    from app.content.providers import redfox, text as tp
    monkeypatch.setattr(redfox, "post", lambda *a, **k: pytest.fail("GET 不得请求 Redfox"))
    monkeypatch.setattr(tp, "call_text", lambda *a, **k: pytest.fail("GET 不得请求模型"))
    wid = make_work()
    j = client.post(base(wid) + "/prompts", json={"request_id": "p", "expected_content_revision": rev(wid)}).json()
    g = client.get(base(wid) + f"/jobs/{j['job_id']}")
    assert g.status_code == 200 and g.json()["state"] == "queued" and "cancel" in g.json()["actions"]
    s = client.get(base(wid) + "/state")
    assert s.status_code == 200
    body = s.json()
    assert body["content_revision"] == rev(wid) and body["active_jobs"] and body["reference"]["transcript"]["state"] == "ready"
    assert client.get(base(wid) + "/jobs/cjob_nope").status_code == 404
    other = make_work()
    assert client.get(base(other) + f"/jobs/{j['job_id']}").status_code == 404  # 不能跨作品读任务


def test_reference_detail_reads_unselected_snapshot_without_external_calls(client, monkeypatch):
    from app.content.providers import qwen_vision, redfox, text as tp

    def forbidden(*args, **kwargs):
        pytest.fail("Reference GET must not submit jobs or call providers")

    monkeypatch.setattr(redfox, "post", forbidden)
    monkeypatch.setattr(tp, "call_text", forbidden)
    monkeypatch.setattr(qwen_vision, "call_frames", forbidden)
    monkeypatch.setattr(tasks, "submit_job", forbidden)
    monkeypatch.setattr(tasks.Scheduler, "start", forbidden)
    wid = make_work()
    work_before = st.get("works", wid)
    selected = repo.get_reference(work_before["reference_snapshot_id"])
    snapshot = dict(selected, id="ref_" + uuid4().hex, parent_id=selected["id"],
                    transcript=dict(selected["transcript"], text="Another imported reference"))
    with st.transaction() as c:
        saved = repo.save_reference(c, snapshot)

    response = client.get(base(wid) + f"/references/{saved['id']}")

    assert response.status_code == 200
    assert response.json() == saved
    assert st.get("works", wid) == work_before
    assert repo.get_reference(saved["id"]) == saved
    assert not tasks.list_jobs(wid)


@pytest.mark.parametrize("target", ["missing_reference", "foreign_work", "missing_work"])
def test_reference_detail_is_404_for_unknown_or_cross_work_snapshots(client, target):
    wid = make_work()
    rid = st.get("works", wid)["reference_snapshot_id"]
    expected_code = "REFERENCE_NOT_FOUND"
    if target == "missing_reference":
        rid = "ref_missing"
    elif target == "foreign_work":
        wid = make_work()
    else:
        wid = "work_missing"
        expected_code = "WORK_NOT_FOUND"

    response = client.get(base(wid) + f"/references/{rid}")

    assert response.status_code == 404
    assert isinstance(response.json().get("error") or response.json().get("detail"), dict)
    assert err(response)["code"] == expected_code


@pytest.mark.parametrize("source,text,expected_state", [
    ("redfox", "静音音频", "failed"),
    ("user_edit", "静音音频", "ready"),
    ("redfox", "这段静音音频用来演示剪辑。", "ready"),
])
def test_reference_detail_projects_legacy_transcript_without_changing_snapshot(client, source, text, expected_state):
    wid = make_work()
    original = repo.get_reference(st.get("works", wid)["reference_snapshot_id"])
    snapshot = dict(original, id="ref_" + uuid4().hex, parent_id=original["id"],
                    transcript={"state": "ready", "text": text, "source": source,
                                "task_id": "legacy-task", "error": "Preserved provider note"})
    with st.transaction() as c:
        saved = repo.save_reference(c, snapshot)

    response = client.get(base(wid) + f"/references/{saved['id']}")

    assert response.status_code == 200
    transcript = response.json()["transcript"]
    assert transcript["state"] == expected_state
    assert transcript["text"] == text and transcript["error"] == "Preserved provider note"
    assert transcript["source"] == source and transcript["task_id"] == "legacy-task"
    assert response.json()["content_digest"] == saved["content_digest"]
    assert repo.get_reference(saved["id"]) == saved


def test_state_projects_legacy_silent_reference_and_summary_without_mutation(client):
    wid = make_work()
    original = repo.get_reference(st.get("works", wid)["reference_snapshot_id"])
    snapshot = dict(original, id="ref_" + uuid4().hex, parent_id=original["id"],
                    transcript={"state": "ready", "text": "静音音频", "source": "redfox", "task_id": "legacy-task"})
    with st.transaction() as c:
        saved = repo.save_reference(c, snapshot)
        repo.bump_content_revision(c, wid, reference_snapshot_id=saved["id"])
    work_before = st.get("works", wid)

    response = client.get(base(wid) + "/state")

    assert response.status_code == 200
    body = response.json()
    assert body["reference"]["transcript"]["state"] == "failed"
    assert body["reference"]["transcript"]["text"] == "静音音频"
    summary = next(ref for ref in body["references"] if ref["id"] == saved["id"])
    assert summary["transcript_state"] == "failed"
    assert repo.get_reference(saved["id"]) == saved
    assert st.get("works", wid) == work_before
    assert not tasks.list_jobs(wid)


def test_cancel_endpoint(client):
    wid = make_work()
    j = client.post(base(wid) + "/prompts", json={"request_id": "p", "expected_content_revision": rev(wid)}).json()
    c = client.post(base(wid) + f"/jobs/{j['job_id']}/cancel")
    assert c.status_code == 200 and c.json()["state"] == "cancelled"


def test_refresh_without_handle_reports_needs_verification_and_never_resends(client):
    wid = make_work()
    j = tasks.submit_job(wid, "generation", "g", {})
    tasks.set_progress(j["id"], state="running")
    from app import jobs
    call = jobs.submit_content_call(parent_id=j["id"], parent_kind="generation", work_id=wid, provider="qwen", model="m",
                                    capability="chat", idempotency_key="k-unk", request_digest="d", reserve_fen=1)
    jobs.mark_sending(call["id"], "w")
    jobs.mark_content_unknown(call["id"], "断线")
    tasks.finish_job(j["id"], "unknown", error={"code": "CONTENT_RESULT_UNKNOWN", "message": "x"})
    r = client.post(base(wid) + f"/jobs/{j['id']}/refresh")
    assert r.status_code == 200 and r.json()["status"] == "needs_verification"
    assert jobs.get_job(call["id"])["attempts"] == 1


def test_reference_binding_and_settings(client):
    wid = make_work()
    ref = st.get("works", wid)["reference_snapshot_id"]
    r = client.put(base(wid) + "/reference", json={"reference_snapshot_id": ref, "expected_content_revision": rev(wid)})
    assert r.status_code == 200
    bad = client.put(base(wid) + "/reference", json={"reference_snapshot_id": "ref_nope", "expected_content_revision": rev(wid)})
    assert bad.status_code == 404
    stale = client.put(base(wid) + "/reference", json={"reference_snapshot_id": ref, "expected_content_revision": 0})
    assert stale.status_code == 409
    s = client.put(base(wid) + "/settings", json={"expected_content_revision": rev(wid),
                                                   "style_snapshot": {"persona": "毒舌但温柔"}})
    assert s.status_code == 200
    assert st.get("works", wid)["style_snapshot"]["digest"] and st.get("works", wid)["writing_prompt_id"] is None
    evil = client.put(base(wid) + "/settings", json={"expected_content_revision": rev(wid), "brief": {"script_version_id": "x"}})
    assert evil.status_code == 422
    empty = client.put(base(wid) + "/settings", json={"expected_content_revision": rev(wid)})
    assert empty.status_code == 422


def test_style_profile_crud_and_snapshot_isolation(client):
    r = client.post("/api/v1/content/style-profiles", json={"name": "A", "persona": "p1"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["revision"] == 1
    wid = make_work()
    assert client.put(base(wid) + "/settings", json={"expected_content_revision": rev(wid), "style_profile_id": sid}).status_code == 200
    snap_digest = st.get("works", wid)["style_snapshot"]["digest"]
    upd = client.put(f"/api/v1/content/style-profiles/{sid}", json={"name": "A", "persona": "p2", "expected_revision": 1})
    assert upd.status_code == 200 and upd.json()["revision"] == 2
    assert st.get("works", wid)["style_snapshot"]["digest"] == snap_digest  # 共享模板改了，已应用作品不变
    stale = client.put(f"/api/v1/content/style-profiles/{sid}", json={"name": "A", "persona": "p3", "expected_revision": 1})
    assert stale.status_code == 409
    assert any(s["id"] == sid for s in client.get("/api/v1/content/style-profiles").json())


def test_capabilities_page_has_no_secret(client):
    r = client.get("/api/v1/content/capabilities")
    assert r.status_code == 200
    dump = json.dumps(r.json(), ensure_ascii=False)
    assert "sk-test" not in dump and "gpt-6.1-sol" in dump and "gpt-6-astra" in dump
    assert r.json()["redfox"]["contract"] == "unverified"


def test_full_flow_via_handlers_then_state_shows_pickable_and_adopt_via_legacy_route(client):
    wid = make_work()
    s = Script()
    s.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply())
    job = run_job("generation", wid, {}, service.handle_generation, s)
    assert job["state"] == "succeeded"
    state = client.get(base(wid) + "/state").json()
    assert [c["pickable"] for c in state["batch"]["candidates"]] == [True, True, True]
    assert state["batch"]["passed"] == 3 and state["prompt"]["prompt_text"]
    pick = state["batch"]["candidates"][0]
    r = client.post(f"/api/v1/works/{wid}/script-selection",
                    json={"script_id": pick["id"], "expected_content_revision": state["content_revision"]})
    assert r.status_code == 200
    assert client.get(base(wid) + "/state").json()["selected_script_id"] == pick["id"]
