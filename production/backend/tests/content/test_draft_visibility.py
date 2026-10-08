"""已生成正文先展示，审核故障只阻止采用；已显示初稿的后续结果不可覆盖用户修改。"""
import pytest

from app import storage as st
from app.content import selection, semantic_review as sr, service
from tests.content._helpers import (Script, cand, make_work, plan_json, repair_reply,
                                    review_reply, run_job, set_env)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")


def ready_work():
    wid = make_work()
    script = Script()
    script.q["planner"].append(plan_json())
    assert run_job("prompt", wid, {}, service.handle_prepare_prompt, script)["state"] == "succeeded"
    script.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    return wid, script


def test_three_initial_drafts_are_visible_before_reviewer_returns():
    wid, script = ready_work()
    snapshots = []

    def review(user):
        snapshots.append(selection.project_batch(wid))
        return review_reply()(user)

    script.q["reviewer"].append(review)
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "succeeded", job.get("error")
    during_review = snapshots[0]
    assert during_review is not None
    assert [c["slot"] for c in during_review["candidates"]] == [1, 2, 3]
    assert all(c["text"] and c["review_state"] == "needs_review" and not c["pickable"]
               for c in during_review["candidates"])
    assert during_review["ready_to_present"] is False
    assert during_review["retry_targets"] == []
    assert [t["slot"] for t in during_review["review_targets"]] == [1, 2, 3]


def test_reviewer_timeout_keeps_editable_drafts_without_rewrite_targets():
    wid, script = ready_work()
    script.q["reviewer"].append(TimeoutError("审核连接中断"))
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "unknown"
    view = selection.project_batch(wid)
    assert len(view["candidates"]) == 3
    assert view["retry_targets"] == [] and len(view["review_targets"]) == 3
    original = view["candidates"][0]
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, original["id"])
    before = dict(script.calls)
    edited = service.revise_candidate(wid, original["id"], original["text"] + "这是用户补充的一句体会。")
    assert script.calls == before
    revised = selection.project_batch(wid)
    assert any(c["id"] == edited["id"] and c["text"] == edited["text"] for c in revised["candidates"])
    assert not revised["ready_to_present"]
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, edited["id"])


def test_hard_gate_failure_is_rewrite_target_even_before_semantic_review():
    wid, script = ready_work()
    script.q["writer"][0]["candidates"][0]["text"] = (
        "参考视频里说了一段独一无二的话用来测试复用检测不要照搬这一整段，这是照搬的问题初稿。")
    script.q["reviewer"].append(TimeoutError("审核连接中断"))
    run_job("generation", wid, {}, service.handle_generation, script)
    view = selection.project_batch(wid)
    assert len(view["candidates"]) == 3
    assert [t["slot"] for t in view["retry_targets"]] == [1]
    assert [t["slot"] for t in view["review_targets"]] == [2, 3]


@pytest.mark.parametrize("rewrite,expected", [((2,), [2]), ((), [1, 2, 3])])
def test_set_review_failure_only_targets_rejected_slots(rewrite, expected):
    wid, script = ready_work()
    script.q["reviewer"].append(review_reply(set_distinct=False, rewrite=rewrite))
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "partial"
    view = selection.project_batch(wid)
    assert len(view["candidates"]) == 3
    assert [t["slot"] for t in view["retry_targets"]] == expected
    assert view["review_targets"] == []
    assert [c["slot"] for c in view["candidates"] if not c["pickable"]] == expected


def test_missing_set_review_requests_review_without_rewriting_passed_singles():
    wid, script = ready_work()
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    for decision in sr.decisions_for(wid):
        if decision["review_kind"] == "semantic_set":
            st.put("quality_decisions", decision["id"], dict(decision, binding={}), {"work_id": wid})
    view = selection.project_batch(wid)
    assert view["passed"] == 3 and not view["ready_to_present"]
    assert len(view["candidates"]) == 3
    assert view["retry_targets"] == []
    assert [t["slot"] for t in view["review_targets"]] == [1, 2, 3]


def test_automatic_repair_new_versions_are_visible_before_reaudit(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "1")
    wid, script = ready_work()
    script.q["writer"].append(repair_reply(2))
    snapshots = []

    def review(user, fail=()):
        snapshots.append(selection.project_batch(wid))
        return review_reply(fail=fail)(user)

    script.q["reviewer"] += [lambda user: review(user, (2,)), review]
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "succeeded", job.get("error")
    before = {c["slot"]: c for c in snapshots[0]["candidates"]}
    after = {c["slot"]: c for c in snapshots[1]["candidates"]}
    assert after[2]["parent_id"] == before[2]["id"] and after[2]["id"] != before[2]["id"]
    assert after[1]["id"] == before[1]["id"] and after[3]["id"] == before[3]["id"]
    assert snapshots[1]["retry_targets"] == []
    assert len(snapshots[1]["review_targets"]) == 3


@pytest.mark.parametrize("fail", [(), (2,)])
def test_late_review_cannot_replace_user_edit_or_start_repair(fail, monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "1")
    wid, script = ready_work()
    edited = []

    def edit_then_review(user):
        current = st.get("script_batches", wid)
        if current:
            original = current["candidates"][0]
            edited.append(service.revise_candidate(wid, original["id"], original["text"] + "用户刚刚修改了这篇稿。"))
        return review_reply(fail=fail)(user)

    script.q["reviewer"].append(edit_then_review)
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert edited, "等待审核时应能编辑已展示的初稿"
    assert job["state"] == "superseded", job.get("error")
    assert script.calls["writer"] == 1
    view = selection.project_batch(wid)
    assert view["candidates"][0]["id"] == edited[0]["id"]
    assert view["candidates"][0]["text"] == edited[0]["text"]
    assert not view["ready_to_present"]


def test_final_metadata_refresh_preserves_adoption_of_same_reviewed_members(monkeypatch):
    wid, script = ready_work()
    script.q["reviewer"].append(review_reply())
    real_review = sr.review_batch
    selected = []

    def review_and_adopt(**kwargs):
        parsed = real_review(**kwargs)
        batch = st.get("script_batches", wid)
        if batch:
            selected.append(batch["candidates"][0]["id"])
            selection.adopt(wid, selected[0])
        return parsed

    monkeypatch.setattr(sr, "review_batch", review_and_adopt)
    job = run_job("generation", wid, {}, service.handle_generation, script)
    assert job["state"] == "succeeded", job.get("error")
    assert selected
    assert st.get("works", wid)["script_version_id"] == selected[0]
    assert selection.current_selection_valid(wid)


def test_manual_repair_note_explains_automatic_repair_is_disabled(monkeypatch):
    history = [{"round": 1, "slot": 2, "outcome": "repaired"}]
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")
    assert service._repair_note(history) == "定向返工 1 轮，共处理 1 稿次；自动返工已关闭"
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "2")
    assert service._repair_note(history) == "自动返工 1 轮（上限 2 轮），共重写 1 稿次"
    assert service._repair_note([]) is None
