import asyncio
import threading

from app import config, storage as st
from app.agents import supervisor, tools


def test_m2_connection_status_uses_actual_worker_environment(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_M2WORKER_API_KEY", raising=False)
    monkeypatch.setenv("AVATAR_STUDIO_M2_WORKER_URL", "http://127.0.0.1:19090")
    monkeypatch.setenv("AVATAR_STUDIO_M2_WORKER_TOKEN", "local-only")
    state = config.public_provider_view("m2_worker")
    assert state["configured"] is True
    assert "M2WORKER_API_KEY" not in state["detail"]


def test_content_tools_do_not_block_progress_requests(monkeypatch):
    work_id, task_id = st.new_id("loop"), st.new_id("task")
    work = {"id": work_id, "brief": {"topic": "RAG"}, "current_task_id": task_id}
    task = {"id": task_id, "work_id": work_id, "mode": "mock", "route": "M2", "stage": "script", "state": "running"}
    st.put("works", work_id, work)
    st.put("tasks", task_id, task, {"work_id": work_id})
    started, release = threading.Event(), threading.Event()
    original = tools.invoke
    def slow_invoke(*args):
        started.set()
        assert release.wait(2), "同步模型调用阻塞了事件循环"
        return original(*args)
    monkeypatch.setattr(supervisor, "invoke", slow_invoke)
    monkeypatch.setattr(tools, "invoke", slow_invoke)
    async def check():
        run = asyncio.create_task(supervisor.run_stage(task, work, "script"))
        assert await asyncio.to_thread(started.wait, 1)
        release.set()
        assert not (await run)["failures"]
    asyncio.run(check())
