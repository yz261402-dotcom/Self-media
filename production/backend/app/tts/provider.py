"""MiniMax Speech 2.8 HD 适配器（T04）。

设计原则：

1. **能力可声明、但必须区分"声称"与"实测"**。官方文档声明的东西写进
   `DECLARED`，本项目真实核验过的写进 `VERIFIED`。任何对外输出都同时带上两者，
   避免把"文档里有"读成"我们测过"。
2. **transport 可注入**。真实 HTTP 调用与响应解析彻底分离，因此认证失败、
   限流、空音频、截断音频、错误采样率、非法字词时间戳这些路径都能在**离线**
   状态下被测试钉死，不需要网络也不需要花钱。
3. **失败不换声**。AC-TTS-03：主音色不存在/被删/限流/单句失败/返回空音频时，
   显式阻塞并保留已完成片段；绝不自动切换备用声音、绝不混入 Mock、
   绝不因为是限流就换个域名重提。
4. **时长必须来自真实音频**。容器头能解出时长就以容器为准；供应商额外字段
   只能交叉校验。两者都拿不到时**失败**，而不是估一个数——
   后续视频时长与字幕时间轴全部依赖它。
5. **字词时间戳只信校验通过的**。校验不通过就置空并留证，
   绝不均分时长伪造一份"已对齐"。
"""
from __future__ import annotations

import hashlib
import json
import struct
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import storage as st
from ..config import budget_policy, classify_provider_error, provider_evidence
from ..contracts import now_iso
from . import voices as vc
from .errors import TtsBlocked, TtsRejected

# 官方同步接口。超时后不自动换域名重提。
MINIMAX_T2A_URL = "https://api.minimax.cn/v1/t2a_v2"
DEFAULT_TIMEOUT_S = 60.0
SECRET_ENV = "AVATAR_STUDIO_MINIMAX_API_KEY"

# 官方文档声明的能力（"声明"不是"实测"）
DECLARED: Dict[str, Any] = {
    "model": "speech-2.8-hd",
    "endpoint": MINIMAX_T2A_URL,
    "region": "cn",
    "audio_formats": ["mp3", "wav", "flac", "pcm"],
    "sample_rates": [8000, 16000, 22050, 24000, 32000, 44100],
    "channels": [1, 2],
    "supported_controls": ["speed", "vol", "pitch", "emotion", "language_boost", "voice_modify"],
    "emotions": list(vc.EMOTIONS),
    "unsupported_controls": ["whisper"],
    "max_text_chars": None,  # 官方上限未核验
    "word_timestamps": True,  # 文档有 subtitle_type=word，粒度说明存在差异
}

# 本项目真实核验过的能力（来自已选样音证据与本工程实测）
VERIFIED: Dict[str, Any] = {
    "model": "speech-2.8-hd",
    "endpoint": MINIMAX_T2A_URL,
    "region": "cn",
    "audio_formats": ["wav"],
    "sample_rate": 32000,
    "channels": 1,
    "supported_controls": ["speed", "vol", "pitch", "emotion", "language_boost"],
    "emotions": ["calm"],  # 仅 calm 有试听证据
    "max_text_chars": None,
    "word_timestamps": False,  # 词级时间戳尚未验收
}

# bitrate 与 format 的对应：PCM/WAV 无损，不需要码率
_FORMAT_BITRATE = {"mp3": 128000, "wav": 128000, "flac": 0, "pcm": 0}

# 时长交叉校验容差：供应商 extra_info 与容器解出的时长允许的差异
DURATION_TOLERANCE_MS = 200

Transport = Callable[[Dict[str, Any]], Dict[str, Any]]


# ---------- 文件哈希与摘要 ----------

def file_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request_digest(model: str, scope_key: str, params: Dict[str, Any], text: str) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        {"model": model, "scope": scope_key, "params": params, "text": text},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# ---------- 能力 ----------

def capabilities() -> Dict[str, Any]:
    """对外能力视图。声明与实测分开，未核验项显式标注。"""
    return {
        "provider": "minimax",
        "declared": DECLARED,
        "verified": VERIFIED,
        "notes": [
            "同一官方服务的其他文档域名不能视为均已测通；超时后不自动换域名重提",
            "官方声明不支持 whisper，不得因通用枚举里出现就启用",
            "词级时间戳尚未验收：拿到即校验，校验不通过则不声明已对齐",
            "官方最大文本长度未核验，本地只用保守值做切分自检",
        ],
    }


# ---------- 请求构造 ----------

def build_request(*, voice: Dict[str, Any], provider_params: Dict[str, Any], text: str,
                  audio_format: str = "wav", sample_rate: int = 32000,
                  channels: int = 1, stream: bool = False) -> Dict[str, Any]:
    """构造请求体。纯函数：不触网、不读环境变量，便于逐字段断言。"""
    scope = vc.assert_scope_complete(voice)
    vc.assert_model_allowed(scope["provider"], scope["model"])
    vc.assert_region_verified(scope["provider"], scope["region"])
    if not text:
        raise TtsRejected("TTS_TEXT_EMPTY", "合成文本为空，不能提交")
    if audio_format not in DECLARED["audio_formats"]:
        raise TtsRejected("TTS_FORMAT_UNSUPPORTED",
                          f"音频格式 {audio_format} 不在供应商声明范围内",
                          {"format": audio_format, "allowed": DECLARED["audio_formats"]})
    if sample_rate not in DECLARED["sample_rates"]:
        raise TtsRejected("TTS_SAMPLE_RATE_UNSUPPORTED",
                          f"采样率 {sample_rate} 不在供应商声明范围内",
                          {"sample_rate": sample_rate})
    if channels not in DECLARED["channels"]:
        raise TtsRejected("TTS_CHANNELS_UNSUPPORTED",
                          f"声道数 {channels} 不在供应商声明范围内", {"channels": channels})
    for key in DECLARED["unsupported_controls"]:
        if provider_params.get(key) not in (None, "", False):
            raise TtsRejected("VOICE_CONTROL_UNSUPPORTED",
                              f"{key} 官方声明不支持，不得启用", {"key": key})
    params = vc.normalize_profile({k: v for k, v in provider_params.items()
                                   if k not in ("model", "voice_id")})
    request = {
        "url": MINIMAX_T2A_URL,
        "json": {
            "model": scope["model"],
            "text": text,
            "stream": stream,
            "language_boost": params.get("language_boost", "Chinese"),
            "output_format": "hex",
            "voice_setting": {
                "voice_id": scope["remote_voice_id"],
                "speed": params.get("speed", 1.0),
                "vol": params.get("vol", 1.0),
                "pitch": params.get("pitch", 0),
            },
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": _FORMAT_BITRATE.get(audio_format, 128000),
                "format": audio_format,
                "channel": channels,
            },
            "subtitle_enable": True,
        },
        "timeout": DEFAULT_TIMEOUT_S,
        "headers": {"Content-Type": "application/json"},
        "scope_key": scope["scope_key"],
    }
    if params.get("emotion", "calm") != "auto":
        request["json"]["voice_setting"]["emotion"] = params.get("emotion", "calm")
    if params.get("voice_modify"):
        request["json"]["voice_modify"] = params["voice_modify"]
    return request


# ---------- HTTP transport（真实实现；测试注入替身） ----------

def urllib_transport(req: Dict[str, Any]) -> Dict[str, Any]:
    """真实 HTTP 调用。**未配置密钥时不发起请求**，直接返回未配置错误。

    实现细节有意保持最小：不重试、不改域名、不吞错。
    """
    import os
    import urllib.error
    import urllib.request

    token = os.environ.get(SECRET_ENV, "")
    if not token:
        return {"status_code": None, "error": "not_configured",
                "message": f"未配置 {SECRET_ENV}：不发起真实请求，也不回退到任何替身"}

    headers = dict(req.get("headers") or {})
    headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(req["json"], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(req["url"], data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=req.get("timeout", DEFAULT_TIMEOUT_S)) as resp:
            raw = resp.read()
            return {"status_code": int(resp.status), "raw": raw,
                    "body": _try_json(raw)}
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        return {"status_code": int(getattr(e, "code", 0) or 0), "raw": raw,
                "body": _try_json(raw), "message": str(e)}
    except Exception as e:  # 网络层异常：明确归类，不伪装成"模型不可用"
        return {"status_code": None, "error": "network_error", "message": type(e).__name__}


def _try_json(raw: bytes) -> Optional[dict]:
    try:
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---------- 音频容器解析 ----------

def parse_wav(data: bytes) -> Optional[Dict[str, Any]]:
    """解析 WAV 头。返回 None 表示不是合法 RIFF/WAVE。

    同时校验 data 块声明长度与实际字节数：不一致即截断，截断音频不得入库。
    """
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, data_size, data_off = 12, None, None, None
    truncated = False
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if chunk_id == b"fmt " and size >= 16:
            if body + size > len(data):
                return None
            audio_format, channels, sample_rate, _, block_align, bits = struct.unpack(
                "<HHIIHH", data[body:body + 16])
            if (channels <= 0 or sample_rate <= 0 or bits <= 0 or bits % 8
                    or block_align != channels * (bits // 8)):
                return None
            fmt = {"audio_format": audio_format, "channels": channels,
                   "sample_rate": sample_rate, "bits_per_sample": bits,
                   "block_align": block_align}
        elif chunk_id == b"data":
            declared = size
            actual = len(data) - body
            data_size = min(declared, actual)
            data_off = body
            truncated = actual < declared
        nxt = body + size + (size % 2)
        if nxt <= pos:
            break
        pos = nxt
    if not fmt or data_size is None:
        return None
    block = max(1, fmt["block_align"])
    frames = data_size // block
    duration_ms = int(round(frames * 1000.0 / max(1, fmt["sample_rate"])))
    return {
        "audio_format": "wav",
        "channels": fmt["channels"],
        "sample_rate": fmt["sample_rate"],
        "bits_per_sample": fmt["bits_per_sample"],
        "data_bytes": data_size,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "declared_data_bytes": declared,
    }


def detect_container(data: bytes) -> Optional[str]:
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[0:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[0:4] == b"fLaC":
        return "flac"
    return None


# ---------- 响应解析 ----------

_BASE_RESP_AUTH = {1004, 1002, 1001}
_BASE_RESP_RATE = {1006, 1039}


def parse_response(resp: Dict[str, Any], *, requested: Dict[str, Any]) -> Dict[str, Any]:
    """把供应商响应归一成可入库的结果；任何一步不成立都显式失败。"""
    if not isinstance(resp, dict):
        raise TtsBlocked("TTS_PROVIDER_RESPONSE_INVALID", "供应商响应结构无效")
    if resp.get("error"):
        raise TtsBlocked("TTS_PROVIDER_UNREACHABLE",
                         "未能取得有效响应；请检查服务配置或网络。"
                         "不自动重试、不更换音色")
    status = resp.get("status_code")
    if status != 200:
        state = classify_provider_error(status, None, resp.get("message", ""))
        raise TtsBlocked("TTS_PROVIDER_" + state.upper(),
                         f"供应商返回 HTTP {status}（{state}）：不重试计费动作、"
                         "不自动换音色",
                         {"status_code": status, "classified": state})

    body = resp.get("body")
    if not isinstance(body, dict):
        raise TtsBlocked("TTS_PROVIDER_RESPONSE_INVALID", "供应商响应不是 JSON 对象")
    base = body.get("base_resp") or {}
    if not isinstance(base, dict):
        raise TtsBlocked("TTS_PROVIDER_RESPONSE_INVALID", "供应商响应状态结构无效")
    code = base.get("status_code")
    if code not in (0, None):
        msg = base.get("status_msg", "")
        if code in _BASE_RESP_AUTH:
            state = "auth_failed"
        elif code in _BASE_RESP_RATE:
            state = "quota_exhausted"
        else:
            state = classify_provider_error(None, str(code), msg)
        raise TtsBlocked("TTS_PROVIDER_" + state.upper(),
                         f"供应商状态码 {code}：{state}",
                         {"status_code": code, "classified": state})

    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise TtsBlocked("TTS_PROVIDER_RESPONSE_INVALID", "供应商音频数据结构无效")
    hex_audio = data.get("audio")
    if not hex_audio:
        # AC-TTS-03：返回空音频必须显式阻塞，不得换备用声音补齐
        raise TtsBlocked("TTS_EMPTY_AUDIO",
                         "供应商返回空音频：显式失败并保留已完成片段，"
                         "不得自动换备用声音或拼接他人音色",
                         {"base_resp": base})
    try:
        audio = bytes.fromhex(hex_audio)
    except (ValueError, TypeError):
        raise TtsRejected("TTS_AUDIO_DECODE_FAILED",
                          "音频数据不是合法的 hex 编码，无法完整解码")
    if not audio:
        raise TtsBlocked("TTS_EMPTY_AUDIO", "解码后音频为空，显式失败")

    requested_format = requested.get("format", "wav")
    container = detect_container(audio)
    if container is None:
        raise TtsRejected("TTS_AUDIO_CORRUPT",
                          f"音频字节无法识别为已知容器（请求格式 {requested_format}）",
                          {"requested_format": requested_format,
                           "head": audio[:12].hex()})

    warnings: List[str] = []
    if container != requested_format:
        warnings.append(f"返回容器 {container} 与请求格式 {requested_format} 不一致，"
                        "已按实际容器登记")

    info = parse_wav(audio) if container == "wav" else None
    if container == "wav" and info is None:
        raise TtsRejected("TTS_AUDIO_CORRUPT", "声明为 WAV 但 RIFF/WAVE 头不合法")
    if info and info.get("truncated"):
        raise TtsRejected("TTS_AUDIO_TRUNCATED",
                          "WAV data 块声明长度大于实际字节数：音频被截断，不得入库",
                          {"declared": info.get("declared_data_bytes"),
                           "actual": info.get("data_bytes")})

    extra = (body.get("extra_info") or {})
    declared_ms = extra.get("audio_length")
    duration_ms = info["duration_ms"] if info else None
    duration_source = "container" if duration_ms is not None else None
    if duration_ms is None and isinstance(declared_ms, (int, float)) and declared_ms > 0:
        duration_ms = int(declared_ms)
        duration_source = "provider_extra_info"
        warnings.append("时长取自供应商 extra_info（容器无法解析）：可信度低于容器解出")
    if isinstance(declared_ms, (int, float)) and declared_ms > 0 and info:
        if abs(int(declared_ms) - info["duration_ms"]) > DURATION_TOLERANCE_MS:
            warnings.append(
                f"供应商声明时长 {int(declared_ms)}ms 与容器解出 {info['duration_ms']}ms "
                f"相差超过 {DURATION_TOLERANCE_MS}ms，以容器为准并留证")
    if duration_ms is None or duration_ms <= 0:
        raise TtsBlocked("TTS_DURATION_UNDETERMINED",
                         "无法确定真实音频时长：后续视频时长与字幕时间轴均依赖它，"
                         "不接受估算值，显式失败",
                         {"requested_format": requested_format})

    sample_rate = (info or {}).get("sample_rate") or extra.get("audio_sample_rate")
    channels = (info or {}).get("channels") or extra.get("audio_channel")
    for label, actual, wanted in (("采样率", sample_rate, requested.get("sample_rate")),
                                  ("声道数", channels, requested.get("channels"))):
        if wanted and actual and int(actual) != int(wanted):
            raise TtsRejected("TTS_AUDIO_SPEC_MISMATCH",
                              f"{label}与请求不符：请求 {wanted}，实际 {actual}。"
                              "后续 MuseTalk/字幕转换会依赖该规格，不接受静默差异",
                              {"requested": wanted, "actual": actual})

    words, timing_source, word_notes = _parse_words(
        body, duration_ms=duration_ms, audio_digest=file_hash(audio),
        text_len=requested.get("text_len"))
    warnings.extend(word_notes)

    return {
        "audio_bytes": audio,
        "file_hash": file_hash(audio),
        "audio_format": container,
        "sample_rate": int(sample_rate) if sample_rate else None,
        "channels": int(channels) if channels else None,
        "duration_ms": int(duration_ms),
        "duration_source": duration_source,
        "declared_duration_ms": int(declared_ms) if isinstance(declared_ms, (int, float)) else None,
        "usage_characters": extra.get("usage_characters"),
        "provider_request_id": body.get("trace_id") or data.get("trace_id"),
        "words": words,
        "timing_source": timing_source,
        "warnings": warnings,
    }


def _parse_words(body: Dict[str, Any], *, duration_ms: int, audio_digest: str,
                 text_len: Optional[int]) -> Tuple[List[Dict[str, Any]], Optional[str], List[str]]:
    """解析并**校验**原生字词时间戳。校验不通过即置空，绝不均分伪造。"""
    notes: List[str] = []
    data = body.get("data") or {}
    raw = data.get("subtitles") or data.get("words") or []
    if not raw:
        notes.append("响应未含字词时间戳：该能力的实际粒度尚未验收，"
                     "按未对齐处理，由后期补齐后另行登记对齐产物")
        return [], None, notes

    words: List[Dict[str, Any]] = []
    for i, w in enumerate(raw):
        try:
            words.append({
                "segment_id": str(w.get("segment_id") or w.get("segment") or "seg_1"),
                "start_char": int(w.get("start_char", w.get("text_start", -1))),
                "end_char": int(w.get("end_char", w.get("text_end", -1))),
                "start_ms": int(w.get("start_ms", w.get("start_time", -1))),
                "end_ms": int(w.get("end_ms", w.get("end_time", -1))),
            })
        except (TypeError, ValueError):
            notes.append(f"第 {i} 条时间戳字段无法解析：整体按未对齐处理")
            return [], None, notes

    ok, reasons = validate_word_timings(words, duration_ms=duration_ms, text_len=text_len)
    if not ok:
        notes.append("原生字词时间戳校验未通过，已置空并按未对齐处理：" + "；".join(reasons))
        return [], None, notes
    return words, "provider", notes


def validate_word_timings(words: List[Dict[str, Any]], *, duration_ms: int,
                          text_len: Optional[int] = None,
                          tolerance_ms: int = 50) -> Tuple[bool, List[str]]:
    """字词时间戳的确定性校验。错误时间戳必须被拒，而不是直接拿去建字幕。"""
    reasons: List[str] = []
    if not words:
        return False, ["没有任何字词时间戳"]
    prev_end = None
    prev_char_end = None
    for i, w in enumerate(words):
        s, e = w.get("start_ms"), w.get("end_ms")
        if s is None or e is None or s < 0 or e <= s:
            reasons.append(f"第 {i} 条区间非法：start={s} end={e}")
            continue
        if s > duration_ms + tolerance_ms:
            reasons.append(f"第 {i} 条起点 {s}ms 超出音频时长 {duration_ms}ms")
        if e > duration_ms + tolerance_ms:
            reasons.append(f"第 {i} 条终点 {e}ms 超出音频时长 {duration_ms}ms")
        if prev_end is not None and s < prev_end - tolerance_ms:
            reasons.append(f"第 {i} 条与上一条时间重叠（{s} < {prev_end}）")
        prev_end = max(prev_end or 0, e)
        cs, ce = w.get("start_char"), w.get("end_char")
        if cs is None or ce is None or cs < 0 or ce <= cs:
            reasons.append(f"第 {i} 条字符区间非法：{cs}-{ce}")
            continue
        if text_len is not None and ce > text_len:
            reasons.append(f"第 {i} 条字符区间 {ce} 超出文本长度 {text_len}")
        if prev_char_end is not None and cs < prev_char_end:
            reasons.append(f"第 {i} 条字符区间与上一条重叠（{cs} < {prev_char_end}）")
        prev_char_end = ce
    return (not reasons), reasons


def words_from_even_split(*_args: Any, **_kwargs: Any) -> None:
    """显式禁止：均分时长不是对齐。

    保留这个函数只为让"不能均分伪造对齐"这条规则可被测试直接断言，
    任何调用都会失败。真实对齐须由 ASR/强制对齐产出并登记对齐产物版本。
    """
    raise TtsRejected(
        "TTS_EVEN_SPLIT_FORBIDDEN",
        "禁止用均分时长伪造字词对齐：只有平均分配或未验证逐句时长时，"
        "不得宣称字词对齐已完成",
    )


# ---------- 运行登记 ----------

def _estimate_amount_fen(text: str) -> int:
    """费用估算。本项目尚未取得可核验的 TTS 单价，因此不伪造费率。"""
    return 0


def submit_provider_run(*, work_id: str, voice: Dict[str, Any], provider_params: Dict[str, Any],
                        text: str, amount_fen: int = 0, task_id: Optional[str] = None,
                        idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """经作业台账提交一次 TTS 运行。重放不重复计费。"""
    from .. import jobs

    scope = vc.assert_scope_complete(voice)
    digest = request_digest(scope["model"], scope["scope_key"], provider_params, text)
    key = idempotency_key or f"tts:{work_id}:{digest}"
    return jobs.submit(work_id=work_id, capability="tts", provider=scope["provider"],
                       model=scope["model"],
                       args={"digest": digest, "voice_asset_id": voice["id"]},
                       idempotency_key=key, task_id=task_id, billable=True,
                       amount_fen=amount_fen,
                       note="TTS 合成；失败保留已完成片段，不换音色、不静默重试")


# ---------- 主入口 ----------

def _failed_attempt(job_id: str, worker: str, error: dict, *,
                    not_submitted: bool = False, unknown: bool = False) -> None:
    """释放租约；只有已确定未发出 HTTP 的尝试才撤销待结算费用。"""
    from .. import jobs
    jobs.fail(job_id, worker, error)
    if unknown:
        jobs.mark_unknown(job_id, reason=error["message"])
    if not_submitted:
        with st.transaction() as connection:
            abandoned = jobs.get_job(job_id)
            assert abandoned is not None
            abandoned.update(estimated_amount_fen=abandoned.get("amount_fen", 0),
                             amount_fen=0, settlement_required=False, not_submitted=True)
            st.insert_row(connection, "provider_jobs", job_id, abandoned,
                          {"work_id": abandoned["work_id"]})


def _clone_failure_kind(response: Any) -> str:
    """畸形供应商结构也必须安全收敛，不能在错误处理路径再次崩溃。"""
    if not isinstance(response, dict):
        return "unknown"
    if response.get("error") == "not_configured":
        return "not_submitted"
    status = response.get("status_code")
    body = response.get("body")
    body = body if isinstance(body, dict) else {}
    base, data = body.get("base_resp"), body.get("data")
    code = base.get("status_code") if isinstance(base, dict) else None
    if isinstance(data, dict) and data.get("audio"):
        return "unknown"
    known_http = isinstance(status, int) and 400 <= status < 500 and status != 408
    known_code = isinstance(code, int) and code not in (0, 1000, 1001, 1013)
    return "rejected" if known_http or known_code else "unknown"


def synthesize(*, work_id: str, voice: Dict[str, Any], provider_params: Dict[str, Any],
               text: str, transport: Optional[Transport] = None, audio_format: str = "wav",
               sample_rate: int = 32000, channels: int = 1, amount_fen: int = 0,
               task_id: Optional[str] = None, dry_run: bool = False,
               idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """合成音频。真实调用受模式、密钥、预算三重约束。

    `dry_run=True` 时不触网，返回 `is_mock=True` 的占位结果；
    该结果**不得**被确认为最终音频（见 gate.py）。
    """
    request = build_request(voice=voice, provider_params=provider_params, text=text,
                            audio_format=audio_format, sample_rate=sample_rate,
                            channels=channels)

    if dry_run:
        return {
            "is_mock": True, "simulated": True, "audio_bytes": None, "file_hash": "",
            "audio_format": audio_format, "sample_rate": sample_rate, "channels": channels,
            "duration_ms": None, "duration_source": None, "words": [], "timing_source": None,
            "usage_characters": None, "provider_request_id": None,
            "request_digest": request_digest(request["json"]["model"], request["scope_key"],
                                             provider_params, text),
            "warnings": ["MOCK：未提交真实 Provider，未产生费用与音频字节"],
            "note": "占位结果，不继承任何质量通过结论，不得作为最终音频",
        }

    clone = None
    if voice.get("clone_metadata"):
        from . import cloning as clone
        amount_fen = max(amount_fen, clone.synthesis_charge(voice, text))
    job = submit_provider_run(work_id=work_id, voice=voice, provider_params=provider_params,
                              text=text, amount_fen=amount_fen, task_id=task_id,
                              idempotency_key=idempotency_key)
    if job.get("state") == "blocked":
        raise TtsBlocked(job.get("block_code", "BUDGET_NOT_AUTHORIZED"),
                         job.get("block_detail", "预算未授权，未提交任何真实请求"),
                         {"job_id": job.get("id")})
    if job.get("replayed"):
        raise TtsBlocked("TTS_JOB_REPLAYED",
                         "该参数组合已有在册运行：复用其产物而不是重复计费提交",
                         {"job_id": job.get("id")})

    from .. import jobs

    worker = f"tts-{st.new_id('w')}"
    jobs.acquire_lease(job["id"], worker,
                       ttl_seconds=max(jobs.lease_ttl_seconds(), int(request["timeout"]) + 30))
    if clone:
        try:
            clone.begin_activation(voice["id"], job["id"])
        except (TtsBlocked, TtsRejected) as e:
            _failed_attempt(job["id"], worker, {"code": e.code, "message": e.message}, not_submitted=True)
            raise
    xport = transport or urllib_transport
    resp = None
    try:
        resp = xport(request)
        parsed = parse_response(resp, requested={"format": audio_format,
                                                 "sample_rate": sample_rate,
                                                 "channels": channels,
                                                 "text_len": len(text)})
    except (TtsBlocked, TtsRejected) as e:
        kind = _clone_failure_kind(resp) if clone else "rejected"
        if clone:
            hook = clone.cancel_activation if kind != "unknown" else clone.mark_activation_unknown
            hook(voice["id"], job["id"])
        _failed_attempt(job["id"], worker, {"code": e.code, "message": e.message},
                        not_submitted=kind == "not_submitted", unknown=bool(clone and kind == "unknown"))
        raise
    except Exception as e:
        if clone:
            clone.mark_activation_unknown(voice["id"], job["id"])
        message = f"语音服务请求未取得可用结果（{type(e).__name__}），未自动重试"
        _failed_attempt(job["id"], worker, {"code": "TTS_PROVIDER_UNREACHABLE", "message": message},
                        unknown=bool(clone))
        raise TtsBlocked("TTS_PROVIDER_UNREACHABLE", message) from None
    if clone:
        clone.mark_activated(voice["id"], job["id"])
    jobs.complete(job["id"], worker, {
        "file_hash": parsed["file_hash"], "duration_ms": parsed["duration_ms"],
        "timing_source": parsed["timing_source"],
        "note": "真实响应已解析；产物质量由音频门禁与人工听检判定",
    })
    parsed.update({"is_mock": False, "simulated": False, "job_id": job["id"],
                   "request_digest": request_digest(request["json"]["model"],
                                                    request["scope_key"],
                                                    provider_params, text)})
    return parsed


def adapter_status() -> Dict[str, Any]:
    """适配器当前可用性。区分"未配置"与"已实测"，不把配置项存在报成可用。"""
    import os

    configured = bool(os.environ.get(SECRET_ENV))
    rows = provider_evidence("minimax", "tts")
    succeeded = [r for r in rows if r.get("outcome") == "succeeded"]
    if not configured:
        state = "not_configured"
        detail = f"未配置 {SECRET_ENV}；URL/模型名齐全不等于可用"
    elif succeeded:
        state = "ready"
        detail = f"已有实测证据：{succeeded[-1].get('evidence')}"
    else:
        state = "unverified"
        detail = "配置项存在，但本项目尚未完成真实 TTS 连通与词级时间戳实测"
    return {"provider": "minimax", "state": state, "detail": detail,
            "configured": configured, "capabilities": capabilities(),
            "budget": budget_policy().to_dict()}
