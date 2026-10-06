"""T04 MiniMax 适配器（AC-TTS-03/04 的供应商侧）。

核心思路：真实 HTTP 与响应解析彻底分离，因此"认证失败 / 限流 / 空音频 /
截断音频 / 错误采样率 / 非法字词时间戳 / 预算未授权"这些路径全部离线验证。
本文件不联网：要么走 dry_run，要么注入 transport。

另外两类断言在这里落地：
- **失败不换声**：限流不换域名、空音频不换备用声音、HTTP 错误不重试。
- **不伪造**：时长不接受估算、字词对齐不接受均分、能力不接受"文档里有"
  当成"我们测过"。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "tts"))

from app import storage as st  # noqa: E402
from app.tts import provider as pv  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsBlocked, TtsRejected  # noqa: E402
from tts_helpers import allow_billable, make_transport, make_wav, ok_body, wav_hex  # noqa: E402

PARAMS = {"speed": 1.0, "vol": 1.0, "pitch": 0, "emotion": "calm",
          "language_boost": "Chinese"}
TEXT = "检索质量决定最终回答质量。"
# 一段无法解出时长的 mp3 头：用于验证"容器解不出时才用 extra_info"
MP3_HEX = (b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00").hex()


def test_synthesis_lease_covers_provider_timeout(monkeypatch, work, voice):
    from app import jobs

    allow_billable(monkeypatch)
    original = jobs.acquire_lease
    ttls = []
    def acquire(*args, **kwargs):
        ttls.append(kwargs.get("ttl_seconds", jobs.lease_ttl_seconds()))
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, "acquire_lease", acquire)
    transport, _ = make_transport(body=ok_body(duration_ms=1800))
    pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS, text=TEXT, transport=transport)
    assert ttls[0] > pv.DEFAULT_TIMEOUT_S


def test_unexpected_transport_exception_is_safe_and_recorded(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    secret = "test-provider-private-token"
    def transport(_):
        raise RuntimeError(secret)
    with pytest.raises(TtsBlocked) as err:
        pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS, text=TEXT, transport=transport)
    assert secret not in str(err.value)
    jobs = st.all_rows("provider_jobs", "work_id=?", [work["id"]])
    assert jobs[-1]["state"] == "failed"
    assert secret not in str(jobs[-1])


def _req(**kw) -> dict:
    base = dict(format="wav", sample_rate=32000, channels=1, text_len=len(TEXT))
    base.update(kw)
    return base


# ---------- 请求构造 ----------

def test_build_request_carries_four_part_scope(voice):
    req = pv.build_request(voice=voice, provider_params=PARAMS, text=TEXT)
    body = req["json"]
    assert body["model"] == "speech-2.8-hd"
    assert body["voice_setting"]["voice_id"] == vc.PRESET_REMOTE_VOICE_ID
    assert body["voice_setting"]["emotion"] == "calm"
    assert body["output_format"] == "hex"
    assert body["audio_setting"] == {"sample_rate": 32000, "bitrate": 128000,
                                     "format": "wav", "channel": 1}
    assert req["scope_key"] == "minimax|speech-2.8-hd|cn|Chinese (Mandarin)_Warm_Bestie"
    assert "Authorization" not in req["headers"]  # 密钥只在真正发请求时才注入


def test_params_cannot_override_remote_voice_id(voice):
    """参数里塞 voice_id 不得改变实际发音的音色：作用域只能来自资产登记。"""
    req = pv.build_request(voice=voice, provider_params=dict(PARAMS, voice_id="别人的音色"),
                           text=TEXT)
    assert req["json"]["voice_setting"]["voice_id"] == vc.PRESET_REMOTE_VOICE_ID


@pytest.mark.parametrize("text", ["", None])
def test_empty_text_rejected(voice, text):
    with pytest.raises(TtsRejected) as e:
        pv.build_request(voice=voice, provider_params=PARAMS, text=text or "")
    assert e.value.code == "TTS_TEXT_EMPTY"


@pytest.mark.parametrize("kw,code", [
    ({"audio_format": "aac"}, "TTS_FORMAT_UNSUPPORTED"),
    ({"sample_rate": 12345}, "TTS_SAMPLE_RATE_UNSUPPORTED"),
    ({"channels": 3}, "TTS_CHANNELS_UNSUPPORTED"),
])
def test_request_spec_outside_declared_range_rejected(voice, kw, code):
    with pytest.raises(TtsRejected) as e:
        pv.build_request(voice=voice, provider_params=PARAMS, text=TEXT, **kw)
    assert e.value.code == code


def test_declared_unsupported_control_rejected(voice):
    with pytest.raises(TtsRejected) as e:
        pv.build_request(voice=voice, provider_params=dict(PARAMS, whisper=True), text=TEXT)
    assert e.value.code == "VOICE_CONTROL_UNSUPPORTED"


def test_build_request_requires_complete_scope():
    incomplete = {"id": "voice_x", "provider": "minimax", "model": "speech-2.8-hd",
                  "region": "cn", "remote_voice_id": ""}
    with pytest.raises(TtsRejected) as e:
        pv.build_request(voice=incomplete, provider_params=PARAMS, text=TEXT)
    assert e.value.code == "VOICE_SCOPE_INCOMPLETE"


# ---------- transport：未配置密钥不得"偷偷发出去" ----------

def test_transport_without_key_does_not_issue_request(monkeypatch, voice):
    monkeypatch.delenv(pv.SECRET_ENV, raising=False)
    req = pv.build_request(voice=voice, provider_params=PARAMS, text=TEXT)
    resp = pv.urllib_transport(req)
    assert resp["status_code"] is None and resp["error"] == "not_configured"
    assert pv.SECRET_ENV in resp["message"]


# ---------- 响应归一：认证 / 限流 / 网络 ----------

@pytest.mark.parametrize("status,expected", [
    (401, "AUTH_FAILED"),
    (403, "AUTH_FAILED"),
    (429, "QUOTA_EXHAUSTED"),
    (503, "NETWORK_ERROR"),
])
def test_http_error_classified_and_blocked(voice, status, expected):
    transport, calls = make_transport(status_code=status, message="boom")
    with pytest.raises(TtsBlocked) as e:
        pv.parse_response(transport(None), requested=_req())
    assert e.value.code == f"TTS_PROVIDER_{expected}"
    assert len(calls) == 1


@pytest.mark.parametrize("code,expected", [
    (1004, "AUTH_FAILED"),
    (1002, "AUTH_FAILED"),
    (1006, "QUOTA_EXHAUSTED"),
])
def test_base_resp_error_normalized(code, expected):
    body = {"base_resp": {"status_code": code, "status_msg": "failed"}}
    with pytest.raises(TtsBlocked) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == f"TTS_PROVIDER_{expected}"


def test_network_error_is_blocked_not_retried():
    resp = {"status_code": None, "error": "network_error", "message": "timeout"}
    with pytest.raises(TtsBlocked) as e:
        pv.parse_response(resp, requested=_req())
    assert e.value.code == "TTS_PROVIDER_UNREACHABLE"
    assert "不自动重试" in e.value.message


# ---------- 解析：时长与规格 ----------

def test_ok_response_duration_from_container():
    parsed = pv.parse_response({"status_code": 200, "body": ok_body(duration_ms=1500)},
                               requested=_req())
    assert parsed["duration_ms"] == 1500
    assert parsed["duration_source"] == "container"
    assert parsed["sample_rate"] == 32000 and parsed["channels"] == 1
    assert parsed["audio_format"] == "wav"
    assert parsed["file_hash"].startswith("sha256:")
    assert parsed["usage_characters"] == 9
    assert parsed["provider_request_id"] == "trace_test_001"
    assert parsed["audio_bytes"][:4] == b"RIFF"


def test_container_duration_wins_over_declared_with_warning():
    body = ok_body(duration_ms=1000, declared_ms=5000)
    parsed = pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert parsed["duration_ms"] == 1000          # 以真实容器为准
    assert any("相差超过" in w for w in parsed["warnings"])


def test_duration_falls_back_to_extra_info_when_container_unknown():
    """容器解不出时长时才用 extra_info，并显式标注可信度更低。"""
    body = ok_body(duration_ms=800, audio=MP3_HEX)
    parsed = pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert parsed["audio_format"] == "mp3"
    assert parsed["duration_ms"] == 800
    assert parsed["duration_source"] == "provider_extra_info"
    assert any("与请求格式" in w for w in parsed["warnings"])
    assert any("可信度低于容器" in w for w in parsed["warnings"])


def test_duration_undetermined_is_blocked():
    """时长拿不到就必须失败：下游视频时间轴不接受估算值。"""
    body = ok_body(audio=MP3_HEX)
    body["extra_info"] = {}
    with pytest.raises(TtsBlocked) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_DURATION_UNDETERMINED"


def test_empty_audio_is_blocked_not_replaced_by_backup_voice():
    body = ok_body()
    body["data"]["audio"] = ""
    with pytest.raises(TtsBlocked) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_EMPTY_AUDIO"
    assert "不得自动换备用声音" in e.value.message


def test_invalid_hex_rejected():
    body = ok_body()
    body["data"]["audio"] = "zzzz-not-hex"
    with pytest.raises(TtsRejected) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_AUDIO_DECODE_FAILED"


def test_unknown_container_rejected():
    body = ok_body(audio=(b"\x00" * 64).hex())
    with pytest.raises(TtsRejected) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_AUDIO_CORRUPT"


def test_truncated_wav_rejected():
    """header 声明长度大于实际字节数：截断音频不得入库。"""
    body = ok_body(duration_ms=1000, truncate_bytes=2048)
    with pytest.raises(TtsRejected) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_AUDIO_TRUNCATED"


def test_sample_rate_mismatch_rejected():
    body = ok_body(duration_ms=500, sample_rate=16000)
    with pytest.raises(TtsRejected) as e:
        pv.parse_response({"status_code": 200, "body": body}, requested=_req())
    assert e.value.code == "TTS_AUDIO_SPEC_MISMATCH"
    assert "采样率" in e.value.message


def test_channels_mismatch_rejected():
    body = ok_body(duration_ms=500, channels=2)
    with pytest.raises(TtsRejected) as e:
        pv.parse_response({"status_code": 200, "body": body},
                          requested=_req(channels=1))
    assert e.value.code == "TTS_AUDIO_SPEC_MISMATCH"


# ---------- 字词时间戳：校验通过才敢用 ----------

WORDS_OK = [{"segment_id": "seg_1", "start_char": 0, "end_char": 5,
             "start_ms": 0, "end_ms": 400},
            {"segment_id": "seg_1", "start_char": 5, "end_char": 12,
             "start_ms": 400, "end_ms": 900}]


def test_valid_words_kept_with_provider_source():
    parsed = pv.parse_response({"status_code": 200, "body": ok_body(duration_ms=1000,
                                                                   words=WORDS_OK)},
                               requested=_req())
    assert parsed["timing_source"] == "provider"
    assert len(parsed["words"]) == 2


def test_no_words_marks_unaligned_without_claiming():
    parsed = pv.parse_response({"status_code": 200, "body": ok_body(duration_ms=1000)},
                               requested=_req())
    assert parsed["timing_source"] is None and parsed["words"] == []
    assert any("不得宣称字词对齐已完成" in w or "尚未验收" in w
               for w in parsed["warnings"])


@pytest.mark.parametrize("words,reason_kw", [
    ([{"start_char": 0, "end_char": 5, "start_ms": -10, "end_ms": 100}], "非法"),
    ([{"start_char": 0, "end_char": 5, "start_ms": 0, "end_ms": 5000}], "超出音频时长"),
    ([{"start_char": 0, "end_char": 5, "start_ms": 0, "end_ms": 900},
      {"start_char": 5, "end_char": 9, "start_ms": 100, "end_ms": 950}], "重叠"),
    ([{"start_char": 0, "end_char": 5, "start_ms": 0, "end_ms": 900},
      {"start_char": 3, "end_char": 20, "start_ms": 900, "end_ms": 950}], "字符区间"),
])
def test_bad_words_dropped_and_recorded(words, reason_kw):
    """校验不通过就把时间戳整体置空并留证，绝不均分伪造一份"已对齐"。"""
    parsed = pv.parse_response({"status_code": 200, "body": ok_body(duration_ms=1000,
                                                                   words=words)},
                               requested=_req())
    assert parsed["words"] == [] and parsed["timing_source"] is None
    assert any(reason_kw in w for w in parsed["warnings"])


def test_even_split_is_forbidden():
    with pytest.raises(TtsRejected) as e:
        pv.words_from_even_split(3, 1000)
    assert e.value.code == "TTS_EVEN_SPLIT_FORBIDDEN"


def test_validate_word_timings_unit():
    ok, reasons = pv.validate_word_timings([], duration_ms=1000)
    assert not ok and "没有任何字词时间戳" in reasons[0]
    ok, reasons = pv.validate_word_timings(WORDS_OK, duration_ms=1000, text_len=12)
    assert ok and reasons == []
    ok, reasons = pv.validate_word_timings(WORDS_OK, duration_ms=1000, text_len=8)
    assert not ok and any("超出文本长度" in r for r in reasons)


# ---------- 运行约束：预算先于副作用 ----------

def test_without_budget_nothing_is_sent(work, voice):
    """默认预算为 0：作业停在 blocked，transport 一次都不被调用。"""
    transport, calls = make_transport(body=ok_body())
    with pytest.raises(TtsBlocked) as e:
        pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                      text=TEXT, transport=transport)
    assert e.value.code == "BUDGET_NOT_AUTHORIZED"
    assert calls == [], "预算未授权时不得发出任何真实请求"


def test_dry_run_placeholder_touches_nothing(work, voice):
    out = pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                        text=TEXT, dry_run=True)
    assert out["is_mock"] is True and out["simulated"] is True
    assert out["audio_bytes"] is None and out["duration_ms"] is None
    assert out["request_digest"].startswith("sha256:")
    assert "不得作为最终音频" in out["note"]


def test_replayed_submission_is_not_recharged(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=600))
    first = pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                          text=TEXT, transport=transport)
    assert first["is_mock"] is False and len(calls) == 1
    with pytest.raises(TtsBlocked) as e:
        pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                      text=TEXT, transport=transport)
    assert e.value.code == "TTS_JOB_REPLAYED"
    assert len(calls) == 1, "重放不得再发一次请求"


def test_provider_failure_marks_job_failed_and_does_not_retry(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(status_code=429, message="rate limited")
    with pytest.raises(TtsBlocked):
        pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                      text=TEXT, transport=transport)
    assert len(calls) == 1, "限流不得重试、不得换域名"
    rows = st.all_rows("provider_jobs", "work_id=?", [work["id"]])
    assert rows and rows[-1]["state"] == "failed"


def test_success_registers_job_and_returns_real_specs(monkeypatch, work, voice):
    allow_billable(monkeypatch)
    transport, calls = make_transport(body=ok_body(duration_ms=1200, sample_rate=32000))
    out = pv.synthesize(work_id=work["id"], voice=voice, provider_params=PARAMS,
                        text=TEXT, transport=transport)
    assert out["job_id"] and out["duration_ms"] == 1200
    assert out["duration_source"] == "container"
    assert out["is_mock"] is False
    assert len(calls) == 1


# ---------- 能力声明：区分"文档里有"与"我们测过" ----------

def test_capabilities_separate_declared_and_verified():
    caps = pv.capabilities()
    assert caps["declared"]["word_timestamps"] is True
    assert caps["verified"]["word_timestamps"] is False, \
        "词级时间戳尚未验收，不得声明为已实测"
    assert caps["declared"]["max_text_chars"] is None
    assert caps["verified"]["emotions"] == ["calm"]
    assert caps["declared"]["unsupported_controls"] == ["whisper"]


def test_adapter_status_reports_not_configured(monkeypatch):
    monkeypatch.delenv(pv.SECRET_ENV, raising=False)
    st = pv.adapter_status()
    assert st["state"] == "not_configured"
    assert st["budget"]["max_billable_submissions"] == 0


def test_adapter_status_configured_but_unverified(monkeypatch):
    """配置项存在不等于可用：没有实测证据时不得报 ready。"""
    monkeypatch.setenv(pv.SECRET_ENV, "sk-test-not-used")
    st = pv.adapter_status()
    assert st["configured"] is True
    assert st["state"] in ("unverified", "ready")
    if st["state"] == "unverified":
        assert "尚未完成真实 TTS 连通" in st["detail"]
    assert st["capabilities"]["verified"]["word_timestamps"] is False


# ---------- 容器工具 ----------

def test_detect_container_recognizes_formats():
    assert pv.detect_container(make_wav()) == "wav"
    assert pv.detect_container(b"ID3\x04\x00\x00") == "mp3"
    assert pv.detect_container(b"\xff\xfb\x90\x00") == "mp3"
    assert pv.detect_container(b"fLaC\x00\x00") == "flac"
    assert pv.detect_container(b"\x00\x01\x02") is None


def test_parse_wav_reports_truncation_and_specs():
    info = pv.parse_wav(make_wav(duration_ms=2500, sample_rate=44100, channels=2))
    assert info["sample_rate"] == 44100 and info["channels"] == 2
    assert info["truncated"] is False and 2400 <= info["duration_ms"] <= 2600
    bad = pv.parse_wav(make_wav(duration_ms=1000, truncate_bytes=1024))
    assert bad["truncated"] is True


def test_file_hash_and_request_digest_are_stable():
    assert pv.file_hash(b"abc") == pv.file_hash(b"abc")
    a = pv.request_digest("speech-2.8-hd", "scope", PARAMS, TEXT)
    b = pv.request_digest("speech-2.8-hd", "scope", PARAMS, TEXT)
    c = pv.request_digest("speech-2.8-hd", "scope", dict(PARAMS, speed=0.9), TEXT)
    assert a == b and a != c


def test_wav_hex_helper_is_parseable():
    assert pv.parse_wav(bytes.fromhex(wav_hex(duration_ms=100)))["duration_ms"] == 100
