"""分镜制作指令与现实事实分开检查；避免秒数和剧情结束语触发返工。"""
import pytest

from app import storage as st
from app.content import context as cx, selection, service, tasks
from tests.content._helpers import Script, cand, human_approve, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "0")


def work_and_context():
    wid = make_work(with_reference=False)
    work = st.get("works", wid)
    work["brief"]["content_route"] = "short_video_storyboard"
    st.put("works", wid, work)
    service.ensure_prompt(wid)
    return wid, cx.load_context(wid, require_prompt=True)


def shot(*, visual="窗边人物看向手机，镜头切到表情特写。", dialogue="我不想再争了，先各自静一静。",
         sound="环境声渐弱。"):
    return {"shot_index": 1, "visual": visual, "dialogue": dialogue,
            "sound_effects": sound, "duration_seconds": 8}


def build(shots):
    wid, ctx = work_and_context()
    version = service._build_version(dict(cand(1), storyboard=shots), ctx=ctx,
                                     job={"id": "test-storyboard", "work_id": wid}, slot=1,
                                     parent_id=None, round_no=0, writer={})
    return version


def outcome(version, code):
    return next(c["outcome"] for c in version["quality_report"]["checks"] if c["code"] == code)


@pytest.mark.parametrize("visual,sound", [
    # 取自已保存历史稿 script_0889555c61b8 的第 4 / 6 / 12 镜头制作描述。
    ("0至2秒回到工作室，主角笑一下；2至5秒切独立全屏玄关结果；5至8秒切回主角。",
     "0.2秒处一次轻呼气；键盘声、脚步声。2.2秒处一次轻叹气；低音量音乐在第6秒结束。"),
    ("0至1.5秒：人物手指悬在发送键上；2秒后硬切到嘴部特写。", "低频环境声，停顿3秒，再接轻微按键声。"),
    ("镜头停留2秒，字幕停留1.5秒后消失。", "静音3秒，音效持续1秒。"),
    ("[0-1.5秒]手机特写，1.5秒后切到人物眼神。", "音乐暂停2秒，随后渐入。"),
])
def test_production_timing_in_visual_and_sound_is_not_a_real_fact(visual, sound):
    version = build([shot(visual=visual, sound=sound)])
    assert outcome(version, "CLAIM_COVERAGE") == "pass"
    assert visual in version["storyboard"][0]["visual"]
    assert sound in version["text"]


@pytest.mark.parametrize("dialogue", ["今天先到这里。", "今天就到这里，回头再说。", "今天就先这样吧。",
    # 取自历史稿 script_cb9038f8b0e6 的第 11 镜头。
    "就这样，保存。今天的生活化先到这里。"])
def test_everyday_dialogue_closing_is_not_timely_information(dialogue):
    version = build([shot(dialogue=dialogue)])
    assert outcome(version, "TIMELINESS") == "pass"
    assert dialogue in version["text"]


@pytest.mark.parametrize("field,content,code", [
    ("dialogue", "这个模型推理只要2秒。", "CLAIM_COVERAGE"),
    ("dialogue", "这个套餐价格是99元。", "CLAIM_COVERAGE"),
    ("dialogue", "官方研究表明效果提升一倍。", "CLAIM_COVERAGE"),
    ("visual", "产品屏幕写着：推理耗时2秒。", "CLAIM_COVERAGE"),
    ("visual", "屏幕字幕：系统停顿3秒才返回结果。", "CLAIM_COVERAGE"),
    ("visual", "字幕写着：充电器2秒后显示已充满。", "CLAIM_COVERAGE"),
    ("sound_effects", "新闻播报：信号暂停3秒，用户无法连接。", "CLAIM_COVERAGE"),
    ("visual", "镜头停留2秒；屏幕字幕：系统停顿3秒才返回结果。", "CLAIM_COVERAGE"),
    ("visual", "屏幕文字写着系统停顿3秒才返回结果。", "CLAIM_COVERAGE"),
    ("visual", "界面显示：充电器2秒后显示已充满。", "CLAIM_COVERAGE"),
    ("visual", "展示充电器测试，设备2秒后显示已充满。", "CLAIM_COVERAGE"),
    ("sound_effects", "新闻播报称系统暂停3秒后才恢复服务。", "CLAIM_COVERAGE"),
    ("sound_effects", "新闻主播播报系统暂停3秒后才恢复服务。", "CLAIM_COVERAGE"),
    ("visual", "屏幕字幕：系统需等待；系统停顿3秒才返回结果。", "CLAIM_COVERAGE"),
    ("visual", "屏幕文字写着：暂停3秒后才返回结果。", "CLAIM_COVERAGE"),
    ("visual", "界面显示：2秒后显示已充满。", "CLAIM_COVERAGE"),
    ("visual", "屏幕展示充电实验，2秒后显示已充满。", "CLAIM_COVERAGE"),
    ("visual", "检测结果：2至5秒。", "CLAIM_COVERAGE"),
    ("sound_effects", "新闻播报称：暂停3秒后恢复服务。", "CLAIM_COVERAGE"),
    ("sound_effects", "检测过程中，暂停3秒后恢复服务。", "CLAIM_COVERAGE"),
    ("sound_effects", "新闻播报：官方报告显示用户增长了20%。", "CLAIM_COVERAGE"),
    ("visual", "镜头停留2秒，屏幕展示产品价格99元。", "CLAIM_COVERAGE"),
    ("dialogue", "今天发布的模型价格更低。", "TIMELINESS"),
    ("dialogue", "今天先到这里。最新款手机将在本月发布。", "TIMELINESS"),
    ("visual", "字幕写着2026年10月7日最新发布。", "TIMELINESS"),
])
def test_real_fact_signals_still_require_evidence_in_every_field(field, content, code):
    key = {"sound_effects": "sound"}.get(field, field)
    version = build([shot(**{key: content})])
    assert outcome(version, code) == "review"


def test_same_timing_phrase_in_dialogue_remains_subject_to_fact_coverage():
    version = build([shot(dialogue="系统停顿3秒才返回结果。")])
    assert outcome(version, "CLAIM_COVERAGE") == "review"


def test_user_storyboard_revision_uses_the_same_field_aware_review():
    wid, _ = work_and_context()
    script = Script()
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[shot(dialogue=cand(i)["text"])])
                                              for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    first = selection.project_batch(wid)["candidates"][0]
    revised = service.revise_candidate(wid, first["id"], storyboard=[
        shot(visual="0至1.5秒：人物看向手机；2秒后硬切。", dialogue="今天先到这里。", sound="停顿3秒。")])
    assert outcome(revised, "CLAIM_COVERAGE") == "pass"
    assert outcome(revised, "TIMELINESS") == "pass"
    assert revised["id"] != first["id"]
    assert next(c for c in selection.project_batch(wid)["candidates"] if c["id"] == revised["id"])["review_state"] == "needs_review"
    assert script.calls["writer"] == 1


@pytest.mark.parametrize("outage", [False, True])
def test_historical_rule_false_positives_are_recomputed_without_changing_saved_records(monkeypatch, outage):
    wid, _ = work_and_context()
    script = Script()
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[shot(
        visual="0至2秒回到工作室，主角笑一下；2至5秒切独立全屏结果。",
        dialogue=cand(i)["text"] + "今天的生活化先到这里。", sound="0.2秒处一次轻呼气；音乐在第6秒结束。")])
        for i in (1, 2, 3)]})
    script.q["reviewer"].append(TimeoutError("历史审核未确认") if outage else review_reply())
    # 只在生成历史夹具时恢复旧的“混合全文”规则，固定保存之前真实存在的误判。
    with monkeypatch.context() as old:
        old.setattr(service, "storyboard_review_texts", lambda shots: {})
        run_job("generation", wid, {}, service.handle_generation, script)
    saved = st.get("script_batches", wid)
    saved_versions = [st.get("script_versions", c["id"]) for c in saved["candidates"]]
    assert all(outcome(c, "CLAIM_COVERAGE") == "review" and outcome(c, "TIMELINESS") == "review"
               for c in saved["candidates"])
    counts = dict(script.calls)
    projected = selection.project_batch(wid)
    assert all(outcome(c, "CLAIM_COVERAGE") == "pass" and outcome(c, "TIMELINESS") == "pass"
               for c in projected["candidates"])
    assert projected["ready_to_present"] is (not outage)
    assert projected["retry_targets"] == []
    assert st.get("script_batches", wid) == saved
    assert [st.get("script_versions", c["id"]) for c in saved["candidates"]] == saved_versions
    assert script.calls == counts
    if outage:
        with pytest.raises(selection.SelectionRejected):
            selection.adopt(wid, saved["candidates"][0]["id"])
    else:
        human_approve(wid, saved["candidates"][0]["id"])
        selection.adopt(wid, saved["candidates"][0]["id"])
        assert selection.current_selection_valid(wid)


def test_current_rule_projection_preserves_unrelated_hard_failures(monkeypatch):
    wid, _ = work_and_context()
    script = Script()
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[shot(dialogue=cand(i)["text"])])
                                              for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    raw = st.get("script_batches", wid)
    for check in raw["candidates"][0]["quality_report"]["checks"]:
        if check["code"] == "REFERENCE_REUSE":
            check.update(outcome="fail", message="历史确实复用了参考句")
    st.put("script_batches", wid, raw)
    projected = selection.project_batch(wid)
    assert outcome(projected["candidates"][0], "REFERENCE_REUSE") == "fail"
    assert not projected["ready_to_present"]
    with pytest.raises(selection.SelectionRejected):
        selection.adopt(wid, raw["candidates"][0]["id"])
    assert st.get("script_batches", wid) == raw


def test_failed_single_is_really_rereviewed_without_a_writer_request():
    import json
    import re

    wid, _ = work_and_context()
    script = Script()
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[shot(dialogue=cand(i)["text"])])
                                              for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply(fail=(2,)))
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "partial"
    batch = st.get("script_batches", wid)
    failed = next(c for c in batch["candidates"] if c["slot"] == 2)
    script.q["reviewer"].append(review_reply())
    result = run_job("review", wid, {"batch_id": batch["id"]}, service.handle_review, script)
    assert result["state"] == "succeeded", result.get("error")
    sent_ids = json.loads(re.search(r"review_ids[^\[]*(\[[^\]]*\])", script.seen["reviewer"][-1]).group(1))
    assert sent_ids == [failed["id"]]
    assert script.calls["writer"] == 1 and script.calls["reviewer"] == 2
    assert selection.project_batch(wid)["ready_to_present"]


def test_review_returning_after_edit_is_archived_with_current_draft_preserved():
    wid, _ = work_and_context()
    script = Script()
    script.q["writer"].append({"candidates": [dict(cand(i), storyboard=[shot(dialogue=cand(i)["text"])])
                                              for i in (1, 2, 3)]})
    script.q["reviewer"].append(review_reply())
    assert run_job("generation", wid, {}, service.handle_generation, script)["state"] == "succeeded"
    batch = st.get("script_batches", wid)
    updated = []

    def edit(user):
        first = batch["candidates"][0]
        updated.append(service.revise_candidate(wid, first["id"], storyboard=[shot(dialogue="用户刚改过的对白。")]))
        return review_reply()(user)

    script.q["reviewer"].append(edit)
    result = run_job("review", wid, {"batch_id": batch["id"]}, service.handle_review, script)
    assert result["state"] == "superseded", result.get("error")
    assert st.get("script_batches", wid)["candidates"][0]["id"] == updated[0]["id"]
    assert not selection.project_batch(wid)["ready_to_present"]
