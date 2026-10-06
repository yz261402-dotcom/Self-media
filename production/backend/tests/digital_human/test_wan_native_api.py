"""Native Wan API user path; isolated database and no provider requests."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, storage as st
from app.digital_human import routes, wan
from dh_helpers import prepare_work


def bare_work():
    work = prepare_work(route="WAN_3_0", audio_ms=75000)
    for key in ("identity_id", "variant_id", "final_audio_id", "voice_asset_id",
                "script_version_id", "performance_plan_id"):
        work[key] = None
    st.put("works", work["id"], work)
    return work


def test_native_submit_dispatches_without_tts_and_passes_duration(monkeypatch):
    work = bare_work()
    monkeypatch.setattr(routes, "reject_text_to_video", lambda _: pytest.fail("native Wan does not need TTS"))
    monkeypatch.setattr(routes, "assert_route_selected", lambda *args: "WAN_3_0")
    calls = []
    monkeypatch.setattr(wan, "submit_wan", lambda *a, **kw: calls.append(kw) or {"submitted": False})
    main.talking_head_submit(work["id"], {"duration_seconds": 30, "prompt": "雨中的城市，雨声", "dry_run": True})
    assert calls[0]["duration_seconds"] == 30


def test_native_cost_view_passes_selected_seconds(monkeypatch):
    work = bare_work()
    calls = []
    monkeypatch.setattr(main.dh_cost, "cost_summary", lambda *a, **kw: calls.append(kw) or {})
    main.talking_head_cost(work["id"], prompt="雨声", duration_seconds=30)
    assert calls[0]["duration_seconds"] == 30


def test_native_legacy_estimate_has_price_without_tts(monkeypatch):
    work = bare_work()
    calls = []
    def estimate(*args, **kwargs):
        calls.append(kwargs)
        return {"estimated_fen": 1800, "ceiling_fen": 2160, "price_basis": "30秒720P", "billable": True, "note": "预计"}
    monkeypatch.setattr(main.dh_cost, "estimate", estimate)
    view = main.cost_estimate({"work_id": work["id"], "route": "WAN_3_0", "duration_seconds": 30})
    assert view.estimated_fen == 1800
    assert calls[0]["duration_seconds"] == 30


def test_native_confirmation_binds_explicit_seconds(monkeypatch):
    work = bare_work()
    seen = []
    listing = {"digest": "native30", "segment_count": 1, "generated_seconds": 30}
    monkeypatch.setattr(main.dh_sub, "current_call_list", lambda *a, **kw: seen.append(("list", kw)) or listing)
    monkeypatch.setattr(main.dh_cost, "estimate", lambda *a, **kw: seen.append(("estimate", kw)) or {
        "estimated_fen": 1800, "ceiling_fen": 2160, "generated_seconds": 30, "audio_seconds": 0})
    monkeypatch.setattr(main.dh_cost, "confirm_cost", lambda *a, **kw: kw)
    result = main.talking_head_confirm_cost(work["id"], {"duration_seconds": 30, "prompt": "雨声"})
    assert result["generated_seconds"] == 30
    assert all(kwargs.get("duration_seconds") == 30 for _, kwargs in seen)


def image_bytes(mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, (320, 480), "white").save(out, format="PNG")
    return out.getvalue()


def test_wan_upload_is_real_picture_bound_to_work(tmp_path, monkeypatch):
    from app import wan_api
    monkeypatch.setattr(wan_api, "IMAGE_ROOT", wan_api.IMAGE_ROOT / st.new_id("test"))
    work = bare_work()
    client = TestClient(main.app)
    response = client.post(f"/api/v1/works/{work['id']}/wan/image", content=image_bytes(),
                           headers={"Content-Type": "image/png", "X-Filename": "scene.png"})
    assert response.status_code == 200, response.text
    source = response.json()["source"]
    assert source["asset_hash"].startswith("sha256:")
    assert source["width"] == 320
    assert st.get("works", work["id"])["wan_image"]["asset_id"] == source["asset_id"]
    assert client.get(f"/api/v1/works/{work['id']}/wan/image").json()["source"]["asset_id"] == source["asset_id"]


@pytest.mark.parametrize("body", [b"not an image", b"", image_bytes("RGBA")], ids=["invalid", "empty", "transparent"])
def test_wan_upload_rejects_invalid_picture_without_changing_work(body):
    work = bare_work()
    response = TestClient(main.app).post(f"/api/v1/works/{work['id']}/wan/image", content=body,
                                         headers={"Content-Type": "image/png"})
    assert response.status_code in (413, 422), response.text
    assert not st.get("works", work["id"]).get("wan_image")


def test_native_confirmation_endpoint_delegates_user_ack(monkeypatch):
    from app.digital_human import wan_native
    work = bare_work()
    calls = []
    monkeypatch.setattr(wan_native, "confirm_and_deliver", lambda *a, **kw: calls.append((a, kw)) or {"ok": True})
    response = TestClient(main.app).post(f"/api/v1/works/{work['id']}/wan/videos/v1/confirm",
                                         json={"actor_id": "user_ui", "review_ack": True})
    assert response.status_code == 200, response.text
    assert calls[0][1]["review_ack"] is True


def test_choose_wan_creates_preparation_task_without_audio_or_schedule(monkeypatch):
    work = bare_work()
    work["current_task_id"] = None
    st.put("works", work["id"], work)
    monkeypatch.setattr(main.engine, "schedule", lambda *_: pytest.fail("route selection cannot start providers"))
    main.confirm_route(work["id"], {"route": "WAN_3_0", "cost_notice_ack": True})
    task = main._current_task(work["id"])
    assert task and task["route_confirmed"]
    assert task["stage"] == "talking_head" and task["state"] == "waiting_selection"


def test_native_wait_does_not_compare_old_tts_snapshot(monkeypatch):
    from app.digital_human import wan_inputs
    work = prepare_work(route="WAN_3_0", audio_ms=75000)
    task = main._current_task(work["id"])
    monkeypatch.setattr(wan_inputs, "resolve_source", lambda _: {"asset_id": "img1", "asset_hash": "hash1"})
    run = {"id": st.new_id("run"), "work_id": work["id"], "task_id": task["id"],
           "route": "WAN_3_0", "provider": "dashscope_wan", "state": "waiting_provider",
           "capability": "short_video_cloud", "provider_params": {
               "generation_mode": "image_prompt_native_audio", "sound_mode": "native_audio",
               "input_snapshot": {"work_id": work["id"], "source_asset_id": "img1", "source_asset_hash": "hash1",
                                  "identity_id": None, "audio_asset_id": None}}}
    st.put("provider_runs", run["id"], run, {"work_id": work["id"]})
    assert main.engine.talking_head_wait(task, work)["code"] == "ORIGINAL_PENDING"
