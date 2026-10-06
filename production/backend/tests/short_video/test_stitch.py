"""Exercise real FFmpeg concatenation; native audio stays aligned at shot cuts."""
import hashlib
from pathlib import Path

import pytest

from app import jobs, storage as st
from app.video import ffmpeg
from tests.short_video.test_storyboard import prepared, ready_images


def _clip(path, color, frequency, seconds):
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-f", "lavfi", "-i", f"color={color}:s=240x426:r=25:d={seconds}",
                           "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
                           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    assert cp.returncode == 0, cp.stderr


def test_worker_stitches_in_shot_order_with_complete_native_audio(prepared, monkeypatch):
    from app.short_video import worker
    service, graph, root = prepared
    graph = ready_images(service, graph)
    for index, (color, frequency, seconds) in enumerate((("red", 440, 2), ("blue", 880, 3)), 1):
        path = root / f"clip_{index}.mp4"
        _clip(path, color, frequency, seconds)
        clip = {"storage_uri": path.relative_to(root).as_posix(),
                "file_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "duration_ms": seconds * 1000, "has_audio_track": True, "has_video_track": True,
                "native_audio": True, "remote_task_id": f"original-{index}"}
        service._update_shot(graph["id"], index, video_state="succeeded", remote_task_id=f"original-{index}", clip=clip)
    graph = service.project(service.get_graph(graph["id"]))
    service.approve_clips(graph["id"], actor_id="user", clip_digest=graph["clip_digest"], review_ack=True)
    queued = service.queue_stitch(graph["id"])
    service._mutate(graph["id"], lambda g: g.update(state="stitching"))
    original_read_bytes = Path.read_bytes
    def reject_full_movie_read(path):
        if path.name.startswith("stitch_") and path.suffix == ".mp4":
            pytest.fail("拼接成片不得整文件读入内存")
        return original_read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", reject_full_movie_read)
    results = worker.run_queued(worker_id="worker-test")
    assert len(results) == 1
    result = results[0]
    assert result["state"] == "succeeded"
    assert jobs.get_job(queued["stitch_job_id"])["state"] == "succeeded"
    path = root / result["output"]["storage_uri"]
    probe = ffmpeg.probe(str(path))
    assert {s["codec_type"] for s in probe["streams"]} >= {"video", "audio"}
    assert abs(float(probe["format"]["duration"]) - 5) < .15
    for seconds, expected in ((.2, "red"), (4.5, "blue")):
        cp = ffmpeg.run_ffmpeg(["-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1",
                               "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        r, g, b = cp.stdout[:3]
        assert (r > 150 and b < 80) if expected == "red" else (b > 150 and r < 80)
    assert worker.run_stitch(graph["id"], worker_id="worker-test")["output"]["file_hash"] == result["output"]["file_hash"]


def test_audio_fades_do_not_overlap_or_shorten_the_timeline():
    from app.short_video.media import stitch_filter
    value = stitch_filter([2, 3], width=720, height=1280, transition_seconds=.06)
    assert "concat=n=2:v=1:a=1" in value
    assert "afade=t=out:st=1.94:d=0.06" in value
    assert "afade=t=in:st=0:d=0.06" in value
    assert "acrossfade" not in value


def test_clip_integrity_check_streams_file_without_loading_entire_movie(prepared, monkeypatch):
    from app.short_video import media
    _, _, root = prepared
    path = root / "large.mp4"
    path.write_bytes(b"test movie bytes")
    clip = {"storage_uri": path.relative_to(root).as_posix(),
            "file_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
    original_read_bytes = Path.read_bytes
    def reject_movie_read(candidate):
        if candidate == path:
            pytest.fail("原片校验不得整文件读入内存")
        return original_read_bytes(candidate)
    monkeypatch.setattr(Path, "read_bytes", reject_movie_read)
    assert media.local_clip(clip) == path


def test_completed_job_recovery_restores_video_id(prepared):
    from app.short_video import worker
    service, graph, _ = prepared
    job = jobs.submit(work_id=graph["work_id"], capability="storyboard_stitch", provider="local_ffmpeg",
                      model="ffmpeg", args={"graph_id": graph["id"]}, idempotency_key=f"{graph['id']}:recovery")
    jobs.acquire_lease(job["id"], "prior-worker")
    jobs.complete(job["id"], "prior-worker", {"output": {"storage_uri": "finished.mp4"}, "video_id": "video-finished"})
    service._mutate(graph["id"], lambda g: g.update(state="stitching", stitch_job_id=job["id"],
        clip_approval={"clip_digest": service._clip_digest(g)}))
    result = worker.run_stitch(graph["id"])
    assert result["video_id"] == "video-finished"


def test_implicit_worker_identity_is_unique_per_execution(prepared, monkeypatch):
    from app.short_video import worker
    service, graph, _ = prepared
    job = jobs.submit(work_id=graph["work_id"], capability="storyboard_stitch", provider="local_ffmpeg",
                      model="ffmpeg", args={"graph_id": graph["id"]}, idempotency_key=f"{graph['id']}:worker-id")
    service._mutate(graph["id"], lambda g: g.update(state="stitch_queued", stitch_job_id=job["id"],
        clip_approval={"clip_digest": service._clip_digest(g)}))
    identities = []
    def capture(job_id, worker_id, **kwargs):
        identities.append(worker_id)
        raise RuntimeError("stop before FFmpeg")
    monkeypatch.setattr(jobs, "acquire_lease", capture)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="stop before FFmpeg"):
            worker.run_stitch(graph["id"])
    assert len(set(identities)) == 2


def test_failed_local_stitch_can_queue_new_attempt_for_same_reviewed_clips(prepared, monkeypatch):
    from app.short_video import media, service, worker
    _, graph, _ = prepared
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    first = service.queue_stitch(graph["id"])
    monkeypatch.setattr(media, "stitch", lambda current: (_ for _ in ()).throw(OSError("disk busy")))
    with pytest.raises(OSError, match="disk busy"):
        worker.run_stitch(graph["id"])
    assert jobs.get_job(first["stitch_job_id"])["state"] == "failed"
    second = service.queue_stitch(graph["id"])
    assert second["stitch_job_id"] != first["stitch_job_id"]
    assert jobs.get_job(second["stitch_job_id"])["state"] == "queued"


def test_failed_local_stitch_does_not_terminate_queued_worker(prepared, monkeypatch):
    from app.short_video import media, service, worker
    _, graph, _ = prepared
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    queued = service.queue_stitch(graph["id"])
    monkeypatch.setattr(media, "stitch", lambda current: (_ for _ in ()).throw(OSError("disk busy")))

    assert worker.run_queued(worker_id="worker-test") == []
    assert jobs.get_job(queued["stitch_job_id"])["state"] == "failed"
    assert service.get_graph(graph["id"])["state"] == "stitch_failed"


def test_unrecorded_stitch_error_is_not_silenced(prepared, monkeypatch):
    from app.short_video import media, service, worker
    _, graph, _ = prepared
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    queued = service.queue_stitch(graph["id"])
    monkeypatch.setattr(worker, "run_stitch", lambda graph_id, worker_id=None: (_ for _ in ()).throw(OSError("database unavailable")))

    with pytest.raises(OSError, match="database unavailable"):
        worker.run_queued(worker_id="worker-test")
    assert jobs.get_job(queued["stitch_job_id"])["state"] == "queued"


def test_stale_queued_stitch_is_recorded_instead_of_waiting_forever(prepared, monkeypatch):
    from app.short_video import media, service, worker
    _, graph, _ = prepared
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    queued = service.queue_stitch(graph["id"])
    monkeypatch.setattr(
        service,
        "assert_current",
        lambda current: (_ for _ in ()).throw(
            service.StoryboardError("STORYBOARD_INPUT_CHANGED", "选稿已变化，旧任务仅保留为历史记录")
        ),
    )

    assert worker.run_queued(worker_id="worker-test") == []
    assert jobs.get_job(queued["stitch_job_id"])["state"] == "cancelled"
    current = service.get_graph(graph["id"])
    assert current["state"] == "stitch_failed"
    assert current["error"]["code"] == "STORYBOARD_INPUT_CHANGED"


def test_cancelled_stitch_can_queue_a_fresh_attempt(prepared, monkeypatch):
    from app.short_video import media, service
    _, graph, _ = prepared
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    first = service.queue_stitch(graph["id"])
    from app import jobs
    jobs.cancel_unsent_call(first["stitch_job_id"])

    second = service.queue_stitch(graph["id"])

    assert second["stitch_job_id"] != first["stitch_job_id"]
    assert jobs.get_job(second["stitch_job_id"])["state"] == "queued"


def test_stitch_lease_covers_render_and_full_decode_time(prepared, monkeypatch):
    from app.short_video import media, service, worker
    _, graph, _ = prepared
    clock = [1_000_000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(media, "verify_clips", lambda current: [])
    service._mutate(graph["id"], lambda g: g.update(
        clip_approval={"clip_digest": service._clip_digest(g)}, state="ready_to_stitch"))
    queued = service.queue_stitch(graph["id"])
    output = {"storage_uri": "finished.mp4", "file_hash": "sha256:finished", "duration_ms": 5000}

    def finish_after_processing(current):
        clock[0] += 1800 + 900
        return dict(output)

    monkeypatch.setattr(media, "stitch", finish_after_processing)
    result = worker.run_stitch(graph["id"], worker_id="long-stitch-worker")

    assert result["state"] == "succeeded"
    completed = jobs.get_job(queued["stitch_job_id"])
    assert completed["state"] == "succeeded"
    assert completed["result"]["video_id"] == result["video_id"]
    assert completed["result"]["output"]["file_hash"] == output["file_hash"]
    assert st.get("videos", result["video_id"])["file_hash"] == output["file_hash"]


def test_stitch_reuses_valid_final_file_after_crash_before_job_completion(prepared, monkeypatch):
    from app.short_video import media, service
    _, graph, root = prepared
    clips = []
    for index, color in enumerate(("red", "blue"), 1):
        path = root / f"recover_{index}.mp4"
        _clip(path, color, 400 + index * 100, index + 1)
        clips.append(path)
        service._update_shot(graph["id"], index, video_state="succeeded", remote_task_id=f"recover-{index}",
            clip={"storage_uri": path.relative_to(root).as_posix(),
                  "file_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                  "duration_ms": (index + 1) * 1000, "has_audio_track": True,
                  "native_audio": True, "remote_task_id": f"recover-{index}"})
    current = service.get_graph(graph["id"])
    first = media.stitch(current)
    original_ffmpeg = media.ffmpeg.run_ffmpeg
    def reject_render(args, **kwargs):
        if "-filter_complex" in args:
            pytest.fail("已落盘的成片不应再次渲染")
        return original_ffmpeg(args, **kwargs)
    monkeypatch.setattr(media.ffmpeg, "run_ffmpeg", reject_render)
    second = media.stitch(current)
    assert second["file_hash"] == first["file_hash"]
