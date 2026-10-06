"""Real local FFmpeg proves subtitle timing, immutable source and copied audio."""
import copy
import hashlib
import importlib
from pathlib import Path
import wave

import pytest

from app.video import asr, ffmpeg
from app.video.errors import VideoBlocked, VideoRejected


def _module():
    assert importlib.util.find_spec("app.short_video.subtitle_media"), "native subtitle renderer is missing"
    return importlib.import_module("app.short_video.subtitle_media")


@pytest.fixture
def native_graph(tmp_path, monkeypatch):
    root = tmp_path / "中文工作区 'a,[b]"
    root.mkdir()
    monkeypatch.setenv("AVATAR_STUDIO_WORKSPACE", str(root))
    folder = root / "production/backend/runtime/wan_storyboards/storyboard_fixture"
    folder.mkdir(parents=True)
    path = folder / "stitched.mp4"
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-f", "lavfi", "-i", "color=blue:s=240x426:r=25:d=2",
                           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", str(path)])
    assert cp.returncode == 0, cp.stderr
    output = {"storage_uri": path.relative_to(root).as_posix(),
              "file_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
              "native_audio": True, "transition": "native-test", "shot_order": [1, 2],
              "input_clip_hashes": ["sha256:a", "sha256:b"]}
    return {"id": "storyboard_fixture", "video_id": "video_fixture", "output": output,
            "shots": [{"dialogue": "规划台词绝对不能进入字幕"}]}, root


def _adapter(monkeypatch, *, cues=None, transcript="实际对白。", error=None):
    calls = []

    class NativeAdapter:
        provider_id = "qwen3_cloud"
        alignment_source = "forced_alignment"
        simulated = False

        def transcribe_native(self, audio_path, *, cache_path=None, attempt_id=None):
            calls.append((audio_path, cache_path))
            assert attempt_id == Path(audio_path).parent.name
            with wave.open(str(audio_path), "rb") as wav:
                assert wav.getframerate() == 16000
                assert wav.getnchannels() == 1
                assert wav.getsampwidth() == 2
            if error:
                raise error
            return {"transcript": transcript, "cues": cues if cues is not None else [
                {"start_ms": 300, "end_ms": 1300, "text": transcript}],
                "asr_job": {"job_id": "asr_fixture"}, "align_job": {"job_id": "align_fixture"},
                "alignment_source": "forced_alignment"}

    monkeypatch.setattr(asr, "require_adapter", lambda: NativeAdapter())
    return calls


def _frame(path, seconds):
    cp = ffmpeg.run_ffmpeg(["-v", "error", "-ss", str(seconds), "-i", str(path),
                           "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    assert cp.returncode == 0, cp.stderr
    return cp.stdout


def test_render_burns_actual_audio_cues_preserving_source_and_native_audio(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    frozen = copy.deepcopy(graph)
    source = root / graph["output"]["storage_uri"]
    source_bytes = source.read_bytes()
    calls = _adapter(monkeypatch)
    result = media.render(graph, job_id="job_subtitle_1")
    path = root / result["storage_uri"]
    assert source.read_bytes() == source_bytes
    assert graph == frozen
    assert path != source
    assert "/subtitles/job_subtitle_1/" in result["storage_uri"]
    assert result["subtitles_burned"] is True
    assert result["subtitle_source"] == {"video_id": "video_fixture", "file_hash": graph["output"]["file_hash"]}
    assert result["native_audio"] is True
    assert (result["width"], result["height"]) == (240, 426)
    assert result["transition"] == graph["output"]["transition"]
    assert result["shot_order"] == [1, 2]
    assert result["input_clip_hashes"] == ["sha256:a", "sha256:b"]
    assert result["video_duration_ms"] == 2000
    assert result["audio_duration_ms"] == 2000
    checks = result["subtitle_checks"]
    assert checks["outcome"] == "pass"
    assert checks["transcript"] == "实际对白。"
    assert checks["alignment_source"] == "forced_alignment"
    assert checks["cue_count"] == 1
    assert checks["source_audio_packet_hash"] == checks["output_audio_packet_hash"]
    assert checks["source_audio_hash"].startswith("sha256:")
    assert len(calls) == 1
    assert {row["kind"] for row in result["subtitle_files"]} == {"subtitles_ass", "subtitles_srt"}
    for row in result["subtitle_files"]:
        artifact = root / row["storage_uri"]
        assert row["file_hash"] == "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert row["bytes"] == artifact.stat().st_size
        assert "规划台词" not in artifact.read_text(encoding="utf-8-sig")
    srt = next(root / row["storage_uri"] for row in result["subtitle_files"] if row["kind"] == "subtitles_srt")
    assert "00:00:00,300 --> 00:00:01,300" in srt.read_text(encoding="utf-8-sig")
    # The visual evidence is a local synthetic movie, not a claim of real ASR inference.
    before, during, after = (_frame(path, at) for at in (.1, .7, 1.7))
    def white_pixels(frame):
        return sum(min(frame[offset:offset + 3]) > 180 for offset in range(0, len(frame), 3))
    assert white_pixels(before) == white_pixels(after) == 0
    assert white_pixels(during) > 40, "the verified timed cue must actually appear in decoded frames"


def test_render_completed_job_replays_without_asr_or_overwriting_outputs(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    calls = _adapter(monkeypatch)
    first = media.render(graph, job_id="job_replay")
    before = (root / first["storage_uri"]).read_bytes()
    second = media.render(graph, job_id="job_replay")
    assert second == first
    assert (root / first["storage_uri"]).read_bytes() == before
    assert len(calls) == 1


@pytest.mark.parametrize("cues,transcript,code", [
    ([], "", "ASR_NATIVE_TRANSCRIPT_EMPTY"),
    ([{"start_ms": -1, "end_ms": 300, "text": "字"}], "字", "CUE_OUT_OF_RANGE"),
    ([{"start_ms": 300, "end_ms": 2300, "text": "字"}], "字", "CUE_OUT_OF_RANGE"),
    ([{"start_ms": 500, "end_ms": 900, "text": "你"},
      {"start_ms": 700, "end_ms": 1200, "text": "好"}], "你好", "CUE_OVERLAP"),
    ([{"start_ms": 300, "end_ms": 300, "text": "字"}], "字", "ALIGN_TIMING_INVALID"),
    ([{"start_ms": 300, "end_ms": 309, "text": "字"}], "字", "CUE_ASS_TIMING_COLLAPSED"),
    ([{"start_ms": 300, "end_ms": 1000, "text": "错字"}], "实际", "SCRIPT_COVERAGE_MISMATCH"),
])
def test_invalid_inference_never_claims_burned_subtitles(native_graph, monkeypatch, cues, transcript, code):
    media = _module()
    graph, root = native_graph
    source = root / graph["output"]["storage_uri"]
    original = source.read_bytes()
    _adapter(monkeypatch, cues=cues, transcript=transcript)
    with pytest.raises((VideoBlocked, VideoRejected)) as error:
        media.render(graph, job_id="job_invalid")
    assert error.value.code == code
    assert source.read_bytes() == original
    folder = source.parent / "subtitles/job_invalid"
    assert not (folder / "result.json").exists()
    assert not (folder / "subtitled.mp4").exists()


def test_asr_failure_does_not_create_subtitles_or_mutate_original(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    _adapter(monkeypatch, error=VideoBlocked("ASR_STAGE_FAILED", "fixture worker unavailable"))
    with pytest.raises(VideoBlocked, match="ASR_STAGE_FAILED"):
        media.render(graph, job_id="job_asr_failure")
    folder = root / graph["output"]["storage_uri"]
    assert folder.is_file()
    assert not list((folder.parent / "subtitles/job_asr_failure").glob("*.ass"))


def test_ass_untrusted_text_cannot_inject_styles_and_font_scales_with_picture(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    transcript = "{\\an8}你好\\N\n朋友。"
    _adapter(monkeypatch, transcript=transcript)
    result = media.render(graph, job_id="job_literal")
    ass_file = next(root / row["storage_uri"] for row in result["subtitle_files"] if row["kind"] == "subtitles_ass")
    content = ass_file.read_text(encoding="utf-8-sig")
    event = next(line for line in content.splitlines() if line.startswith("Dialogue:"))
    assert "{\\an8}" not in event
    assert "\\N" not in event
    assert "｛＼an8｝你好＼N 朋友。" in event
    style = next(line for line in content.splitlines() if line.startswith("Style:"))
    assert 12 <= float(style.split(",")[2]) <= 18
    assert result["subtitle_checks"]["transcript"] == transcript


@pytest.mark.parametrize("job_id", ["../job", "a/b", "a\\b", "", "x" * 100])
def test_render_rejects_unsafe_job_folder(native_graph, job_id):
    media = _module()
    graph, _ = native_graph
    with pytest.raises(Exception) as error:
        media.render(graph, job_id=job_id)
    assert getattr(error.value, "code", None) == "STORYBOARD_ASSET_PATH_INVALID"


def test_source_change_and_replayed_artifact_tampering_are_rejected(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    _adapter(monkeypatch)
    result = media.render(graph, job_id="job_changed")
    path = root / result["storage_uri"]
    path.write_bytes(b"tampered")
    with pytest.raises(Exception) as error:
        media.render(graph, job_id="job_changed")
    assert getattr(error.value, "code", None) == "STORYBOARD_CLIP_CHANGED"


def test_extracted_audio_cache_cannot_be_modified_between_worker_attempts(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    _adapter(monkeypatch, error=VideoBlocked("ASR_STAGE_FAILED", "fixture retry"))
    with pytest.raises(VideoBlocked):
        media.render(graph, job_id="job_audio_cache")
    source = root / graph["output"]["storage_uri"]
    audio = source.parent / "subtitles/job_audio_cache/native.wav"
    audio.write_bytes(b"tampered audio")
    calls = _adapter(monkeypatch)
    with pytest.raises(VideoBlocked) as error:
        media.render(graph, job_id="job_audio_cache")
    assert error.value.code == "SUBTITLE_AUDIO_CACHE_CHANGED"
    assert calls == []


def test_retry_job_is_permanently_bound_to_original_movie(native_graph, monkeypatch):
    media = _module()
    graph, root = native_graph
    _adapter(monkeypatch, error=VideoBlocked("ASR_STAGE_FAILED", "fixture retry"))
    with pytest.raises(VideoBlocked):
        media.render(graph, job_id="job_source_cache")
    graph["video_id"] = "different_video"
    calls = _adapter(monkeypatch)
    with pytest.raises(VideoBlocked) as error:
        media.render(graph, job_id="job_source_cache")
    assert error.value.code == "SUBTITLE_SOURCE_CHANGED"
    assert calls == []
