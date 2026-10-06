"""音频确认后即可运行的字词对齐（不依赖原片）。

规格 T07：Postprocess Worker 可在**音频确认后并行**对齐字幕。但后期阶段同时还要
核视频与剪辑偏移，实现里它整体被"必须有已审原片"挡住——于是约 60 秒以上的音频
会卡进一个死循环：

    OmniHuman 分段要字词时间戳 → 时间戳只在后期产出 →
    后期要原片 → 原片要 OmniHuman → OmniHuman 要分段

这里把"跑对齐"单独摘出来。输入只有**已确认最终音频 + 冻结计划正文**，
产出可直接登记的对齐产物（`tts_gate.register_alignment`），
供 OmniHuman 分段按句边界取切点。

边界：本模块只负责"取真实时间轴并登记"，不做任何均分、估算或插值——
时间轴唯一来源是 ASR/强制对齐，取不到就阻断（`ASR_NOT_CONFIGURED` 等）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

from . import storage as st
from .digital_human import segments as dh_segments
from .digital_human.errors import TalkingHeadBlocked, TalkingHeadRejected
from .tts import audio as tts_audio
from .tts import gate as tts_gate
from .video import asr as v_asr


def _method_of(adapter: Any) -> str:
    provider = v_asr.KNOWN_PROVIDERS.get(adapter.provider_id, {}) or {}
    models = [str(m) for m in (provider.get("models") or [])]
    return f"{adapter.provider_id}:{'+'.join(models)}" if models else str(adapter.provider_id)


def run_for_work(work_id: str) -> Dict[str, Any]:
    """为作品的已确认最终音频跑一次真实 ASR + 强制对齐并登记。

    幂等：同一音频若已有可追溯的对齐产物，直接复用，不重复调用远端模型
    （对齐是付费/占 GPU 的远端作业，重复提交没有意义）。
    """
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")

    audio = tts_audio.final_audio(work_id)
    if not audio:
        raise TalkingHeadBlocked("FINAL_AUDIO_REQUIRED", "尚无已确认最终音频，对齐无从谈起")
    if audio.get("is_mock"):
        raise TalkingHeadBlocked("FINAL_AUDIO_IS_MOCK", "占位音频不得用于对齐与分段")
    if not audio.get("file_hash"):
        raise TalkingHeadRejected("FINAL_AUDIO_NO_HASH", "最终音频缺少文件哈希")

    existing = tts_gate.alignment_status(work_id)
    if existing.get("aligned"):
        # 复用分支与新建分支返回同一组字段：调用方不该因为"这次是复用"
        # 就少拿到 alignment_id 之类的键。
        return {"ok": True, "reused": True,
                "alignment_id": existing.get("alignment_id"),
                "word_count": existing.get("word_count", 0),
                "source": existing.get("source"), "method": existing.get("method"),
                "alignment": existing,
                "note": "已有可追溯的对齐产物，未重复调用远端模型"}

    # 正文只从冻结计划读：对齐产出的字符位置必须与分段使用的字符位置指向同一字符串。
    body = dh_segments.frozen_body(work)
    audio_path = tts_audio.runtime_path(audio.get("storage_uri") or "")
    if not os.path.isfile(audio_path):
        raise TalkingHeadBlocked("MEDIA_FILE_MISSING", f"最终音频文件缺失：{audio_path}")

    adapter = v_asr.require_adapter()
    if not hasattr(adapter, "align_units"):
        raise TalkingHeadBlocked(
            "ASR_ADAPTER_UNSUPPORTED",
            f"ASR 适配器 {adapter.provider_id} 未提供字级时间轴接口，无法登记对齐")
    # 音频按登记哈希直传给 worker（与 M2 底片同一条资产通路），
    # 不经过公开对象存储：少一套存储凭据依赖，也不把口播音频放到公网可读。
    result = adapter.align_units(
        audio_path, body, audio_ref=(str(audio.get("storage_uri") or ""), str(audio.get("file_hash") or "")))
    words = v_asr.units_to_words(result["units"], result["alignment_text"], body)
    rec = tts_gate.register_alignment(
        audio["id"], source=adapter.alignment_source, method=_method_of(adapter),
        words=words,
        rule_version=f"{adapter.provider_id}-forced-align-v1",
        note="音频确认后并行对齐；供 OmniHuman 约 60 秒以上音频按句边界分段")
    return {
        "ok": True, "reused": False, "alignment_id": rec["id"],
        "word_count": len(words), "source": rec["source"], "method": rec["method"],
        "transcript": getattr(adapter, "last_transcript", ""),
        "alignment": tts_gate.alignment_status(work_id),
        "note": "对齐已登记；分段计划可基于同一音轨的句边界建立",
    }
