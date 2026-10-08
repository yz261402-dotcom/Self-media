"""Explicit repairs receive the same complete problems as automatic repair rounds."""
import pytest

from app import storage as st
from app.content import repository as repo, service
from tests.content._helpers import Script, cand, make_work, repair_reply, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")


def prepared_work(script, route="talking_script"):
    wid = make_work(with_reference=False)
    work = st.get("works", wid)
    work["brief"]["content_route"] = route
    st.put("works", wid, work)
    service.ensure_prompt(wid)
    return wid


@pytest.mark.parametrize("single_fails", [True, False])
def test_explicit_repair_receives_set_issue_suggestions_quote_and_other_candidates(single_fails):
    script = Script()
    wid = prepared_work(script)
    original = {"candidates": [cand(1), cand(2), cand(3)]}
    script.q["writer"].append(original)

    def review(user):
        result = review_reply(fail=(2,) if single_fails else (), set_distinct=False, rewrite=(2,))(user)
        if single_fails:
            finding = next(c for c in result["candidates"] if c["findings"])["findings"][0]
            finding.update(quote="真正拉开差距的是晚上几点关灯", issue="睡眠视角没有落到具体选择",
                           suggestion="用人物决定关闭提醒的动作体现选择")
        result["set"]["findings"] = [{"issue": "与第一篇都在讲减少工具选择",
                                        "suggestion": "保留睡眠视角，以稳定作息的选择作为结尾"}]
        return result

    script.q["reviewer"].append(review)
    generated = run_job("generation", wid, {}, service.handle_generation, script)
    assert generated["state"] == "partial", generated.get("error")
    batch = st.get("script_batches", wid)
    old = {v["slot"]: v for v in batch["candidates"]}
    script.q["writer"].append(repair_reply(2))
    script.q["reviewer"].append(review_reply())
    repaired = run_job("repair", wid, {"batch_id": batch["id"], "candidate_ids": [old[2]["id"]]},
                       service.handle_repair, script)
    assert repaired["state"] == "succeeded", repaired.get("error")
    user = script.seen["writer"][-1]
    assert "与第一篇都在讲减少工具选择" in user
    assert "保留睡眠视角，以稳定作息的选择作为结尾" in user
    if single_fails:
        assert "用人物决定关闭提醒的动作体现选择" in user
        assert "真正拉开差距的是晚上几点关灯" in user.split("问题：", 1)[1].split("当前正文", 1)[0]
    assert "<other_candidates_data>" in user
    for slot in (1, 3):
        assert original["candidates"][slot - 1]["text"] in user
    latest = {v["slot"]: v for v in st.get("script_batches", wid)["candidates"]}
    assert latest[2]["parent_id"] == old[2]["id"]
    assert latest[1]["id"] == old[1]["id"] and latest[3]["id"] == old[3]["id"]
    assert script.calls["writer"] == 2 and script.calls["reviewer"] == 2


def test_explicit_storyboard_repair_does_not_forward_obsolete_rule_false_positives(monkeypatch):
    script = Script()
    wid = prepared_work(script, route="short_video_storyboard")
    def storyboard(slot, dialogue):
        return dict(cand(slot), storyboard=[{"shot_index": 1, "visual": "镜头停留4秒。",
            "dialogue": dialogue, "sound_effects": "静音3秒。", "duration_seconds": 8, "prompt": "固定机位，原声。"}])
    script.q["writer"].append({"candidates": [storyboard(i, cand(i)["text"]) for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply(fail=(2,)))
    with monkeypatch.context() as legacy:
        legacy.setattr(service, "storyboard_review_texts", lambda shots: {})
        assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "partial"
    batch = st.get("script_batches", wid)
    target = next(v for v in batch["candidates"] if v["slot"] == 2)
    original_version = repo.get_script_version(target["id"])
    assert any(c["code"] == "CLAIM_COVERAGE" and c["outcome"] == "review" for c in target["quality_report"]["checks"])
    script.q["writer"].append({"candidates": [storyboard(2, "请先把提醒关掉，睡够了再做决定。") ]})
    script.q["reviewer"].append(review_reply())
    repaired = run_job("repair", wid, {"batch_id": batch["id"], "candidate_ids": [target["id"]]},
                       service.handle_repair, script)
    assert repaired["state"] == "succeeded", repaired.get("error")
    assert "[CLAIM_COVERAGE]" not in script.seen["writer"][-1]
    assert "缺少新观点" in script.seen["writer"][-1]
    assert repo.get_script_version(target["id"]) == original_version
