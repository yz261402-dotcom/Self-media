"""全链失效：改输入/换批次/改选，撤销选用与下游确认，保留文件与费用，任务回到重新选稿，事件在同一事务。"""
import pytest

from app import storage as st
from app.content import repository as repo
from app.content import selection, service, tasks
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


def generated_work():
    wid = make_work()
    s = Script()
    s.q["planner"].append(plan_json())
    run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply())
    run_job("generation", wid, {}, service.handle_generation, s)
    return wid, s


def attach_task(wid, stage, state, pending=()):
    tid = "task_" + wid
    st.put("tasks", tid, {"id": tid, "work_id": wid, "stage": stage, "state": state,
                          "pending_selection_ids": list(pending), "mode": "mock"}, {"work_id": wid})
    w = st.get("works", wid)
    w["current_task_id"] = tid
    st.put("works", wid, w)
    return tid


def adopted_work():
    wid, s = generated_work()
    tid = attach_task(wid, "tts", "running")
    cid = {c["slot"]: c["id"] for c in st.get("script_batches", wid)["candidates"]}[1]
    selection.adopt(wid, cid)
    st.put("audios", "aud_" + wid, {"id": "aud_" + wid, "work_id": wid, "confirmed": True, "kind": "full", "file_hash": "h"},
           {"work_id": wid})
    w = st.get("works", wid)
    w["final_audio_id"] = "aud_" + wid
    st.put("works", wid, w)
    return wid, tid, cid


def test_changing_shared_input_revokes_adoption_and_downstream():
    wid, tid, _ = adopted_work()
    with st.transaction() as c:
        repo.bump_content_revision(c, wid, style_snapshot={"persona": "新风格", "digest": "z"})
    w = st.get("works", wid)
    assert w["script_version_id"] is None and w["final_audio_id"] is None
    audio = st.get("audios", "aud_" + wid)
    assert audio["confirmed"] is False and audio["invalidated"] is True
    task = st.get("tasks", tid)
    assert task["stage"] == "content_review" and task["state"] == "waiting_selection"
    assert "sel_script" in task["pending_selection_ids"]
    assert any(e["type"] == "selection.revoked" for e in st.events_since(tid))
    assert all(s["state"] != "selected" for s in st.all_rows("selections", "work_id=?", [wid]) if s.get("kind") == "script")


def test_new_batch_replaces_old_and_revokes_selection():
    wid, tid, _ = adopted_work()
    s = Script()
    texts = {1: "第一篇换个话题：聊聊我怎么整理桌面，东西少了心情都轻了。",
             2: "第二篇换个话题：说说通勤路上听播客的习惯，一天多学一点点。",
             3: "第三篇换个话题：关于运动我只坚持一个原则，别和别人比，比自己从前。"}
    s.q["writer"].append({"candidates": [cand(i, text=texts[i]) for i in (1, 2, 3)]})
    s.q["reviewer"].append(review_reply())
    job = tasks.submit_job(wid, "generation", "again", {})
    tasks.set_progress(job["id"], state="running")
    service.handle_generation(job, transport=s)
    assert st.get("works", wid)["script_version_id"] is None
    assert st.get("tasks", tid)["stage"] == "content_review"


def test_switching_selection_revokes_previous_downstream():
    wid, _, _ = adopted_work()
    other = {c["slot"]: c["id"] for c in st.get("script_batches", wid)["candidates"]}[2]
    selection.adopt(wid, other)
    w = st.get("works", wid)
    assert w["script_version_id"] == other and w["final_audio_id"] is None
    assert st.get("audios", "aud_" + wid)["confirmed"] is False
    selected = [s for s in st.all_rows("selections", "work_id=?", [wid]) if s.get("kind") == "script" and s["state"] == "selected"]
    assert len(selected) == 1 and selected[0]["selected_ref"] == other


def test_adopting_advances_task_from_content_review_and_emits_event():
    wid, _ = generated_work()
    tid = attach_task(wid, "content_review", "waiting_selection", pending=["sel_script"])
    cid = {c["slot"]: c["id"] for c in st.get("script_batches", wid)["candidates"]}[1]
    out = selection.adopt(wid, cid)
    assert out["advance"] is True
    t = st.get("tasks", tid)
    assert t["stage"] == "avatar_variant" and "sel_script" not in t["pending_selection_ids"]
    assert any(e["type"] == "selection.decided" for e in st.events_since(tid))


def test_event_insert_is_transactional_and_rolls_back():
    tid = "task_tx"
    st.put("tasks", tid, {"id": tid, "work_id": "w", "stage": "topic", "state": "queued"}, {"work_id": "w"})
    with pytest.raises(RuntimeError):
        with st.transaction() as c:
            st.add_event_tx(c, tid, {"type": "x", "message": "m"})
            raise RuntimeError("boom")
    assert st.events_since(tid) == []
