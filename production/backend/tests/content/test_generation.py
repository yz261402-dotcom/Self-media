"""三稿生成与有限返工：调用计数、slot 差异、单飞返工、2/3 partial、成功稿不被动、审核故障不放行。"""
import pytest

from app import storage as st
from app.content import repository as repo, semantic_review as sr, service, tasks, selection
from tests.content._helpers import (MATERIAL, Script, cand, make_work, plan_json, repair_reply, review_json,
                                    review_reply, run_job, set_env)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


def ready_work(materials=None):
    wid = make_work(materials=materials)
    s = Script()
    s.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    return wid, s


def generate(wid, s):
    return run_job("generation", wid, {}, service.handle_generation, s)


def three():
    return {"candidates": [cand(1), cand(2), cand(3)]}


def test_happy_path_three_slots_one_review_no_repair():
    wid, s = ready_work()
    s.q["writer"].append(three())
    s.q["reviewer"].append(review_reply())
    job = generate(wid, s)
    assert job["state"] == "succeeded", job.get("error")
    assert (s.calls["planner"], s.calls["writer"], s.calls["reviewer"]) == (1, 1, 1)
    batch = st.get("script_batches", wid)
    assert [c["slot"] for c in batch["candidates"]] == [1, 2, 3]
    assert len({c["angle"] for c in batch["candidates"]}) == 3 and batch["requested"] == 3
    assert job["result_refs"]["passed"] == 3
    assert repo.get_batch(batch["id"])["set_digest"] == batch["set_digest"]


def test_same_angle_or_same_text_do_not_count_as_three():
    wid, s = ready_work()
    dup_angle = three()
    dup_angle["candidates"][1]["angle"] = dup_angle["candidates"][0]["angle"]
    dup_text = dup_angle["candidates"]
    dup_text[2]["text"] = dup_text[0]["text"]
    s.q["writer"].append(dup_angle)
    s.q["reviewer"].append(review_reply())
    job = generate(wid, s)
    batch = st.get("script_batches", wid)
    assert len(batch["candidates"]) == 1 and job["state"] == "partial"
    assert "1/3" in batch["shortfall_note"]


def test_partial_two_of_three_after_two_repair_rounds_and_call_caps():
    wid, s = ready_work()
    s.q["writer"] += [three(), {"candidates": [dict(cand(3), text="第一轮返工的第三篇稿子一样不行，但是内容各不相同呀。")]},
                      {"candidates": [dict(cand(3), text="第二轮返工的第三篇仍然被判失败，是另一段完全不同的话语。")]}]
    s.q["reviewer"] += [review_reply(fail=(3,)), review_reply(fail=(3,)), review_reply(fail=(3,))]
    job = generate(wid, s)
    assert job["state"] == "partial"
    assert s.calls["planner"] == 1 and s.calls["writer"] <= 3 and s.calls["reviewer"] <= 3
    batch = st.get("script_batches", wid)
    assert job["result_refs"]["passed"] == 2 and len(batch["repair_history"]) == 2
    assert {h["round"] for h in batch["repair_history"]} == {1, 2}


def test_repair_only_rewrites_failed_slots_and_keeps_passed_versions():
    wid, s = ready_work()
    s.q["writer"] += [three(), repair_reply(2)]
    s.q["reviewer"] += [review_reply(fail=(2,)), review_reply()]
    job = generate(wid, s)
    assert job["state"] == "succeeded"
    batch = st.get("script_batches", wid)
    first = repo.get_script_version
    by_slot = {c["slot"]: c for c in batch["candidates"]}
    assert by_slot[2]["parent_id"] is not None and by_slot[1]["parent_id"] is None and by_slot[3]["parent_id"] is None
    old2 = repo.get_script_version(by_slot[2]["parent_id"])
    assert old2["id"] != by_slot[2]["id"] and old2["text"] != by_slot[2]["text"]  # 新版本新 ID，旧版本保留
    assert s.calls["writer"] == 2 and s.calls["reviewer"] == 2
    # 第二次审稿只请求返工稿，成功稿不重审
    import re, json
    ids = json.loads(re.search(r"review_ids[^\[]*(\[[^\]]*\])", s.seen["reviewer"][1]).group(1))
    assert ids == [by_slot[2]["id"]]


def test_reuse_of_reference_text_is_hard_blocked_and_repaired():
    wid, s = ready_work()
    reuse = three()
    reuse["candidates"][0]["text"] = "开场先来一句参考视频里说了一段独一无二的话用来测试复用检测不要照搬这一整段然后接自己的话。"
    s.q["writer"] += [reuse, repair_reply(1)]
    s.q["reviewer"] += [review_reply(), review_reply()]
    job = generate(wid, s)
    batch = st.get("script_batches", wid)
    first = repo.get_script_version(batch["candidates"][0]["parent_id"])
    assert any(c["code"] == "REFERENCE_REUSE" and c["outcome"] == "fail" for c in first["quality_report"]["checks"])
    assert job["state"] == "succeeded"


def test_no_forced_question_hook_check():
    wid, s = ready_work()
    s.q["writer"].append(three())
    s.q["reviewer"].append(review_reply())
    generate(wid, s)
    for c in st.get("script_batches", wid)["candidates"]:
        assert all(k["code"] != "HOOK_STRENGTH" for k in c["quality_report"]["checks"])


def test_claims_need_real_evidence_and_quote():
    wid, s = ready_work(materials=[MATERIAL])
    from app.content import context as cx
    ev_id = cx.materials_for(st.get("works", wid)["brief"])[0]["evidence_id"]
    good = cand(1, text="这次发布里上下文窗口提升到了一百万个词元，我自己试下来确实更能塞。再讲讲我的体会一二三。",
                claims=[{"text": "上下文窗口提升到一百万个词元", "evidence_id": ev_id,
                         "source_quote": "上下文窗口提升到了一百万个词元", "position": "上下文窗口提升到了一百万个词元"}])
    fake_quote = cand(2, claims=[{"text": "编造的数字两千万", "evidence_id": ev_id, "source_quote": "两千万个词元",
                                  "position": ""}])
    missing_ev = cand(3, claims=[{"text": "官方说了一个东西", "evidence_id": "ev_nope", "source_quote": "x", "position": ""}])
    s.q["writer"].append({"candidates": [good, fake_quote, missing_ev]})
    s.q["writer"] += [repair_reply(2, 3), repair_reply(2, 3)]
    s.q["reviewer"] += [review_reply(), review_reply(), review_reply()]
    generate(wid, s)
    batch = st.get("script_batches", wid)
    by_slot = {c["slot"]: c for c in batch["candidates"]}
    g = by_slot[1]
    assert all(k["outcome"] == "pass" for k in g["quality_report"]["checks"] if k["hard_gate"])
    assert g["claims"][0]["verdict"] == "supported" and g["origin"]["evidence"][0]["verified"] is True


def test_reviewer_outage_binds_unreviewed_candidates_that_cannot_be_picked():
    wid, s = ready_work()
    s.q["writer"].append(three())
    s.q["reviewer"].append(TimeoutError("断线"))
    job = generate(wid, s)
    assert job["state"] == "unknown"
    batch = selection.project_batch(wid)
    assert batch["produced"] == 3 and len(batch["candidates"]) == 3 and not batch["ready_to_present"]
    assert batch["retry_targets"] == [] and len(batch["review_targets"]) == 3
    assert all(c["review_state"] == "needs_review" for c in batch["review_targets"])
    assert s.calls["reviewer"] == 1


def test_invalid_review_output_never_passes():
    wid, s = ready_work()
    s.q["writer"].append(three())
    bad = lambda user: {"candidates": [], "set": {"distinct": True, "rewrite_slots": []}}
    s.q["reviewer"].append(bad)
    job = generate(wid, s)
    assert job["state"] == "failed" and job["error"]["code"] == "REVIEW_INVALID"
    assert not any(c["pickable"] for c in selection.project_batch(wid)["candidates"])


def test_review_quote_must_exist_in_text():
    wid, s = ready_work()
    s.q["writer"].append(three())
    s.q["reviewer"].append(lambda user: review_reply(fail=(1,), quote="正文里根本没有的话")(user))
    job = generate(wid, s)
    assert job["state"] == "failed" and job["error"]["code"] == "REVIEW_QUOTE_INVALID"


def test_late_result_for_old_revision_is_archived_not_bound():
    wid, s = ready_work()
    s.q["writer"].append(three())

    def bump_then_review(user):
        with st.transaction() as c:
            repo.bump_content_revision(c, wid, brief={"topic": "中途改了主题", "target_duration_ms": 0})
        return review_reply()(user)

    s.q["reviewer"].append(bump_then_review)
    job = generate(wid, s)
    assert job["state"] == "superseded" and job["archived_result"]["batch_id"]
    # 审核开始前已展示初稿；输入变更后它成为失效稿，晚到审核不能重新绑定最终批次。
    view = selection.project_batch(wid)
    assert view["id"] != job["archived_result"]["batch_id"]
    assert all(c["review_state"] == "stale" and not c["pickable"] for c in view["candidates"])


def test_repair_job_targets_only_named_candidate_and_needs_current_batch():
    wid, s = ready_work()
    s.q["writer"] += [three(), repair_reply(2)]
    s.q["reviewer"] += [review_reply(fail=(2,)), review_reply()]
    generate(wid, s)
    batch = st.get("script_batches", wid)
    target = next(c for c in batch["candidates"] if c["slot"] == 1)
    calls_before = dict(s.calls)
    s.q["writer"].append(repair_reply(1))
    s.q["reviewer"].append(review_reply())
    job = run_job("repair", wid, {"batch_id": batch["id"], "candidate_ids": [target["id"]], "instruction": "更口语"},
                  service.handle_repair, s)
    assert job["state"] in ("succeeded", "partial"), job.get("error")
    assert s.calls["writer"] == calls_before["writer"] + 1
    newb = st.get("script_batches", wid)
    assert newb["id"] != batch["id"]
    by_slot = {c["slot"]: c for c in newb["candidates"]}
    old = {c["slot"]: c for c in batch["candidates"]}
    assert by_slot[1]["id"] != old[1]["id"] and by_slot[1]["parent_id"] == old[1]["id"]
    assert by_slot[2]["id"] == old[2]["id"] and by_slot[3]["id"] == old[3]["id"]
    stale = run_job("repair", wid, {"batch_id": batch["id"], "candidate_ids": [target["id"]]}, service.handle_repair, s)
    assert stale["state"] == "failed" and stale["error"]["code"] == "STALE_SELECTION"
