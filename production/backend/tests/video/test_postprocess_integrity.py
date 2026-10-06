"""后期版本绑定与失败证据回归；不提交任何远端作业。"""
import threading

import pytest

from app import postprocess_worker as pw
from app import storage as st
from app.video import asr, ffmpeg
from app.video.errors import VideoBlocked


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "postprocess.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()


def test_final_audio_uses_selected_id_and_current_script():
    work = {"id": "w", "final_audio_id": "selected", "voice_asset_id": "voice",
            "script_version_id": "script-current"}
    audio = {"id": "selected", "work_id": "w", "kind": "full", "approved": True,
             "confirmed": True, "voice_asset_id": "voice", "is_mock": False,
             "script_version_id": "script-current"}
    old = {**audio, "id": "old", "script_version_id": "script-old"}
    assert pw._final_audio(work, [old, audio])["id"] == "selected"
    assert pw._final_audio(work, [old]) is None
    assert pw._final_audio(work, [{**audio, "is_mock": True}]) is None
    assert pw._final_audio(work, [{**audio, "script_version_id": "script-old"}]) is None


def test_changed_script_or_provider_never_reuses_cached_alignment(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"audio hash fixture")
    calls = []

    class Adapter:
        provider_id = "one"
        alignment_source = "forced_alignment"
        simulated = False

        def transcribe(self, path, text):
            calls.append((self.provider_id, text))
            return [{"start_ms": 0, "end_ms": 500, "text": text}]

    adapter = Adapter()
    monkeypatch.setattr(asr, "require_adapter", lambda: adapter)
    audio = {"id": "a"}
    first = pw._asr_cues_cached(audio, str(audio_path), "甲")
    assert pw._asr_cues_cached(audio, str(audio_path), "甲")["reused"]
    changed = pw._asr_cues_cached(audio, str(audio_path), "乙")
    adapter.provider_id = "two"
    provider_changed = pw._asr_cues_cached(audio, str(audio_path), "乙")
    assert changed["reused"] is False
    assert provider_changed["reused"] is False
    assert calls == [("one", "甲"), ("one", "乙"), ("two", "乙")]
    assert st.get("alignment_records", first["id"])["cues"][0]["text"] == "甲"


def test_ffmpeg_installed_only_on_path_is_found(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.touch()
    monkeypatch.delenv(ffmpeg._ENV_BIN, raising=False)
    monkeypatch.setattr(ffmpeg, "_KNOWN_DIRS", ())
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: str(exe))
    assert ffmpeg._candidates() == [str(exe)]


def test_failed_qwen_job_is_explicit_block_not_typeerror(monkeypatch):
    from app.digital_human import worker_client
    monkeypatch.setenv(asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    monkeypatch.setattr(worker_client, "transport", lambda *a, **k: {
        "json": {"state": "failed", "code": "GPU_OOM", "job_id": "job"}})
    with pytest.raises(VideoBlocked) as error:
        asr.Qwen3CloudAdapter()._run_stage("align", "http://audio.invalid", "sha256:x", "正文")
    assert error.value.code == "ASR_STAGE_FAILED"
    assert error.value.detail["job_id"] == "job"


def test_simulated_asr_cannot_enter_real_worker(monkeypatch):
    class Fixture:
        provider_id = "fixture"
        alignment_source = "forced_alignment"
        simulated = True

    monkeypatch.setattr(asr, "get_adapter", lambda: Fixture())
    with pytest.raises(VideoBlocked) as error:
        asr.require_adapter()
    assert error.value.code == "ASR_SIMULATED"


def test_word_alignment_preserves_latin_tokens_and_quoted_punctuation():
    units = asr._restore_punctuation([
        {"text": "试", "start": 0.0, "end": 0.2},
        {"text": "用", "start": 0.2, "end": 0.4},
        {"text": "RAG", "start": 0.4, "end": 0.9},
    ], "“试用 RAG”。")
    cues = asr._group_units(units)
    assert "".join(c["text"] for c in cues) == "“试用RAG”。"
    assert cues[-1]["end_ms"] == 900


def test_word_alignment_rejects_invalid_intervals():
    with pytest.raises(VideoBlocked) as error:
        asr._restore_punctuation([{"text": "字", "start": 1.0, "end": 0.2}], "字")
    assert error.value.code == "ALIGN_TIMING_INVALID"


def test_runtime_path_cannot_escape_media_root():
    with pytest.raises(VideoBlocked) as error:
        pw._runtime_path("../../outside.wav")
    assert error.value.code == "MEDIA_PATH_INVALID"


def test_original_video_must_be_selected_confirmed_and_bound_to_current_audio():
    work = {"id": "w", "video_asset_id": "chosen", "final_audio_id": "audio",
            "identity_id": "person", "variant_id": "variant", "route": "M2"}
    video = {"id": "chosen", "work_id": "w", "kind": "original", "is_mock": False,
             "confirmed": True, "approval_state": "approved", "audio_asset_id": "audio",
             "identity_id": "person", "variant_id": "variant", "route": "M2"}
    st.put("videos", video["id"], video, {"work_id": "w"})
    assert pw._original_video(work)["id"] == "chosen"
    for changed in ({"audio_asset_id": "old"}, {"confirmed": False},
                    {"approval_state": "revoked"}, {"identity_id": "other"},
                    {"variant_id": "old"}):
        st.put("videos", video["id"], {**video, **changed}, {"work_id": "w"})
        assert pw._original_video(work) is None


def test_unreviewed_original_is_blocked_before_asr(monkeypatch):
    audio = {"id": "a"}
    video = {"id": "v"}
    monkeypatch.setattr(pw, "_final_audio", lambda *args: audio)
    monkeypatch.setattr(pw, "_original_video", lambda *args: video)
    monkeypatch.setattr(pw, "_asr_cues_cached", lambda *args: pytest.fail("unreviewed input reached ASR"))
    with pytest.raises(VideoBlocked) as error:
        pw._run_real_postprocess({"id": "t", "work_id": "w"}, {},
                                 {"audios": [], "script_batch": {}})
    assert error.value.code == "ORIGINAL_NOT_APPROVED"


def test_unexpected_worker_exception_is_persisted(monkeypatch):
    monkeypatch.setattr(pw, "_run_real_postprocess", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    task = {"id": "t", "work_id": "w", "mode": "production"}
    record = pw.run_stage(task, {"id": "w"}, "postprocess")
    assert record["state"] == "failed"
    assert record["error"]["code"] == "POSTPROCESS_EXECUTION_FAILED"
    assert st.get("media_jobs", record["id"]) == record


def test_srt_preserves_real_millisecond_boundaries():
    from app.video import subtitles
    text = subtitles.build_srt([subtitles.Cue(0, 123, 2345, "原始时间。")])
    assert text == "1\n00:00:00,123 --> 00:00:02,345\n原始时间。\n"


def test_explicit_retry_preserves_failure_and_reuses_success(monkeypatch):
    attempts = []
    def run(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise VideoBlocked("TEMPORARY", "dependency unavailable")
        return {"state": "succeeded", "processing_outcome": "rendered", "error": None}
    monkeypatch.setattr(pw, "_run_real_postprocess", run)
    task, work = {"id": "t", "work_id": "w", "mode": "production"}, {"id": "w"}
    first = pw.run_stage(task, work, "postprocess")
    second = pw.run_stage(task, work, "postprocess", retry=True)
    assert second["state"] == "succeeded"
    assert first["id"] != second["id"]
    assert st.get("media_jobs", first["id"])["state"] == "blocked"
    assert pw.run_stage(task, work, "postprocess", retry=True)["id"] == second["id"]
    assert len(attempts) == 2


def test_input_file_mutation_changes_execution_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(pw, "_runtime_root", lambda: str(tmp_path))
    path = tmp_path / "a.wav"
    path.write_bytes(b"first")
    audio = {"id": "a", "storage_uri": "a.wav", "work_id": "w"}
    st.put("audios", "a", audio, {"work_id": "w"})
    task, work = {"id": "t", "work_id": "w"}, {"id": "w", "final_audio_id": "a"}
    _, before = pw._snapshot(task, work, "postprocess")
    path.write_bytes(b"mutated")
    _, after = pw._snapshot(task, work, "postprocess")
    assert before != after


def test_master_bounds_ffmpeg_threads_and_applies_deterministic_clarity(monkeypatch, tmp_path):
    import subprocess
    from app.video import render
    source = tmp_path / "input"
    source.write_bytes(b"fixture")
    out = tmp_path / "output.mp4"
    args_seen = []
    monkeypatch.setattr(render, "_find_font", lambda: "font.ttf")
    monkeypatch.setattr(render, "_cvs_flag", lambda: "-fps_mode")
    monkeypatch.setattr(ffmpeg, "measure_loudness", lambda p: {
        "input_i": -20, "input_tp": -10, "input_lra": 2, "input_thresh": -30})
    def run(args, **kwargs):
        args_seen.extend(args)
        out.write_bytes(b"rendered")
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(ffmpeg, "run_ffmpeg", run)
    log = render.render_master(video_path=str(source), audio_path=str(source), out_path=str(out))
    assert args_seen[args_seen.index("-threads:v") + 1] == "2"
    assert args_seen[args_seen.index("-filter_threads") + 1] == "2"
    assert "setsar=1" in log["video_filters"]
    assert "hqdn3d=" in log["video_filters"] and "unsharp=" in log["video_filters"]


@pytest.mark.parametrize("detector", ["detect_black_freeze", "measure_loudness"])
def test_failed_ffmpeg_detection_cannot_be_reported_as_measurement(monkeypatch, detector):
    import subprocess
    monkeypatch.setattr(ffmpeg, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpeg, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(
        [], 1, b"", b'{"input_i":"-14", "input_tp":"-2"}\nfilter execution failed'))
    with pytest.raises(VideoBlocked):
        getattr(ffmpeg, detector)("input.mp4")
