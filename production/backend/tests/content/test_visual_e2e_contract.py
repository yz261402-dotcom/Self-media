"""视觉参考端到端契约：上传 → 探测 → 抽帧 → mock 视觉 → 子快照 → 分镜路线选主参考 → 生成分镜提示。
另覆盖无音轨/纯音乐、未知结果只查不重发、媒体不可访问。真实 ffmpeg，模型用 mock transport。"""
import io
import subprocess
from uuid import uuid4

import pytest

from app import storage as st
from app.content import media, prompts, references, service, tasks, visual
from app.content import repository as repo
from app.content.errors import ContentRejected
from app.video import ffmpeg
from tests.content._helpers import Script, plan_json, run_job, set_env
from tests.content.test_visual_analysis import Transport, confirm, visual_calls


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))


def make_clip(tmp_path_factory_dir, with_audio):
    out = tmp_path_factory_dir / ("a.mp4" if with_audio else "n.mp4")
    cmd = [ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=10:duration=10"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=330:duration=10", "-shortest"]
    else:
        cmd += ["-an"]
    subprocess.run(cmd + ["-pix_fmt", "yuv420p", str(out)], check=True, capture_output=True)
    return out.read_bytes()


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("e2e")
    return {"music": make_clip(d, True), "silent": make_clip(d, False)}


def setup(clip):
    wid = "work_" + uuid4().hex
    st.put("works", wid, {"id": wid, "name": "t", "version": 1, "content_revision": 0,
                          "brief": {"topic": "舞蹈教学", "audience": "年轻人", "target_duration_ms": 10000,
                                    "content_route": "short_video_storyboard"}})
    ref = {"id": "ref_" + uuid4().hex[:8], "work_id": wid, "platform_work_id": "5", "caption": "BGM 舞蹈",
           "fetched_at": "t", "extraction_version": "redfox-transcript-v1",
           "transcript": {"state": "failed", "error": "转写成功但正文为空（可能是纯音乐/无口播）"}}
    with st.transaction() as c:
        repo.save_reference(c, ref)
    asset = media.save_uploaded_video(wid, ref["id"], io.BytesIO(clip), "dance.mp4", "video/mp4")
    return wid, ref["id"], asset


def analyze(wid, rid, asset, transport):
    job = visual.submit_visual_analysis(wid, rid, asset.id, confirmation_id=confirm(wid, rid, asset)["id"])
    tasks.set_progress(job["id"], state="running")
    try:
        visual.handle_visual_analysis(job, transport=transport)
    except Exception as exc:  # noqa: BLE001
        tasks.run_job_error(job, exc)
    return tasks.get_job(job["id"])


@pytest.mark.parametrize("kind", ["music", "silent"])
def test_dance_reference_end_to_end_for_music_and_silent_video(clips, kind):
    wid, rid, asset = setup(clips[kind])
    assert 9000 <= asset.duration_ms <= 11000
    done = analyze(wid, rid, asset, Transport(asset.duration_ms))
    assert done["state"] == "succeeded", done.get("error")  # 无音轨/纯音乐也不影响视觉任务
    child = repo.get_reference(done["result_refs"]["reference_id"])
    assert child["visual"]["state"] == "ready" and child["transcript"]["state"] == "failed"
    assert child["visual"]["coverage"][0]["frame_count"] >= 5
    if kind == "silent":
        assert child["visual"]["speech_presence"] == "none" and child["visual"]["audio"]["tempo_bpm"] is None
    # 口播路线仍拒绝无转写参考；分镜路线可选视觉 ready 的新快照
    with pytest.raises(ContentRejected) as e:
        references.select_reference(wid, child["id"], 0, content_route="talking_script")
    assert e.value.code == "REFERENCE_TRANSCRIPT_REQUIRED"
    work = references.select_reference(wid, child["id"], 0)
    assert work["reference_snapshot_id"] == child["id"]
    # 生成分镜提示：planner 收到画面观察块，且不把视觉当事实资料
    s = Script()
    s.q["planner"].append(plan_json())
    job = run_job("prompt", wid, {}, service.handle_prepare_prompt, s)
    assert job["state"] == "succeeded", job.get("error")
    seen = s.seen["planner"][0]
    assert prompts.VISUAL_HEADING in seen and "无有效口播转写" in seen and "舞蹈" in seen
    from app.content import context as cx
    assert cx.load_context(wid)["materials"] == []


def test_unknown_model_result_shows_unknown_and_refresh_never_adds_a_provider_job(clips):
    from fastapi.testclient import TestClient
    from app.main import app
    wid, rid, asset = setup(clips["music"])
    done = analyze(wid, rid, asset, Transport(asset.duration_ms, exc=TimeoutError("net")))
    assert done["state"] == "unknown"
    client = TestClient(app)
    before = len(visual_calls(done["id"]))
    r = client.post(f"/api/v1/works/{wid}/content/jobs/{done['id']}/refresh")
    assert r.status_code == 200 and r.json()["status"] == "needs_verification"
    assert len(visual_calls(done["id"])) == before == 1
    got = client.get(f"/api/v1/works/{wid}/content/jobs/{done['id']}").json()
    assert got["state"] == "unknown" and "verify" in got["actions"]


def test_media_unavailable_maps_to_stable_code_and_upload_entry(clips):
    wid, rid, asset = setup(clips["music"])
    with pytest.raises(ContentRejected) as e:
        visual.quote(wid, rid, "")
    assert e.value.code == "REFERENCE_MEDIA_UNAVAILABLE"
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).post(f"/api/v1/works/{wid}/content/visual-quotes",
                             json={"reference_snapshot_id": rid, "media_asset_id": ""})
    body = r.json()
    assert r.status_code == 422 and (body.get("error") or body["detail"]["error"])["code"] == "REFERENCE_MEDIA_UNAVAILABLE"
