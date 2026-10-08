"""Human review gates, atomic edits and idempotent rewrite queueing; no live model calls."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import context as cx, human_review, repository as repo, selection, semantic_review as sr, service, tasks
from app.content.errors import ContentRejected
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env
from tests.content.test_partial_adoption import reviewed_batch


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")
    monkeypatch.setattr(tasks, "get_scheduler", lambda: SimpleNamespace(start=lambda: None))


def payload(wid, batch, action="adopt", scores=None, **fields):
    return {"request_id": uuid4().hex, "script_id": batch["candidates"][0]["id"],
            "expected_content_revision": st.get("works", wid)["content_revision"], "expected_batch_id": batch["id"],
            "action": action, "rubric": "core5", "scores": scores or dict.fromkeys(human_review.CORE, 5), **fields}


@pytest.mark.parametrize("rubric,keys", [("core5", human_review.CORE), ("extended8", human_review.EXTENDED)])
def test_exact_four_is_not_passed(rubric, keys):
    assert not human_review.score_summary(rubric, dict.fromkeys(keys, 4))["passed"]
    assert human_review.score_summary(rubric, {**dict.fromkeys(keys, 4), "hook": 5})["passed"]


def test_scored_adoption_is_required_and_idempotent():
    wid, script, batch = reviewed_batch(storyboard=True, fail=(2, 3))
    cid = batch["candidates"][0]["id"]
    with pytest.raises(selection.SelectionRejected) as failure:
        selection.adopt(wid, cid)
    assert failure.value.code == "HUMAN_REVIEW_REQUIRED"
    client = TestClient(app)
    endpoint = f"/api/v1/works/{wid}/content/human-review"
    view = client.get(endpoint + "/" + cid).json()
    assert view["eligible"] and view["review"] is None
    req = payload(wid, batch)
    before_calls = dict(script.calls)
    first = client.post(endpoint, json=req)
    assert first.status_code == 200, first.text
    assert first.json()["review"]["mode"] == "scored_pass"
    assert first.json()["review"]["average"] == 5
    assert selection.current_selection_valid(wid)
    selections_before = st.all_rows("selections", "work_id=?", [wid])
    replay = client.post(endpoint, json=req)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert st.all_rows("selections", "work_id=?", [wid]) == selections_before
    assert script.calls == before_calls
    changed = client.post(endpoint, json={**req, "notes": "changed"})
    assert changed.status_code == 409
    candidate = selection.project_batch(wid)["candidates"][0]
    assert candidate["human_review_state"] == "scored_pass"


@pytest.mark.parametrize("scores,status", [({"hook": 5}, 422), (dict.fromkeys(human_review.CORE, 4), 422),
                                          (dict.fromkeys(human_review.CORE, True), 422),
                                          (dict.fromkeys(human_review.CORE, 4.5), 422)])
def test_invalid_or_insufficient_scores_never_adopt(scores, status):
    wid, _, batch = reviewed_batch(storyboard=True)
    response = TestClient(app).post(f"/api/v1/works/{wid}/content/human-review", json=payload(wid, batch, scores=scores))
    assert response.status_code == status, response.text
    assert st.get("works", wid).get("script_version_id") is None


def test_edit_adopt_preserves_old_draft_and_does_not_forge_ai_review(monkeypatch, tmp_path):
    from app.avatar import person_image
    from app.short_video import service as video_service

    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(person_image.KEY_ENV, "test-image-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    wid, script, batch = reviewed_batch(storyboard=True)
    original = batch["candidates"][0]
    shots = deepcopy(original["storyboard"])
    shots[0]["dialogue"] = "先想想你最常卡在哪一步，再选一个顺手的办法，就试着做起来。"
    before = dict(script.calls)
    req = payload(wid, batch, "edit_adopt", scores=dict.fromkeys(human_review.CORE, 3), storyboard=shots)
    result = human_review.submit(wid, req)
    cid = result["script_id"]
    assert cid != original["id"] and result["review"]["mode"] == "human_edit"
    assert not result["review"]["passed"]
    assert selection.current_selection_valid(wid)
    projected = selection.project_batch(wid)
    assert projected["id"] != batch["id"]
    edited = projected["candidates"][0]
    assert edited["id"] == cid and edited["parent_id"] == original["id"]
    assert edited["human_review_state"] == "human_edit" and edited["review_state"] == "needs_review"
    assert repo.get_script_version(original["id"])["storyboard"] == original["storyboard"]
    assert not [decision for decision in sr.decisions_for(wid) if decision["subject_id"] == cid]
    assert script.calls == before
    assert human_review.submit(wid, req)["script_id"] == cid
    graph = video_service.create_graph(wid, image_ceiling_fen_per_shot=100)
    assert graph["script_version_id"] == cid
    assert graph["shots"][0]["dialogue"] == shots[0]["dialogue"]
    video_service.assert_current(graph)
    assert script.calls == before


@pytest.mark.parametrize("mutation", ["unchanged", "hard_fail", "invalid_shot"])
def test_failed_edit_rolls_back_entire_transaction(mutation):
    wid, _, batch = reviewed_batch(storyboard=True)
    shots = deepcopy(batch["candidates"][0]["storyboard"])
    if mutation == "hard_fail":
        shots[0]["dialogue"] = "这个产品价格是399元，马上购买。"
    elif mutation == "invalid_shot":
        shots[0]["duration_seconds"] = 0
    before_batches = repo.list_batches(wid)
    before_versions = st.all_rows("script_versions", "work_id=?", [wid])
    with pytest.raises((ContentRejected, selection.SelectionRejected)):
        human_review.submit(wid, payload(wid, batch, "edit_adopt", storyboard=shots))
    assert st.get("script_batches", wid)["id"] == batch["id"]
    assert repo.list_batches(wid) == before_batches
    assert st.all_rows("script_versions", "work_id=?", [wid]) == before_versions
    assert st.get("works", wid).get("script_version_id") is None


def test_stale_or_active_requests_do_not_adopt():
    wid, _, batch = reviewed_batch(storyboard=True)
    req = payload(wid, batch)
    with pytest.raises(repo.StaleContentWrite):
        human_review.submit(wid, {**req, "expected_content_revision": 100})
    tasks.submit_job(wid, "review", "queued-review", {"batch_id": batch["id"]})
    with pytest.raises(tasks.FlowActive):
        human_review.submit(wid, req)
    assert st.get("works", wid).get("script_version_id") is None


def test_human_rewrite_targets_one_candidate_then_requires_ai_and_human_review():
    wid, script, batch = reviewed_batch(storyboard=True)
    req = payload(wid, batch, "rewrite", scores=dict.fromkeys(human_review.CORE, 3), feedback="开头换成直接提出问题")
    result = human_review.submit(wid, req)
    job = result["job"]
    assert job["payload"]["candidate_ids"] == [req["script_id"]]
    assert job["payload"]["instruction"] == req["feedback"]
    replay = human_review.submit(wid, req)
    assert replay["job"]["id"] == job["id"]
    assert len([j for j in tasks.list_jobs(wid) if j["kind"] == "repair"]) == 1
    assert st.get("works", wid).get("script_version_id") is None
    shots = deepcopy(batch["candidates"][0]["storyboard"])
    shots[0]["dialogue"] = "你是不是也常在工具之间来回切换？选一个顺手的，把心思放回眼前的事情。"
    script.q["writer"].append({"candidates": [{"slot": 1, "storyboard": shots, "claims": []}]})
    script.q["reviewer"].append(review_reply())
    tasks.set_progress(job["id"], state="running")
    service.handle_repair(job, transport=script)
    assert req["feedback"] in script.seen["writer"][-1]
    projected = selection.project_batch(wid)
    first = projected["candidates"][0]
    assert first["parent_id"] == req["script_id"] and first["pickable"]
    assert first["human_review_state"] == "pending"
    assert [c["id"] for c in projected["candidates"][1:]] == [c["id"] for c in batch["candidates"][1:]]
    with pytest.raises(selection.SelectionRejected) as failure:
        selection.adopt(wid, first["id"])
    assert failure.value.code == "HUMAN_REVIEW_REQUIRED"


def test_visual_edit_keeps_existing_verified_factual_dialogue():
    wid = make_work(materials=["产品价格为399元。"])
    work = st.get("works", wid)
    work["brief"]["content_route"] = "short_video_storyboard"
    st.put("works", wid, work)
    script = Script()
    script.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, script)
    evidence_id = cx.load_context(wid)["materials"][0]["evidence_id"]
    candidates = [dict(cand(i), storyboard=[{"shot_index": 1, "visual": "人物望向镜头。",
                  "dialogue": cand(i)["text"], "sound_effects": "环境声", "duration_seconds": 8,
                  "prompt": "人物望向镜头。"}]) for i in (1, 2, 3)]
    candidates[0]["storyboard"][0]["dialogue"] = "产品价格为399元。先考虑预算，再想想自己的需求。"
    candidates[0]["claims"] = [{"text": "产品价格为399元。", "position": "产品价格为399元。",
                               "source_quote": "产品价格为399元。", "evidence_id": evidence_id}]
    script.q["writer"].append({"candidates": candidates})
    script.q["reviewer"].append(review_reply())
    generated = run_job("generation", wid, {}, service.handle_generation, script)
    assert generated["state"] == "succeeded", generated.get("error")
    batch = selection.project_batch(wid)
    shots = deepcopy(batch["candidates"][0]["storyboard"])
    shots[0]["visual"] = "人物在桌前展示产品。"
    adopted = human_review.submit(wid, payload(wid, batch, "edit_adopt", storyboard=shots))
    revised = repo.get_script_version(adopted["script_id"])
    assert revised["claims"][0]["verdict"] == "supported"
    assert revised["origin"]["evidence"][0]["verified"]
    assert selection.current_selection_valid(wid)
