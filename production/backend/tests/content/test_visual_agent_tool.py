"""analyze_reference_visual：只属于主控；不接受裸 paid_ack/金额；作品隔离；幂等；状态映射。"""
import pytest

from app import storage as st
from app.agents import tools
from app.agents.roles import ROLES
from app.content import tasks, visual
from tests.content.test_visual_analysis import confirm, env, mp4, setup_work  # noqa: F401


def ctx(wid, item="wi_v"):
    return {"work_id": wid, "work_item_id": item, "task_id": "t_none"}


def args(wid, rid, asset, conf):
    return {"work_id": wid, "reference_snapshot_id": rid, "media_asset_id": asset.id, "confirmation_id": conf}


def test_only_supervisor_may_call_it():
    assert "analyze_reference_visual" in ROLES["supervisor"].tools
    assert "analyze_reference_visual" not in ROLES["content"].tools
    assert "analyze_reference_visual" not in ROLES["quality"].tools
    assert "analyze_reference_visual" in tools.TOOLS and "analyze_reference_visual" in tools.SIDE_EFFECT_TOOLS
    for role in ("content", "quality"):
        with pytest.raises(tools.ToolDenied) as e:
            tools.invoke(role, "analyze_reference_visual", {"work_id": "w"}, {"work_item_id": "wi_deny"})
        assert e.value.code == "TOOL_NOT_ALLOWED"


def test_without_user_confirmation_returns_requires_user_action_and_sends_nothing(mp4):
    wid, rid, asset = setup_work(mp4)
    out = tools.invoke("supervisor", "analyze_reference_visual", args(wid, rid, asset, ""), ctx(wid))["result"]
    assert out["requires_user_action"] is True and out["code"] == "VISUAL_COST_CONFIRMATION_REQUIRED"
    assert out["state"] == "requires_user_action" and not st.all_rows("content_jobs", "work_id=?", [wid])


def test_agent_cannot_smuggle_paid_ack_or_amount(mp4):
    wid, rid, asset = setup_work(mp4)
    bad = dict(args(wid, rid, asset, ""), paid_ack=True, total_ceiling_fen=80)
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("supervisor", "analyze_reference_visual", bad, ctx(wid))
    assert e.value.code == "VALIDATION_FAILED"
    assert not st.all_rows("content_jobs", "work_id=?", [wid])


def test_confirmed_submission_is_pending_idempotent_and_shares_ui_job(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)["id"]
    a = tools.invoke("supervisor", "analyze_reference_visual", args(wid, rid, asset, conf), ctx(wid))["result"]
    assert a["job_id"] and a["state"] in ("queued", "running") and a["requires_user_action"] is False
    assert a["visual_state"] == "pending"
    b = tools.invoke("supervisor", "analyze_reference_visual", args(wid, rid, asset, conf), ctx(wid, "wi_v2"))["result"]
    assert b["job_id"] == a["job_id"]  # 不同工作项也取回同一任务，不重复计费
    ui = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf)
    assert ui["id"] == a["job_id"]


def test_work_scope_and_confirmation_binding(mp4):
    wid, rid, asset = setup_work(mp4)
    other, orid, oasset = setup_work(mp4)
    conf = confirm(wid, rid, asset)["id"]
    with pytest.raises(tools.ToolDenied) as e:
        tools.invoke("supervisor", "analyze_reference_visual", args(other, rid, asset, conf), ctx(wid))
    assert e.value.code == "WORK_SCOPE_DENIED"
    out = tools.invoke("supervisor", "analyze_reference_visual", args(other, orid, oasset, conf), ctx(other))["result"]
    assert out["requires_user_action"] is True  # 别的作品的确认不能借用


def test_ready_and_unknown_and_failed_mapping(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)["id"]
    job = tools.invoke("supervisor", "analyze_reference_visual", args(wid, rid, asset, conf), ctx(wid))["result"]
    tasks.finish_job(job["job_id"], "unknown", error={"code": "CONTENT_RESULT_UNKNOWN", "message": "未知"})
    unk = tools.invoke("supervisor", "analyze_reference_visual", args(wid, rid, asset, conf), ctx(wid, "wi_u"))["result"]
    assert unk["state"] == "unknown" and unk["visual_state"] == "unknown" and unk["requires_user_action"] is True


def test_supervisor_blocks_writing_until_visual_ready_and_never_runs_writer(mp4, monkeypatch):
    import asyncio
    from app.agents import supervisor
    from app.content import repository as repo
    wid, rid, asset = setup_work(mp4)
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, reference_snapshot_id=rid)
    tid = "t_" + wid
    st.put("tasks", tid, {"id": tid, "work_id": wid, "mode": "mock", "stage": "topic", "state": "queued",
                          "route": "M2", "route_confirmed": False, "pending_selection_ids": [], "stage_rows": []},
           {"work_id": wid})
    w = st.get("works", wid)
    w["current_task_id"] = tid
    st.put("works", wid, w)
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    called = []
    monkeypatch.setitem(tools.TOOLS, "register_script_candidates", lambda a, c: called.append(1) or {})
    asyncio.run(supervisor.advance(tid))
    t = st.get("tasks", tid)
    assert t["state"] == "blocked" and t["block_reason"] == "VISUAL_USER_ACTION_REQUIRED" and not called
    items = {i["id"].rsplit("_", 1)[-1]: i for i in st.all_rows("agent_work_items", "task_id=?", [tid])}
    assert items["visual"]["role"] == "supervisor" and items["visual"]["state"] == "blocked"
    assert items["script"]["state"] == "queued" and items["script"]["depends_on_ids"]
    assert not st.all_rows("content_jobs", "work_id=?", [wid])  # 没有确认，一个任务都没提交


def test_transcript_ready_reference_has_no_visual_work_item(mp4):
    from app.agents import supervisor
    wid = "work_" + __import__("uuid").uuid4().hex
    w = {"id": wid, "name": "t", "brief": {"topic": "x", "content_route": "short_video_storyboard"},
         "version": 1, "content_revision": 0}
    st.put("works", wid, w)
    from app.content import visual
    assert visual.reference_needs_visual(w) is None
