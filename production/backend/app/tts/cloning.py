"""MiniMax 音色克隆：保留原始样本、单次提交台账和首次激活费用状态。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import config, storage as st
from ..avatar import licensing
from ..contracts import now_iso
from ..video import ffmpeg
from . import audio, sources, voices
from .catalog import DEFAULT_SAMPLE_TEXT
from .errors import TtsBlocked, TtsError, TtsRejected

MAX_AUDIO_BYTES = 20_000_000
MAX_REQUEST_BYTES = 4 * ((MAX_AUDIO_BYTES + 2) // 3) + 32_768
CLONE_FEE_FEN = 990
MINIMAX_BASE_URL = "https://api.minimax.cn/v1"
TABLE = "voice_clone_jobs"
Transport = Callable[[dict[str, Any]], dict[str, Any]]
NOTE = ("音色克隆创建不自动试听。首次正式合成收取 9.9 元/音色及合成费；"
        "需在创建后 7 天内正式合成以保留音色。请依次试听、登记基准、批准后选用。")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validated_request(payload: dict) -> tuple[dict, bytes]:
    required = {"request_id", "name", "filename", "content_base64", "source_evidence",
                "rights_ack", "activation_cost_ack"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise TtsRejected("VOICE_CLONE_REQUEST_INVALID", "克隆请求字段不完整或包含未知字段")
    data = dict(payload)
    for field, limit in (("request_id", 128), ("name", 100), ("filename", 255),
                         ("source_evidence", 8000)):
        if not isinstance(data[field], str) or not data[field].strip() or len(data[field]) > limit:
            code = "VOICE_CLONE_SOURCE_REQUIRED" if field == "source_evidence" else "VOICE_CLONE_REQUEST_INVALID"
            raise TtsRejected(code, f"请填写有效的 {field}")
        data[field] = data[field].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data["request_id"]):
        raise TtsRejected("VOICE_CLONE_REQUEST_INVALID", "request_id 仅允许字母、数字、下划线和横线")
    if data["rights_ack"] is not True:
        raise TtsRejected("VOICE_CLONE_RIGHTS_REQUIRED", "请确认本人声音或已获得声音权利人的授权及商业使用许可")
    if data["activation_cost_ack"] is not True:
        raise TtsRejected("VOICE_CLONE_COST_ACK_REQUIRED", "请确认首次正式合成的 9.9 元音色费及合成费")
    filename = data["filename"].replace("\\", "/").rsplit("/", 1)[-1]
    if any(ord(c) < 32 for c in filename) or Path(filename).suffix.lower() not in (".mp3", ".m4a", ".wav"):
        raise TtsRejected("VOICE_CLONE_FORMAT_INVALID", "仅支持 mp3、m4a、wav 音频")
    data["filename"] = filename
    encoded = data["content_base64"]
    if not isinstance(encoded, str):
        raise TtsRejected("VOICE_CLONE_BASE64_INVALID", "音频必须为标准 Base64 字符串")
    if len(encoded) > 4 * ((MAX_AUDIO_BYTES + 2) // 3):
        raise TtsRejected("VOICE_CLONE_TOO_LARGE", "原始音频不能超过 20,000,000 字节")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise TtsRejected("VOICE_CLONE_BASE64_INVALID", "音频 Base64 无效") from None
    if len(content) > MAX_AUDIO_BYTES:
        raise TtsRejected("VOICE_CLONE_TOO_LARGE", "原始音频不能超过 20,000,000 字节")
    if not content:
        raise TtsRejected("VOICE_CLONE_AUDIO_INVALID", "音频不能为空")
    data.pop("content_base64")
    data["source_sha256"] = _sha(content)
    return data, content


def _decode_audio(path: Path, demuxer: str) -> float:
    decoded = ffmpeg.run_ffmpeg([
        "-v", "error", "-xerror", "-threads", "2", "-protocol_whitelist", "file",
        "-format_whitelist", demuxer, "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-progress", "pipe:1", "-f", "null", "-",
    ], timeout=120)
    if decoded.returncode != 0 or decoded.stderr.strip():
        raise ValueError("decode failed")
    progress = re.findall(rb"out_time_us=(\d+)", decoded.stdout)
    actual = int(progress[-1]) / 1_000_000 if progress else 0
    if not actual:
        raise ValueError("missing decoded duration")
    if not 10 <= actual <= 300:
        raise TtsRejected("VOICE_CLONE_DURATION_INVALID", "实际解码时长须在 10–300 秒之间")
    return actual


def _probe_audio(path: Path, content: bytes) -> dict:
    """受限 demuxer + 仅本地协议 + 完整解码，绝不信任浏览器声明的时长。"""
    suffix = path.suffix.lower()
    if content.startswith(b"RIFF"):
        from .provider import parse_wav
        info = parse_wav(content)
        if not info or info.get("truncated"):
            raise TtsRejected("VOICE_CLONE_AUDIO_INVALID", "WAV 音频不完整或已截断")
        if suffix != ".wav":
            raise TtsRejected("VOICE_CLONE_FORMAT_INVALID", "扩展名与实际 WAV 音频格式不一致")
    demuxer = {".wav": "wav", ".mp3": "mp3", ".m4a": "mov"}[suffix]
    try:
        result = ffmpeg._run([
            ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", demuxer, "-show_format", "-show_streams", "-of", "json", str(path),
        ], timeout=20)
        if result.returncode != 0:
            raise ValueError("probe")
        metadata = json.loads(result.stdout)
        stream = next((s for s in metadata.get("streams", []) if s.get("codec_type") == "audio"), None)
        if not stream:
            raise ValueError("missing audio")
        duration = float(stream.get("duration") or metadata.get("format", {}).get("duration") or 0)
        sample_rate, channels = int(stream.get("sample_rate", 0)), int(stream.get("channels", 0))
        if not math.isfinite(duration) or not 10 <= duration <= 300:
            raise TtsRejected("VOICE_CLONE_DURATION_INVALID", "原始音频时长须在 10–300 秒之间")
        if sample_rate <= 0 or channels not in (1, 2):
            raise ValueError("unsupported audio specification")
        actual = _decode_audio(path, demuxer)
        if abs(actual - duration) > max(0.2, duration * 0.01):
            raise ValueError("decoded duration mismatch")
        return {"duration_seconds": actual, "sample_rate": sample_rate, "channels": channels,
                "audio_format": suffix[1:], "decoded": True}
    except TtsRejected:
        raise
    except Exception:
        raise TtsRejected("VOICE_CLONE_AUDIO_INVALID", "音频无法完整解码，请提供可正常播放的 mp3、m4a 或 wav 文件") from None


def _prepare_upload_audio(path: Path, content: bytes) -> tuple[Path, bytes, dict]:
    # 部分录音工具将原始 ADTS AAC 命名为 .m4a；只兼容该已识别格式，保留原件。
    is_adts = len(content) >= 7 and content[0] == 0xFF and content[1] & 0xF6 == 0xF0
    if path.suffix.lower() != ".m4a" or not is_adts:
        return path, content, _probe_audio(path, content)
    try:
        # 先完整解码原件，避免重新封装容错丢包后掩盖原始录音损坏。
        original_duration = _decode_audio(path, "aac")
        upload_path = path.with_name("upload.m4a")
        remuxed = ffmpeg.run_ffmpeg([
            "-n", "-v", "error", "-xerror", "-protocol_whitelist", "file",
            "-format_whitelist", "aac", "-i", str(path), "-map", "0:a:0",
            "-vn", "-sn", "-dn", "-c:a", "copy", "-f", "mp4", str(upload_path),
        ], timeout=120)
        if remuxed.returncode != 0 or remuxed.stderr.strip():
            raise ValueError("remux failed")
        if upload_path.stat().st_size > MAX_AUDIO_BYTES:
            raise TtsRejected("VOICE_CLONE_TOO_LARGE", "重新封装后的音频不能超过 20,000,000 字节")
        upload_content = upload_path.read_bytes()
        metadata = _probe_audio(upload_path, upload_content)
        if abs(metadata["duration_seconds"] - original_duration) > 0.001:
            raise ValueError("remux duration mismatch")
        metadata.update(normalization="adts_to_m4a", original_duration_seconds=original_duration)
        return upload_path, upload_content, metadata
    except TtsRejected:
        raise
    except Exception:
        raise TtsRejected("VOICE_CLONE_AUDIO_INVALID", "录音无法完整解码或重新封装，请检查原始音频是否完整") from None


def http_transport(req: dict) -> dict:
    """只执行一次 HTTP；不保存凭证、原始响应或异常字符串。"""
    import httpx

    key = os.environ.get("AVATAR_STUDIO_MINIMAX_API_KEY", "")
    if not key:
        return {"status_code": None, "error": "not_configured"}
    headers = {"Authorization": f"Bearer {key}"}
    try:
        options = {"json": req["json"]} if "json" in req else {"data": req["data"], "files": req["files"]}
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            response = client.post(req["url"], headers=headers, **options)
        try:
            body = response.json()
        except ValueError:
            body = None
        return {"status_code": response.status_code, "body": body}
    except Exception:
        return {"status_code": None, "error": "network_error"}


def get_clone(request_id: str) -> dict | None:
    return st.get(TABLE, request_id)


def _result(job: dict) -> dict:
    voice = st.get("voices", job["voice_asset_id"]) if job.get("state") == "succeeded" else None
    return {"voice": voice, "clone_job": job, "note": NOTE}


def _save(job: dict, **changes: Any) -> None:
    job.update(changes, updated_at=now_iso())
    st.put(TABLE, job["id"], job)


def _call(job: dict, transport: Transport, req: dict) -> dict | None:
    try:
        response = transport(req)
    except Exception:
        response = {"error": "network_error"}
    if not isinstance(response, dict):
        response = {}
    status, body = response.get("status_code"), response.get("body")
    base = body.get("base_resp") if isinstance(body, dict) else None
    code = base.get("status_code") if isinstance(base, dict) else None
    # 保留可核对的状态码，供应商任意字符串可能含凭证或用户素材，不原样落库。
    evidence = {"stage": job["stage"], "http_status": status if isinstance(status, int) else None,
                "provider_status": code if isinstance(code, int) else None, "at": now_iso()}
    job["provider_evidence"].append(evidence)
    if status == 200 and type(code) is int and code == 0:
        _save(job)
        return body
    known_rejected = ((isinstance(status, int) and 400 <= status < 500 and status != 408)
                      or (isinstance(code, int) and code != 0 and code not in (1000, 1001, 1013)))
    not_configured = response.get("error") == "not_configured"
    state = "blocked" if not_configured else ("failed" if known_rejected else "unknown")
    message = ("MiniMax 密钥未配置，未发起远端请求" if not_configured else
               "MiniMax 拒绝本次请求，请检查账号认证、余额及音频规格" if known_rejected else
               "未取得确定结果，不自动重试；请使用作业中的远端音色 ID 向 MiniMax 核对")
    _save(job, state=state, error={"code": "VOICE_CLONE_" + state.upper(), "message": message,
                                 "retryable": False})
    return None


def create_clone(payload: dict, *, transport: Transport | None = None, mode: str | None = None) -> dict:
    data, content = _validated_request(payload)
    digest = _sha(json.dumps(data, ensure_ascii=False, sort_keys=True).encode())
    request_id = data["request_id"]
    # 原子占用请求 ID；重复提交（包括进程重启后的 uploading/cloning）永不重发。
    with st.transaction() as connection:
        existing = get_clone(request_id)
        if existing:
            if existing["request_digest"] != digest:
                raise TtsRejected("VOICE_CLONE_REQUEST_CONFLICT", "相同 request_id 已绑定不同内容，请勿覆盖原作业")
            return _result(existing)
        job = {"id": request_id, "request_id": request_id, "request_digest": digest,
               **data, "state": "processing", "stage": "validating", "created_at": now_iso(),
               "voice_asset_id": st.new_id("voice"), "remote_voice_id": "Voice" + uuid.uuid4().hex,
               "provider_evidence": [], "source_size_bytes": len(content), "remote_file_id": None}
        st.insert_row(connection, TABLE, request_id, job)
    path = Path(audio.runtime_root()) / "voice-clones" / job["voice_asset_id"] / ("source" + Path(data["filename"]).suffix.lower())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        _save(job, source_path=str(path.resolve()))
        upload_path, upload_content, metadata = _prepare_upload_audio(path, content)
        _save(job, audio_metadata=metadata, upload_path=str(upload_path.resolve()),
              upload_sha256=_sha(upload_content), upload_size_bytes=len(upload_content))
    except Exception as exc:
        error = exc if isinstance(exc, TtsError) else TtsRejected("VOICE_CLONE_STORAGE_FAILED", "音频无法保存到本地工作区")
        _save(job, state="failed", error={**error.to_dict(), "retryable": False})
        raise error
    selected_mode = mode if transport is not None and mode is not None else config.resolve_mode()
    if selected_mode == "mock":
        _save(job, state="blocked", error={"code": "VOICE_CLONE_MOCK_MODE", "retryable": False,
              "message": "当前为 Mock 模式，样本仅保存至本机，未上传，未创建可生产音色"})
        return _result(job)
    sender = transport or http_transport
    mime = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}[upload_path.suffix]
    _save(job, stage="uploading")
    uploaded = _call(job, sender, {"url": MINIMAX_BASE_URL + "/files/upload",
        "data": {"purpose": "voice_clone"}, "files": {"file": (data["filename"], upload_content, mime)}})
    if uploaded is None:
        return _result(job)
    file = uploaded.get("file")
    file_id = file.get("file_id") if isinstance(file, dict) else None
    if type(file_id) is not int or file_id <= 0:
        _save(job, state="unknown", error={"code": "VOICE_CLONE_UPLOAD_UNKNOWN", "retryable": False,
              "message": "上传响应未包含有效 file_id，不自动重传，请核对原作业"})
        return _result(job)
    _save(job, remote_file_id=file_id, stage="cloning")
    cloned = _call(job, sender, {"url": MINIMAX_BASE_URL + "/voice_clone",
                               "json": {"file_id": file_id, "voice_id": job["remote_voice_id"]}})
    if cloned is None:
        return _result(job)
    sensitive = cloned.get("input_sensitive")
    sensitive_type = sensitive.get("type", 0) if isinstance(sensitive, dict) else cloned.get("input_sensitive_type", 0)
    if sensitive is True or sensitive_type not in (0, None):
        _save(job, state="failed", error={"code": "VOICE_CLONE_INPUT_REJECTED", "retryable": False,
                                          "message": "MiniMax 未通过上传音频审查"})
        return _result(job)
    created_at = now_iso()
    metadata = {"request_id": request_id, "remote_file_id": file_id, "source_sha256": data["source_sha256"],
                "created_at": created_at,
                "expires_at": (datetime.fromisoformat(created_at) + timedelta(days=7)).isoformat(),
                "activated_at": None, "activation_state": "pending", "first_use_job_id": None,
                "activation_cost_ack": True, "activation_cost_ack_at": job["created_at"],
                "clone_fee_fen": CLONE_FEE_FEN, "rights_ack": True,
                "rights_ack_at": job["created_at"], "source_evidence": data["source_evidence"]}
    _save(job, stage="registering", clone_metadata=metadata, remote_created=True)
    try:
        license_row = licensing.register(subject=job["voice_asset_id"], kind="voice_source",
            evidence=data["source_evidence"], note="用户上传及授权声明；系统未独立核验声音所有权")
        license_row = licensing.attest_commercial(license_row["id"], "user:voice-clone",
                                                  data["source_evidence"], licensing.ATTEST_TOKEN)
        source = sources.register_source(subject=job["voice_asset_id"], source_type="newly_licensed",
            provider="minimax", model=voices.MINIMAX_TTS_MODEL, rights_evidence_asset_id=license_row["id"],
            note=f"用户确认授权；原始样本 {data['source_sha256']}；克隆作业 {request_id}；非法律核验")
        sources.mark_license_review(source["id"], "reviewed", "user:voice-clone", "依据本次用户明确授权，非系统法律判断")
        voice = voices.register_voice(voice_id=job["voice_asset_id"], name=data["name"], provider="minimax",
            model=voices.MINIMAX_TTS_MODEL, remote_voice_id=job["remote_voice_id"], region="cn",
            sample_text=DEFAULT_SAMPLE_TEXT, synthesis_profile={"speed": 1.0, "vol": 1.0, "pitch": 0},
            source_record_id=source["id"], license_id=license_row["id"], catalog_group="我的克隆音色",
            description="用户授权的真人音色克隆", note=NOTE)
        voice["clone_metadata"] = metadata
        st.put("voices", voice["id"], voice)
    except Exception:
        _save(job, state="unknown", error={"code": "VOICE_CLONE_REGISTRATION_INCOMPLETE", "retryable": False,
              "message": "远端已创建音色，本地资产登记未完成；保留远端 ID，不可重复创建，请人工核对"})
        return _result(job)
    _save(job, state="succeeded", stage="completed")
    return _result(job)


def _current(voice: dict) -> tuple[dict, dict]:
    latest = st.get("voices", voice.get("id", "")) or voice
    return latest, latest.get("clone_metadata") or {}


def _assert_pending_usable(meta: dict) -> None:
    if meta.get("activation_cost_ack") is not True:
        raise TtsBlocked("VOICE_CLONE_COST_ACK_REQUIRED", "克隆音色首次使用费用尚未确认")
    if meta.get("activation_state") in ("in_flight", "unknown"):
        raise TtsBlocked("VOICE_CLONE_ACTIVATION_UNKNOWN", "首次合成正在执行或结果不确定，请核对 MiniMax 后再继续，禁止重复扣费")
    try:
        expiry = datetime.fromisoformat(meta["expires_at"])
        expired = expiry <= datetime.now(timezone.utc)
    except (ValueError, KeyError, TypeError):
        expired = True
    if expired:
        raise TtsBlocked("VOICE_CLONE_EXPIRED", "临时音色已超过 7 天或有效期无法确认，请先核对远端音色")


def synthesis_charge(voice: dict, text: str) -> int:
    """返回本次克隆音色总预计费用（分）；普通预置音色返回 0，沿用原预算。"""
    _, meta = _current(voice)
    if not meta:
        return 0
    activated = meta.get("activation_state") == "active" and bool(meta.get("activated_at"))
    if not activated:
        _assert_pending_usable(meta)
    chars = sum(2 if "\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U000323af" else 1 for char in text)
    tts_fen = math.ceil(chars * 350 / 10_000)
    return tts_fen + (0 if activated else CLONE_FEE_FEN)


def begin_activation(voice_id: str, job_id: str) -> None:
    with st.transaction() as connection:
        voice, meta = _current({"id": voice_id})
        if not meta or meta.get("activation_state") == "active":
            return
        _assert_pending_usable(meta)
        meta.update(activation_state="in_flight", first_use_job_id=job_id, first_use_started_at=now_iso())
        voice["clone_metadata"] = meta
        st.insert_row(connection, "voices", voice_id, voice)


def _finish_activation(voice_id: str, job_id: str, state: str) -> None:
    with st.transaction() as connection:
        voice, meta = _current({"id": voice_id})
        if not meta or meta.get("activation_state") == "active":
            return
        if meta.get("first_use_job_id") != job_id or meta.get("activation_state") not in ("in_flight", "unknown"):
            raise TtsBlocked("VOICE_CLONE_ACTIVATION_JOB_MISMATCH", "首次合成作业不匹配，不能修改激活状态")
        meta["activation_state"] = state
        if state == "active":
            meta["activated_at"] = now_iso()
        elif state == "pending":
            meta["first_use_job_id"] = None
        meta["activation_updated_at"] = now_iso()
        voice["clone_metadata"] = meta
        st.insert_row(connection, "voices", voice_id, voice)


def mark_activated(voice_id: str, job_id: str) -> None:
    """仅在真实 T2A 合成成功后调用；试听接口内部 demo 不构成此证据。"""
    _finish_activation(voice_id, job_id, "active")


def mark_activation_unknown(voice_id: str, job_id: str) -> None:
    _finish_activation(voice_id, job_id, "unknown")


def cancel_activation(voice_id: str, job_id: str) -> None:
    """供应商明确拒绝且未开始合成时，允许用户修正后另行发起。"""
    _finish_activation(voice_id, job_id, "pending")
