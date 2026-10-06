"""发布后台状态机：所有浏览器调用替身，不触碰真实抖音。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import storage as st
from app.main import app
from app.publishing import service


class RecordingAdapter:
    name = "mock"
    is_mock = True

    def __init__(self):
        self.prepared = []
        self.submitted = []
        self.prepare_result = {"state": "ready_to_confirm", "account_id": "test-account", "account_label": "模拟账号"}
        self.submit_result = {"state": "submitted", "provider_ref": "mock-receipt", "url": None}

    def login(self, session_dir):
        return {"state": "awaiting_login", "logged_in": False}

    def status(self, session_dir):
        return {"state": "logged_in", "account_id": "test-account", "account_label": "模拟账号", "is_mock": True}

    def prepare(self, **kwargs):
        self.prepared.append(kwargs)
        return self.prepare_result

    def submit(self, **kwargs):
        self.submitted.append(kwargs)
        return self.submit_result


def await_state(work_id, states, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        row = service.get_task(work_id)
        if row and row["state"] in states:
            return row
        time.sleep(.02)
    raise AssertionError(service.get_task(work_id))


@pytest.fixture()
def ctx(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(service, "SESSION_DIR", tmp_path / "profile")
    monkeypatch.delenv("AVATAR_STUDIO_DOUYIN_SESSION_DIR", raising=False)
    adapter = RecordingAdapter()
    monkeypatch.setattr(service, "get_adapter", lambda platform: adapter)
    packages = {}
    def seed():
        wid = st.new_id("work")
        video, cover = tmp_path / (wid + ".mp4"), tmp_path / (wid + ".jpg")
        video.write_bytes(b"video")
        cover.write_bytes(b"cover")
        pkg = {"id": st.new_id("pkg"), "work_id": wid, "title": "RAG", "description": "解释 RAG", "tags": ["AI"],
               "manifest_digest": "digest-" + wid}
        st.put("works", wid, {"id": wid})
        packages[wid] = (pkg, video, cover)
        return wid, pkg
    monkeypatch.setattr(service, "resolve_publish_package", lambda wid: packages[wid])
    wid, pkg = seed()
    return wid, pkg, adapter, seed


def confirm(wid, task):
    return service.confirm_task(wid, task_id=task["id"], confirmed=True, actor_id="user_local",
                                package_digest=task["package_digest"], confirmation_digest=task.get("confirmation_digest"))


def test_missing_review_cannot_start(monkeypatch):
    monkeypatch.setattr(service, "resolve_publish_package", lambda _: (_ for _ in ()).throw(
        service.PublishBlocked("FINAL_REVIEW_REQUIRED", "请先确认成片")))
    response = TestClient(app).post("/api/v1/works/missing/publish/start", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FINAL_REVIEW_REQUIRED"


def test_start_uploads_before_ready_and_custom_keys_cannot_duplicate(ctx):
    wid, pkg, adapter, _ = ctx
    first = service.start_task(wid, auto_submit=True, idempotency_key="arbitrary1")
    ready = await_state(wid, {"ready_to_confirm"})
    second = service.start_task(wid, idempotency_key="arbitrary2")
    assert first["id"] == second["id"]
    assert ready["account_id"] == "test-account"
    assert len(adapter.prepared) == 1
    assert not adapter.submitted
    assert ready["is_mock"] is True


def test_confirm_requires_task_id_digest_user_and_ready_state(ctx):
    wid, _, adapter, _ = ctx
    task = service.start_task(wid)
    await_state(wid, {"ready_to_confirm"})
    client = TestClient(app)
    for body in ({"confirmed": True, "actor_id": "user_local"},
                 {"task_id": task["id"], "confirmed": False, "actor_id": "user_local"},
                 {"task_id": task["id"], "confirmed": True, "actor_id": "agent", "package_digest": task["package_digest"]},
                 {"task_id": task["id"], "confirmed": True, "actor_id": "user_local", "package_digest": "stale"}):
        response = client.post(f"/api/v1/works/{wid}/publish/confirm", json=body)
        assert response.status_code in (409, 422), response.text
    assert not adapter.submitted


def test_simultaneous_confirms_submit_once_and_bind_account(ctx):
    wid, _, adapter, _ = ctx
    service.start_task(wid)
    ready = await_state(wid, {"ready_to_confirm"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(confirm, wid, ready) for _ in range(4)]
        for future in futures:
            try:
                future.result()
            except service.PublishBlocked as exc:
                assert exc.code == "PUBLISH_SUBMISSION_LOCKED"
    submitted = await_state(wid, {"submitted"})
    assert len(adapter.submitted) == 1
    assert adapter.submitted[0]["expected_account_id"] == ready["account_id"]
    assert submitted["is_mock"] is True
    assert submitted["attempt_started_at"]


@pytest.mark.parametrize("result", [{"state": "nonsense"}, {"state": "needs_verification"}])
def test_unknown_result_cannot_resubmit_cancel_or_prepare_again(ctx, result):
    wid, _, adapter, _ = ctx
    adapter.submit_result = result
    service.start_task(wid)
    ready = await_state(wid, {"ready_to_confirm"})
    confirm(wid, ready)
    unknown = await_state(wid, {"needs_verification"})
    for fn in (lambda: confirm(wid, ready), lambda: service.cancel_task(wid),
               lambda: service.resume_task(wid, task_id=ready["id"])):
        with pytest.raises(service.PublishBlocked):
            fn()
    assert service.start_task(wid, idempotency_key="new-key")["id"] == unknown["id"]
    assert len(adapter.submitted) == 1


def test_upload_does_not_hold_global_lock_and_other_work_is_blocked(ctx):
    wid, _, adapter, seed = ctx
    started, release = threading.Event(), threading.Event()
    original = adapter.prepare
    def slow(**kwargs):
        started.set()
        assert release.wait(3)
        return original(**kwargs)
    adapter.prepare = slow
    begin = time.monotonic()
    service.start_task(wid)
    assert time.monotonic() - begin < .5
    assert started.wait(1)
    other, _ = seed()
    try:
        with pytest.raises(service.PublishBlocked) as exc:
            service.start_task(other)
        assert exc.value.code == "PUBLISH_BROWSER_BUSY"
    finally:
        release.set()
    await_state(wid, {"ready_to_confirm"})


def test_awaiting_login_can_resume_without_creating_task(ctx):
    wid, _, adapter, _ = ctx
    adapter.prepare_result = {"state": "awaiting_login", "error": {"code": "DOUYIN_LOGIN_REQUIRED", "message": "扫码登录"}}
    started = service.start_task(wid)
    await_state(wid, {"awaiting_login"})
    adapter.prepare_result = {"state": "ready_to_confirm", "account_id": "test-account"}
    resumed = service.resume_task(wid, task_id=started["id"])
    assert resumed["id"] == started["id"]
    await_state(wid, {"ready_to_confirm"})
    assert len(adapter.prepared) == 2


def test_missing_dependency_returns_failed_task_with_clear_error(ctx):
    from app.publishing.adapters import AdapterError
    wid, _, adapter, _ = ctx
    def unavailable(**kwargs):
        raise AdapterError("PLAYWRIGHT_NOT_INSTALLED", "请安装 Playwright")
    adapter.prepare = unavailable
    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/start", json={})
    assert response.status_code == 200
    failed = await_state(wid, {"failed"})
    assert failed["error"]["code"] == "PLAYWRIGHT_NOT_INSTALLED"


def test_recovery_preserves_uncertain_submission_and_resumable_upload(ctx):
    wid, _, _, _ = ctx
    row = service.start_task(wid)
    await_state(wid, {"ready_to_confirm"})
    row.update(state="submitting", attempt_started_at="started")
    st.put("publish_tasks", row["id"], row, {"work_id": wid})
    service.recover_pending()
    assert service.get_task(wid)["state"] == "needs_verification"
    with pytest.raises(service.PublishBlocked):
        confirm(wid, row)


@pytest.mark.parametrize("state", ["draft", "uploading", "ready_to_confirm"])
def test_restart_preparation_requires_explicit_resume_and_new_confirmation(ctx, monkeypatch, state):
    wid, _, adapter, _ = ctx
    service.start_task(wid)
    previous = await_state(wid, {"ready_to_confirm"})
    interrupted = {**previous, "state": state}
    st.put("publish_tasks", interrupted["id"], interrupted, {"work_id": wid})
    browser_key = service._browser_key(interrupted["session_dir"])
    assert st.get("resource_locks", browser_key)["publish_task_id"] == previous["id"]
    prepared_count = len(adapter.prepared)

    def unexpected_spawn(*args):
        pytest.fail("重启恢复不能自动启动上传或提交")

    with monkeypatch.context() as recovery_patch:
        recovery_patch.setattr(service, "_spawn", unexpected_spawn)
        service.recover_pending()

    failed = service.get_task(wid, previous["id"])
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "PUBLISH_PREPARATION_INTERRUPTED"
    assert failed["attempt_started_at"] is None
    assert st.get("resource_locks", browser_key) is None
    assert len(adapter.prepared) == prepared_count
    assert not adapter.submitted
    with pytest.raises(service.PublishBlocked):
        confirm(wid, previous)

    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/resume",
                                    json={"task_id": previous["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == previous["id"]
    assert response.json()["state"] == "uploading"
    assert response.json()["confirmation_digest"] is None
    resumed = await_state(wid, {"ready_to_confirm"})
    assert resumed["id"] == previous["id"]
    assert resumed["revision"] == previous["revision"] + 1
    assert resumed["confirmation_digest"] != previous["confirmation_digest"]
    assert resumed["attempt_started_at"] is None
    assert st.get("resource_locks", browser_key)["publish_task_id"] == previous["id"]
    assert len(service.list_tasks(wid)) == 1
    assert len(adapter.prepared) == prepared_count + 1
    assert not adapter.submitted
    with pytest.raises(service.PublishBlocked) as exc:
        confirm(wid, previous)
    assert exc.value.code == "PUBLISH_CONFIRMATION_STALE"
    assert not adapter.submitted
    confirm(wid, resumed)
    await_state(wid, {"submitted"})
    assert len(adapter.submitted) == 1


@pytest.mark.parametrize("body", [{}, {"task_id": None}, {"task_id": ""}])
def test_resume_http_requires_explicit_task_version(ctx, body):
    wid, _, adapter, _ = ctx
    service.start_task(wid)
    await_state(wid, {"ready_to_confirm"})
    cancelled = service.cancel_task(wid)
    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/resume", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PUBLISH_VERSION_REQUIRED"
    assert service.get_task(wid, cancelled["id"]) == cancelled
    assert len(adapter.prepared) == 1
    assert not adapter.submitted


def test_resume_http_rejects_task_from_another_work(ctx):
    wid, _, adapter, seed = ctx
    service.start_task(wid)
    await_state(wid, {"ready_to_confirm"})
    cancelled = service.cancel_task(wid)
    other_work_id, _ = seed()
    response = TestClient(app).post(f"/api/v1/works/{other_work_id}/publish/resume",
                                    json={"task_id": cancelled["id"]})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PUBLISH_TASK_NOT_FOUND"
    assert service.get_task(wid, cancelled["id"]) == cancelled
    assert not service.list_tasks(other_work_id)
    assert len(adapter.prepared) == 1
    assert not adapter.submitted


@pytest.mark.parametrize("state,attempt_started_at", [
    ("needs_verification", None), ("submitting", None),
    ("submitted", None), ("failed", "started"),
])
def test_resume_http_cannot_bypass_submission_lock(ctx, state, attempt_started_at):
    wid, _, adapter, _ = ctx
    service.start_task(wid)
    locked = await_state(wid, {"ready_to_confirm"})
    locked.update(state=state, attempt_started_at=attempt_started_at)
    st.put("publish_tasks", locked["id"], locked, {"work_id": wid})
    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/resume",
                                    json={"task_id": locked["id"]})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PUBLISH_SUBMISSION_LOCKED"
    assert service.get_task(wid, locked["id"]) == locked
    assert len(adapter.prepared) == 1
    assert not adapter.submitted


def test_resume_http_rejects_changed_package_before_browser(ctx):
    wid, package, adapter, _ = ctx
    service.start_task(wid)
    await_state(wid, {"ready_to_confirm"})
    cancelled = service.cancel_task(wid)
    package["manifest_digest"] = "changed-after-preparation"
    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/resume",
                                    json={"task_id": cancelled["id"]})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "PUBLISH_INPUT_CHANGED"
    assert service.get_task(wid, cancelled["id"]) == cancelled
    assert len(adapter.prepared) == 1
    assert not adapter.submitted


def test_login_endpoint_does_not_claim_logged_in(ctx):
    response = TestClient(app).post("/api/v1/publish/douyin/login", json={})
    assert response.status_code == 200
    assert response.json()["state"] == "awaiting_login"
    assert response.json()["logged_in"] is False


def test_session_status_reports_verified_account_and_mock_flag(ctx):
    response = TestClient(app).get("/api/v1/publish/douyin/session")
    assert response.status_code == 200
    assert response.json()["account_id"] == "test-account"
    assert response.json()["is_mock"] is True


def test_real_delivery_gate_requires_final_review_and_detects_changed_file(tmp_path, monkeypatch):
    from test_delivery_workflow import seeded
    delivery, work, task, _ = seeded(tmp_path, monkeypatch)
    with pytest.raises(service.PublishBlocked) as exc:
        service.resolve_publish_package(work["id"])
    assert exc.value.code == "FINAL_REVIEW_REQUIRED"
    delivery.confirm_review(work["id"], actor_id="user_local", review_ack=True)
    delivery.build_delivery(task, work)
    package, video, cover = service.resolve_publish_package(work["id"])
    assert video.name == "source.mp4" and cover.name == "source.jpg"
    video.write_bytes(b"changed after review")
    with pytest.raises(service.PublishBlocked):
        service.resolve_publish_package(work["id"])


def test_cancel_before_submission_keeps_history(ctx):
    wid, _, _, _ = ctx
    service.start_task(wid)
    row = await_state(wid, {"ready_to_confirm"})
    cancelled = service.cancel_task(wid, task_id=row["id"])
    assert cancelled["state"] == "cancelled"
    assert TestClient(app).get(f"/api/v1/works/{wid}/publish/status").json()["id"] == row["id"]


def test_adapter_login_error_preserves_awaiting_login_state(ctx):
    from app.publishing.adapters import AdapterError
    wid, _, adapter, _ = ctx
    def need_login(**kwargs):
        raise AdapterError("DOUYIN_LOGIN_REQUIRED", "请扫码", state="awaiting_login", retryable=True)
    adapter.prepare = need_login
    service.start_task(wid)
    row = await_state(wid, {"awaiting_login", "failed"})
    assert row["state"] == "awaiting_login"


def test_ui_ack_receipt_is_accepted_without_fabricated_work_url(ctx):
    wid, _, adapter, _ = ctx
    adapter.submit_result = {"state": "submitted", "receipt": {"kind": "ui_ack", "text": "发布成功"}}
    service.start_task(wid)
    ready = await_state(wid, {"ready_to_confirm"})
    confirm(wid, ready)
    result = await_state(wid, {"submitted", "needs_verification"})
    assert result["state"] == "submitted"
    assert result["provider_url"] is None
    assert result["receipt"]["kind"] == "ui_ack"


@pytest.mark.parametrize("extra", [{"tags": "AI"}, {"tags": [42]}, {"title": "很长" * 30},
                                  {"description": "字" * 1000}, {"scheduled_at": "2099-01-01T00:00:00Z"}])
def test_invalid_text_and_unsupported_schedule_are_rejected_before_browser(ctx, extra):
    wid, _, adapter, _ = ctx
    response = TestClient(app).post(f"/api/v1/works/{wid}/publish/start", json=extra)
    assert response.status_code == 422, response.text
    assert not adapter.prepared


def test_cancel_then_edit_reuses_task_but_invalidates_old_confirmation(ctx):
    wid, _, adapter, _ = ctx
    service.start_task(wid)
    old = await_state(wid, {"ready_to_confirm"})
    service.cancel_task(wid, task_id=old["id"])
    replacement = service.start_task(wid, title="新标题")
    assert replacement["id"] == old["id"]
    current = await_state(wid, {"ready_to_confirm"})
    assert current["title"] == "新标题"
    assert current["confirmation_digest"] != old["confirmation_digest"]
    assert current["metadata_history"][0]["title"] == "RAG"
    with pytest.raises(service.PublishBlocked) as exc:
        confirm(wid, old)
    assert exc.value.code == "PUBLISH_CONFIRMATION_STALE"
    assert not adapter.submitted


@pytest.mark.parametrize("failure_state", ["failed", "awaiting_login"])
def test_proven_no_click_failure_can_resume_with_new_confirmation(ctx, failure_state):
    from app.publishing.adapters import AdapterError
    wid, _, adapter, _ = ctx
    clicks = []
    def blocked_before_click(**kwargs):
        raise AdapterError("DOUYIN_ACCOUNT_CHANGED", "投稿账号已变化", state=failure_state,
                           detail={"submission_attempted": False})
    adapter.submit = blocked_before_click
    service.start_task(wid)
    ready = await_state(wid, {"ready_to_confirm"})
    confirm(wid, ready)
    failed = await_state(wid, {"failed", "awaiting_login", "needs_verification"})
    assert failed["state"] == failure_state
    assert failed["attempt_started_at"] is None
    assert failed["confirmation_digest"] is None
    assert failed["submission_attempts"][-1]["phase"] == "pre_submit_checks"
    assert failed["submission_attempts"][-1]["submission_attempted"] is False
    assert failed["confirmation_history"][-1]["confirmation_digest"] == ready["confirmation_digest"]
    assert st.get("resource_locks", service._browser_key(failed["session_dir"])) is None
    assert clicks == []
    def successful_click(**kwargs):
        clicks.append(kwargs["task_id"])
        return {"state": "submitted", "provider_ref": "mock-final-receipt"}
    adapter.submit = successful_click
    service.resume_task(wid, task_id=ready["id"])
    resumed = await_state(wid, {"ready_to_confirm"})
    with pytest.raises(service.PublishBlocked):
        confirm(wid, ready)
    assert clicks == []
    confirm(wid, resumed)
    done = await_state(wid, {"submitted"})
    assert clicks == [ready["id"]]
    assert len(done["confirmation_history"]) == 2
    assert len(done["submission_attempts"]) == 2


@pytest.mark.parametrize("marker", [None, True, "false", 0])
def test_only_explicit_boolean_false_allows_recovery_before_click(ctx, marker):
    from app.publishing.adapters import AdapterError
    wid, _, adapter, _ = ctx
    def uncertain(**kwargs):
        detail = {} if marker is None else {"submission_attempted": marker}
        raise AdapterError("DOUYIN_BROWSER_ERROR", "页面状态不确定", state="failed", detail=detail)
    adapter.submit = uncertain
    service.start_task(wid)
    ready = await_state(wid, {"ready_to_confirm"})
    confirm(wid, ready)
    unknown = await_state(wid, {"needs_verification", "failed"})
    assert unknown["state"] == "needs_verification"
    assert unknown["attempt_started_at"]
    with pytest.raises(service.PublishBlocked):
        service.resume_task(wid, task_id=ready["id"])
