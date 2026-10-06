"""T02：重启恢复、持久化完整性、检查点不能代替账本、依赖失效与血缘。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import jobs, lineage, storage as st  # noqa: E402
from app.contracts import Task, Work, WorkBrief  # noqa: E402


def _new_work(topic="恢复验证"):
    w = Work(id=st.new_id("work"), name=topic, brief=WorkBrief(topic=topic),
             identity_id="av_base_b_v1", voice_asset_id="voice_minimax_warm_bestie_v1")
    st.put("works", w.id, w.model_dump())
    return w


# ---------- 刷新与重启后的恢复 ----------

def test_work_survives_reread_from_storage():
    """不依赖浏览器内存或进程内缓存：从仓储重新读回同一作品。"""
    w = _new_work()
    again = Work(**st.get("works", w.id))
    assert again.id == w.id
    assert again.identity_id == "av_base_b_v1"
    assert again.voice_asset_id == "voice_minimax_warm_bestie_v1"


def test_task_state_is_persisted_not_in_memory():
    w = _new_work()
    t = Task(id=st.new_id("task"), work_id=w.id, stage="talking_head", state="running",
             input_snapshot_digest="sha256:snap1")
    st.put("tasks", t.id, t.model_dump(), {"work_id": w.id})
    reread = Task(**st.get("tasks", t.id))
    assert reread.state == "running"
    assert reread.input_snapshot_digest == "sha256:snap1"


def test_events_are_replayable_after_restart():
    w = _new_work()
    t = Task(id=st.new_id("task"), work_id=w.id)
    st.put("tasks", t.id, t.model_dump(), {"work_id": w.id})
    st.add_event(t.id, {"type": "task.created", "occurred_at": "2026-01-01T00:00:00+00:00",
                        "stage": "topic", "state": "queued", "message": "任务已创建"})
    st.add_event(t.id, {"type": "stage.started", "occurred_at": "2026-01-01T00:00:01+00:00",
                        "stage": "script", "state": "running", "message": "开始写稿"})
    evs = st.events_since(t.id)
    assert [e["seq"] for e in evs] == [1, 2]
    assert evs[-1]["type"] == "stage.started"


def test_pending_selection_task_is_not_auto_resumed():
    """等待用户选择的任务在重启恢复时必须停住，不能被自动推进或代选。"""
    from app import engine

    w = _new_work()
    t = Task(id=st.new_id("task"), work_id=w.id, stage="content_review",
             state="waiting_selection", pending_selection_ids=["sel_script"])
    st.put("tasks", t.id, t.model_dump(), {"work_id": w.id})
    assert t.id not in engine.resumable_tasks(), "等待选择的任务不得进入可恢复清单"
    assert engine.recover_pending() == 0
    assert st.get("tasks", t.id)["state"] == "waiting_selection"


def test_recover_pending_counts_only_resumable_tasks():
    from app import engine

    w = _new_work()
    t = Task(id=st.new_id("task"), work_id=w.id, state="running")
    st.put("tasks", t.id, t.model_dump(), {"work_id": w.id})
    assert t.id in engine.resumable_tasks()


# ---------- 检查点不能代替账本 ----------

def test_ledger_inconsistency_is_detectable_after_restart():
    w = _new_work()
    job = jobs.submit(work_id=w.id, capability="tts", provider="minimax", model="speech-2.8-hd",
                      args={"text": "x"}, idempotency_key=f"i_{st.new_id('k')}")
    jobs.acquire_lease(job["id"], "w1", ttl_seconds=30)
    raw = jobs.get_job(job["id"])
    raw["state"] = "succeeded"  # 人为破坏：宣称成功但没有结果摘要
    raw["lease"] = None
    st.put("provider_jobs", job["id"], raw, {"work_id": w.id})
    rep = jobs.ledger_consistent(w.id)
    assert rep["consistent"] is False
    assert any(p["code"] == "SUCCESS_WITHOUT_DIGEST" for p in rep["problems"])


# ---------- 依赖失效与血缘 ----------

def _seed_lineage(work_id: str):
    lineage.record(artifact_id="sv1", kind="script", work_id=work_id, digest="sha256:s")
    lineage.record(artifact_id="au1", kind="audio", work_id=work_id, digest="sha256:a",
                   depends_on=["sv1"])
    lineage.record(artifact_id="li1", kind="lipsync", work_id=work_id, digest="sha256:l",
                   depends_on=["au1"])
    lineage.record(artifact_id="st1", kind="subtitle", work_id=work_id, digest="sha256:st",
                   depends_on=["sv1"])
    lineage.record(artifact_id="vd1", kind="video", work_id=work_id, digest="sha256:v",
                   depends_on=["li1", "st1"])


def test_changing_script_text_invalidates_audio_lipsync_subtitle():
    w = _new_work()
    _seed_lineage(w.id)
    out = lineage.invalidate(work_id=w.id, change_type="script_text", source_artifact_id="sv1")
    kinds = {x["kind"] for x in out["invalidated"]}
    assert {"audio", "lipsync", "subtitle", "video"} <= kinds
    assert out["action"] == "rebuild"


def test_changing_subtitle_style_only_rerenders():
    w = _new_work()
    _seed_lineage(w.id)
    out = lineage.invalidate(work_id=w.id, change_type="subtitle_style")
    assert out["action"] == "rerender"
    kinds = {x["kind"] for x in out["invalidated"]}
    assert "audio" not in kinds, "仅改字幕样式不得重做音频"
    assert "lipsync" not in kinds, "仅改字幕样式不得重做嘴型"
    assert "video" in kinds


def test_invalidation_preserves_original_results():
    w = _new_work()
    _seed_lineage(w.id)
    before = lineage.get("au1")
    lineage.invalidate(work_id=w.id, change_type="script_text")
    after = lineage.get("au1")
    assert after["digest"] == before["digest"], "原始摘要必须保留"
    assert after["invalidated_at"] is not None
    assert after["invalidated_by"] == "script_text"


def test_unknown_change_type_rejected():
    w = _new_work()
    with pytest.raises(lineage.LineageDenied) as e:
        lineage.invalidate(work_id=w.id, change_type="whatever")
    assert e.value.code == "VALIDATION_FAILED"


def test_late_candidate_cannot_rewrite_current_selection():
    w = _new_work()
    st.put("selections", "sel_1", {"id": "sel_1", "work_id": w.id, "kind": "script",
                                   "selected_ref": "sv_chosen", "state": "selected"},
           {"work_id": w.id})
    guard = lineage.late_candidate_guard(w.id, "sv_late")
    assert guard["accepted"] is False
    assert guard["current"] == "sv_chosen"
    ok = lineage.late_candidate_guard(w.id, "sv_chosen")
    assert ok["accepted"] is True


def test_reverify_detects_missing_dependency():
    w = _new_work()
    lineage.record(artifact_id="vd2", kind="video", work_id=w.id, digest="sha256:v",
                   depends_on=["missing_artifact"])
    rep = lineage.reverify(w.id)
    assert rep["consistent"] is False
    assert any(p["code"] == "MISSING_DEPENDENCY" for p in rep["problems"])


def test_reverify_detects_invalid_dependency():
    w = _new_work()
    _seed_lineage(w.id)
    lineage.invalidate(work_id=w.id, change_type="script_text")
    rep = lineage.reverify(w.id)
    assert rep["consistent"] is False
    assert any(p["code"] == "DEPENDS_ON_INVALID" for p in rep["problems"])
