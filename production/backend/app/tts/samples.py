"""音色的本项目基准合成证据（AC-TTS-01）。

"基准合成证据"不是一个布尔值，而是**一个可独立核验的音频文件**：
路径 + SHA-256 + 容器解出的时长/采样率/声道 + 它念的那句短句。
缺任一项，预设音色就只是"库里有个名字"，不足以支撑"该音色已在本项目验证过"。

时长与采样率一律**从文件容器解出**，不抄供应商文档、不抄历史记录里的文字描述。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from . import provider as pv
from .errors import TtsRejected

SAMPLE_TABLE = "voice_samples"


def register_sample(*, voice_id: str, audio_path: str, sample_text: str, source: str,
                    provider_run_id: str = "", storage_uri: str = "",
                    declared_hash: str = "", note: str = "") -> Dict[str, Any]:
    """登记一份基准合成音频。容器无法解析即拒收——解不出时长就无从对齐。"""
    if not os.path.isfile(audio_path):
        raise TtsRejected("VOICE_BASELINE_FILE_MISSING",
                          f"基准音频文件不存在：{audio_path}")
    with open(audio_path, "rb") as f:
        data = f.read()
    actual = pv.file_hash(data)
    if declared_hash and declared_hash != actual:
        raise TtsRejected(
            "VOICE_BASELINE_HASH_TAMPERED",
            "基准音频哈希与登记记录不符：文件已被替换，不得据此声明已基准验证",
            {"declared": declared_hash, "actual": actual},
        )
    info = pv.parse_wav(data)
    if info is None:
        raise TtsRejected("VOICE_BASELINE_UNDECODABLE",
                          "基准音频容器无法解析：无法确定时长与采样率，不得登记",
                          {"audio_path": audio_path})
    if info.get("truncated"):
        raise TtsRejected("VOICE_BASELINE_TRUNCATED", "基准音频 data 块被截断，不得登记")

    rec = {
        "id": st.new_id("vs"), "voice_asset_id": voice_id,
        "audio_path": os.path.abspath(audio_path), "storage_uri": storage_uri,
        "file_hash": actual, "declared_hash": declared_hash,
        "duration_ms": info["duration_ms"], "sample_rate": info["sample_rate"],
        "channels": info["channels"], "audio_format": info["audio_format"],
        "sample_text": sample_text, "provider_run_id": provider_run_id or None,
        "source": source,
        "note": note or "本项目基准合成证据；时长与规格由容器解出，非文字转述",
        "registered_at": now_iso(),
    }
    st.put(SAMPLE_TABLE, rec["id"], rec, {"voice_id": voice_id})
    return rec


def get_sample(sample_id: str) -> Optional[Dict[str, Any]]:
    return st.get(SAMPLE_TABLE, sample_id) if sample_id else None


def list_samples(voice_id: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = st.all_rows(SAMPLE_TABLE)
    return [r for r in rows if r.get("voice_asset_id") == voice_id] if voice_id else rows


def find_by_hash(file_hash: str) -> Optional[Dict[str, Any]]:
    return next((r for r in list_samples() if r.get("file_hash") == file_hash), None)
