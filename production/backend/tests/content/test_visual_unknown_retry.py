"""未知视觉调用只能经用户新授权发送一次；全部供应商响应均为离线 transport。"""
import json

import pytest

from app import storage as st
from app.content import tasks, visual
from app.content.errors import ContentRejected
from app.content_api import receipt
from tests.content.test_visual_analysis import Transport, confirm, env, mp4, setup_work, visual_calls  # noqa: F401


def unknown(wid, rid, asset, request_id="original", confirmation=None):
    conf = confirmation or confirm(wid, rid, asset)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"], request_id=request_id)
    tasks.set_progress(job["id"], state="running")
    provider = Transport(asset.duration_ms, exc=TimeoutError("offline"))
    try:
        visual.handle_visual_analysis(job, transport=provider)
    except tasks.CallPending as exc:
        tasks.run_job_error(job, exc)
    return job, visual_calls(job["id"])[0], conf


def authorize(wid, rid, asset, call):
    return visual.create_confirmation(wid, rid, asset.id, ceiling_fen=80,
                                      retry_unknown_call_id=call["id"], retry_paid_ack=True)


def api_code(response):
    body = response.json()
    return (body.get("error") or body["detail"]["error"])["code"]


def test_quote_identifies_unknown_without_changing_original(mp4):
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    before = st.get("provider_jobs", call["id"])
    assert visual.quote(wid, rid, asset.id)["retry_unknown_call"] == {
        "call_id": call["id"], "sent_at": call["sent_at"], "reserved_fen": 80}
    assert st.get("provider_jobs", call["id"]) == before


def test_normal_confirmation_never_authorizes_unknown_retry(mp4):
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    fresh = confirm(wid, rid, asset)
    retry = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=fresh["id"], request_id="ordinary")
    provider = Transport(asset.duration_ms)
    tasks.set_progress(retry["id"], state="running")
    with pytest.raises(tasks.CallPending) as exc:
        visual.handle_visual_analysis(retry, transport=provider)
    assert exc.value.code == "VISUAL_PREVIOUS_CALL_UNKNOWN"
    assert exc.value.detail == call["id"] and provider.calls == 0


def test_old_unknown_receipt_and_new_unsent_receipt_are_distinct(mp4):
    wid, rid, asset = setup_work(mp4)
    old, call, _ = unknown(wid, rid, asset)
    err = {"code": "CONTENT_CALL_PENDING", "message": "旧版提示", "detail": call["id"]}
    old = dict(old, state="unknown", error=err)
    new = dict(old, id="unsent-parent")
    assert receipt(old, wid)["error"]["code"] == "PROVIDER_RESPONSE_TIMEOUT"
    out = receipt(new, wid)["error"]
    assert out["code"] == "VISUAL_PREVIOUS_CALL_UNKNOWN" and "本次未发送" in out["message"]


def test_retry_requires_new_explicit_unknown_fee_ack(mp4):
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    with pytest.raises(ContentRejected) as exc:
        visual.create_confirmation(wid, rid, asset.id, ceiling_fen=80, retry_unknown_call_id=call["id"])
    assert exc.value.code == "VISUAL_RETRY_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("change", ["foreign", "different-reference", "different-model", "unsent", "active", "missing"])
def test_retry_rejects_invalid_source_before_creating_authorization(mp4, monkeypatch, change):
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    if change == "foreign":
        wid, rid, asset = setup_work(mp4)
    elif change == "different-reference":
        ref = dict(st.get("content_references", rid), id=rid + "_new")
        st.put("content_references", ref["id"], ref, {"work_id": wid})
        rid = ref["id"]
    elif change == "different-model":
        monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", "qwen3-vl-future")
    elif change in ("unsent", "active"):
        changed = dict(call, state="queued" if change == "active" else "unknown",
                       sent_at=call["sent_at"] if change == "active" else None,
                       attempts=1 if change == "active" else 0)
        st.put("provider_jobs", call["id"], changed, {"work_id": wid})
    else:
        call = dict(call, id="missing")
    before = len(st.all_rows("cost_confirmations", "work_id=?", [wid]))
    with pytest.raises(ContentRejected) as exc:
        authorize(wid, rid, asset, call)
    assert exc.value.code == "VISUAL_RETRY_SOURCE_INVALID"
    assert len(st.all_rows("cost_confirmations", "work_id=?", [wid])) == before


def test_authorized_retry_sends_once_preserves_unknown_and_blocks_reuse(mp4):
    wid, rid, asset = setup_work(mp4)
    _, old, ordinary = unknown(wid, rid, asset)
    before = st.get("provider_jobs", old["id"])
    auth = authorize(wid, rid, asset, old)
    assert auth["id"] != ordinary["id"]
    retry = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="retry")
    tasks.set_progress(retry["id"], state="running")
    provider = Transport(asset.duration_ms)
    visual.handle_visual_analysis(retry, transport=provider)
    visual.handle_visual_analysis(retry, transport=provider)
    assert provider.calls == 1 and len(visual_calls(retry["id"])) == 1
    assert st.get("provider_jobs", old["id"]) == before
    replay = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="retry")
    assert replay["id"] == retry["id"] and replay["replayed"]
    with pytest.raises(ContentRejected) as exc:
        visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="another")
    assert exc.value.code in {"VISUAL_RETRY_AUTHORIZATION_USED", "VISUAL_ALREADY_ANALYZED"}
    with pytest.raises(ContentRejected) as exc:
        authorize(wid, rid, asset, old)
    assert exc.value.code == "VISUAL_ALREADY_ANALYZED"


def test_newer_unknown_requires_fresh_source_and_new_authorization(mp4):
    wid, rid, asset = setup_work(mp4)
    _, first, _ = unknown(wid, rid, asset)
    auth = authorize(wid, rid, asset, first)
    _, second, _ = unknown(wid, rid, asset, request_id="retry-1", confirmation=auth)
    assert visual.quote(wid, rid, asset.id)["retry_unknown_call"]["call_id"] == second["id"]
    with pytest.raises(ContentRejected) as exc:
        authorize(wid, rid, asset, first)
    assert exc.value.code == "VISUAL_RETRY_SOURCE_INVALID"
    fresh = authorize(wid, rid, asset, second)
    retry = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=fresh["id"], request_id="retry-2")
    tasks.set_progress(retry["id"], state="running")
    provider = Transport(asset.duration_ms)
    visual.handle_visual_analysis(retry, transport=provider)
    assert provider.calls == 1
    assert st.get("provider_jobs", first["id"])["state"] == "unknown"
    assert st.get("provider_jobs", second["id"])["state"] == "unknown"


def test_source_changes_after_confirmation_block_before_send(mp4):
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    auth = authorize(wid, rid, asset, call)
    st.put("provider_jobs", call["id"], dict(call, state="waiting_external"), {"work_id": wid})
    with pytest.raises(ContentRejected) as exc:
        visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="retry")
    assert exc.value.code == "VISUAL_RETRY_SOURCE_INVALID"


def test_api_explicit_retry_is_idempotent_even_after_completion(mp4, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    client = TestClient(app)
    wid, rid, asset = setup_work(mp4)
    _, call, _ = unknown(wid, rid, asset)
    base = f"/api/v1/works/{wid}/content"
    body = {"request_id": "api-retry", "reference_snapshot_id": rid, "media_asset_id": asset.id,
            "expected_content_revision": 0, "paid_ack": True, "total_ceiling_fen": 80,
            "retry_unknown_call_id": call["id"], "retry_paid_ack": False}
    denied = client.post(base + "/visual-analyses", json=body)
    assert denied.status_code == 422
    assert api_code(denied) == "VISUAL_RETRY_CONFIRMATION_REQUIRED"
    body["retry_paid_ack"] = True
    first = client.post(base + "/visual-analyses", json=body)
    assert first.status_code == 202, first.text
    job = tasks.get_job(first.json()["job_id"])
    tasks.set_progress(job["id"], state="running")
    provider = Transport(asset.duration_ms)
    visual.handle_visual_analysis(job, transport=provider)
    count = len(st.all_rows("cost_confirmations", "work_id=?", [wid]))
    replay = client.post(base + "/visual-analyses", json=body)
    assert replay.status_code == 202 and replay.json()["job_id"] == job["id"]
    assert replay.json()["replayed"] and provider.calls == 1
    assert len(st.all_rows("cost_confirmations", "work_id=?", [wid])) == count
    changed = client.post(base + "/visual-analyses", json=dict(body, total_ceiling_fen=81))
    assert changed.status_code == 409
    assert api_code(changed) == "IDEMPOTENCY_CONFLICT"


def test_successful_retry_can_be_reused_without_another_payment(mp4):
    wid, rid, asset = setup_work(mp4)
    _, old, _ = unknown(wid, rid, asset)
    auth = authorize(wid, rid, asset, old)
    retry = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="retry")
    tasks.set_progress(retry["id"], state="running")
    visual.handle_visual_analysis(retry, transport=Transport(asset.duration_ms))
    fresh = confirm(wid, rid, asset)
    reuse = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=fresh["id"], request_id="reuse")
    tasks.set_progress(reuse["id"], state="running")
    provider = Transport(asset.duration_ms)
    visual.handle_visual_analysis(reuse, transport=provider)
    assert tasks.get_job(reuse["id"])["state"] == "succeeded" and provider.calls == 0
    assert visual_calls(reuse["id"]) == []
    assert st.get("provider_jobs", old["id"])["state"] == "unknown"


def test_frame_changes_after_authorization_cannot_bypass_unknown(mp4, monkeypatch):
    from app.content import visual_pipeline as vp
    wid, rid, asset = setup_work(mp4)
    _, old, _ = unknown(wid, rid, asset)
    auth = authorize(wid, rid, asset, old)
    retry = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="retry")
    original = vp.sample_frames
    monkeypatch.setattr(vp, "sample_frames", lambda *args: [dict(f, sha256="new-frame") for f in original(*args)])
    tasks.set_progress(retry["id"], state="running")
    provider = Transport(asset.duration_ms)
    with pytest.raises(ContentRejected) as exc:
        visual.handle_visual_analysis(retry, transport=provider)
    assert exc.value.code == "VISUAL_RETRY_SOURCE_INVALID" and provider.calls == 0


def test_failed_job_submission_releases_unsent_authorization(mp4, monkeypatch):
    wid, rid, asset = setup_work(mp4)
    _, old, _ = unknown(wid, rid, asset)
    auth = authorize(wid, rid, asset, old)
    submit = tasks.submit_job
    monkeypatch.setattr(tasks, "submit_job", lambda *a, **kw: (_ for _ in ()).throw(tasks.FlowActive("busy")))
    with pytest.raises(tasks.FlowActive):
        visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="unsent")
    assert not st.get("cost_confirmations", auth["id"]).get("consumed_request_id")
    monkeypatch.setattr(tasks, "submit_job", submit)
    assert visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=auth["id"], request_id="new")["state"] == "queued"


def test_nonvisual_unknown_receipt_preserves_its_message():
    wid = "nonvisual-work"
    call = {"id": "nonvisual-call", "work_id": wid, "state": "unknown", "capability": "transcription",
            "parent_content_job_id": "original", "unknown_reason": "TimeoutError: offline"}
    st.put("provider_jobs", call["id"], call, {"work_id": wid})
    err = {"code": "CONTENT_CALL_PENDING", "message": "transcription pending", "detail": call["id"]}
    job = {"id": "new", "work_id": wid, "kind": "import", "state": "unknown", "error": err}
    assert receipt(job, wid)["error"] == err


def test_concurrent_api_retry_with_same_request_id_returns_one_parent(mp4, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    wid, rid, asset = setup_work(mp4)
    _, old, _ = unknown(wid, rid, asset)
    body = {"request_id": "parallel", "reference_snapshot_id": rid, "media_asset_id": asset.id,
            "expected_content_revision": 0, "paid_ack": True, "total_ceiling_fen": 80,
            "retry_unknown_call_id": old["id"], "retry_paid_ack": True}
    gate = Barrier(2)
    submit = tasks.submit_job

    def parallel_submit(*args, **kwargs):
        if args[2] == "parallel":
            gate.wait(timeout=10)
        return submit(*args, **kwargs)

    monkeypatch.setattr(tasks, "submit_job", parallel_submit)
    client = TestClient(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(f"/api/v1/works/{wid}/content/visual-analyses", json=body), range(2)))
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json()["job_id"] == responses[1].json()["job_id"]
    assert len([r for r in tasks.list_jobs(wid) if r["request_id"] == "parallel"]) == 1
    assert len(st.all_rows("provider_jobs", "work_id=?", [wid])) == 1


def test_api_supplied_confirmation_ceiling_must_match_request(mp4, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset, 800)
    response = TestClient(app).post(f"/api/v1/works/{wid}/content/visual-analyses", json={
        "request_id": "small-fee", "reference_snapshot_id": rid, "media_asset_id": asset.id,
        "expected_content_revision": 0, "paid_ack": True, "total_ceiling_fen": 80, "confirmation_id": conf["id"]})
    assert response.status_code == 422 and api_code(response) == "COST_AMOUNT_INVALID"
    assert tasks.list_jobs(wid) == []
