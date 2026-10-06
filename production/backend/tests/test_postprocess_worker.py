"""v2.4 代码后期边界；独立 SQLite 数据，不发起生成/网络请求。"""
import asyncio

import pytest

from app import storage as st
from app.agents import supervisor


def seed(stage="postprocess", mode="mock"):
    wid, tid = st.new_id("w_worker"), st.new_id("t_worker")
    work = {"id": wid, "name": "worker test", "brief": {"topic": "RAG"},
            "identity_id": "av_base_b_v1", "voice_asset_id": "voice_minimax_warm_bestie_v1",
            "script_version_id": None, "route": "M2", "current_task_id": tid}
    task = {"id": tid, "work_id": wid, "mode": mode, "stage": stage,
            "state": "queued", "route": "M2", "route_confirmed": True,
            "pending_selection_ids": []}
    st.put("works", wid, work)
    st.put("tasks", tid, task, {"work_id": wid})
    return task, work


def test_prompt_generation_has_one_work_item_and_no_research_step():
    task, work = seed("script")
    summary = asyncio.run(supervisor.run_stage(task, work, "script"))
    items = [i for i in st.all_rows("agent_work_items", "task_id=?", [task["id"]])
             if i["stage"] == "script"]
    assert len(items) == 1
    assert summary["failures"] == []
    assert len(st.get("script_batches", work["id"])["candidates"]) == 3


def test_quality_stage_uses_internal_history_without_web_search():
    task, work = seed("script")
    asyncio.run(supervisor.run_stage(task, work, "script"))
    asyncio.run(supervisor.run_stage(task, work, "content_review"))
    items = st.all_rows("agent_work_items", "task_id=?", [task["id"]])
    audit = [a for i in items if i["role"] == "quality"
             for a in st.all_rows("agent_tool_audit", "work_item_id=?", [i["id"]])]
    assert "search_content_history" in {a["tool"] for a in audit}
    assert not {"search_sources", "fetch_source"} & {a["tool"] for a in audit}


@pytest.mark.parametrize("stage", ["postprocess", "publish_package"])
def test_unready_worker_stage_blocks_instead_of_completing_empty_plan(stage):
    task, _ = seed(stage)
    asyncio.run(supervisor.advance(task["id"]))
    result = st.get("tasks", task["id"])
    assert result["state"] == "blocked"
    assert result["stage"] == stage
    assert not st.all_rows("packages", "work_id=?", [task["work_id"]])
    records = st.all_rows("media_jobs", "work_id=?", [task["work_id"]])
    assert len(records) == 1
    assert records[0]["executor_kind"] == "worker"
    assert records[0]["worker_kind"] == "postprocess"
    assert records[0]["state"] == "blocked"
    assert records[0]["output_asset_ids"] == []


def test_real_postprocess_without_real_media_blocks_and_never_calls_agent(monkeypatch):
    """T07 起真实后期已实现：缺真实媒体时仍必须 blocked（不得伪造产物），
    且整条路径绝不调用 Agent。"""
    task, _ = seed("postprocess", "production")
    monkeypatch.setattr(supervisor, "build_model", lambda *a, **k: pytest.fail("Worker invoked an Agent"))
    asyncio.run(supervisor.advance(task["id"]))
    result = st.get("tasks", task["id"])
    assert result["state"] == "blocked"
    assert result["block_reason"] in ("DEPENDENCY_NOT_MET", "MEDIA_NOT_READY",
                                      "ASR_NOT_CONFIGURED", "FFMPEG_NOT_AVAILABLE")


def test_blocked_worker_resume_keeps_one_record_for_identical_inputs():
    task, _ = seed()
    asyncio.run(supervisor.advance(task["id"]))
    task = st.get("tasks", task["id"])
    task["state"] = "queued"
    st.put("tasks", task["id"], task, {"work_id": task["work_id"]})
    asyncio.run(supervisor.advance(task["id"]))
    assert st.get("tasks", task["id"])["state"] == "blocked"
    records = st.all_rows("media_jobs", "work_id=?", [task["work_id"]])
    assert len(records) == 1
    assert records[0]["simulated"] is True
    assert records[0]["processing_outcome"] == "not_run"


def test_persisted_failed_agent_work_cannot_be_skipped_on_resume():
    task, work = seed("script")
    item = next(i for i in supervisor.build_work_items(task, work) if i.stage == "script")
    item.state = "failed"
    item.last_error = {"code": "TEST_FAILED", "message": "saved failure"}
    st.put("agent_work_items", item.id, item.model_dump(), {"task_id": task["id"]})
    asyncio.run(supervisor.advance(task["id"]))
    result = st.get("tasks", task["id"])
    assert result["state"] == "failed"
    assert result["stage"] == "script"


def test_legacy_editor_checkpoint_is_blocked_without_rewriting_history():
    task, _ = seed("publish_package")
    legacy = {"id": st.new_id("wi_legacy"), "task_id": task["id"],
              "stage": "publish_package", "role": "editor", "state": "queued"}
    st.put("agent_work_items", legacy["id"], legacy, {"task_id": task["id"]})
    asyncio.run(supervisor.advance(task["id"]))
    result = st.get("tasks", task["id"])
    assert result["state"] == "blocked"
    assert result["block_reason"] == "LEGACY_AGENT_GRAPH"
    assert st.get("agent_work_items", legacy["id"]) == legacy


def test_unregistered_stage_cannot_return_empty_success():
    task, work = seed()
    summary = asyncio.run(supervisor.run_stage(task, work, "missing_stage"))
    assert summary["failures"]
    assert summary["failures"][0]["error"]["code"] == "STAGE_NOT_REGISTERED"
