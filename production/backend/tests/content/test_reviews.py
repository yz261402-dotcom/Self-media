"""独立审核：严格解析、绑定摘要、旧结论不冒充、任何输入变化使审核失效。"""
import pytest

from app import storage as st
from app.content import context as cx
from app.content import contracts as cc
from app.content import repository as repo
from app.content import selection, semantic_review as sr, service
from app.content.errors import ContentRejected
from tests.content._helpers import (MATERIAL, Script, cand, make_work, plan_json, review_json, review_reply,
                                    run_job, set_env)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


def generated(materials=None):
    wid = make_work(materials=materials)
    s = Script()
    s.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply())
    run_job("generation", wid, {}, service.handle_generation, s)
    return wid, s


def versions_of(wid):
    return {c["slot"]: c for c in st.get("script_batches", wid)["candidates"]}


def test_legacy_rule_decision_is_never_independent():
    legacy = {"id": "q", "independent": False, "review_kind": "rules", "reviewer": {}, "binding": {}}
    assert not cc.is_independent_review(legacy)
    assert not cc.is_independent_review({"independent": True, "review_kind": "semantic_single", "reviewer": {},
                                          "binding": {"a": 1}})
    assert not cc.is_independent_review({"independent": True, "review_kind": "semantic_single",
                                          "reviewer": {"provider": "qwen", "model": "m"}, "binding": {}})


def test_all_current_reviews_make_all_three_pickable():
    wid, _ = generated()
    batch = selection.project_batch(wid)
    assert [c["pickable"] for c in batch["candidates"]] == [True, True, True]
    assert batch["passed"] == 3 and all(c["review_state"] == "passed" for c in batch["candidates"])


def _first(d, **over):
    return dict(d, candidates=[dict(d["candidates"][0], **over)] + d["candidates"][1:])


MUTATIONS = {
    "empty": lambda d: {"candidates": [], "set": d["set"]},
    "missing": lambda d: dict(d, candidates=d["candidates"][:2]),
    "duplicate": lambda d: dict(d, candidates=d["candidates"] + [dict(d["candidates"][0])]),
    "unknown": lambda d: _first(d, candidate_id="script_unknown"),
    "criteria": lambda d: _first(d, criteria={"originality": "pass"}),
    "value": lambda d: _first(d, criteria={k: "maybe" for k in d["candidates"][0]["criteria"]}),
    "no-set": lambda d: {"candidates": d["candidates"]},
    "bad-slot": lambda d: dict(d, set=dict(d["set"], rewrite_slots=[9])),
}


@pytest.mark.parametrize("label", sorted(MUTATIONS))
def test_review_parser_rejects_malformed(label):
    wid, _ = generated()
    vs = list(versions_of(wid).values())
    ids = [v["id"] for v in vs]
    with pytest.raises(ContentRejected):
        sr.parse_review(MUTATIONS[label](review_json(ids)), review_ids=ids, versions=vs)


def test_failed_criteria_without_finding_still_recorded_as_failed():
    wid, _ = generated()
    vs = list(versions_of(wid).values())
    ids = [v["id"] for v in vs]
    d = review_json(ids)
    d["candidates"][0]["criteria"]["originality"] = "fail"
    d["candidates"][0]["findings"] = []
    r = sr.parse_review(d, review_ids=ids, versions=vs)["singles"][ids[0]]
    assert not r["passed"] and r["findings"][0]["issue"]


def _pickables(wid):
    return [c["pickable"] for c in selection.project_batch(wid)["candidates"]]


def test_text_edit_new_version_invalidates_that_single_and_the_set_but_not_other_singles():
    wid, _ = generated()
    v = versions_of(wid)
    edited = dict(v[3], id="script_edited", text=v[3]["text"] + "再加一句。", parent_id=v[3]["id"], work_id=wid)
    with st.transaction() as c:
        repo.save_script_version(c, edited)
    batch = st.get("script_batches", wid)
    replacement = {k: val for k, val in edited.items() if k != "work_id"}
    batch["candidates"] = [c if c["slot"] != 3 else replacement for c in batch["candidates"]]
    st.put("script_batches", wid, batch)
    projection = selection.project_batch(wid)
    assert projection["candidates"] == [] and projection["ready_to_present"] is False
    assert any(t["slot"] == 3 and t["review_state"] == "needs_review" for t in projection["retry_targets"])
    ctx = cx.load_context(wid)
    assert sr.current_single(wid, v[1], ctx) is not None
    assert sr.current_single(wid, v[2], ctx) is not None
    assert sr.current_single(wid, replacement, ctx) is None


def _apply_change(wid, change):
    work = st.get("works", wid)
    with st.transaction() as c:
        if change == "materials":
            brief = dict(work["brief"], materials=[{"text": MATERIAL, "source": "官方", "content_hash": "", "added_at": ""}])
            repo.bump_content_revision(c, wid, brief=brief)
        elif change == "style":
            repo.bump_content_revision(c, wid, style_snapshot={"persona": "新人设", "digest": "d1"})
        elif change == "topic":
            repo.bump_content_revision(c, wid, brief=dict(work["brief"], topic="换了主题"))
        elif change == "prompt":
            repo.bump_content_revision(c, wid, writing_prompt_id=None)
        else:
            repo.bump_content_revision(c, wid, reference_snapshot_id=None)


@pytest.mark.parametrize("change", ["materials", "style", "topic", "prompt", "reference"])
def test_shared_input_change_makes_reviews_stale(change):
    wid, _ = generated()
    assert all(_pickables(wid))
    _apply_change(wid, change)
    assert not any(_pickables(wid))
    assert all(c["review_state"] in ("stale", "needs_review") for c in selection.project_batch(wid)["candidates"])


def test_changing_reviewer_model_makes_old_reviews_stale(monkeypatch):
    wid, _ = generated()
    assert all(_pickables(wid))
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_REVIEWER_MODEL", "qwen3.7-max")
    assert not any(_pickables(wid))


def test_review_of_one_candidate_cannot_be_reused_for_another_id():
    wid, _ = generated()
    v = versions_of(wid)
    other = dict(v[1], id="script_clone")
    assert sr.current_single(wid, other, cx.load_context(wid)) is None
