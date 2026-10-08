"""Direct generation preserves input, billing and replay guarantees without DeepAgents."""
import pytest
from fastapi.testclient import TestClient

from app import engine, jobs, storage as st
from app.content import direct_generation, repository as repo, service, tasks
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, review_reply, run_job, set_env


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    service.register_handlers()
    monkeypatch.setattr(engine, "schedule", lambda *_: pytest.fail("direct content scheduled DeepAgents"))
    from app.agents import supervisor
    async def broken(*args, **kwargs):
        pytest.fail("direct content invoked DeepAgents")
    monkeypatch.setattr(supervisor, "run_stage", broken)
    monkeypatch.setattr(supervisor, "advance", broken)


def post(work_id, payload):
    return TestClient(app).post(f"/api/v1/works/{work_id}/content/direct-generations", json=payload)


def request(work_id, **kwargs):
    work = st.get("works", work_id)
    return {"request_id": "direct-one", "expected_content_revision": work["content_revision"],
            "topic": work["brief"]["topic"], **kwargs}


def script(monkeypatch):
    result = Script()
    result.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    result.q["reviewer"].append(review_reply())
    monkeypatch.setattr("app.content.providers.text._default_transport", result)
    return result


def test_submission_prepares_prompt_atomically_and_replays_original_revision():
    wid = make_work(with_reference=False)
    original = request(wid, topic="  一个新主题  ", materials=["  原始资料  ", {"text": "第二份资料", "source": "本地"}])
    first = post(wid, original)
    assert first.status_code == 202, first.text
    job = tasks.get_job(first.json()["job_id"])
    work = st.get("works", wid)
    assert job["kind"] == "generation" and job["payload"]["orchestration"] == "direct"
    assert work["brief"]["topic"] == "一个新主题"
    assert len(work["brief"]["materials"]) == 2
    assert all(m["content_hash"] and m["added_at"] for m in work["brief"]["materials"])
    assert job["content_revision"] == work["content_revision"] > original["expected_content_revision"]
    assert job["payload"]["prompt_id"] == work["writing_prompt_id"]
    assert jobs.content_calls(job["id"]) == []
    task = st.get("tasks", work["current_task_id"])
    assert task["state"] == "waiting_selection" and task["id"] not in engine.resumable_tasks()
    again = post(wid, original)
    assert again.status_code == 202 and again.json()["replayed"]
    assert again.json()["job_id"] == job["id"] and st.get("works", wid) == work
    conflict = post(wid, {**original, "topic": "另一个主题"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(tasks.list_jobs(wid)) == 1 and st.get("works", wid) == work


def test_stale_or_active_submission_has_no_input_side_effects():
    wid = make_work(with_reference=False)
    work = st.get("works", wid)
    stale = post(wid, request(wid, expected_content_revision=10, topic="不得保存"))
    assert stale.status_code == 409 and st.get("works", wid) == work
    tasks.submit_job(wid, "prompt_revision", "existing", {})
    before = st.get("works", wid)
    active = post(wid, request(wid, topic="不得保存"))
    assert active.status_code == 409 and active.json()["error"]["code"] == "CONTENT_FLOW_ACTIVE"
    assert st.get("works", wid) == before and repo.list_prompts(wid) == []


def test_selected_reference_requires_prepared_prompt_and_rolls_back_changes():
    wid = make_work()
    work = st.get("works", wid)
    blocked = post(wid, request(wid, topic="不得保存的主题"))
    assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "PROMPT_STALE"
    assert st.get("works", wid) == work and tasks.list_jobs(wid) == [] and repo.list_prompts(wid) == []


def test_valid_reference_prompt_is_reused_and_reference_digest_changes_rejected():
    wid = make_work()
    transport = Script()
    transport.q["planner"].append(plan_json())
    assert run_job("prompt", wid, {}, service.handle_prepare_prompt, transport)["state"] == "succeeded"
    work = st.get("works", wid)
    result = post(wid, request(wid))
    assert result.status_code == 202 and result.json()["prompt_id"] == work["writing_prompt_id"]
    assert st.get("works", wid)["content_revision"] == work["content_revision"]
    tasks.finish_job(result.json()["job_id"], "cancelled")
    prompt = repo.get_prompt(work["writing_prompt_id"])
    prompt["reference_digest"] = "incorrect"
    st.put("content_prompts", prompt["id"], prompt, {"work_id": wid})
    invalid = post(wid, request(wid, request_id="bad-reference"))
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "PROMPT_STALE"


def test_unknown_replay_retains_original_job_and_blocks_new_intent(monkeypatch):
    wid = make_work(with_reference=False)
    body = request(wid)
    first = post(wid, body)
    job_id = first.json()["job_id"]
    transport = Script()
    transport.q["writer"].append(TimeoutError("unknown"))
    monkeypatch.setattr("app.content.providers.text._default_transport", transport)
    original = tasks.run_inline(job_id, wait_seconds=5)
    assert original["state"] == "unknown" and transport.calls["writer"] == 1
    call = jobs.content_calls(job_id)[0]
    assert call["state"] == "unknown" and call["settlement_state"] == "held"
    again = post(wid, body)
    assert again.status_code == 202 and again.json()["job_id"] == job_id and again.json()["state"] == "unknown"
    before = st.get("works", wid)
    blocked = post(wid, request(wid, request_id="new-one", topic="也不能换主题重发"))
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "CONTENT_RESULT_UNKNOWN"
    assert st.get("works", wid) == before and len(tasks.list_jobs(wid)) == 1
    assert jobs.get_job(call["id"]) == call and transport.calls["writer"] == 1


def test_unconfirmed_provider_call_blocks_even_when_parent_cancelled():
    wid = make_work(with_reference=False)
    parent = tasks.submit_job(wid, "generation", "old", {})
    tasks.finish_job(parent["id"], "cancelled")
    call = {"id": "unknown_" + wid, "work_id": wid, "namespace": "content-v2", "capability": "chat",
            "state": "unknown", "parent_content_job_id": parent["id"], "reserved_fen": 50}
    st.put("provider_jobs", call["id"], call, {"work_id": wid})
    before = st.get("works", wid)
    blocked = post(wid, request(wid))
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "CONTENT_CALL_PENDING"
    assert st.get("works", wid) == before and repo.list_prompts(wid) == []


@pytest.mark.parametrize("mode", ["production", "integration"])
def test_unapproved_mode_never_enqueues_or_modifies_inputs(monkeypatch, mode):
    wid = make_work(with_reference=False)
    before = st.get("works", wid)
    monkeypatch.setenv("AVATAR_STUDIO_MODE", mode)
    monkeypatch.delenv(f"AVATAR_STUDIO_ALLOW_{mode.upper()}", raising=False)
    result = post(wid, request(wid, topic="不保存"))
    assert result.status_code == 409 and result.json()["error"]["code"] == "CONTENT_MODE_NOT_AUTHORIZED"
    assert st.get("works", wid) == before and tasks.list_jobs(wid) == [] and repo.list_prompts(wid) == []


def test_legacy_api_completes_with_broken_framework_and_does_not_reschedule(monkeypatch):
    wid = make_work(with_reference=False)
    transport = script(monkeypatch)
    client = TestClient(app)
    first = client.post(f"/api/v1/works/{wid}/scripts", json={"request_id": "legacy-explicit"})
    assert first.status_code == 200, first.text
    assert len(first.json()["candidates"]) == 3 and first.json()["passed"] == 3
    again = client.post(f"/api/v1/works/{wid}/scripts", json={"request_id": "legacy-explicit"})
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert transport.calls == {"planner": 0, "writer": 1, "reviewer": 1}
    task = st.get("tasks", st.get("works", wid)["current_task_id"])
    assert task["stage"] == "content_review" and task["state"] == "waiting_selection"
    assert "sel_script" in task["pending_selection_ids"] and task["id"] not in engine.resumable_tasks()


def test_direct_main_task_is_never_resumable_by_agent_engine():
    wid = make_work(with_reference=False)
    job = direct_generation.submit(wid, {}, legacy=True)
    task = st.get("tasks", st.get("works", wid)["current_task_id"])
    assert task["stage"] == "script" and task["state"] == "waiting_selection"
    assert task["id"] not in engine.resumable_tasks()
    tasks.finish_job(job["id"], "blocked", error={"code": "PROVIDER_NOT_CONFIGURED", "message": "缺密钥"})
    updated = st.get("tasks", task["id"])
    assert updated["state"] == "blocked" and updated["last_error"]["code"] == "PROVIDER_NOT_CONFIGURED"


def test_legacy_no_request_id_replays_failed_job_without_automatic_retry(monkeypatch):
    wid = make_work(with_reference=False)
    monkeypatch.delenv("AVATAR_STUDIO_OPENAI_API_KEY")
    client = TestClient(app)
    first = client.post(f"/api/v1/works/{wid}/scripts", json={})
    again = client.post(f"/api/v1/works/{wid}/scripts", json={})
    assert first.status_code == again.status_code == 409
    assert first.json()["error"]["job_id"] == again.json()["error"]["job_id"]
    assert len(tasks.list_jobs(wid)) == 1 and jobs.content_calls(tasks.list_jobs(wid)[0]["id"]) == []


def test_submit_failure_rolls_back_inputs_prompt_adoption_and_main_task(monkeypatch):
    wid = make_work(with_reference=False)
    before = st.get("works", wid)
    def reject(*args, **kwargs):
        raise tasks.IdempotencyConflict("simulated conflict")
    monkeypatch.setattr(tasks, "submit_job", reject)
    result = post(wid, request(wid, topic="应一起回滚", materials=["不保存的资料"]))
    assert result.status_code == 409
    assert st.get("works", wid) == before and repo.list_prompts(wid) == [] and tasks.list_jobs(wid) == []
    assert st.all_rows("tasks", "work_id=?", [wid]) == []


def test_async_generation_completes_and_exact_replay_does_not_clear_adoption(monkeypatch):
    from app.content import selection
    wid = make_work(with_reference=False)
    transport = script(monkeypatch)
    body = request(wid)
    accepted = post(wid, body)
    done = tasks.run_inline(accepted.json()["job_id"], wait_seconds=5)
    assert done["state"] == "succeeded"
    batch = selection.project_batch(wid)
    selection.adopt(wid, batch["candidates"][0]["id"])
    before = st.get("works", wid)
    task_before = st.get("tasks", before["current_task_id"])
    replay = post(wid, body)
    assert replay.status_code == 202 and replay.json()["replayed"] and replay.json()["state"] == "succeeded"
    assert st.get("works", wid) == before and st.get("tasks", before["current_task_id"]) == task_before
    assert transport.calls == {"planner": 0, "writer": 1, "reviewer": 1}
