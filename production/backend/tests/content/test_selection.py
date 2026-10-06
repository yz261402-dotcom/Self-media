"""选用门禁：服务端复验；旧空 checks、伪审核、失败审核、历史选用都不能绕过。"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import repository as repo
from app.content import selection, semantic_review as sr, service
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


@pytest.fixture()
def client():
    return TestClient(app)


def generated(fail=(), **review_kw):
    wid = make_work()
    s = Script()
    s.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply(fail=fail, **review_kw))
    job = run_job("generation", wid, {}, service.handle_generation, s)
    return wid, s, job


def ids(wid):
    return {c["slot"]: c["id"] for c in st.get("script_batches", wid)["candidates"]}


def err(r):
    b = r.json()
    return b.get("error") or b["detail"]["error"]


def test_missing_independent_review_cannot_adopt(client):
    wid, sid = "work_" + uuid4().hex, "script_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "review gate", "brief": {"topic": "AI"},
                          "script_version_id": None, "current_task_id": None, "version": 1})
    st.put("script_batches", wid, {"id": "batch_" + uuid4().hex, "work_id": wid, "source_kind": "llm",
           "candidates": [{"id": sid, "title": "候选", "text": "测试正文", "angle": "a", "description": "d", "tags": [],
                           "duration_estimate_ms": 1000, "claims": [],
                           "quality_report": {"checks": [], "overall": "pass"}}]})
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": sid})
    assert r.status_code == 409 and err(r)["code"] == "SCRIPT_REVIEW_REQUIRED"
    assert st.get("works", wid).get("script_version_id") is None


def test_adopt_reviewed_candidate_and_record_binding(client):
    wid, _, _ = generated()
    cid = ids(wid)[1]
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": cid})
    assert r.status_code == 200, r.text
    assert st.get("works", wid)["script_version_id"] == cid
    sel = [s for s in st.all_rows("selections", "work_id=?", [wid]) if s.get("state") == "selected"][0]
    assert sel["review_id"] and sel["set_review_id"] and sel["selected_digest"].startswith("sha256:")


def test_failed_review_cannot_adopt(client):
    wid, _, _ = generated(fail=(3,))
    i = ids(wid)
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": i[3]})
    assert r.status_code == 409 and err(r)["code"] in ("SCRIPT_QUALITY_FAILED", "SCRIPT_REVIEW_REQUIRED")
    proj = client.get(f"/api/v1/works/{wid}/scripts").json()
    assert proj["candidates"] == [] and not proj["ready_to_present"]
    assert any(c["slot"] == 3 for c in proj["retry_targets"])


def test_stale_or_unknown_ids_rejected(client):
    wid, _, _ = generated()
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": "script_nope"})
    assert r.status_code == 409 and err(r)["code"] == "STALE_SELECTION"
    cid = ids(wid)[1]
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": cid, "review_id": "qd_fake"})
    assert r.status_code == 409 and err(r)["code"] == "STALE_SELECTION"
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": cid, "expected_content_revision": 99})
    assert r.status_code == 409 and err(r)["code"] == "CONTENT_REVISION_STALE"
    assert st.get("works", wid).get("script_version_id") is None


def test_forged_decision_without_binding_does_not_count(client):
    wid, _, _ = generated()
    cid = ids(wid)[2]
    for d in sr.decisions_for(wid):
        if d["subject_id"] == cid:
            st.put("quality_decisions", d["id"], dict(d, binding={}), {"work_id": wid})
    r = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": cid})
    assert r.status_code == 409 and err(r)["code"] == "SCRIPT_REVIEW_REQUIRED"


def test_set_review_failure_blocks_flagged_slot():
    wid, _, _ = generated(set_distinct=False, rewrite=(2,))
    proj = selection.project_batch(wid)
    assert proj["candidates"] == [] and not proj["ready_to_present"]


def test_historical_selected_record_no_longer_satisfies_downstream_gates(client):
    from app import main
    wid, _, _ = generated()
    cid = ids(wid)[1]
    assert client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": cid}).status_code == 200
    assert main._script_selected(wid) is True
    with st.transaction() as c:
        w = st.get("works", wid)
        w["script_version_id"] = None
        st.insert_row(c, "works", wid, w)
    assert main._script_selected(wid) is False
    r = client.post(f"/api/v1/works/{wid}/audio", json={})
    assert r.status_code in (409, 422) and err(r)["code"] == "SELECTION_REQUIRED"


def test_adopted_script_loses_validity_when_inputs_change():
    from app import main
    wid, _, _ = generated()
    selection.adopt(wid, ids(wid)[1])
    assert main._script_selected(wid)
    with st.transaction() as c:
        w = st.get("works", wid)
        repo.bump_content_revision(c, wid, brief=dict(w["brief"], topic="又换了"))
    assert not main._script_selected(wid) and st.get("works", wid)["script_version_id"] is None


def test_mock_batch_keeps_working_and_is_marked():
    wid = "work_" + uuid4().hex
    from app.mockdata import make_candidates
    st.put("works", wid, {"id": wid, "name": "m", "brief": {"topic": "RAG"}, "script_version_id": None, "version": 1})
    st.put("script_batches", wid, {"id": "b", "work_id": wid, "source_kind": "mock",
                                   "candidates": [c.model_dump() for c in make_candidates("RAG")]})
    proj = selection.project_batch(wid)
    assert proj["source_kind"] == "mock"
    for c in proj["candidates"]:
        hard = any(k["hard_gate"] and k["outcome"] != "pass" for k in c["quality_report"]["checks"])
        assert c["pickable"] is (not hard)
