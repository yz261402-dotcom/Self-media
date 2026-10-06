"""音频产物登记（AC-TTS-02/03/04 的产物侧）。

关键约束：

- **真实音频必须能追溯到文件哈希**。没有哈希的音频等于来源不可追溯，
  不能作为任何结论的依据。
- **预览与正文严格分开**。`kind` 不是标签而是权限：`preview` 永不可能被确认
  为最终音频，因为它的文本本来就不是正文（试听短句里"用三个步骤"没有展开）。
- **Mock 不继承质量结论**。Mock 产物可以存在（用于串通流程），
  但它带着 `is_mock=True`，任何"通过"判定都必须先把它挡掉。
- **正文音频必须绑定稿件与计划**。缺 `script_version_id` / `performance_plan_id`
  的"正文音频"无法证明它念的是哪一版稿，直接拒收。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import AudioAssetInfo, QualityCheck, now_iso
from .errors import TtsRejected

KIND_PREVIEW = "preview"
KIND_FULL = "full"

_HASH_PREFIX = "sha256:"


def register_audio(*, work_id: str, kind: str, voice_id: str, duration_ms: int,
                   file_hash: str = "", storage_uri: str = "", is_mock: bool = False,
                   script_version_id: Optional[str] = None,
                   performance_plan_id: Optional[str] = None,
                   provider_run_id: Optional[str] = None,
                   cost_reservation_id: Optional[str] = None,
                   timing_source: Optional[str] = None, sample_rate: Optional[int] = None,
                   channels: Optional[int] = None, words: Optional[List[Dict[str, Any]]] = None,
                   checks: Optional[List[Dict[str, Any]]] = None,
                   revision_of: Optional[str] = None, note: str = "") -> Dict[str, Any]:
    if kind not in (KIND_PREVIEW, KIND_FULL):
        raise TtsRejected("AUDIO_KIND_INVALID", f"音频种类非法：{kind}")
    if not voice_id:
        raise TtsRejected("VOICE_SCOPE_MISMATCH", "登记音频必须绑定音色")
    if kind == KIND_FULL:
        if not script_version_id:
            raise TtsRejected("AUDIO_SCRIPT_UNBOUND",
                              "正文音频缺少稿件绑定：无法证明念的是哪一版正文")
        if not performance_plan_id:
            raise TtsRejected("AUDIO_PLAN_UNBOUND",
                              "正文音频缺少计划绑定：分段时间与朗读映射将无法追溯")
    if not is_mock and not file_hash:
        raise TtsRejected("AUDIO_HASH_MISSING",
                          "缺少音频文件哈希：来源不可追溯，不得作为任何结论依据")
    if file_hash and not file_hash.startswith(_HASH_PREFIX):
        raise TtsRejected("AUDIO_HASH_INVALID", f"音频哈希格式不合法：{file_hash}")

    audio = AudioAssetInfo(
        id=st.new_id("audio"), kind=kind,  # type: ignore[arg-type]
        voice_asset_id=voice_id,
        duration_ms=int(duration_ms or 0),
        approved=False, confirmed=False, url=None,
        checks=[QualityCheck(**c) for c in (checks or [])],
        storage_uri=storage_uri, file_hash=file_hash, is_mock=is_mock,
        script_version_id=script_version_id, performance_plan_id=performance_plan_id,
        provider_run_id=provider_run_id, cost_reservation_id=cost_reservation_id,
        timing_source=timing_source,  # type: ignore[arg-type]
        sample_rate=sample_rate, channels=channels,
        revision_of=revision_of, note=note,
    )
    row = audio.model_dump()
    row["work_id"] = work_id
    row["words"] = list(words or [])
    row["created_at"] = now_iso()
    st.put("audios", audio.id, row, {"work_id": work_id})
    return row


def get_audio(audio_id: str) -> Optional[Dict[str, Any]]:
    return st.get("audios", audio_id) if audio_id else None


def runtime_root() -> str:
    """本机 runtime 目录。落盘与回读共用一份，避免两处各写一层相对层数。"""
    import os

    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "runtime")


def runtime_path(rel_uri: str) -> str:
    """storage_uri（如 audio/<work>/<id>.wav）→ 本机绝对路径。

    只接受落在 runtime 内的相对路径。不这么挡的话，一条被改过的
    `storage_uri` 就能让"登记基准证据"去读本机任意文件——
    而基准证据的全部意义正是"这个文件确实是本项目合成的那个"。
    """
    import os

    root = os.path.realpath(runtime_root())
    if not rel_uri or os.path.isabs(rel_uri):
        raise TtsRejected("AUDIO_PATH_INVALID", f"音频存储地址不合法：{rel_uri!r}")
    path = os.path.realpath(os.path.join(root, rel_uri.replace("/", os.sep)))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise TtsRejected("AUDIO_PATH_INVALID", f"音频存储地址越出 runtime：{rel_uri}")
    return path


def store_audio_bytes(work_id: str, audio_id: str, data: bytes) -> str:
    """真实音频落盘，返回相对存储位置。

    相对路径而非绝对路径：发布包不得包含本机内部路径。
    文件名就是资产 id，因此同一作品多次合不会互相覆盖——
    这也是**落盘必须晚于登记**的原因（登记时才知道 id）。
    """
    import os

    root = os.path.join(runtime_root(), "audio", work_id)
    os.makedirs(root, exist_ok=True)
    name = f"{audio_id}.wav"
    with open(os.path.join(root, name), "wb") as f:
        f.write(data)
    return f"audio/{work_id}/{name}"


def list_audio(work_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = st.all_rows("audios", "work_id=?", [work_id])
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return sorted(rows, key=lambda r: r.get("created_at", ""))


def final_audio(work_id: str) -> Optional[Dict[str, Any]]:
    rows = [r for r in list_audio(work_id, KIND_FULL) if r.get("confirmed") and not currentness_problems(r)]
    return rows[-1] if rows else None


def currentness_problems(audio: Dict[str, Any], work: Optional[dict] = None) -> List[str]:
    """晚回包、历史音轨与当前作品不匹配时不能再确认或驱动下游。"""
    from . import voices as vc
    reasons = []
    if audio.get("invalidated"):
        reasons.append("音频已失效：" + (audio.get("invalidated_reason") or "上游参数已变化"))
    work = work if work is not None else st.get("works", audio.get("work_id") or "")
    if not work:
        return reasons
    if audio.get("kind") == KIND_FULL:
        if audio.get("voice_asset_id") != work.get("voice_asset_id"):
            reasons.append("音频音色已不是作品当前选择")
        if audio.get("script_version_id") != work.get("script_version_id"):
            reasons.append("音频稿件已不是作品当前选择")
    if int(audio.get("tts_controls_revision") or 0) != int(work.get("tts_controls_revision") or 0):
        reasons.append("音频生成时的参数版本已过期")
    if audio.get("tts_controls_digest"):
        voice = vc.get_voice(audio.get("voice_asset_id"))
        if not voice or vc.controls_digest(voice, vc.effective_profile(work, voice)) != audio['tts_controls_digest']:
            reasons.append("音频参数摘要与作品当前参数不一致")
    return reasons


def preserve_result_profile(audio: dict, profile: dict, digest: str, revision: int) -> dict:
    """结果回收后原子比较当前设置，保存历史产物但不激活过期请求。"""
    with st.transaction() as connection:
        audio.update(synthesis_profile=profile, tts_controls_digest=digest,
                     tts_controls_revision=revision)
        reasons = currentness_problems(audio)
        if reasons:
            audio.update(invalidated=True, invalidated_at=now_iso(),
                         invalidated_reason="；".join(reasons), confirmed=False, approved=False)
        st.insert_row(connection, "audios", audio['id'], audio, {'work_id': audio['work_id']})
    return audio


def hard_failures(audio: Dict[str, Any]) -> List[Dict[str, Any]]:
    """硬门禁失败项。Mock 产物单独一条：它不能继承任何"通过"结论。"""
    fails: List[Dict[str, Any]] = []
    if audio.get("kind") == KIND_FULL:
        for reason in currentness_problems(audio):
            fails.append({"code": "AUDIO_STALE", "hard_gate": True, "outcome": "fail", "message": reason})
    for c in audio.get("checks", []):
        if c.get("hard_gate") and c.get("outcome") != "pass":
            fails.append(c)
    if audio.get("is_mock"):
        fails.append({"code": "AUDIO_IS_MOCK", "hard_gate": True, "outcome": "fail",
                      "message": "Mock 音频不得进入生产、不得被确认为最终音频"})
    if not audio.get("file_hash"):
        fails.append({"code": "AUDIO_HASH_MISSING", "hard_gate": True, "outcome": "fail",
                      "message": "缺少音频文件哈希，来源不可追溯"})
    return fails


def is_confirmable(audio: Dict[str, Any]) -> tuple[bool, List[str]]:
    reasons: List[str] = currentness_problems(audio)
    if audio.get("kind") != KIND_FULL:
        reasons.append("试听片不能作为最终音频：其文本是试听短句而非正文")
    if audio.get("is_mock"):
        reasons.append("Mock 音频不得被确认为最终音频")
    if not audio.get("file_hash"):
        reasons.append("缺少音频文件哈希")
    for c in audio.get("checks", []):
        if c.get("hard_gate") and c.get("outcome") != "pass":
            reasons.append(f"{c.get('code')}：{c.get('message', '')}")
    if not audio.get("duration_ms"):
        reasons.append("时长未知：最终音频必须能确定时长")
    return (not reasons), reasons
