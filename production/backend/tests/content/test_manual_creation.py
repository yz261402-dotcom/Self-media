"""Human takeover stays separate from AI approval and never resends unresolved provider calls."""
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import storage as st
from app.content import context as cx, human_review, manual_creation as manual, repository as repo, selection, tasks
from app.content.errors import ContentRejected
from app.content.providers import text as tp
from app.content_api import router as content_router
from tests.content._helpers import make_work, set_env


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setattr(tp, "call_text", lambda *args, **kwargs: pytest.fail("manual takeover must not call a model"))


def work(target=0):
    wid = make_work(with_reference=False)
    row = st.get("works", wid)
    row["brief"].update(content_route="short_video_storyboard", target_duration_ms=target)
    st.put("works", wid, row)
    return wid


def request(wid, **fields):
    return {"request_id": uuid4().hex, "expected_content_revision": cx.load_context(wid)["content_revision"],
            "title": "人工创作稿", "description": "由用户核对事实后提交", "takeover_reason": "模型服务不可用，人工接管",
            "manual_ack": True, "facts_ack": True,
            "storyboard": [{"shot_index": 1, "visual": "人物站在桌前，屏幕显示价格399元。",
                            "dialogue": "产品价格为399元，请根据自己的需要决定。", "sound_effects": "环境声",
                            "duration_seconds": 8}], **fields}


def review_payload(wid, action="adopt", score=5, **fields):
    batch = st.get("script_batches", wid)
    return {"request_id": uuid4().hex, "script_id": batch["candidates"][0]["id"],
            "expected_content_revision": cx.load_context(wid)["content_revision"], "expected_batch_id": batch["id"],
            "action": action, "rubric": "core5", "scores": dict.fromkeys(human_review.CORE, score), **fields}


def client():
    app = FastAPI()
    app.include_router(manual.router)
    app.include_router(content_router)
    return TestClient(app)


def test_import_provenance_one_candidate_and_idempotency():
    wid = work()
    req = request(wid)
    before = st.all_rows("provider_jobs", "work_id=?", [wid])
    first = manual.create(wid, req)
    second = manual.create(wid, req)
    assert first == second
    assert st.all_rows("provider_jobs", "work_id=?", [wid]) == before
    assert len(repo.list_batches(wid)) == 1
    candidate = first["candidate"]
    batch = selection.project_batch(wid)
    assert batch["source_kind"] == "manual" and batch["requested"] == batch["produced"] == 1
    assert batch["passed"] == 0 and batch["ready_to_present"]
    assert batch["retry_targets"] == batch["review_targets"] == []
    assert candidate["origin"]["kind"] == "manual_import"
    assert candidate["claims"] == []
    assert batch["candidates"][0]["ai_review_status"] == "not_performed"
    assert batch["candidates"][0]["review_state"] == "needs_review"
    assert not repo.get_script_version(candidate["id"]).get("creation_source")
    assert any(check["hard_gate"] and check["outcome"] != "pass"
               for check in candidate["origin"]["automated_rule_report"]["checks"])
    imported = manual.current_import(wid, candidate, batch, cx.load_context(wid))
    assert imported["facts_ack"] and imported["ai_review_status"] == "not_performed"
    assert not st.get("works", wid).get("script_version_id")
    with pytest.raises(tasks.IdempotencyConflict):
        manual.create(wid, {**req, "title": "不同标题"})


def test_api_get_and_complete_scoring_are_required_before_video(tmp_path, monkeypatch):
    from app.avatar import person_image
    from app.short_video import service as video

    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(person_image.KEY_ENV, "test-image-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    wid = work()
    api = client()
    response = api.post(f"/api/v1/works/{wid}/content/manual-storyboards", json=request(wid))
    assert response.status_code == 200, response.text
    sid = response.json()["script_id"]
    view = api.get(f"/api/v1/works/{wid}/content/human-review/{sid}").json()
    assert view["eligible"] and view["creation_source"] == "manual"
    assert view["ai_review_status"] == "not_performed" and view["takeover_reason"]
    with pytest.raises(selection.SelectionRejected) as failure:
        selection.adopt(wid, sid)
    assert failure.value.code == "HUMAN_REVIEW_REQUIRED"
    with pytest.raises(ContentRejected) as failure:
        human_review.submit(wid, review_payload(wid, score=4))
    assert failure.value.code == "HUMAN_SCORE_NOT_PASSED"
    assert not selection.current_selection_valid(wid)
    req = review_payload(wid, score=4)
    req["scores"]["hook"] = 5
    result = human_review.submit(wid, req)
    assert result["review"]["average"] == 4.2
    assert result["review"]["mode"] == "manual_takeover" and result["review"]["scored_pass"]
    assert result["review"]["ai_review_id"] is None and result["review"]["ai_set_review_id"] is None
    assert human_review.submit(wid, req) == result
    assert selection.current_selection_valid(wid)
    assert selection.project_batch(wid)["candidates"][0]["human_review_state"] == "manual_takeover"
    graph = video.create_graph(wid, image_ceiling_fen_per_shot=100)
    assert len(graph["shots"]) == 1 and graph["script_version_id"] == sid and not graph["is_mock"]
    assert graph["shots"][0]["dialogue"] == view["candidate"]["storyboard"][0]["dialogue"]
    video.assert_current(graph)
    assert st.all_rows("provider_jobs", "work_id=?", [wid]) == []


@pytest.mark.parametrize("mutation", ["missing_ack", "facts_ack", "non_integer", "boolean_duration", "numbering",
                                      "empty_visual", "extra_field", "too_many"])
def test_api_rejects_invalid_manual_import(mutation):
    wid = work()
    req = request(wid)
    if mutation == "missing_ack":
        req["manual_ack"] = False
    elif mutation == "facts_ack":
        req["facts_ack"] = False
    elif mutation == "non_integer":
        req["storyboard"][0]["duration_seconds"] = 8.5
    elif mutation == "boolean_duration":
        req["storyboard"][0]["duration_seconds"] = True
    elif mutation == "numbering":
        req["storyboard"][0]["shot_index"] = 2
    elif mutation == "empty_visual":
        req["storyboard"][0]["visual"] = "  "
    elif mutation == "extra_field":
        req["origin"] = {"kind": "manual_import"}
    else:
        req["storyboard"] = [dict(req["storyboard"][0], shot_index=i) for i in range(1, 32)]
    response = client().post(f"/api/v1/works/{wid}/content/manual-storyboards", json=req)
    assert response.status_code == 422, response.text
    assert repo.list_batches(wid) == []


def test_target_duration_route_and_revision_checks():
    wid = work(target=60000)
    with pytest.raises(ContentRejected, match="总时长"):
        manual.create(wid, request(wid))
    wid = work()
    with pytest.raises(repo.StaleContentWrite):
        manual.create(wid, request(wid, expected_content_revision=500))
    row = st.get("works", wid)
    row["brief"]["content_route"] = "talking_script"
    st.put("works", wid, row)
    with pytest.raises(ContentRejected) as failure:
        manual.create(wid, request(wid))
    assert failure.value.code == "HUMAN_REVIEW_ROUTE_REQUIRED"


def test_active_flow_blocks_but_unknown_is_preserved_and_can_be_taken_over():
    wid = work()
    queued = tasks.submit_job(wid, "generation", uuid4().hex, {})
    response = client().post(f"/api/v1/works/{wid}/content/manual-storyboards", json=request(wid))
    assert response.status_code == 409
    tasks.finish_job(queued["id"], "unknown", error={"code": "CONTENT_CALL_PENDING", "message": "未确认"})
    unknown_before = tasks.get_job(queued["id"])
    call = {"id": "job_" + uuid4().hex, "work_id": wid, "parent_content_job_id": queued["id"],
            "namespace": "content-v2", "capability": "chat", "state": "unknown", "reserved_fen": 35,
            "provider": "openai", "model": "gpt-6.1-sol"}
    st.put("provider_jobs", call["id"], call, {"work_id": wid})
    before_call = st.get("provider_jobs", call["id"])
    created = manual.create(wid, request(wid))
    human_review.submit(wid, review_payload(wid))
    assert selection.current_selection_valid(wid)
    assert tasks.get_job(queued["id"]) == unknown_before and st.get("provider_jobs", call["id"]) == before_call
    assert st.get("works", wid)["current_content_job_id"] == queued["id"]
    with pytest.raises(tasks.CallPending):
        human_review.submit(wid, review_payload(wid, action="rewrite", feedback="重新编写"))
    assert st.get("script_batches", wid)["id"] == created["batch_id"]
    assert len(tasks.list_jobs(wid)) == 1


def test_manual_edit_requires_new_fact_confirmation_and_preserves_original():
    wid = work()
    created = manual.create(wid, request(wid))
    original = repo.get_script_version(created["script_id"])
    before_batches = repo.list_batches(wid)
    shots = deepcopy(original["storyboard"])
    shots[0]["dialogue"] = "产品价格已更新为699元，请考虑清楚。"
    req = review_payload(wid, action="edit_adopt", score=3, storyboard=shots)
    with pytest.raises(ContentRejected) as failure:
        human_review.submit(wid, req)
    assert failure.value.code == "MANUAL_EDIT_FACTS_ACK_REQUIRED"
    assert repo.list_batches(wid) == before_batches
    result = human_review.submit(wid, {**req, "facts_ack": True})
    new = repo.get_script_version(result["script_id"])
    assert new["parent_id"] == original["id"] and new["origin"]["kind"] == "manual_edit"
    assert new["storyboard"][0]["dialogue"] == shots[0]["dialogue"]
    assert result["review"]["mode"] == "human_edit" and not result["review"]["passed"]
    assert result["review"]["facts_ack"]
    assert new["claims"] == [] and selection.current_selection_valid(wid)
    assert repo.get_script_version(original["id"]) == original
    assert st.all_rows("provider_jobs", "work_id=?", [wid]) == []


@pytest.mark.parametrize("mutation", ["revision", "materials", "origin", "title", "binding"])
def test_manual_evidence_becomes_invalid_when_input_changes_or_projection_is_forged(mutation):
    wid = work()
    created = manual.create(wid, request(wid))
    human_review.submit(wid, review_payload(wid))
    assert selection.current_selection_valid(wid)
    if mutation in ("revision", "materials"):
        row = st.get("works", wid)
        if mutation == "revision":
            row["content_revision"] += 1
        else:
            row["brief"]["materials"] = [{"text": "资料已变化。"}]
        st.put("works", wid, row)
    else:
        batch = st.get("script_batches", wid)
        if mutation == "origin":
            batch["candidates"][0]["origin"]["manual_import_id"] = "fake"
        elif mutation == "title":
            batch["candidates"][0]["title"] = "偷偷改题目"
        else:
            batch["set_digest"] = "fake"
        st.put("script_batches", wid, batch)
    assert not selection.current_selection_valid(wid)
    view = human_review.get_review(wid, created["script_id"])
    assert not view["eligible"] and view["review"] is None
    assert not selection.project_batch(wid)["candidates"][0]["pickable"]


def test_replacing_manual_batch_revokes_selection_without_deleting_history():
    wid = work()
    first = manual.create(wid, request(wid))
    human_review.submit(wid, review_payload(wid))
    assert selection.current_selection_valid(wid)
    second = manual.create(wid, request(wid, title="另一个人工稿"))
    assert first["script_id"] != second["script_id"]
    assert not selection.current_selection_valid(wid)
    assert not st.get("works", wid).get("script_version_id")
    assert repo.get_script_version(first["script_id"])
    assert repo.get_batch(first["batch_id"])
    assert all(row.get("state") != "selected" for row in st.all_rows("selections", "work_id=?", [wid]))


def test_manual_workflow_does_not_require_a_working_model_configuration(monkeypatch):
    monkeypatch.setattr(tp, "profile", lambda *args: pytest.fail("manual flow must not load model profiles"))
    wid = work()
    created = manual.create(wid, request(wid))
    assert selection.project_batch(wid)["candidates"][0]["pickable"]
    assert human_review.get_review(wid, created["script_id"])["eligible"]
    human_review.submit(wid, review_payload(wid))
    assert selection.current_selection_valid(wid)


def test_manual_takeover_permanently_stops_old_framework_context_and_reuses_new_task_for_video(monkeypatch, tmp_path):
    import asyncio
    from app.avatar import person_image
    from app.agents import framework, supervisor, tools as agent_tools
    from app.contracts import Task
    from app.short_video import service as video

    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(person_image.KEY_ENV, "test-image-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    wid = work()
    old = Task(id="task_" + uuid4().hex, work_id=wid, mode="integration", route="WAN_3_0",
               stage="script", state="running").model_dump()
    st.put("tasks", old["id"], old, {"work_id": wid})
    row = st.get("works", wid)
    row.update(current_task_id=old["id"], route="WAN_3_0")
    st.put("works", wid, row)
    context = {"work_id": wid, "task_id": old["id"], "work_item_id": "delegated-old-content", "role": "content"}
    assert framework.execution_stop_reason(context) is None
    created = manual.create(wid, request(wid))
    new_id = st.get("works", wid)["current_task_id"]
    assert new_id != old["id"]
    assert st.get("tasks", old["id"])["state"] == "cancelled"
    assert st.get("tasks", new_id)["stage"] == "content_review"
    assert st.get("tasks", new_id)["state"] == "waiting_selection"
    assert st.get("tasks", new_id)["pending_selection_ids"] == ["sel_script"]
    assert framework.execution_stop_reason(context)
    with pytest.raises(agent_tools.ToolDenied):
        framework.require_active_execution(context)
    human_review.submit(wid, review_payload(wid))
    assert st.get("tasks", new_id)["state"] == "waiting_review"
    assert framework.execution_stop_reason(context)
    # Even a stale engine writer cannot make the old task current again.
    st.put("tasks", old["id"], old, {"work_id": wid})
    assert framework.execution_stop_reason(context)["code"] == "TASK_SUPERSEDED"
    with pytest.raises(agent_tools.ToolDenied):
        agent_tools.t_register_script_candidates({"work_id": wid, "topic": "不得覆盖人工稿"}, context)
    asyncio.run(supervisor.advance(old["id"]))
    assert st.get("script_batches", wid)["id"] == created["batch_id"]
    assert tasks.list_jobs(wid) == []
    graph = video.create_graph(wid, image_ceiling_fen_per_shot=100)
    assert graph["task_id"] == new_id
    assert graph["script_version_id"] == created["script_id"]
    video.assert_current(graph)
    assert st.all_rows("provider_jobs", "work_id=?", [wid]) == []


def test_content_state_remains_available_when_writer_configuration_is_broken(monkeypatch):
    from app.content.errors import ContentBlocked

    def broken(*args, **kwargs):
        raise ContentBlocked("写稿模型配置不可用", "PROVIDER_UNSUPPORTED")

    monkeypatch.setattr(tp, "runtime_profile", broken)
    wid = work()
    response = client().get(f"/api/v1/works/{wid}/content/state")
    assert response.status_code == 200
    assert response.json()["writer_runtime"] is None
    assert response.json()["writer_error"]["code"] == "PROVIDER_UNSUPPORTED"
    created = manual.create(wid, request(wid))
    response = client().get(f"/api/v1/works/{wid}/content/state")
    assert response.status_code == 200
    assert response.json()["batch"]["candidates"][0]["id"] == created["script_id"]
    assert response.json()["writer_runtime"] is None


def test_existing_model_drafts_remain_visible_when_reviewer_configuration_is_broken(monkeypatch):
    from app.content.errors import ContentBlocked
    wid = work()
    manual.create(wid, request(wid))
    batch = st.get("script_batches", wid)
    batch["source_kind"] = "llm"
    st.put("script_batches", wid, batch)
    def broken(*args, **kwargs):
        raise ContentBlocked("审核模型配置不可用", "PROVIDER_UNSUPPORTED")
    monkeypatch.setattr(tp, "profile", broken)
    response = client().get(f"/api/v1/works/{wid}/content/state")
    assert response.status_code == 200
    assert response.json()["batch"]["id"] == batch["id"]
    assert response.json()["content_revision"] == st.get("works", wid)["content_revision"]
    assert all(candidate["pickable"] is False for candidate in response.json()["batch"]["candidates"])
    created = manual.create(wid, request(wid))
    assert human_review.get_review(wid, created["script_id"])["eligible"]
