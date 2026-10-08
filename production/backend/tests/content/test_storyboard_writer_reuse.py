"""整数时长无损归一及失败父任务的已完成写稿结果复用；仅 fake transport。"""
import copy
import json
from uuid import uuid4

import pytest

from app import jobs, storage as st
from app.content import service, tasks
from app.content.errors import ContentRejected
from tests.content._helpers import make_work, set_env


def shot(duration):
    return {"shot_index": 1, "duration_seconds": duration, "visual": "街头练习", "dialogue": "原创旁白", "sound_effects": "节奏音乐"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)


@pytest.mark.parametrize("duration", [2.0, 3.0, 30.0])
def test_whole_float_duration_normalizes_without_changing_time(duration):
    shots, _, duration_ms = service._storyboard([shot(duration)], int(duration * 1000))
    assert type(shots[0]["duration_seconds"]) is int
    assert shots[0]["duration_seconds"] == duration and duration_ms == duration * 1000


@pytest.mark.parametrize("duration", [True, False, "3", 3.1, float("nan"), float("inf"), float("-inf"), 1.0, 31.0])
def test_invalid_duration_remains_rejected(duration):
    with pytest.raises(ContentRejected) as exc:
        service._storyboard([shot(duration)], 0)
    assert exc.value.code == "STORYBOARD_INVALID"


def cached_source():
    wid = make_work(with_reference=False)
    source = tasks.submit_job(wid, "generation", uuid4().hex, {"prompt_id": "p"})
    drafts = {"candidates": [{"slot": i, "storyboard": [shot(3.0)]} for i in (1, 2, 3)]}
    def transport(*_args):
        return 200, json.dumps({"id": "source-request", "model": "gpt-6.1-sol",
            "choices": [{"message": {"content": json.dumps(drafts)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 50}}).encode()
    result, _ = service._writer_call(source, {"prompt": {"id": "p"}}, "system", "user", 0, "writer", transport)
    tasks.run_job_error(source, ContentRejected("local candidate validation failed", "NO_VALID_CANDIDATES"))
    new = tasks.submit_job(wid, "generation", uuid4().hex, {"prompt_id": "p", "reuse_writer_call_id": result["job_id"]})
    return source, new, result


def never_send(*_args, **_kwargs):
    pytest.fail("Invalid reuse or cache replay must not send a provider request")


def test_reuse_completed_three_drafts_does_not_change_original_ledger():
    source, new, old_result = cached_source()
    original = copy.deepcopy(st.get("provider_jobs", old_result["job_id"]))
    original_parent = copy.deepcopy(tasks.get_job(source["id"]))
    result, writer = service._writer_call(new, {"prompt": {"id": "p"}}, "system", "user", 0, "writer", never_send)
    assert result["json"] == old_result["json"] and result["replayed"]
    assert writer["job_id"] == old_result["job_id"]
    assert all(service._storyboard(draft["storyboard"], 3000)[2] == 3000 for draft in result["json"]["candidates"])
    assert st.get("provider_jobs", old_result["job_id"]) == original
    assert tasks.get_job(source["id"]) == original_parent
    assert tasks.jobs_calls(new["id"]) == []


@pytest.mark.parametrize("field,value", [("state", "unknown"), ("namespace", "media"), ("capability", "tts"),
    ("step", "repair"), ("round", 1), ("provider", "qwen"), ("model", "wrong-model"), ("work_id", "another-work"),
    ("idempotency_key", "unbound-key")])
def test_wrong_source_provider_call_is_rejected_before_send(field, value):
    _, new, result = cached_source()
    call = st.get("provider_jobs", result["job_id"])
    call[field] = value
    st.put("provider_jobs", call["id"], call)
    with pytest.raises(ContentRejected) as exc:
        service._writer_call(new, {"prompt": {"id": "p"}}, "system", "user", 0, "writer", never_send)
    assert exc.value.code == "WRITER_REUSE_INVALID"


@pytest.mark.parametrize("field,value", [("state", "succeeded"), ("kind", "repair"), ("error", {"code": "OTHER"}),
    ("work_id", "foreign-work"), ("content_revision", 99), ("payload", {"prompt_id": "old-prompt"}),
    ("writer_runtime_profiles", {"writer": "old", "repair": "old"})])
def test_wrong_source_parent_is_rejected_before_send(field, value):
    source, new, _ = cached_source()
    parent = tasks.get_job(source["id"])
    parent[field] = value
    with st.transaction() as connection:
        connection.execute("UPDATE content_jobs SET data=? WHERE id=?", [json.dumps(parent), source["id"]])
    with pytest.raises(ContentRejected) as exc:
        service._writer_call(new, {"prompt": {"id": "p"}}, "system", "user", 0, "writer", never_send)
    assert exc.value.code == "WRITER_REUSE_INVALID"


def test_changed_current_prompt_cannot_reuse_before_send():
    _, new, _ = cached_source()
    with pytest.raises(ContentRejected):
        service._writer_call(new, {"prompt": {"id": "changed"}}, "system", "user", 0, "writer", never_send)


def test_changed_request_still_hits_original_digest_guard_before_send():
    _, new, _ = cached_source()
    with pytest.raises(jobs.JobRejected) as exc:
        service._writer_call(new, {"prompt": {"id": "p"}}, "system", "changed input", 0, "writer", never_send)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("reuse_id", ["", None, False, 0, {"id": "not-a-call-id"}])
def test_invalid_reuse_id_cannot_silently_start_new_paid_call(reuse_id):
    _, new, _ = cached_source()
    new["payload"]["reuse_writer_call_id"] = reuse_id
    with pytest.raises(ContentRejected) as exc:
        service._writer_call(new, {"prompt": {"id": "p"}}, "system", "user", 0, "writer", never_send)
    assert exc.value.code == "WRITER_REUSE_INVALID"


def test_writer_later_round_cannot_reuse_initial_call():
    _, new, _ = cached_source()
    with pytest.raises(ContentRejected) as exc:
        service._writer_call(new, {"prompt": {"id": "p"}}, "system", "user", 1, "writer", never_send)
    assert exc.value.code == "WRITER_REUSE_INVALID"


def test_quality_repair_uses_normal_new_call_not_initial_cache():
    _, new, original = cached_source()
    calls = []
    def fake_repair(*args):
        calls.append(args)
        return 200, json.dumps({"id": "repair-request", "model": "gpt-6.1-sol",
            "choices": [{"message": {"content": '{"candidates": []}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 50}}).encode()
    result, _ = service._writer_call(new, {"prompt": {"id": "p"}}, "repair system", "feedback", 1, "repair", fake_repair)
    assert len(calls) == 1 and result["job_id"] != original["job_id"]
    assert tasks.jobs_calls(new["id"])[0]["step"] == "repair"
