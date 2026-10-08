"""候选分别把关：失败的其他稿不能挡住已审核通过且未被集合否决的稿。"""
import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.content import selection, semantic_review as sr, service
from app.main import app
from tests.content._helpers import Script, cand, human_approve, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")


def reviewed_batch(*, fail=(), set_distinct=True, rewrite=(), hard_failure=False, storyboard=False):
    wid = make_work()
    if storyboard:
        work = st.get("works", wid)
        work["brief"]["content_route"] = "short_video_storyboard"
        st.put("works", wid, work)
    script = Script()
    script.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, script)
    candidates = [cand(i) for i in (1, 2, 3)]
    if hard_failure:
        candidates[2]["text"] = "这款产品价格为399元，先确认预算再考虑购买。"
    if storyboard:
        for candidate in candidates:
            candidate["storyboard"] = [{"shot_index": 1, "visual": "人物看着手边的笔记本。",
                                       "dialogue": candidate["text"], "sound_effects": "安静室内环境声。",
                                       "duration_seconds": 8, "prompt": "人物看着笔记本，固定机位。"}]
    script.q["writer"].append({"candidates": candidates})
    script.q["reviewer"].append(review_reply(fail=fail, set_distinct=set_distinct, rewrite=rewrite))
    run_job("generation", wid, {}, service.handle_generation, script)
    return wid, script, selection.project_batch(wid)


def test_passed_candidate_can_be_adopted_when_other_singles_fail():
    wid, script, batch = reviewed_batch(fail=(2, 3))
    assert batch["passed"] == 1 and not batch["ready_to_present"]
    assert [c["pickable"] for c in batch["candidates"]] == [True, False, False]
    assert batch["candidates"][0]["unpickable_reasons"] == []
    before = dict(script.calls)
    client = TestClient(app)
    result = client.post(f"/api/v1/works/{wid}/script-selection",
                         json={"script_id": batch["candidates"][0]["id"]})
    assert result.status_code == 200, result.text
    assert selection.current_selection_valid(wid)
    assert script.calls == before
    for candidate in batch["candidates"][1:]:
        rejected = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": candidate["id"]})
        assert rejected.status_code == 409
    assert st.get("works", wid)["script_version_id"] == batch["candidates"][0]["id"]


def test_another_candidates_hard_failure_does_not_block_passed_candidate():
    wid, _, batch = reviewed_batch(hard_failure=True)
    assert [c["pickable"] for c in batch["candidates"]] == [True, True, False]
    assert any("CLAIM_COVERAGE" in r for r in batch["candidates"][2]["unpickable_reasons"])
    selection.adopt(wid, batch["candidates"][0]["id"])
    assert selection.current_selection_valid(wid)


def test_set_review_only_blocks_slots_it_explicitly_rejects():
    wid, _, batch = reviewed_batch(set_distinct=False, rewrite=(2, 3))
    assert not batch["ready_to_present"]
    assert [c["pickable"] for c in batch["candidates"]] == [True, False, False]
    selection.adopt(wid, batch["candidates"][0]["id"])
    assert selection.current_selection_valid(wid)


def test_set_failure_without_identified_slots_still_blocks_all_candidates():
    wid, _, batch = reviewed_batch(set_distinct=False)
    assert not any(c["pickable"] for c in batch["candidates"])
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, batch["candidates"][0]["id"])


def test_missing_current_set_review_still_blocks_passed_candidates():
    wid, _, _ = reviewed_batch()
    for decision in sr.decisions_for(wid):
        if decision["review_kind"] == "semantic_set":
            st.put("quality_decisions", decision["id"], dict(decision, binding={}), {"work_id": wid})
    batch = selection.project_batch(wid)
    assert not any(c["pickable"] for c in batch["candidates"])
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, batch["candidates"][0]["id"])


@pytest.mark.parametrize("set_distinct,rewrite", [(True, ()), (False, (2, 3))])
def test_adopted_passed_storyboard_can_prepare_media_without_other_candidates_passing(
        monkeypatch, tmp_path, set_distinct, rewrite):
    from app.avatar import person_image
    from app.short_video import service as video_service

    monkeypatch.setenv("AVATAR_STUDIO_MODE", "integration")
    monkeypatch.setenv("AVATAR_STUDIO_ALLOW_INTEGRATION", "1")
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(person_image.KEY_ENV, "test-image-key")
    monkeypatch.setenv(person_image.BASE_ENV, "https://image.example")
    wid, script, batch = reviewed_batch(fail=(2, 3), set_distinct=set_distinct,
                                      rewrite=rewrite, storyboard=True)
    assert not batch["ready_to_present"] and batch["candidates"][0]["pickable"]
    selected_id = batch["candidates"][0]["id"]
    human_approve(wid, selected_id)
    selection.adopt(wid, selected_id)
    calls_before = dict(script.calls)
    graph = video_service.create_graph(wid, image_ceiling_fen_per_shot=100)
    assert graph["script_version_id"] == selected_id
    assert graph["state"] == "waiting_image_approval"
    assert not graph["image_approval"] and not graph["video_approval"]
    video_service.assert_current(graph)
    with pytest.raises(video_service.StoryboardError) as failure:
        video_service.generate_images(graph["id"])
    assert failure.value.code == "STORYBOARD_IMAGE_APPROVAL_REQUIRED"
    assert script.calls == calls_before
