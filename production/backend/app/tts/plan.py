"""PerformancePlan 的派生与冻结（AC-AG-03）。

**分段只能有一份来源**。因此这里既是计划的唯一构造入口，也是唯一读取入口：
- 角色工具 `compile_performance_plan` 与本模块共用 `create_plan`，不各写一套；
- TTS 合成必须拿到冻结计划（`plan_digest` 非空且与内容相符）才继续。

计划是**意图层**：只写"这段用什么情绪、什么语速"，不写时间戳。
实际时间必须来自最终音频与校验后的对齐。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..agents import contracts as ac
from .errors import TtsRejected


def derive_segments(text: str) -> List[Dict[str, Any]]:
    """按段落切分。段落是策划写的自然单元，不再二次切分——
    二次切分会产生"计划里的分段"和"合成时的分段"两套边界，对齐时必然对不上。

    **分段必须是原稿的一个划分**：各段按序拼接后逐字符等于原稿。
    所以段间换行不是被丢掉，而是留在**前一段末尾**（空行并入相邻段）。
    这一点不是洁癖：一旦用 `strip()` 丢掉段间换行，
    ① 朗读文本就与原文不再相等，AC-TTS-02 的"保留原文"只剩口头承诺；
    ② 分段字符偏移与稿件的 1:1 映射断掉，字词对齐失去可核对的基准。
    """
    src = text or ""
    # 逐字符扫：遇换行即收尾一段，换行符本身留在该段末尾，天然构成划分。
    units: List[str] = []
    buf: List[str] = []
    for ch in src:
        buf.append(ch)
        if ch == "\n":
            units.append("".join(buf))
            buf = []
    if buf:
        units.append("".join(buf))
    if not any(u.strip() for u in units):
        return []

    # 合并纯空白单元（空行）：并入上一段；开头的前导空白则并入首个内容段。
    merged: List[str] = []
    for unit in units:
        if not unit.strip():
            if merged:
                merged[-1] += unit          # 空行并入上一段
            else:
                merged.append(unit)         # 前导空行先占位，稍后并入首个内容段
            continue
        if merged and not merged[-1].strip():
            merged[-1] += unit              # 前导空行并进首个内容段
        else:
            merged.append(unit)

    return [ac.PerformanceSegment(
        segment_id=f"seg_{i + 1}", text=para,
        emotion="calm", pace="normal", emphasis=[], motion="neutral").model_dump()
        for i, para in enumerate(merged)]


def create_plan(work_id: str, script_version_id: str, *,
                plan_id: Optional[str] = None, revision_of: Optional[str] = None) -> Dict[str, Any]:
    """由已采用稿件生成并冻结计划。稿件不存在或正文为空即拒收。"""
    batch = st.get("script_batches", work_id) or {}
    cand = next((c for c in batch.get("candidates", []) if c["id"] == script_version_id), None)
    if not cand:
        raise TtsRejected("STALE_SELECTION",
                          "稿件不存在或已被新版本取代，无法生成表演计划",
                          {"work_id": work_id, "script_version_id": script_version_id})
    text = cand.get("text", "")
    if not text.strip():
        raise TtsRejected("PLAN_SCRIPT_EMPTY", "稿件正文为空，无法生成表演计划")
    work = st.get("works", work_id) or {}
    from . import voices as vc
    voice = vc.get_voice(work.get("voice_asset_id"))
    profile = vc.effective_profile(work, voice) if voice else {}
    segs = derive_segments(text)
    plan = ac.PerformancePlan(
        id=plan_id or st.new_id("plan"), work_id=work_id, script_version_id=cand["id"],
        script_digest=ac.digest(text), identity_id=work.get("identity_id"),
        voice_asset_id=work.get("voice_asset_id"), segments=segs,
        synthesis_profile=profile,
        tts_controls_digest=vc.controls_digest(voice, profile) if voice else "",
        tts_controls_revision=int(work.get("tts_controls_revision") or 0),
        target_duration_ms=int((work.get("brief") or {}).get("target_duration_ms", 0)),
        revision_of=revision_of,
    )
    from .compiler import build_plan_digest
    plan.plan_digest = build_plan_digest(plan.model_dump())
    row = plan.model_dump()
    row["auto_derived"] = True
    st.put("performance_plans", plan.id, row, {"work_id": work_id})
    return row


def plans_for_work(work_id: str) -> List[Dict[str, Any]]:
    return sorted(st.all_rows("performance_plans", "work_id=?", [work_id]),
                  key=lambda r: r.get("created_at", ""))


def latest_plan_for_work(work_id: str, script_version_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    rows = plans_for_work(work_id)
    if script_version_id:
        rows = [r for r in rows if r.get("script_version_id") == script_version_id]
    return rows[-1] if rows else None


def is_usable(plan: Optional[Dict[str, Any]]) -> bool:
    """可用的前提是计划已冻结且内容未被改动。"""
    if not plan or not plan.get("plan_digest") or not plan.get("segments"):
        return False
    from .compiler import build_plan_digest

    return build_plan_digest(plan) == plan["plan_digest"]


def binds_current_choice(plan: Optional[Dict[str, Any]], work: Dict[str, Any]) -> bool:
    """计划的音色/人物绑定是否仍指向作品当前的选择。

    换声或换人之后，旧计划依然"结构完整"：摘要对得上、分段也齐，
    所以 `is_usable()` 会判它可用。但它绑的是**上一版**的声音或人物，
    继续复用会在提交前被 `assert_voice_binding` / `assert_identity_binding`
    挡下，表现出来就是"刚选完音色，合成却报错"——报错点离原因太远。

    因此绑定不符即视为不可用，按当前选择重新派生。旧计划不删，
    新计划用 `revision_of` 指回去，版本链保持可追溯。
    """
    if not plan:
        return False
    from . import voices as vc
    voice = vc.get_voice(work.get("voice_asset_id"))
    current_digest = vc.controls_digest(voice, vc.effective_profile(work, voice)) if voice else ""
    if plan.get("tts_controls_digest") and plan["tts_controls_digest"] != current_digest:
        return False
    if int(plan.get("tts_controls_revision") or 0) != int(work.get("tts_controls_revision") or 0):
        return False
    bound_voice = plan.get("voice_asset_id")
    if bound_voice and bound_voice != work.get("voice_asset_id"):
        return False
    bound_identity = plan.get("identity_id")
    if bound_identity and bound_identity != work.get("identity_id"):
        return False
    return True


def ensure_plan(work_id: str, *, force_new: bool = False) -> Dict[str, Any]:
    """取当前可用计划；没有（或已损坏、或绑定已过期）就按采用稿派生一份并冻结。

    "派生"不等于"另建一份草稿"：它的分段与稿件逐字对应，
    之后所有环节都只读这一份。
    """
    work = st.get("works", work_id) or {}
    script_version_id = work.get("script_version_id")
    if not script_version_id:
        raise TtsRejected("SELECTION_REQUIRED",
                          "尚未采用稿件：全文合成以已采用稿件为唯一正文来源",
                          {"work_id": work_id})
    existing = latest_plan_for_work(work_id, script_version_id) if not force_new else None
    if existing is not None and is_usable(existing) and binds_current_choice(existing, work):
        return existing
    return create_plan(work_id, script_version_id,
                       revision_of=(existing.get("id") if existing else None))
