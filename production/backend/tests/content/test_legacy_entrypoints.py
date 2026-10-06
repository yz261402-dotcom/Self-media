"""旧入口（POST scripts / script-revisions / script-repairs）委托内容服务：
结构不变、超时带原 job_id 的 504、重试不增加供应商调用、改稿新版本、Agent 不再二次生成/审稿。"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.agents import tools
from app.content import repository as repo
from app.content import service, tasks
from app.main import app
from tests.content._helpers import Script, cand, make_work, plan_json, repair_reply, review_reply, set_env


class Gate:
    """在 Script 之上加一道门：让第一次写稿阻塞，模拟旧同步入口等待超时。"""

    def __init__(self, script):
        self.script, self.release, self.entered = script, threading.Event(), threading.Event()

    def __call__(self, url, headers, body, timeout):
        if "anthropic" in url and not self.release.is_set():
            self.entered.set()
            self.release.wait(10)
        return self.script(url, headers, body, timeout)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    service.register_handlers()
    from app.content import references
    references.register_handlers()


def install(monkeypatch, script):
    from app.content.providers import text as tp
    monkeypatch.setattr(tp, "_default_transport", script)


def err(r):
    b = r.json()
    return b.get("error") or b["detail"]["error"]


def script_for_generation():
    s = Script()
    s.q["planner"].append(plan_json())
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply())
    return s


def test_legacy_post_scripts_returns_scriptbatch_shape_and_persists_job(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()  # 无参考：直接提示，不调 planner
    install(monkeypatch, s)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/works/{wid}/scripts", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_kind"] == "llm" and len(body["candidates"]) == 3 and body["requested"] == 3
    assert body["passed"] == 3 and all(c["pickable"] for c in body["candidates"])
    jobs = tasks.list_jobs(wid, kinds=["generation"])
    assert len(jobs) == 1 and jobs[0]["request_id"].startswith("legacy_") and jobs[0]["state"] == "succeeded"
    assert (s.calls["planner"], s.calls["writer"], s.calls["reviewer"]) == (0, 1, 1)


def test_legacy_retry_reuses_original_operation_no_extra_provider_calls(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    install(monkeypatch, s)
    with TestClient(app) as client:
        first = client.post(f"/api/v1/works/{wid}/scripts", json={})
        again = client.post(f"/api/v1/works/{wid}/scripts", json={})
    assert first.status_code == 200, first.text
    assert again.status_code == 200, again.text
    assert first.json()["id"] == again.json()["id"]
    assert (s.calls["writer"], s.calls["reviewer"]) == (1, 1)  # 重试取回原结果，供应商不再被调用


def test_legacy_timeout_is_504_with_original_job_id_and_no_resubmission(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    gate = Gate(s)
    install(monkeypatch, gate)
    monkeypatch.setattr(service, "LEGACY_WAIT_SECONDS", 0.3)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/works/{wid}/scripts", json={})
        assert r.status_code == 504
        e = err(r)
        assert e["code"] == "CONTENT_JOB_PENDING" and e["retryable"] is False and e["job_id"] and e["poll_url"].endswith(e["job_id"])
        assert gate.entered.wait(5)
        again = client.post(f"/api/v1/works/{wid}/scripts", json={})  # 客户端超时重试
        assert again.status_code == 504 and err(again)["job_id"] == e["job_id"]
        assert len(tasks.list_jobs(wid, kinds=["generation"])) == 1
        gate.release.set()
        for _ in range(100):
            if tasks.get_job(e["job_id"])["state"] in tasks.TERMINAL_STATES:
                break
            time.sleep(0.05)
        assert tasks.get_job(e["job_id"])["state"] == "succeeded"
    assert s.calls["writer"] == 1


def test_other_errors_do_not_carry_job_fields(monkeypatch):
    with TestClient(app) as client:
        r = client.post("/api/v1/works/nope/scripts", json={})
    assert "job_id" not in err(r) and "poll_url" not in err(r)


def test_legacy_revision_creates_new_version_and_revokes_selection(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    install(monkeypatch, s)
    with TestClient(app) as client:
        batch = client.post(f"/api/v1/works/{wid}/scripts", json={}).json()
        first = batch["candidates"][0]
        assert client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": first["id"]}).status_code == 200
        calls_before = dict(s.calls)
        r = client.post(f"/api/v1/works/{wid}/script-revisions",
                        json={"script_id": first["id"], "text": first["text"] + "这是我补的一句自己的体会。"})
        assert r.status_code == 200
        new = r.json()
        assert new["id"] != first["id"] and new["parent_id"] == first["id"] and new["slot"] == first["slot"]
        assert s.calls == calls_before  # 改稿不调模型
        assert repo.get_script_version(first["id"])["text"] == first["text"]  # 旧版本不可变
        assert st.get("works", wid)["script_version_id"] is None  # 旧选用撤销
        after = client.get(f"/api/v1/works/{wid}/scripts").json()
        assert after["candidates"] == [] and after["ready_to_present"] is False
        assert any(t["slot"] == first["slot"] and t["review_state"] == "needs_review"
                   for t in after["retry_targets"])
        blocked = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": new["id"]})
        assert blocked.status_code == 409 and err(blocked)["code"] == "SCRIPT_REVIEW_REQUIRED"


def test_legacy_revision_with_reference_reuse_is_hard_blocked(monkeypatch):
    wid = make_work()
    s = Script()
    s.q["planner"].append(plan_json())
    s.q["writer"].append({"candidates": [cand(1), cand(2), cand(3)]})
    s.q["reviewer"].append(review_reply())
    install(monkeypatch, s)
    with TestClient(app) as client:
        service.run_legacy(wid, "prompt", {}, request_id="p")
        job = service.run_legacy(wid, "generation", {"prompt_id": st.get("works", wid)["writing_prompt_id"]}, request_id="g")
        assert job["state"] == "succeeded"
        first = st.get("script_batches", wid)["candidates"][0]
        r = client.post(f"/api/v1/works/{wid}/script-revisions",
                        json={"script_id": first["id"], "text": "参考视频里说了一段独一无二的话用来测试复用检测不要照搬这一整段"})
        assert r.status_code == 200
        assert any(c["code"] == "REFERENCE_REUSE" and c["outcome"] == "fail" for c in r.json()["quality_report"]["checks"])


def test_legacy_repair_creates_new_version_only_for_target(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    install(monkeypatch, s)
    with TestClient(app) as client:
        batch = client.post(f"/api/v1/works/{wid}/scripts", json={}).json()
        target = batch["candidates"][0]
        s.q["writer"].append(repair_reply(target["slot"]))
        s.q["reviewer"].append(review_reply())
        r = client.post(f"/api/v1/works/{wid}/script-repairs", json={"script_id": target["id"], "request_id": "rp1"})
        assert r.status_code == 200, r.text
        assert r.json()["id"] != target["id"] and r.json()["parent_id"] == target["id"]
        after = {c["slot"]: c for c in client.get(f"/api/v1/works/{wid}/scripts").json()["candidates"]}
        others = {c["slot"]: c["id"] for c in batch["candidates"]}
        for slot in (2, 3):
            assert after[slot]["id"] == others[slot]
        stale = client.post(f"/api/v1/works/{wid}/script-repairs", json={"script_id": target["id"]})
        assert stale.status_code == 409 and err(stale)["code"] == "STALE_SELECTION"


def test_legacy_repair_does_not_return_unapproved_candidate_body(monkeypatch):
    from app.content import selection
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_MAX_REPAIR_ROUNDS", "1")
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    install(monkeypatch, s)
    with TestClient(app) as client:
        batch = client.post(f"/api/v1/works/{wid}/scripts", json={}).json()
        target = batch["candidates"][0]
        s.q["writer"].append(repair_reply(target["slot"]))
        s.q["reviewer"].append(review_reply(fail=(target["slot"],)))
        response = client.post(f"/api/v1/works/{wid}/script-repairs", json={"script_id": target["id"]})
    assert response.status_code == 200, response.text
    assert selection.project_batch(wid)["ready_to_present"] is False
    assert "text" not in response.json() and "storyboard" not in response.json()
    assert "candidates" not in response.json()


def test_legacy_repair_on_pre_upgrade_batch_is_explicitly_refused():
    wid = make_work(with_reference=False)
    sid = "script_legacy"
    st.put("script_batches", wid, {"id": "b_old", "work_id": wid, "source_kind": "llm", "candidates": [
        {"id": sid, "angle": "a", "title": "t", "description": "d", "tags": [], "text": "旧稿", "duration_estimate_ms": 1000,
         "claims": [], "quality_report": {"checks": [], "overall": "pass"}}]})
    with TestClient(app) as client:
        r = client.post(f"/api/v1/works/{wid}/script-repairs", json={"script_id": sid})
    assert r.status_code == 409 and err(r)["code"] == "LEGACY_BATCH"


def test_missing_credentials_in_non_mock_mode_blocks_without_fake_scripts(monkeypatch):
    monkeypatch.delenv("AVATAR_STUDIO_ANTHROPIC_API_KEY")
    wid = make_work(with_reference=False)
    ctx = {"work_item_id": "wi_x"}
    monkeypatch.setattr(tools, "MODE", "production")
    with pytest.raises(tools.ToolDenied) as e:
        tools.t_register_script_candidates({"work_id": wid, "topic": "AI"}, ctx)
    assert e.value.code == "PROVIDER_NOT_CONFIGURED"
    assert st.get("script_batches", wid) is None


def test_agent_tool_and_api_share_one_execution_no_double_generation(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    install(monkeypatch, s)
    ctx = {"work_item_id": "wi_a", "attempt": 0}
    out1 = tools.t_register_script_candidates({"work_id": wid, "topic": "AI 助手怎么用"}, ctx)
    out2 = tools.t_register_script_candidates({"work_id": wid, "topic": "AI 助手怎么用"}, dict(ctx, attempt=5))
    assert out1["batch_id"] == out2["batch_id"] and out1["source_kind"] == "llm"
    assert (s.calls["writer"], s.calls["reviewer"]) == (1, 1)
    report = tools.t_submit_quality_report({"work_id": wid, "subject_id": "__all__"}, {"work_item_id": "wi_q"})
    assert report["reviewed"] == 3
    assert (s.calls["writer"], s.calls["reviewer"]) == (1, 1)  # 质量阶段复用已持久的审核，不再收费
    assert st.get("script_batches", wid)["id"] == out1["batch_id"]


def test_tool_reports_pending_job_instead_of_regenerating(monkeypatch):
    wid = make_work(with_reference=False)
    s = script_for_generation()
    s.q["planner"].clear()
    gate = Gate(s)
    install(monkeypatch, gate)
    monkeypatch.setattr(service, "LEGACY_WAIT_SECONDS", 0.2)
    with pytest.raises(tools.ToolDenied) as e:
        tools.t_register_script_candidates({"work_id": wid, "topic": "AI"}, {"work_item_id": "wi_p"})
    assert e.value.code == "CONTENT_JOB_PENDING"
    gate.release.set()
    time.sleep(0.5)
    assert s.calls["writer"] == 1
