"""初始预设音色的受控登记（AC-TTS-01）。

依据 `specs/selection/03-人物与音色/02-人物B与音色3定版记录.md`：

| 项 | 值 |
|---|---|
| 声音资产 | `voice_minimax_warm_bestie_v1` |
| 服务 / 模型 | MiniMax 官方 API / `speech-2.8-hd` |
| 音色 | 温暖闺蜜 · `Chinese (Mandarin)_Warm_Bestie` |
| 基础参数 | `speed=1.0 vol=1.0 pitch=0 emotion=calm language_boost=Chinese` |
| 基准试听 | WAV、32000 Hz、单声道、约 15.22 秒 |
| 试听 SHA-256 | `a6945eac…fc81`（本模块启动时**重新计算**并比对，不信任文字转述） |

登记**不等于**获准生产：权属记录按证据如实登记为"未核验为可商用"，
因此 `voice_ready()` 仍会报告阻塞项。这不是遗漏，而是这个项目的一贯做法——
把"还差什么"写清楚，而不是替用户做出法律结论。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .. import storage as st
from ..avatar import licensing
from ..contracts import now_iso
from . import samples as vs
from . import sources as src
from . import voices as vc

def _find_workspace_root() -> str:
    """向上找到同时含 `assets/` 与 `production/` 的目录。

    不用固定层数：目录层级一变，固定写法会静默指向错误的目录，
    结果是"基准证据缺失"这种看起来像业务问题的假故障。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(here, "assets")) and \
                os.path.isdir(os.path.join(here, "production")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    # 兜底：backend/app/tts → 四级回到工作区根
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        *[".."] * 4))


WORKSPACE_ROOT = _find_workspace_root()

PRESET_WAV_REL = os.path.join("assets", "固定主播", "主播B", "版本1", "音色3-温暖闺蜜试听.wav")
PRESET_SAMPLE_TEXT = "大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。"
# 定版记录中的试听哈希；登记时与实测值比对，不一致即拒收
PRESET_WAV_SHA256 = ("sha256:a6945eac9e317ea3643151e039f3de1a929277aceb605a2d13dee708f815fc81")
PRESET_DURATION_MS = 15219

# 权属证据：只记录事实边界，不做法律结论
VOICE_LICENSE_EVIDENCE = (
    "音色为 MiniMax 开放平台官方预置音色（非声音克隆、非本项目自注册），"
    "通过官方 `POST /v1/t2a_v2` 接口随账户按量计费使用；"
    "本项目在用户账户下完成基准合成，产物与哈希见 `voice_samples` 基准证据。"
)
VOICE_LICENSE_NOTE = (
    "结论：本项目**未取得书面法律意见**，故不判定为可商用，scope 保持 unverified。"
    "升级路径（任一）：① 归档官方《服务协议》/《使用条款》原文快照并确认商业使用范围；"
    "② 取得 MiniMax 书面商用授权。未升级前，该音色的生产就绪判定会持续阻塞。"
)


def _preset_row() -> Optional[Dict[str, Any]]:
    return vc.get_voice(vc.PRESET_VOICE_ID)


def bootstrap_preset_voice(*, assets_root: Optional[str] = None,
                           force: bool = False) -> Dict[str, Any]:
    """登记预设音色 3 的作用域、来源、权属与基准合成证据。幂等。"""
    root = assets_root or WORKSPACE_ROOT
    voice = _preset_row()
    if not voice:
        return {"ok": False, "reason": f"预设音色行不存在：{vc.PRESET_VOICE_ID}",
                "note": "先由 storage._seed 建立初始行，再补登记证据"}

    if voice.get("baseline_audio_asset_id") and not force:
        return {"ok": True, "skipped": True, "voice_asset_id": voice["id"],
                "baseline_audio_asset_id": voice["baseline_audio_asset_id"],
                "note": "基准合成证据已登记，跳过"}

    wav_path = os.path.join(root, PRESET_WAV_REL)
    result: Dict[str, Any] = {"ok": True, "voice_asset_id": voice["id"], "steps": []}

    # 1) 权属记录：如实登记为未核验，不伪造法律结论
    lic = next((r for r in licensing.list_records()
                if r.get("subject") == vc.PRESET_VOICE_ID), None)
    if not lic:
        lic = licensing.register(
            subject=vc.PRESET_VOICE_ID, kind="voice_source", scope="unverified",
            evidence=VOICE_LICENSE_EVIDENCE, verified_by="user（依据官方条款核验）",
            note=VOICE_LICENSE_NOTE,
        )
        result["steps"].append(f"权属记录 {lic['id']}（{lic['scope']}）")

    # 2) 来源记录：预设音色选择，不要求不存在的声音克隆回执
    source = next((r for r in src.list_sources(vc.PRESET_VOICE_ID)
                   if r.get("source_type") == "preset_voice_selection"), None)
    if not source:
        source = src.register_source(
            subject=vc.PRESET_VOICE_ID, source_type="preset_voice_selection",
            provider="minimax", model=vc.MINIMAX_TTS_MODEL,
            version="官方预置音色（非克隆）",
            note="来自本项目首轮候选与定版记录的选择；不要求额外注册/克隆",
        )
        result["steps"].append(f"来源记录 {source['id']}")

    # 3) 基准合成证据：哈希与容器参数实测，不比文字转述
    sample = vs.find_by_hash(PRESET_WAV_SHA256)
    if not sample:
        if not os.path.isfile(wav_path):
            result["ok"] = False
            result["reason"] = f"基准音频缺失：{wav_path}"
            result["baseline_registered"] = False
            return result
        sample = vs.register_sample(
            voice_id=vc.PRESET_VOICE_ID, audio_path=wav_path,
            sample_text=PRESET_SAMPLE_TEXT,
            source="本项目基准合成（MiniMax speech-2.8-hd，真实试听）",
            declared_hash=PRESET_WAV_SHA256,
            storage_uri=PRESET_WAV_REL.replace("\\", "/"),
            note="首次试听时长约 15.22 秒；时长与规格由容器解出",
        )
        result["steps"].append(f"基准音频 {sample['id']}（{sample['duration_ms']}ms，"
                               f"{sample['sample_rate']}Hz/{sample['channels']}ch）")

    # 4) 回填空缺字段（初始行只登记了名称/模型/voice_id）
    voice = _preset_row() or voice
    changed = []
    if voice.get("region") != vc.MINIMAX_REGION:
        voice["region"] = vc.MINIMAX_REGION
        changed.append("region")
    for field, value in (("source_record_id", source["id"]), ("license_id", lic["id"]),
                         ("baseline_audio_asset_id", sample["id"]),
                         ("baseline_audio_hash", sample["file_hash"]),
                         ("profile_digest", vc.digest_of({"scope": vc.scope_key(
                             "minimax", vc.MINIMAX_TTS_MODEL, vc.MINIMAX_REGION,
                             vc.PRESET_REMOTE_VOICE_ID),
                             "profile": vc.PRESET_SYNTHESIS_PROFILE}))):
        if voice.get(field) != value:
            voice[field] = value
            changed.append(field)
    if not voice.get("synthesis_profile"):
        voice["synthesis_profile"] = dict(vc.PRESET_SYNTHESIS_PROFILE)
        changed.append("synthesis_profile")
    if not voice.get("sample_text"):
        voice["sample_text"] = PRESET_SAMPLE_TEXT
        changed.append("sample_text")
    voice["is_preset"] = True
    voice["scope_key"] = vc.scope_key("minimax", vc.MINIMAX_TTS_MODEL, vc.MINIMAX_REGION,
                                      vc.PRESET_REMOTE_VOICE_ID)
    voice["preset_bootstrapped_at"] = now_iso()
    st.put("voices", voice["id"], voice)
    result["steps"].append("回填 " + "、".join(changed) if changed else "字段已是最新")

    ok, reasons = vc.voice_ready(voice["id"])
    result.update({"production_ready": ok, "production_blockers": reasons,
                   "baseline_registered": True,
                   "note": "登记完成；权属未核验为可商用，生产就绪仍被阻塞属预期结果"})
    return result


def preset_status() -> Dict[str, Any]:
    voice = _preset_row()
    if not voice:
        return {"exists": False, "voice_asset_id": vc.PRESET_VOICE_ID}
    ok, reasons = vc.voice_ready(voice["id"])
    sample = vs.get_sample(voice.get("baseline_audio_asset_id") or "")
    return {
        "exists": True, "voice_asset_id": voice["id"],
        "scope": vc.voice_scope(voice),
        "approval_state": voice.get("approval_state"),
        "baseline": {"sample_id": sample.get("id"), "file_hash": sample.get("file_hash"),
                     "duration_ms": sample.get("duration_ms"),
                     "sample_rate": sample.get("sample_rate"),
                     "channels": sample.get("channels")} if sample else None,
        "production_ready": ok, "production_blockers": reasons,
    }
