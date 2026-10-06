"""Qwen3 ASR/强制对齐 provider 的契约测试（不触网、不依赖 GPU）。

钉的是几条不能松的口子：
- 没配 worker 就是 blocked，不许用任何替代时间轴；
- 时间轴来源必须在白名单，均分时长不算对齐；
- 对齐没覆盖全文 → 阻断（"差不多"不能出字幕）；
- 断句只能落在标点后，拼接后与冻结正文逐字符一致。
"""
from __future__ import annotations

import os
import json

import pytest

from app.video import asr as v_asr
from app.video import subtitles as v_sub
from app.video.errors import VideoBlocked


def _use_worker_transport(monkeypatch, send):
    from app.digital_human import worker_client as wc

    monkeypatch.setenv(v_asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    monkeypatch.setattr(wc, "urllib_transport", send)
    transport = wc.transport
    monkeypatch.setattr(wc, "transport", lambda *a, **kw: transport(
        *a, **kw, sleep=lambda _: None))


@pytest.mark.parametrize("cached", [False, True], ids=["cold-poll-json", "warm-submit-body"])
def test_qwen_cold_and_cached_success_produce_identical_cues(monkeypatch, cached):
    """真实运输层的轮询结果与幂等命中结果，必须都能读回相同字幕。"""
    requests = []
    documents = {
        "asr": {"text": "你好。"},
        "align": {"alignment_text": "你好。", "time_unit": "seconds",
                  "alignment": [{"text": "你", "start": 0.2, "end": 0.4},
                                {"text": "好", "start": 0.4, "end": 0.6}]},
    }

    def send(request):
        requests.append(request)
        if request["method"] == "POST":
            stage = json.loads(request["body"])["stage"]
            doc = {"job_id": stage, "state": "succeeded" if cached else "queued",
                   "output_url": f"http://remote:8080/v1/outputs/{stage}.json"}
        elif "/v1/jobs/" in request["url"]:
            stage = request["url"].rsplit("/", 1)[-1]
            doc = {"job_id": stage, "state": "succeeded",
                   "output_url": f"http://remote:8080/v1/outputs/{stage}.json"}
        else:
            assert request["url"].startswith("http://worker.invalid/v1/outputs/")
            stage = request["url"].rsplit("/", 1)[-1].removesuffix(".json")
            doc = documents[stage]
        return {"status_code": 200, "body": json.dumps(doc).encode()}

    _use_worker_transport(monkeypatch, send)
    adapter = v_asr.Qwen3CloudAdapter()
    monkeypatch.setattr(adapter, "_publish_audio", lambda _: ("http://audio.invalid/a.wav", "sha256:x"))
    assert adapter.transcribe("unused.wav", "你好。") == [
        {"start_ms": 200, "end_ms": 600, "text": "你好。"}]
    assert adapter.last_transcript == "你好。"
    assert adapter.last_asr_job["job_id"] == "asr"
    assert adapter.last_align_job["job_id"] == "align"
    assert sum(r["method"] == "POST" for r in requests) == 2
    assert sum("/v1/jobs/" in r["url"] for r in requests) == (0 if cached else 2)


@pytest.mark.parametrize("response,expected_code,expected_text", [
    ({"status_code": 200, "body": json.dumps({
        "state": "failed", "job_id": "job_failed", "code": "GPU_OOM", "error": "显存不足"}).encode()},
     "ASR_STAGE_FAILED", "GPU_OOM"),
    ({"status_code": 401, "body": b'{"code":"AUTH_REQUIRED","error":"invalid token"}',
      "message": "HTTP Error 401: Unauthorized"}, "ASR_WORKER_AUTH_FAILED", "401"),
    ({"status_code": 503, "body": b'{"error":"worker unavailable"}'},
     "ASR_WORKER_HTTP_ERROR", "503"),
    ({"status_code": None, "error": "network_error", "message": "URLError: connection refused"},
     "ASR_WORKER_UNREACHABLE", "connection refused"),
])
def test_qwen_transport_failure_keeps_actual_reason(monkeypatch, response, expected_code, expected_text):
    requests = []

    def send(request):
        requests.append(request)
        return response

    _use_worker_transport(monkeypatch, send)
    with pytest.raises(VideoBlocked) as error:
        v_asr.Qwen3CloudAdapter()._run_stage("asr", "http://audio.invalid", "sha256:x", "正文")
    assert error.value.code == expected_code
    assert expected_text in error.value.message
    assert error.value.detail["status_code"] == response["status_code"]
    if expected_code == "ASR_STAGE_FAILED":
        assert error.value.detail["job_id"] == "job_failed"
        assert "显存不足" in error.value.message
    assert len(requests) == 1, "阻断不得重提作业或下载不存在的产物"


@pytest.mark.parametrize("state", ["queued", "running", "unknown"])
def test_qwen_unfinished_job_is_not_reported_as_failed(monkeypatch, state):
    from app.digital_human import worker_client as wc

    monkeypatch.setenv(v_asr.ASR_WORKER_URL_ENV, "http://worker.invalid")
    monkeypatch.setattr(wc, "transport", lambda *a, **kw: {
        "status_code": 200, "json": {"state": state, "job_id": "job_pending"}})
    with pytest.raises(VideoBlocked) as error:
        v_asr.Qwen3CloudAdapter()._run_stage("align", "http://audio.invalid", "sha256:x", "正文")
    assert error.value.code == ("ASR_STAGE_PENDING" if state in ("queued", "running") else "ASR_STAGE_UNKNOWN")
    assert error.value.detail["job_id"] == "job_pending"
    assert state in error.value.message


def test_qwen_output_http_error_cannot_be_accepted_as_json_output(monkeypatch):
    def send(request):
        if request["method"] == "POST":
            return {"status_code": 200, "body": json.dumps({
                "state": "succeeded", "job_id": "asr",
                "output_url": "http://worker.invalid/v1/outputs/asr.json"}).encode()}
        return {"status_code": 401, "body": b'{"error":"invalid token"}'}

    _use_worker_transport(monkeypatch, send)
    with pytest.raises(VideoBlocked) as error:
        v_asr.Qwen3CloudAdapter()._run_stage("asr", "http://audio.invalid", "sha256:x", "正文")
    assert error.value.code == "ASR_OUTPUT_UNREADABLE"
    assert "401" in error.value.message


def test_unconfigured_worker_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("AVATAR_STUDIO_ASR_PROVIDER", "qwen3_cloud")
    monkeypatch.delenv("AVATAR_STUDIO_ASR_WORKER_URL", raising=False)
    adapter = v_asr.get_adapter()
    assert adapter is not None
    with pytest.raises(VideoBlocked) as e:
        adapter.transcribe(str(tmp_path / "a.wav"), "正文")
    assert e.value.code == "ASR_NOT_CONFIGURED"


def test_provider_registered_with_allowed_source():
    spec = v_asr.KNOWN_PROVIDERS["qwen3_cloud"]
    adapter = spec["adapter"]()
    assert adapter.alignment_source in v_asr.ALLOWED_ALIGNMENT_SOURCES
    assert adapter.simulated is False, "真实 provider 不得标 simulated"


def test_even_split_source_is_rejected(monkeypatch):
    """均分时长不算对齐：白名单之外的来源一律拒。"""
    monkeypatch.setenv("AVATAR_STUDIO_ASR_PROVIDER", "qwen3_cloud")

    class EvenSplit:
        provider_id = "even_split_fake"
        alignment_source = "even_split"
        simulated = False

        def transcribe(self, audio_path, script_text):
            return [{"start_ms": 0, "end_ms": 1000, "text": script_text}]

    monkeypatch.setitem(v_asr.KNOWN_PROVIDERS, "even_split_fake",
                        {"adapter": EvenSplit})
    monkeypatch.setenv("AVATAR_STUDIO_ASR_PROVIDER", "even_split_fake")
    with pytest.raises(VideoBlocked) as e:
        v_asr.require_adapter()
    assert e.value.code == "ASR_ALIGNMENT_SOURCE_INVALID"


def test_incomplete_coverage_is_blocked():
    items = [{"text": "你", "start": 0.0, "end": 0.2},
             {"text": "好", "start": 0.2, "end": 0.4}]
    # 正文里还有第三个字，对齐只给了两条
    with pytest.raises(VideoBlocked) as e:
        v_asr._restore_punctuation(items, "你好吗")
    assert e.value.code == "ALIGN_COVERAGE_INCOMPLETE"


def test_punctuation_restored_and_grouped_at_punctuation():
    text = "你有没有发现，越是想写好开头，越容易卡住？其实开头不需要惊艳，只需要具体。"
    words = [c for c in text if c not in "，。？"]
    items = [{"text": w, "start": 0.2 * i, "end": 0.2 * (i + 1)}
             for i, w in enumerate(words)]
    units = v_asr._restore_punctuation(items, text)
    assert "".join(u["text"] + u["tail"] for u in units) == text

    cues = v_asr._group_units(units)
    assert cues, "有声正文必须有字幕行"
    for c in cues:
        assert c["text"][-1] in "，。？！、；：", f"断句没落在标点上：{c['text']!r}"
    assert v_sub.normalize_text("".join(c["text"] for c in cues)) == \
        v_sub.normalize_text(text)


def test_cues_pass_subtitle_hard_checks():
    """分组结果要能过下游 check_cues：覆盖、顺序、越界、末句不贴边。"""
    text = "你有没有发现，越是想写好开头，越容易卡住？其实开头不需要惊艳，只需要具体。"
    words = [c for c in text if c not in "，。？"]
    items = [{"text": w, "start": 0.2 * i, "end": 0.2 * (i + 1)}
             for i, w in enumerate(words)]
    cues = v_asr._group_units(v_asr._restore_punctuation(items, text))

    class TL:
        def map_source_to_final(self, ms: int) -> int:
            return ms

    cue_objs = v_sub.map_cues(cues, TL())
    final_ms = int(items[-1]["end"] * 1000) + 3000
    res = v_sub.check_cues(cue_objs, final_duration_ms=final_ms, script_text=text)
    assert int(res["cue_count"]) == len(cues)


def test_qwen_point_token_keeps_text_without_inventing_duration():
    """真实对齐回归：46 秒音频中的“得”是 3.68→3.68 的点时间词。"""
    items = [{"text": "讲", "start": 3.44, "end": 3.68},
             {"text": "得", "start": 3.68, "end": 3.68},
             {"text": "头", "start": 3.68, "end": 3.92},
             {"text": "头", "start": 3.92, "end": 4.16},
             {"text": "是道", "start": 4.16, "end": 4.64}]
    import copy
    original = copy.deepcopy(items)
    units = v_asr._restore_punctuation(items, "讲得头头是道。")
    cues = v_asr._group_units(units)
    assert "".join(c["text"] for c in cues) == "讲得头头是道。"
    assert all(u["start_ms"] < u["end_ms"] for u in units)
    assert [(u["start_ms"], u["end_ms"]) for u in units] == [
        (3440, 3680), (3680, 3920), (3920, 4160), (4160, 4640)]
    assert items == original, "模型原始输出不能被改写"


@pytest.mark.parametrize("items,text", [
    ([{"text": "你", "start": 0.2, "end": 0.2},
      {"text": "好", "start": 0.2, "end": 0.6}], "你，好。"),
    ([{"text": "你", "start": 0.2, "end": 0.6},
      {"text": "好", "start": 0.6, "end": 0.6}], "你，好。"),
    ([{"text": "你", "start": 0.2, "end": 0.2},
      {"text": "们", "start": 0.2, "end": 0.2},
      {"text": "好", "start": 0.2, "end": 0.6}], "你们，好。"),
    ([{"text": "你", "start": 0.2, "end": 0.6},
      {"text": "们", "start": 0.6, "end": 0.6},
      {"text": "好", "start": 0.6, "end": 0.6}], "你们，好。"),
])
def test_boundary_point_tokens_preserve_punctuation_and_model_interval(items, text):
    cues = v_asr._group_units(v_asr._restore_punctuation(items, text))
    assert cues == [{"start_ms": 200, "end_ms": 600, "text": text}]


@pytest.mark.parametrize("items", [
    [{"text": "你", "start": 0.2, "end": 0.2}],
    [{"text": "你", "start": 0.2, "end": 0.2},
     {"text": "好", "start": 0.5, "end": 0.7}],
    [{"text": "你", "start": 0.2, "end": 0.4},
     {"text": "好", "start": 0.7, "end": 0.7}],
    [{"text": "你", "start": 0.4, "end": 0.2}],
    [{"text": "你", "start": 0.2, "end": 0.6},
     {"text": "好", "start": 0.4, "end": 0.7}],
    [{"text": "你", "start": 0.2, "end": float("nan")}],
    [{"text": "你", "start": 0.2, "end": 0.2},
     {"text": "好", "start": 0.2004, "end": 0.6}],
    [{"text": "你", "start": 0.2, "end": 0.6},
     {"text": "好", "start": 0.6004, "end": 0.6004}],
])
def test_point_token_support_does_not_accept_invalid_or_unanchored_timing(items):
    text = "".join(item["text"] for item in items)
    with pytest.raises(VideoBlocked) as error:
        v_asr._restore_punctuation(items, text)
    assert error.value.code == "ALIGN_TIMING_INVALID"
