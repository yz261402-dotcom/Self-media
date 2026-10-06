"""音色试听（AC-TTS-01 的试听侧）。

规则来源：09 §4.2「音色库先提供同一短句试听，区分已缓存样音与需要付费的新合成」。

- **同一短句**。要比较声音就必须用同一句；试听短句取自 VoiceAsset 登记的
  `sample_text`，调用方换一句就拒收——否则"音色 A 听起来更自然"这个结论
  本身就不可比。
- **缓存精确命中**。缓存键 = 作用域四元组 + 合成参数 + 文本摘要，三者任一变化
  都不命中，避免"参数改了却复用旧样音"。
- **缓存与计费分开报**。`cached=True` 的试听不产生新费用；
  `cached=False` 的新合成按可计费作业走台账与预算。
- **试听片不是正文音频**。它带着 `kind="preview"` 与一条硬门禁，
  从结构上不可能被确认为最终音频。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import now_iso
from . import audio as au
from . import provider as pv
from . import voices as vc
from .errors import TtsRejected

PREVIEW_TABLE = "voice_previews"
# 每个作品一条：记录该作品用于横向比较的试听短句
AUDITION_TABLE = "audition_sentences"

# 试听短句的用途说明：内容本身不是为了发布，避免被当成完整稿
PREVIEW_NOTE = "试听短句仅用于比较音色，不是完整稿件，不能作为正文音频"


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def preview_cache_key(voice: Dict[str, Any], sample_text: str, profile: Optional[dict] = None) -> str:
    scope = vc.assert_scope_complete(voice)
    return vc.profile_digest(scope["provider"], scope["model"], scope["region"],
                             scope["remote_voice_id"],
                             profile if profile is not None else vc.effective_profile(None, voice),
                             text_digest(sample_text))


def find_cached(voice: Dict[str, Any], sample_text: str, profile: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    key = preview_cache_key(voice, sample_text, profile)
    row = st.get(PREVIEW_TABLE, key)
    if not row:
        return None
    audio = au.get_audio(row.get("audio_asset_id", ""))
    if not audio or audio.get("is_mock"):
        # Mock 试听不算缓存命中：否则会把"没真做过"报成"已有样音"
        return None
    if not _audio_file_matches(audio):
        return None
    return row


def _audio_file_matches(audio: dict) -> bool:
    # 缓存必须仍是原始字节；文件失踪/被替换不能伪称已有试听。
    from pathlib import Path
    try:
        payload = Path(au.runtime_path(audio.get("storage_uri") or "")).read_bytes()
    except (OSError, TtsRejected):
        return False
    return pv.file_hash(payload) == audio.get("file_hash")


def audition_sentence(work_id: Optional[str]) -> Optional[str]:
    """该作品当前绑定的试听短句。没有绑定过则返回 None。"""
    row = st.get(AUDITION_TABLE, work_id) if work_id else None
    return ((row or {}).get("sentence") or "").strip() or None


def resolve_audition_sentence(voice: Dict[str, Any], work_id: Optional[str],
                              sample_text: Optional[str] = None) -> str:
    """确定本次试听用哪一句，并把它绑定到作品上。

    **同一作品必须用同一短句**：跨音色比较的前提是"只有声音不同"。
    一旦允许中途换句，"音色 A 更自然"这个结论本身就不可复核。
    因此首次使用的短句成为该作品的比较基准；之后显式传入不同短句一律拒收，
    确需换句必须走 `reset_audition_sentence()` 明示重置（新结论不与旧结论可比）。
    """
    voice_text = (voice.get("sample_text") or "").strip()
    requested = (sample_text or "").strip()
    if not work_id:
        # 无作品上下文（例如音色库列表试听）：只能用音色自身登记的基准短句
        return requested or voice_text
    existing = audition_sentence(work_id)
    if existing and requested and requested != existing:
        raise TtsRejected(
            "PREVIEW_SAMPLE_TEXT_MISMATCH",
            f"同一作品必须用同一短句试听：该作品已使用「{existing}」。"
            "换句会让跨音色比较失去可比性；确需换句请显式重置该作品的试听短句",
            {"work_id": work_id, "existing": existing, "requested": requested},
        )
    chosen = existing or requested or voice_text
    if chosen and chosen != existing:
        st.put(AUDITION_TABLE, work_id,
               {"id": work_id, "work_id": work_id, "sentence": chosen,
                "created_at": now_iso()},
               {"work_id": work_id})
    return chosen


def reset_audition_sentence(work_id: str, sentence: str = "", reason: str = "") -> Dict[str, Any]:
    """显式重置该作品的试听短句。

    重置会打断与既有试听结论的可比性，所以必须是一次明确动作而不是副作用：
    传 `sentence` 则改为新基准句，不传则解绑（下次 resolve 重新选定）。
    """
    if not work_id:
        raise TtsRejected("WORK_NOT_FOUND", "重置试听短句必须指明作品")
    body = (sentence or "").strip()
    row = {"id": work_id, "work_id": work_id, "sentence": body,
           "reset_at": now_iso(), "reset_reason": reason or "显式重置",
           "created_at": now_iso()}
    st.put(AUDITION_TABLE, work_id, row, {"work_id": work_id})
    return row


def baseline_sample(voice_id: str) -> Dict[str, Any]:
    """音色列表可直接播放的来源清楚样例（本项目基准合成证据）。"""
    voice = vc.get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    return {
        "voice_asset_id": voice_id,
        "audio_asset_id": voice.get("baseline_audio_asset_id"),
        "file_hash": voice.get("baseline_audio_hash", ""),
        "sample_text": voice.get("sample_text", ""),
        "source": "本项目基准合成证据（真实试听）",
        "available": bool(voice.get("baseline_audio_asset_id")),
        "note": "列表样例可播放；要比较声音必须使用同一短句",
    }


def create_preview(*, work_id: Optional[str], voice_id: str,
                   sample_text: Optional[str] = None,
                   transport: Optional[pv.Transport] = None, force_new: bool = False,
                   amount_fen: int = 0, dry_run: bool = False,
                   task_id: Optional[str] = None) -> Dict[str, Any]:
    voice = vc.get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    scope = vc.assert_scope_complete(voice)
    vc.assert_model_allowed(scope["provider"], scope["model"])
    vc.assert_region_verified(scope["provider"], scope["region"])
    if not voice.get("source_record_id"):
        raise TtsRejected("VOICE_SOURCE_MISSING",
                          "音色来源不清：不接受只提供 remote_voice_id 就播放试听")
    sample_text = resolve_audition_sentence(voice, work_id, sample_text)
    if not sample_text:
        raise TtsRejected("VOICE_SAMPLE_TEXT_MISSING",
                          "未给出试听短句，且该音色未登记基准短句，无法进行同句比较试听")

    work = st.get("works", work_id) if work_id else None
    if work_id and not work:
        raise TtsRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    profile = vc.effective_profile(work, voice)
    digest = vc.controls_digest(voice, profile)
    revision = int((work or {}).get("tts_controls_revision") or 0)
    cache_key = preview_cache_key(voice, sample_text, profile)

    if not force_new:
        cached = find_cached(voice, sample_text, profile)
        if cached:
            owner = work_id or f"preview-{voice_id}"
            source_audio = au.get_audio(cached["audio_asset_id"])
            existing = next((a for a in reversed(au.list_audio(owner, au.KIND_PREVIEW))
                             if a.get("preview_cache_key") == cache_key
                             and a.get("tts_controls_revision", 0) == revision
                             and not a.get("invalidated") and not a.get("is_mock")
                             and _audio_file_matches(a)), None)
            if existing:
                audio = existing
            else:
                # 缓存可跨作品复用字节，资产归属必须属于本次作品，才能登记其试听基准。
                from pathlib import Path
                assert source_audio is not None
                audio = au.register_audio(
                    work_id=owner, kind=au.KIND_PREVIEW, voice_id=voice_id,
                    duration_ms=source_audio["duration_ms"], file_hash=source_audio["file_hash"],
                    provider_run_id=source_audio.get("provider_run_id"),
                    timing_source=source_audio.get("timing_source"), sample_rate=source_audio.get("sample_rate"),
                    channels=source_audio.get("channels"), checks=source_audio.get("checks") or [],
                    note="复用同一短句、音色与参数的缓存字节，未发起计费请求")
                audio["storage_uri"] = au.store_audio_bytes(owner, audio["id"],
                    Path(au.runtime_path(source_audio["storage_uri"])).read_bytes())
                audio["preview_cache_key"] = cache_key
                audio["sample_text"] = sample_text
                audio = au.preserve_result_profile(audio, profile, digest, revision)
            cached = {**cached, "work_id": work_id, "audio_asset_id": audio["id"],
                      "synthesis_profile_digest": digest}
            return {"cached": True, "cost_reservation_id": None,
                    "preview": cached, "audio": audio,
                    "message": "命中已缓存试听：不产生新的计费合成",
                    "note": PREVIEW_NOTE}

    # 显式 force_new 是一次**新的**计费尝试（同一句同一参数在供应商侧可能给出不同的
    # 随机会话表现，用户就是想再听一版），因此给它一个独立的幂等键。
    # 否则它会被台账按"重复提交"挡掉——把用户的明确动作当成误操作，是另一种失真。
    attempt_key = f"tts:preview:{cache_key}:{st.new_id('r')}" if force_new else None
    result = pv.synthesize(work_id=work_id or f"preview-{voice_id}", voice=voice,
                           provider_params=profile, text=sample_text,
                           transport=transport, amount_fen=amount_fen,
                           task_id=task_id, dry_run=dry_run,
                           idempotency_key=attempt_key)

    is_mock = bool(result.get("is_mock"))
    duration_ms = int(result.get("duration_ms") or 0)
    checks = [
        {"code": "VOICE_SCOPE", "outcome": "pass", "hard_gate": True,
         "message": "音色作用域与所选 VoiceAsset 一致（provider/model/region/voice_id 四元组）"},
        {"code": "AUDIO_DECODE", "outcome": "not_run" if is_mock else "pass",
         "hard_gate": not is_mock,
         "message": "未产生真实音频字节（占位）" if is_mock
                    else f"容器 {result.get('audio_format')}，"
                         f"{result.get('sample_rate')}Hz/{result.get('channels')}ch 完整解码"},
        {"code": "SAME_SAMPLE_TEXT", "outcome": "pass", "hard_gate": True,
         "message": "使用该音色登记的同一短句，可与其他音色横向比较"},
        {"code": "PREVIEW_NOT_FINAL", "outcome": "pass", "hard_gate": True,
         "message": PREVIEW_NOTE},
        {"code": "DURATION", "outcome": "not_run" if not duration_ms else "pass",
         "hard_gate": False,
         "message": "占位产物无真实时长" if not duration_ms
                    else f"时长取自{'容器' if result.get('duration_source') == 'container' else '供应商附加字段'}：{duration_ms}ms"},
    ]

    audio = au.register_audio(
        work_id=work_id or f"preview-{voice_id}", kind=au.KIND_PREVIEW, voice_id=voice_id,
        duration_ms=duration_ms, file_hash=result.get("file_hash", ""),
        is_mock=is_mock, provider_run_id=result.get("job_id"),
        timing_source=result.get("timing_source"),
        sample_rate=result.get("sample_rate"), channels=result.get("channels"),
        checks=checks,
        note="试听产物；" + ("；".join(result.get("warnings") or []) or "真实合成"),
    )
    if not is_mock and result.get("audio_bytes"):
        owner = work_id or f"preview-{voice_id}"
        audio["storage_uri"] = au.store_audio_bytes(owner, audio["id"], result["audio_bytes"])
        st.put("audios", audio["id"], audio, {"work_id": owner})
    audio["preview_cache_key"] = cache_key
    audio["sample_text"] = sample_text
    audio = au.preserve_result_profile(audio, profile, digest, revision)

    key = cache_key
    row = {
        "work_id": work_id, "voice_asset_id": voice_id,
        "sample_text_digest": text_digest(sample_text),
        "synthesis_profile_digest": digest,
        "audio_asset_id": audio["id"],
        "provider_run_id": result.get("job_id"),
        "cost_reservation_id": result.get("job_id") if not is_mock else None,
        "cached": False,
        "cache_key": key,
        "forced": bool(force_new),
        "created_at": audio["created_at"],
    }
    if not is_mock and not audio.get("invalidated"):
        # 只有真实合成才写入缓存：否则占位结果会被后续请求当成"已有样音"
        st.put(PREVIEW_TABLE, key, row)
    return {"cached": False, "cost_reservation_id": row["cost_reservation_id"],
            "preview": row, "audio": audio,
            "message": "已生成试听（可计费合成已过预算与台账）" if not is_mock
                       else "占位试听：未提交真实 Provider",
            "note": PREVIEW_NOTE}


def list_previews(work_id: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = st.all_rows(PREVIEW_TABLE)
    if work_id:
        rows = [r for r in rows if r.get("work_id") == work_id]
    return rows
