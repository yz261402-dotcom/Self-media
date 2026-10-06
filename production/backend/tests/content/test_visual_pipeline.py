"""抽帧、镜头切点、音频信号：失败分支用 monkeypatch，另有一次真实 ffmpeg 集成检查。"""
import hashlib
import subprocess

import pytest

from app.content import visual_pipeline as vp
from app.content.errors import ContentRejected
from app.video import ffmpeg


def cp(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture(scope="session")
def clip(tmp_path_factory):
    """5 秒合成视频：前 2.5 秒测试图，后 2.5 秒纯色（中间一个硬切点），带 440Hz 音轨。"""
    d = tmp_path_factory.mktemp("vp")
    out = d / "c.mp4"
    graph = ("[0:v][1:v]concat=n=2:v=1:a=0[v]")
    r = subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error",
                        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=2.5",
                        "-f", "lavfi", "-i", "color=c=red:size=640x360:rate=10:duration=2.5",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                        "-filter_complex", graph, "-map", "[v]", "-map", "2:a", "-pix_fmt", "yuv420p",
                        "-shortest", str(out)], capture_output=True)
    assert r.returncode == 0, r.stderr[:300]
    silent = d / "silent.mp4"
    r = subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p", "-an", str(silent)],
                       capture_output=True)
    assert r.returncode == 0
    return {"video": str(out), "silent": str(silent)}


def test_plan_timestamps_caps_frames_and_merges_cuts():
    ts = vp.plan_timestamps(duration_ms=10_000, cuts_ms=[3000, 3100, 9990])
    assert ts == sorted(set(ts)) and ts[0] == 0 and ts[-1] < 10_000
    assert 3000 in ts and 3100 not in ts  # 与已有采样点相距不足 300ms 的切点去重
    long = vp.plan_timestamps(duration_ms=600_000, cuts_ms=list(range(1000, 500_000, 1000)))
    assert len(long) <= vp.MAX_BASE_FRAMES + vp.MAX_CUT_FRAMES


def test_short_clip_sampling_has_half_second_detail_and_safe_tail():
    stamps = vp.plan_timestamps(duration_ms=5000, cuts_ms=[])
    assert stamps[:4] == [0, 500, 1000, 1500]
    assert stamps[-1] == 4900
    assert len(stamps) <= vp.MAX_BASE_FRAMES


@pytest.mark.parametrize("duration_ms", [1, 50, 501, 30_000, 60_000, 600_000])
def test_sampling_keeps_first_and_near_end_within_frame_cap(duration_ms):
    stamps = vp.plan_timestamps(duration_ms=duration_ms, cuts_ms=[])
    assert stamps[0] == 0 and stamps == sorted(set(stamps))
    assert 0 <= stamps[-1] < duration_ms
    assert duration_ms - stamps[-1] <= 100
    assert len(stamps) <= vp.MAX_BASE_FRAMES


def test_ffmpeg_missing_and_timeout_are_reported(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(vp.subprocess, "run", missing)
    with pytest.raises(ContentRejected) as e:
        vp.sample_frames("x.mp4", 3000)
    assert e.value.code == "MEDIA_TOOL_UNAVAILABLE"

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 1)
    monkeypatch.setattr(vp.subprocess, "run", slow)
    with pytest.raises(ContentRejected) as e:
        vp.detect_scene_cuts("x.mp4")
    assert e.value.code == "MEDIA_PROCESS_TIMEOUT"


def test_no_video_stream_and_missing_audio(monkeypatch):
    monkeypatch.setattr(vp.subprocess, "run", lambda *a, **k: cp(1, b"", b"Output file does not contain any stream"))
    with pytest.raises(ContentRejected) as e:
        vp.sample_frames("x.mp4", 3000)
    assert e.value.code == "MEDIA_INVALID"
    monkeypatch.setattr(vp, "has_audio", lambda path: False)
    a = vp.analyze_audio("x.mp4")
    assert a["speech_presence"] == "none" and a["music_presence"] == "none" and a["tempo_bpm"] is None


def test_real_ffmpeg_samples_cuts_and_audio(clip):
    frames = vp.sample_frames(clip["video"], 5000)
    assert 5 <= len(frames) <= vp.MAX_BASE_FRAMES + vp.MAX_CUT_FRAMES
    f = frames[0]
    assert f["jpeg_bytes"][:2] == b"\xff\xd8" and f["sha256"] == "sha256:" + hashlib.sha256(f["jpeg_bytes"]).hexdigest()
    assert len(f["jpeg_bytes"]) <= vp.MAX_FRAME_BYTES
    assert [x["timestamp_ms"] for x in frames] == sorted(x["timestamp_ms"] for x in frames)
    cuts = vp.detect_scene_cuts(clip["video"])
    assert any(2200 <= c <= 2800 for c in cuts), cuts
    audio = vp.analyze_audio(clip["video"])
    assert audio["speech_presence"] in ("none", "uncertain") and audio["music_presence"] in ("music", "uncertain")
    assert audio["tempo_bpm"] is None or audio["tempo_bpm"] > 0  # librosa 不可用时允许为空，不阻断
    assert vp.analyze_audio(clip["silent"])["music_presence"] == "none"


def test_missing_planned_frame_rejects_instead_of_silently_skipping(monkeypatch):
    monkeypatch.setattr(vp, "detect_scene_cuts", lambda path: [])
    monkeypatch.setattr(vp, "_frame_jpeg", lambda path, timestamp: b"jpeg" if timestamp == 0 else b"")
    with pytest.raises(ContentRejected) as error:
        vp.sample_frames("legacy.mp4", 3000)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert "0.500" in error.value.message


def test_failed_full_scene_scan_rejects_corrupt_video(monkeypatch):
    monkeypatch.setattr(vp.subprocess, "run", lambda *a, **k:
                        cp(1, stderr=b"Packet corrupt: Invalid NAL unit size; partial file"))
    with pytest.raises(ContentRejected) as error:
        vp.detect_scene_cuts("legacy.mp4")
    assert error.value.code == "MEDIA_INCOMPLETE"


def test_complete_low_frame_rate_video_keeps_its_last_displayed_frame(tmp_path):
    output = tmp_path / "low-fps.mp4"
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=1:duration=5", "-pix_fmt", "yuv420p", str(output)],
                   check=True, capture_output=True)
    frames = vp.sample_frames(str(output), 5000)
    assert frames[0]["timestamp_ms"] == 0 and frames[-1]["timestamp_ms"] == 4900
    assert frames[-1]["jpeg_bytes"] == vp._frame_jpeg(str(output), 4000)


def test_visual_job_missing_frame_never_consumes_provider(clip, tmp_path, monkeypatch):
    import io
    from app import storage as st
    from app.content import media, repository as repo, tasks, visual
    from tests.content._helpers import make_work, set_env
    from pathlib import Path

    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    work_id = make_work()
    ref_id = st.get("works", work_id)["reference_snapshot_id"]
    asset = media.save_uploaded_video(work_id, ref_id, io.BytesIO(Path(clip["video"]).read_bytes()),
                                      "clip.mp4", "video/mp4")
    confirmation = visual.create_confirmation(work_id, ref_id, asset.id, ceiling_fen=100)
    job = visual.submit_visual_analysis(work_id, ref_id, asset.id, confirmation_id=confirmation["id"])
    original_grab = vp._frame_jpeg
    monkeypatch.setattr(vp, "_frame_jpeg", lambda path, timestamp:
                        b"" if timestamp == 2000 else original_grab(path, timestamp))
    sent = []

    def provider(url, headers, body, timeout):
        sent.append(body)
        raise AssertionError("missing frames must fail before provider submission")

    with pytest.raises(ContentRejected) as error:
        visual.handle_visual_analysis(job, transport=provider)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert sent == [] and tasks.jobs_calls(job["id"]) == []
    assert repo.get_reference(ref_id)["visual"] == {}


def test_legacy_truncated_faststart_asset_fails_before_provider(clip, tmp_path, monkeypatch):
    import io
    from pathlib import Path
    from app import storage as st
    from app.content import media, repository as repo, tasks, visual
    from tests.content._helpers import make_work, set_env

    set_env(monkeypatch)
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(tmp_path))
    complete = tmp_path / "faststart.mp4"
    subprocess.run([ffmpeg.ffmpeg_bin(), "-y", "-v", "error", "-i", clip["video"],
                    "-c", "copy", "-movflags", "+faststart", str(complete)], check=True, capture_output=True)
    work_id = make_work()
    ref_id = st.get("works", work_id)["reference_snapshot_id"]
    asset = media.save_uploaded_video(work_id, ref_id, io.BytesIO(complete.read_bytes()), "clip.mp4", "video/mp4")
    stored = media.resolve_path(asset)
    content = stored.read_bytes()
    stored.write_bytes(content[:len(content) // 2])  # 模拟旧版本已经入库的损坏文件。
    assert media.probe_video(str(stored))["duration_ms"] == asset.duration_ms
    confirmation = visual.create_confirmation(work_id, ref_id, asset.id, ceiling_fen=100)
    job = visual.submit_visual_analysis(work_id, ref_id, asset.id, confirmation_id=confirmation["id"])
    sent = []

    def provider(url, headers, body, timeout):
        sent.append(body)
        raise AssertionError("truncated video must fail before provider submission")

    with pytest.raises(ContentRejected) as error:
        visual.handle_visual_analysis(job, transport=provider)
    assert error.value.code == "MEDIA_INCOMPLETE"
    assert sent == [] and tasks.jobs_calls(job["id"]) == []
    assert repo.get_reference(ref_id)["visual"] == {}
