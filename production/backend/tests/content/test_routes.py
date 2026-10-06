"""Content routes and the all-three review gate."""
import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import selection, service
from app.content.errors import ContentRejected
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


def _ready(route="talking_script"):
    wid = make_work()
    work = st.get("works", wid)
    work["brief"]["content_route"] = route
    st.put("works", wid, work)
    script = Script()
    script.q["planner"].append(plan_json())
    assert run_job("prompt", wid, {}, service.handle_prepare_prompt, script)["state"] == "succeeded"
    return wid, script


def _shot(index, words):
    return {"shot_index": index, "visual": f"画面{index}：{words}", "dialogue": words,
            "sound_effects": "环境声", "duration_seconds": 8, "prompt": f"镜头{index}的画面"}


def test_storyboard_generation_persists_structured_shots_and_route():
    wid, script = _ready("short_video_storyboard")
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[_shot(1, cand(i)["text"])]) for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "succeeded", job.get("error")
    projected = selection.project_batch(wid)
    assert projected["content_route"] == "short_video_storyboard"
    assert len(projected["candidates"]) == 3
    assert all(c["content_route"] == "short_video_storyboard" and c["storyboard"][0]["duration_seconds"] == 8
               for c in projected["candidates"])


def test_storyboard_rejects_out_of_range_shot_duration():
    wid, script = _ready("short_video_storyboard")
    from app.content import context as cx
    ctx = cx.load_context(wid, require_prompt=True)
    job = {"id": "test", "work_id": wid}
    raw = dict(cand(1), storyboard=[dict(_shot(1, "旁白"), duration_seconds=31)])
    with pytest.raises(ContentRejected, match="2.*30"):
        service._build_version(raw, ctx=ctx, job=job, slot=1, parent_id=None, round_no=0, writer={})


def test_storyboard_visual_change_invalidates_single_and_set_review():
    wid, script = _ready("short_video_storyboard")
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[_shot(1, cand(i)["text"])]) for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    batch = st.get("script_batches", wid)
    changed = batch["candidates"][0]
    changed["storyboard"][0]["visual"] = "一处尚未审核的新画面"
    st.put("script_batches", wid, batch)
    projected = selection.project_batch(wid)
    assert projected["candidates"] == [] and projected["ready_to_present"] is False
    assert projected["retry_targets"][0]["candidate_id"] == changed["id"]


def test_storyboard_adoption_pauses_before_media_confirmation():
    wid, script = _ready("short_video_storyboard")
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[_shot(1, cand(i)["text"])]) for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    work = st.get("works", wid)
    work["current_task_id"] = "task_storyboard"
    st.put("works", wid, work)
    st.put("tasks", "task_storyboard", {"id": "task_storyboard", "work_id": wid, "stage": "content_review",
                                           "state": "waiting_selection", "pending_selection_ids": ["sel_script"]},
           {"work_id": wid})
    candidate_id = selection.project_batch(wid)["candidates"][0]["id"]
    result = selection.adopt(wid, candidate_id)
    assert result["advance"] is False
    task = st.get("tasks", "task_storyboard")
    assert task["stage"] == "content_review" and task["state"] == "waiting_review"


def test_partial_batch_redacts_all_candidate_bodies_and_blocks_adoption(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")
    wid, script = _ready()
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    script.q["reviewer"].extend([review_reply(fail=(3,))] * 3)
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "partial"
    raw = st.get("script_batches", wid)
    target_id = raw["candidates"][2]["id"]
    projected = selection.project_batch(wid)
    assert projected["passed"] == 2
    assert projected["candidates"] == []
    assert target_id in [row["candidate_id"] for row in projected["retry_targets"]]
    assert all("text" not in row and "storyboard" not in row for row in projected["retry_targets"])
    response = TestClient(app).get(f"/api/v1/works/{wid}/scripts")
    assert response.status_code == 200
    assert all(raw["text"] not in response.text for raw in raw["candidates"])
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, raw["candidates"][0]["id"])


def test_set_rejection_without_rewrite_slots_never_reports_success(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")
    wid, script = _ready()
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    script.q["reviewer"].append(review_reply(set_distinct=False))
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "partial"
    assert selection.project_batch(wid)["ready_to_present"] is False


def test_rereview_set_failure_remains_partial():
    wid, script = _ready()
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    script.q["reviewer"].append(review_reply(set_distinct=False))
    batch = st.get("script_batches", wid)
    job = run_job("review", wid, {"batch_id": batch["id"]}, service.handle_review, script)
    assert job["state"] == "partial"
    assert selection.project_batch(wid)["ready_to_present"] is False


def test_storyboard_duration_must_be_whole_seconds():
    wid, _ = _ready("short_video_storyboard")
    from app.content import context as cx
    raw = dict(cand(1), storyboard=[dict(_shot(1, "对白"), duration_seconds=2.5)])
    with pytest.raises(ContentRejected, match="整数"):
        service._build_version(raw, ctx=cx.load_context(wid), job={"id": "g", "work_id": wid},
                               slot=1, parent_id=None, round_no=0, writer={})
