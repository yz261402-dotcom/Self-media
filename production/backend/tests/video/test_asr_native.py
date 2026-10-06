"""Native sound recognition must align inferred text, never planned dialogue."""
import pytest

from app.video import asr
from app.video.errors import VideoBlocked


def _native_method(adapter):
    assert hasattr(adapter, "transcribe_native"), "native audio ASR method is missing"
    return adapter.transcribe_native


def _fake_stages(monkeypatch, *, text="实际声音。", alignment_text=None, alignment=None, fail_align=False):
    calls = []
    monkeypatch.setenv(asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    adapter = asr.Qwen3CloudAdapter()
    monkeypatch.setattr(adapter, "_publish_audio", lambda _: ("http://audio.invalid/native.wav", "sha256:test"))

    def run(stage, audio_url, digest, supplied_text, *, attempt_id=None):
        calls.append((stage, supplied_text))
        if stage == "asr":
            return {"doc": {"text": text, "audio": {"duration_seconds": 2}}, "job": {"job_id": "asr-native"}}
        if fail_align:
            raise VideoBlocked("ASR_STAGE_FAILED", "fixture alignment failed")
        return {"doc": {"alignment_text": alignment_text if alignment_text is not None else supplied_text,
                        "time_unit": "seconds", "alignment": alignment or [
                            {"text": c, "start": .1 + i * .2, "end": .3 + i * .2}
                            for i, c in enumerate(text.rstrip("。"))]}, "job": {"job_id": "align-native"}}

    monkeypatch.setattr(adapter, "_run_stage", run)
    return adapter, calls


def test_native_recognition_aligns_actual_inferred_text(monkeypatch):
    adapter, calls = _fake_stages(monkeypatch)
    result = _native_method(adapter)("unused.wav")
    assert calls == [("asr", ""), ("align", "实际声音。")]
    assert result["transcript"] == "实际声音。"
    assert result["cues"] == [{"start_ms": 100, "end_ms": 900, "text": "实际声音。"}]
    assert result["alignment_source"] == "forced_alignment"


@pytest.mark.parametrize("text", ["", "   \n", None, {"unsafe": "wrong type"}])
def test_empty_or_malformed_recognition_never_runs_aligner(monkeypatch, text):
    adapter, calls = _fake_stages(monkeypatch, text=text)
    with pytest.raises(VideoBlocked) as error:
        _native_method(adapter)("unused.wav")
    assert error.value.code == "ASR_NATIVE_TRANSCRIPT_EMPTY"
    assert len(calls) == 1


def test_native_rejects_aligner_text_different_from_inference(monkeypatch):
    adapter, _ = _fake_stages(monkeypatch, alignment_text="原稿台词。",
                             alignment=[{"text": "原稿台词", "start": .2, "end": .8}])
    with pytest.raises(VideoBlocked) as error:
        _native_method(adapter)("unused.wav")
    assert error.value.code == "ALIGN_TEXT_MISMATCH"


def test_native_recognition_cache_survives_alignment_failure(monkeypatch, tmp_path):
    audio = tmp_path / "native.wav"
    audio.write_bytes(b"stable audio fixture")
    cache = tmp_path / "inference.json"
    failed, first_calls = _fake_stages(monkeypatch, fail_align=True)
    with pytest.raises(VideoBlocked, match="ASR_STAGE_FAILED"):
        _native_method(failed)(str(audio), cache_path=cache)
    assert first_calls == [("asr", ""), ("align", "实际声音。")]
    assert cache.is_file()
    recovered, calls = _fake_stages(monkeypatch)
    result = _native_method(recovered)(str(audio), cache_path=cache)
    assert calls == [("align", "实际声音。")]
    assert result["transcript"] == "实际声音。"
    assert recovered.last_asr_job == {"job_id": "asr-native"}
    assert _native_method(recovered)(str(audio), cache_path=cache) == result
    assert len(calls) == 1
    audio.write_bytes(b"changed audio fixture")
    _native_method(recovered)(str(audio), cache_path=cache)
    assert calls[-2:] == [("asr", ""), ("align", "实际声音。")]


def test_native_worker_missing_blocks_before_audio_publish(monkeypatch):
    monkeypatch.delenv(asr.ASR_WORKER_URL_ENV, raising=False)
    adapter = asr.Qwen3CloudAdapter()
    monkeypatch.setattr(adapter, "_publish_audio", lambda _: pytest.fail("unconfigured worker must not publish audio"))
    with pytest.raises(VideoBlocked) as error:
        _native_method(adapter)("unused.wav")
    assert error.value.code == "ASR_NOT_CONFIGURED"


@pytest.mark.parametrize("stage", ["asr", "align"])
def test_native_malformed_worker_json_is_explicit_failure(monkeypatch, stage):
    adapter, _ = _fake_stages(monkeypatch)
    original = adapter._run_stage
    monkeypatch.setattr(adapter, "_run_stage", lambda requested, *args: (
        {"doc": ["invalid schema"], "job": {"job_id": "bad-worker-output"}}
        if requested == stage else original(requested, *args)))
    with pytest.raises(VideoBlocked) as error:
        _native_method(adapter)("unused.wav")
    assert error.value.code == "ASR_OUTPUT_INVALID"


def test_native_remote_identity_is_stable_for_one_attempt_and_distinct_after_explicit_retry(monkeypatch):
    from app.digital_human import worker_client
    requests = []
    documents = {
        "asr": {"text": "实际。"},
        "align": {"alignment_text": "实际。", "time_unit": "seconds", "alignment": [
            {"text": "实", "start": .2, "end": .4}, {"text": "际", "start": .4, "end": .6}]},
    }
    import json

    def transport(request, **_):
        if "stage" in request:
            requests.append(request)
            stage = request["stage"]
            return {"status_code": 200, "json": {"job_id": stage, "state": "succeeded",
                    "output_url": f"http://worker.invalid/v1/outputs/{stage}.json"}}
        stage = request["url"].rsplit("/", 1)[-1].removesuffix(".json")
        return {"status_code": 200, "body": json.dumps(documents[stage]).encode()}

    monkeypatch.setenv(asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    monkeypatch.setattr(worker_client, "transport", transport)
    adapter = asr.Qwen3CloudAdapter()
    monkeypatch.setattr(adapter, "_publish_audio", lambda _: ("http://audio.invalid/native.wav", "sha256:test"))
    adapter.transcribe_native("unused.wav", attempt_id="job_first")
    adapter.transcribe_native("unused.wav", attempt_id="job_first")
    adapter.transcribe_native("unused.wav", attempt_id="job_retry")
    assert requests[:2] == requests[2:4]
    for before, after in zip(requests[:2], requests[4:]):
        assert before["args"]["subtitle_attempt_id"] == "job_first"
        assert after["args"]["subtitle_attempt_id"] == "job_retry"
        assert {k: v for k, v in before["args"].items() if k != "subtitle_attempt_id"} == {
            k: v for k, v in after["args"].items() if k != "subtitle_attempt_id"}


def test_native_cache_is_bound_to_explicit_attempt(monkeypatch, tmp_path):
    audio = tmp_path / "native.wav"
    audio.write_bytes(b"same audio fixture")
    cache = tmp_path / "inference.json"
    adapter, _ = _fake_stages(monkeypatch)
    original = adapter._run_stage
    calls = []

    def stage(name, *args, **kwargs):
        calls.append((name, kwargs.get("attempt_id")))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(adapter, "_run_stage", stage)
    adapter.transcribe_native(str(audio), cache_path=cache, attempt_id="job_first")
    adapter.transcribe_native(str(audio), cache_path=cache, attempt_id="job_first")
    assert calls == [("asr", "job_first"), ("align", "job_first")]
    adapter.transcribe_native(str(audio), cache_path=cache, attempt_id="job_retry")
    assert calls[-2:] == [("asr", "job_retry"), ("align", "job_retry")]


def test_old_narration_worker_requests_keep_original_argument_shape(monkeypatch):
    from app.digital_human import worker_client
    requests = []

    def transport(request, **_):
        requests.append(request)
        return {"status_code": 200, "json": {"state": "failed", "job_id": "old-flow"}}

    monkeypatch.setenv(asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    monkeypatch.setattr(worker_client, "transport", transport)
    adapter = asr.Qwen3CloudAdapter()
    with pytest.raises(VideoBlocked):
        adapter._run_stage("asr", "http://audio.invalid/a.wav", "sha256:old", "冻结稿件")
    assert requests[0]["args"] == {"audio_url": "http://audio.invalid/a.wav", "audio_hash": "sha256:old", "text": "冻结稿件"}
