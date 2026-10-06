"""原生声画从真实短片到下载包；不调用生成服务，不伪造 TTS。"""
import hashlib
import importlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from app import delivery, storage as st
from app.digital_human import gates, videos, worker_client
from app.digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from app.tts import audio as tts_audio
from app.video import ffmpeg


MODE = "image_prompt_native_audio"


def module():
    assert importlib.util.find_spec("app.digital_human.wan_native"), "缺少 Wan 原声原片与交付实现"
    return importlib.import_module("app.digital_human.wan_native")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def native(tmp_path, monkeypatch):
    from PIL import Image
    workspace = Path(tmp_path)
    runtime = workspace / "production/backend/runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(worker_client, "_workspace_root", lambda: workspace)
    monkeypatch.setattr(tts_audio, "runtime_root", lambda: str(runtime))
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime)
    wid, tid, rid = st.new_id("native_work"), st.new_id("task"), st.new_id("run")
    image = workspace / "assets/reference.png"
    image.parent.mkdir()
    Image.new("RGB", (320, 320), "blue").save(image)
    source = {"kind": "wan_image", "work_id": wid, "asset_id": "image_1", "asset_hash": digest(image.read_bytes()),
              "identity_id": None, "variant_id": None, "storage_uri": str(image), "label": "测试图片"}
    work = {"id": wid, "name": "海边风声", "route": "WAN_3_0", "current_task_id": tid,
            "wan_image": source}
    task = {"id": tid, "work_id": wid, "mode": "production", "route": "WAN_3_0",
            "stage": "talking_head", "state": "waiting_review"}
    run = {"id": rid, "task_id": tid, "work_id": wid, "route": "WAN_3_0", "provider": "dashscope_wan",
           "model": "wan3.0-video", "remote_job_id": "remote-native", "state": "submitted", "is_mock": False,
           "request_digest": "native-request", "provider_params": {"generation_mode": MODE,
               "sound_mode": "native_audio", "generated_seconds": 2, "duration_seconds": 2,
               "prompt": "海边的风吹过，浪花拍岸，只有自然环境声。",
               "input_snapshot": {"work_id": wid, "identity_id": None, "variant_id": None,
                                  "source_asset_id": source["asset_id"], "source_asset_hash": source["asset_hash"]}}}
    for table, value in (("works", work), ("tasks", task), ("provider_runs", run)):
        st.put(table, value["id"], value, None if table == "works" else {"work_id": wid})
    path = runtime / "fixture.mp4"
    cp = ffmpeg.run_ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=2",
                           "-f", "lavfi", "-i", "sine=frequency=880:duration=2", "-c:v", "libx264",
                           "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], timeout=30)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")
    return {"runtime": runtime, "raw": path.read_bytes(), "work": work, "task": task,
            "run": run, "source": source, "monkeypatch": monkeypatch}


def prepare(native):
    mod = module()
    return mod.prepare_original(native["run"], {"data": native["raw"], "probe": {"file_hash": "untrusted"}})


def register(native):
    return module().register_original(native["run"]["id"], prepare(native))["video"]


@pytest.mark.parametrize("old_tts", [False, True])
def test_native_confirmation_delivers_identical_bytes_without_tts(native, old_tts):
    if old_tts:
        work = native["work"]
        work.update(final_audio_id="historical_tts", voice_asset_id="voice_old")
        st.put("works", work["id"], work)
        st.put("audios", "historical_tts", {"id": "historical_tts", "work_id": work["id"],
               "kind": "full", "confirmed": True, "approved": True}, {"work_id": work["id"]})
    before_audio = st.all_rows("audios", "work_id=?", [native["work"]["id"]])
    video = register(native)
    assert video.get("generation_mode") == MODE
    assert video.get("audio_asset_id") is None
    assert video.get("confirmed") is False
    assert gates.video_gate(video["id"])["outcome"] == "not_run"
    result = module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)
    assert result["ok"] and result["video"]["confirmed"]
    package = result["package"]
    assert package["state"] == "ready"
    assert gates.video_gate(video["id"])["ok"]
    status = delivery.status(native["work"]["id"])
    assert status["ready_for_review"] and status["preview_url"] and status["cover_url"]
    assert status["task"]["state"] == "succeeded"
    from app.publishing.service import resolve_publish_package
    publish_package, master, cover = resolve_publish_package(native["work"]["id"])
    assert publish_package["id"] == package["id"] and master.read_bytes() == native["raw"] and cover.stat().st_size > 0
    path = delivery.download_path(package["id"])
    with zipfile.ZipFile(path) as bundle:
        assert bundle.read("成片.mp4") == native["raw"]
        assert "字幕.srt" not in bundle.namelist() and "字幕.ass" not in bundle.namelist()
        info = json.loads(bundle.read("作品信息.json"))
        assert info["sound_mode"] == "native_audio" and info["subtitles"] == "not_generated"
        assert json.loads(bundle.read("manifest.json"))["input_assets"]["generation_mode"] == MODE
    assert st.all_rows("audios", "work_id=?", [native["work"]["id"]]) == before_audio
    assert st.get("works", native["work"]["id"]).get("final_audio_id") == ("historical_tts" if old_tts else None)
    again = module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)
    assert again["package"]["id"] == package["id"]


def test_native_requires_real_user_review(native):
    video = register(native)
    for actor, ack in (("agent", True), ("user_local", False)):
        with pytest.raises(TalkingHeadRejected):
            module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id=actor, review_ack=ack)
    assert not st.get("videos", video["id"])["confirmed"]
    assert not st.all_rows("packages", "work_id=?", [native["work"]["id"]])


def test_missing_native_audio_is_rejected_and_raw_preserved(native):
    silent = native["runtime"] / "silent.mp4"
    cp = ffmpeg.run_ffmpeg(["-y", "-i", str(native["runtime"] / "fixture.mp4"), "-an", "-c:v", "copy", str(silent)])
    assert cp.returncode == 0
    native["raw"] = silent.read_bytes()
    with pytest.raises(TalkingHeadBlocked) as error:
        prepare(native)
    assert error.value.code == "WAN_OUTPUT_AUDIO_MISSING"
    assert (native["runtime"] / "wan3" / native["run"]["id"] / "raw.mp4").read_bytes() == native["raw"]


def test_native_rejects_audio_track_longer_than_requested_video(native):
    mismatched = native["runtime"] / "mismatched.mp4"
    cp = ffmpeg.run_ffmpeg(["-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=2",
                            "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
                            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(mismatched)], timeout=30)
    assert cp.returncode == 0
    native["raw"] = mismatched.read_bytes()
    with pytest.raises(TalkingHeadBlocked) as error:
        prepare(native)
    assert error.value.code == "WAN_NATIVE_DURATION_MISMATCH"
    assert (native["runtime"] / "wan3" / native["run"]["id"] / "raw.mp4").read_bytes() == native["raw"]


def test_tts_changes_do_not_revoke_native_video(native):
    from app.tts import gate as tts_gate
    video = register(native)
    confirmed = module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)
    assert confirmed["ok"]
    work = st.get("works", native["work"]["id"])
    with st.transaction() as connection:
        tts_gate._invalidate_inputs(work, "作品语音参数已变化", "tts_controls", connection)
    current = st.get("works", native["work"]["id"])
    current_video = st.get("videos", video["id"])
    assert current.get("video_asset_id") == video["id"]
    assert current_video.get("confirmed") is True and current_video.get("approval_state") == "approved"


def test_changed_source_rejects_confirm_and_invalidates_download(native):
    video = register(native)
    package = module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)["package"]
    work = st.get("works", native["work"]["id"])
    work["wan_image"]["asset_hash"] = "sha256:" + "0" * 64
    st.put("works", work["id"], work)
    with pytest.raises(TalkingHeadRejected):
        module().confirm_and_deliver(work["id"], video["id"], actor_id="user_local", review_ack=True)
    with pytest.raises(delivery.DeliveryBlocked):
        delivery.download_path(package["id"])


def test_tampered_native_video_invalidates_review(native):
    video = register(native)
    package = module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)["package"]
    (native["runtime"] / video["storage_uri"]).write_bytes(b"tampered")
    assert not gates.video_gate(video["id"])["ok"]
    with pytest.raises(delivery.DeliveryBlocked):
        delivery.download_path(package["id"])


def test_new_mode_does_not_relax_existing_original_registration():
    with pytest.raises(TalkingHeadRejected) as error:
        videos.register_video(work_id="absent", kind="original", route="WAN_3_0", provider="dashscope_wan",
                              model="wan3.0-video", duration_ms=2000, file_hash="sha256:" + "1" * 64,
                              has_audio_track=True)
    assert error.value.code == "ORIGINAL_AUDIO_UNBOUND"


def test_native_duplicate_registration_reuses_asset(native):
    first = register(native)
    second = module().register_original(native["run"]["id"], prepare(native))
    assert second["video"]["id"] == first["id"] and second["reused"] is True
    assert len(st.all_rows("videos", "work_id=?", [native["work"]["id"]])) == 1


@pytest.mark.parametrize("state", ["cancelled", "failed", "blocked"])
def test_native_confirmation_never_revives_stopped_task(native, state):
    video = register(native)
    task = st.get("tasks", native["task"]["id"])
    task["state"] = state
    st.put("tasks", task["id"], task, {"work_id": task["work_id"]})
    with pytest.raises(TalkingHeadRejected) as error:
        module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)
    assert error.value.code == "TASK_NOT_READY"
    assert st.get("tasks", task["id"])["state"] == state


@pytest.mark.parametrize("change", ["state", "result"])
def test_native_confirmation_requires_succeeded_run_owning_asset(native, change):
    video = register(native)
    run = st.get("provider_runs", native["run"]["id"])
    if change == "state":
        run["state"] = "unknown"
    else:
        run["result_asset_ids"] = ["unrelated_video"]
    st.put("provider_runs", run["id"], run, {"work_id": run["work_id"]})
    assert not module().matches_current(video, native["work"])
    with pytest.raises(TalkingHeadRejected):
        module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)


def test_cancellation_during_cover_extraction_cannot_be_revived(native):
    video = register(native)
    original = ffmpeg.run_ffmpeg
    def cancel_at_cover(args, **kwargs):
        result = original(args, **kwargs)
        task = st.get("tasks", native["task"]["id"])
        task["state"] = "cancelled"
        st.put("tasks", task["id"], task, {"work_id": task["work_id"]})
        return result
    native["monkeypatch"].setattr(ffmpeg, "run_ffmpeg", cancel_at_cover)
    with pytest.raises(TalkingHeadRejected):
        module().confirm_and_deliver(native["work"]["id"], video["id"], actor_id="user_local", review_ack=True)
    assert st.get("tasks", native["task"]["id"])["state"] == "cancelled"
    assert not st.all_rows("packages", "work_id=?", [native["work"]["id"]])


def test_native_long_audio_cannot_hide_behind_requested_video_duration(native):
    native["run"]["provider_params"]["generated_seconds"] = 3
    st.put("provider_runs", native["run"]["id"], native["run"], {"work_id": native["run"]["work_id"]})
    extended = native["runtime"] / "extended.mp4"
    cp = ffmpeg.run_ffmpeg(["-y", "-i", str(native["runtime"] / "fixture.mp4"),
                           "-c:v", "copy", "-af", "apad", "-t", "3", "-c:a", "aac", str(extended)])
    assert cp.returncode == 0
    native["raw"] = extended.read_bytes()
    with pytest.raises(TalkingHeadBlocked) as error:
        prepare(native)
    assert error.value.code == "WAN_NATIVE_DURATION_MISMATCH"


def test_native_registration_cannot_revive_cancelled_provider_run(native):
    prepared = prepare(native)
    run = st.get("provider_runs", native["run"]["id"])
    run["state"] = "cancelled"
    st.put("provider_runs", run["id"], run, {"work_id": run["work_id"]})
    with pytest.raises(TalkingHeadRejected):
        module().register_original(run["id"], prepared)
    assert st.get("provider_runs", run["id"])["state"] == "cancelled"
