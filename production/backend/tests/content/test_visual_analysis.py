"""视觉分析任务：费用门禁、成功落子快照、未知结果不重发、幂等重放、活动写任务互斥。"""
import io
import json
import subprocess
from uuid import uuid4

import pytest

from app import storage as st
from app.content import media, tasks, visual
from app.content import repository as repo
from app.content.errors import ContentRejected
from app.video import ffmpeg
from tests.content._helpers import set_env


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("AVATAR_STUDIO_QWEN_API_KEY", "sk-test-q")


@pytest.fixture(scope="module")
def mp4():
    import tempfile, os
    d = tempfile.mkdtemp(dir=os.environ.get("TEMP"))
    out = os.path.join(d, "c.mp4")
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x568:rate=10:duration=3", "-pix_fmt", "yuv420p", out], check=True,
                   capture_output=True)
    return open(out, "rb").read()


def setup_work(mp4, route="short_video_storyboard"):
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "brief": {"topic": "舞蹈", "content_route": route},
                          "version": 1, "content_revision": 0})
    ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "1", "fetched_at": "t",
           "extraction_version": "redfox-transcript-v1", "transcript": {"state": "failed", "error": "无口播"}}
    with st.transaction() as c:
        repo.save_reference(c, ref)
    asset = media.save_uploaded_video(wid, ref["id"], io.BytesIO(mp4), "d.mp4", "video/mp4")
    return wid, ref["id"], asset


def model_reply(duration_ms):
    body = {"summary": "竖屏舞蹈", "speech_presence": "none", "music_presence": "music", "warnings": [],
            "scenes": [{"index": 1, "start_ms": 0, "end_ms": duration_ms, "shot_type": "full",
                        "camera_motion": "static", "description": "舞蹈", "actions": ["转身"], "on_screen_text": [],
                        "beat_cues_ms": [], "confidence": 0.8}]}
    return json.dumps({"id": "vq-1", "model": "qwen3-vl-flash", "choices": [{"message": {"content": json.dumps(body)},
                                                                            "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 2000, "completion_tokens": 400}}).encode()


class Transport:
    def __init__(self, duration_ms, exc=None):
        self.calls, self.duration_ms, self.exc = 0, duration_ms, exc

    def __call__(self, url, headers, body, timeout):
        self.calls += 1
        if self.exc:
            raise self.exc
        return 200, model_reply(self.duration_ms)


def confirm(wid, rid, asset, fen=80):
    return visual.create_confirmation(wid, rid, asset.id, ceiling_fen=fen)


def visual_calls(job_id):
    return [c for c in tasks.jobs_calls(job_id)]


def test_default_request_id_changes_with_model_and_analysis_version(monkeypatch):
    from app.content.providers import qwen_vision as qv
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", "qwen3-vl-flash")
    original = visual.request_id_for("reference", "sha256:media")
    assert visual.request_id_for("reference", "sha256:media") == original
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_MODEL", "qwen3-vl-plus")
    upgraded = visual.request_id_for("reference", "sha256:media")
    assert upgraded != original
    monkeypatch.setattr(qv, "ANALYSIS_VERSION", "visual-test-future")
    assert visual.request_id_for("reference", "sha256:media") != upgraded


def test_no_confirmation_never_sends_and_bad_ceiling_rejected(mp4):
    wid, rid, asset = setup_work(mp4)
    with pytest.raises(ContentRejected) as e:
        visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id="")
    assert e.value.code == "VISUAL_COST_CONFIRMATION_REQUIRED"
    for fen in (-1, 0, 79):
        with pytest.raises(ContentRejected) as e:
            confirm(wid, rid, asset, fen)
        assert e.value.code == "COST_AMOUNT_INVALID"
    with pytest.raises(ContentRejected) as e:
        visual.create_confirmation(wid, rid, asset.id, ceiling_fen=80, actor_id="agent")
    assert e.value.code == "COST_ACTOR_INVALID"


@pytest.mark.parametrize("per_flow", ["0", "300"])
def test_quote_has_no_system_confirmation_ceiling(mp4, monkeypatch, per_flow):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", per_flow)
    wid, rid, asset = setup_work(mp4)
    q = visual.quote(wid, rid, asset.id)
    assert q["max_ceiling_fen"] is None
    assert q["reserve_fen"] == 80


@pytest.mark.parametrize("per_flow", ["0", "300"])
@pytest.mark.parametrize("ceiling_fen", [500, 10000])
def test_user_confirmation_can_exceed_legacy_flow_budget(mp4, monkeypatch, per_flow, ceiling_fen):
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_BUDGET_PER_FLOW_FEN", per_flow)
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset, ceiling_fen)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    assert conf["ceiling_fen"] == ceiling_fen
    assert job["payload"]["total_ceiling_fen"] == ceiling_fen
    assert conf["request_digest"] == visual.quote(wid, rid, asset.id)["request_digest"]


def test_increased_reserve_requires_new_user_confirmation_before_send(mp4, monkeypatch):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    monkeypatch.setenv("AVATAR_STUDIO_CONTENT_VISUAL_RESERVE_FEN", "81")
    t = Transport(asset.duration_ms)
    with pytest.raises(ContentRejected) as e:
        visual.handle_visual_analysis(job, transport=t)
    assert e.value.code == "COST_AMOUNT_INVALID"
    assert t.calls == 0 and visual_calls(job["id"]) == []


def test_success_creates_immutable_child_snapshot_and_keeps_parent(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    t = Transport(asset.duration_ms)
    tasks.set_progress(job["id"], state="running")
    visual.handle_visual_analysis(job, transport=t)
    done = tasks.get_job(job["id"])
    assert done["state"] == "succeeded" and t.calls == 1, done.get("error")
    refs = done["result_refs"]
    child = repo.get_reference(refs["reference_id"])
    assert child["parent_id"] == rid and child["visual"]["state"] == "ready" and child["media_asset_id"] == asset.id
    assert child["visual"]["model"] == "qwen3-vl-flash" and child["visual"]["media_sha256"] == asset.sha256
    assert child["visual"]["coverage"][0]["frame_count"] >= 3
    assert child["extraction_version"].endswith(visual.EXTRACTION_SUFFIX) and child["transcript"]["state"] == "failed"
    span = child["visual"]["coverage"][0]
    assert span["first_frame_ms"] == 0 and span["last_frame_ms"] < asset.duration_ms
    assert span["max_frame_gap_ms"] <= 500
    assert repo.get_reference(rid)["visual"] == {}  # 原参考不变
    assert st.get("works", wid).get("reference_snapshot_id") is None  # 不擅自换主参考
    call = visual_calls(job["id"])[0]
    assert call["state"] == "succeeded" and call["reserved_fen"] == 80 and call["provider"] == "qwen"


def test_replay_returns_same_job_and_never_pays_twice(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    a = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    b = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    assert b["id"] == a["id"] and b["replayed"] is True
    t = Transport(asset.duration_ms)
    tasks.set_progress(a["id"], state="running")
    visual.handle_visual_analysis(a, transport=t)
    visual.handle_visual_analysis(a, transport=t)  # 续跑：同一幂等键只取回原结果
    assert t.calls == 1 and len(visual_calls(a["id"])) == 1


def test_unknown_result_is_kept_and_never_resent(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    t = Transport(asset.duration_ms, exc=TimeoutError("boom"))
    tasks.set_progress(job["id"], state="running")
    try:
        visual.handle_visual_analysis(job, transport=t)
    except Exception as exc:  # noqa: BLE001
        tasks.run_job_error(job, exc)
    assert tasks.get_job(job["id"])["state"] == "unknown" and t.calls == 1
    tasks.set_progress(job["id"], state="running")  # 终态不可改
    ok = Transport(asset.duration_ms)
    try:
        visual.handle_visual_analysis(job, transport=ok)
    except Exception as exc:  # noqa: BLE001
        assert isinstance(exc, tasks.CallPending)
    assert ok.calls == 0 and len(visual_calls(job["id"])) == 1
    assert visual_calls(job["id"])[0]["state"] == "unknown"


def test_invalid_model_output_fails_without_ready_snapshot(mp4):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    bad = lambda *a: (200, json.dumps({"id": "x", "model": "m", "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode())
    tasks.set_progress(job["id"], state="running")
    try:
        visual.handle_visual_analysis(job, transport=bad)
    except Exception as exc:  # noqa: BLE001
        tasks.run_job_error(job, exc)
    done = tasks.get_job(job["id"])
    assert done["state"] == "failed" and done["error"]["code"] == "MODEL_OUTPUT_INVALID"
    assert [r for r in repo.list_references(wid) if r.get("visual")] == []


def test_missing_key_is_not_sent_and_active_write_job_blocks_submission(mp4, monkeypatch):
    wid, rid, asset = setup_work(mp4)
    conf = confirm(wid, rid, asset)
    monkeypatch.delenv("AVATAR_STUDIO_QWEN_API_KEY")
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=conf["id"])
    t = Transport(asset.duration_ms)
    tasks.set_progress(job["id"], state="running")
    try:
        visual.handle_visual_analysis(job, transport=t)
    except Exception as exc:  # noqa: BLE001
        tasks.run_job_error(job, exc)
    assert t.calls == 0 and tasks.get_job(job["id"])["state"] == "blocked"
    other, orid, oasset = setup_work(mp4)
    tasks.submit_job(other, "generation", "g1", {"prompt_id": "p"})
    oconf = confirm(other, orid, oasset)
    with pytest.raises(tasks.FlowActive):
        visual.submit_visual_analysis(other, orid, oasset.id, confirmation_id=oconf["id"])


def test_missing_media_is_reference_media_unavailable(mp4):
    wid, rid, asset = setup_work(mp4)
    with pytest.raises(ContentRejected) as e:
        visual.quote(wid, rid, "")
    assert e.value.code == "REFERENCE_MEDIA_UNAVAILABLE"


# ---------------------------------------------------------------- API

def test_visual_api_flow_and_errors(mp4, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(tasks.Scheduler, "start", lambda self: None)
    client = TestClient(app)
    wid, rid, asset = setup_work(mp4)
    base = f"/api/v1/works/{wid}/content"
    body = {"request_id": "v1", "reference_snapshot_id": rid, "media_asset_id": asset.id,
            "expected_content_revision": 0, "paid_ack": False, "total_ceiling_fen": 80}

    def code(r):
        b = r.json()
        return (b.get("error") or b["detail"]["error"])["code"]

    assert code(client.post(base + "/visual-analyses", json=body)) == "VISUAL_COST_CONFIRMATION_REQUIRED"
    assert code(client.post(base + "/visual-analyses", json=dict(body, paid_ack=True, total_ceiling_fen=0))) == "COST_AMOUNT_INVALID"
    assert code(client.post(base + "/visual-analyses", json=dict(body, paid_ack=True, total_ceiling_fen=79))) == "COST_AMOUNT_INVALID"
    other, orid, oasset = setup_work(mp4)
    cross = client.post(base + "/visual-analyses", json=dict(body, paid_ack=True, media_asset_id=oasset.id))
    assert code(cross) == "MEDIA_NOT_FOUND"
    ok = client.post(base + "/visual-analyses", json=dict(body, paid_ack=True))
    assert ok.status_code == 202 and ok.json()["kind"] == "visual_analysis"
    again = client.post(base + "/visual-analyses", json=dict(body, paid_ack=True))
    assert again.json()["job_id"] == ok.json()["job_id"] and again.json()["replayed"] is True
    changed = client.post(base + "/visual-analyses", json=dict(body, paid_ack=True, total_ceiling_fen=90))
    assert changed.status_code == 409 and code(changed) == "IDEMPOTENCY_CONFLICT"
    refresh = client.post(f"{base}/jobs/{ok.json()['job_id']}/refresh")
    assert refresh.status_code == 200 and refresh.json()["status"] in ("nothing_to_refresh", "needs_verification")
    state = client.get(base + "/state").json()
    assert state["references"][0]["has_video_url"] is False and state["references"][0]["visual_state"] == "none"
    caps = client.get("/api/v1/content/capabilities").json()["visual"]
    assert caps["model"] and "api_key" not in json.dumps(caps) and "sk-test" not in json.dumps(caps)
