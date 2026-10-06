"""表达编译器：冻结 PerformancePlan → MiniMax 调用参数 + 朗读映射（AC-TTS-02、AC-AG-03）。

设计要点（每一条都对应一个明确的反例）：

1. **单一分段来源**。分段、情绪、语速、重音只从冻结的 PerformancePlan 读，
   编译产物是**派生视图**，不生成第二份"可编辑分段稿"。
   计划摘要在编译前复算：对不上即说明计划在冻结后被改过，直接拒收。
2. **分段与稿件逐字一致**。把各段正文按序拼接后必须与绑定稿件**逐字符相同**；
   缺字、重字、顺序错乱都在提交媒体之前拒绝，而不是靠人工听出来。
3. **控制能力分三档报告**。supported / approximate / unsupported 各自留证；
   不支持的控制在调用**之前**就可见，绝不静默丢弃，也绝不假装已生效。
4. **表达标记永不进入朗读文本**。`spoken_text` 与原文逐字符相等——
   编译器不往文本里注入任何标记，因此"标记被读出来"在结构上不可能发生。
   若原文自身含疑似标记语法（`<#...#>`、`[pause]` 之类），只**报告**不静默改写：
   悄悄改动作者正文比读出一个标记更危险。
5. **不发送任意 SSML**。只产出供应商已声明支持的标量参数。
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

from ..contracts import SpeechAnnotation
from . import voices as vc
from .errors import TtsRejected

# 供应商情绪枚举（MiniMax Speech 系列声明的取值集合）。
# supported_emotions 只记录已试听证据，不限制用户选择供应商声明支持的全局情绪。
PROVIDER_EMOTIONS = vc.EMOTIONS

# 表达意图 → 供应商情绪候选（按优先级）。用于把策划写的意图词归一到枚举值。
EMOTION_INTENT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "calm": ("calm", "neutral", "gentle", "平静", "温和", "平和"),
    "happy": ("happy", "cheerful", "warm", "friendly", "开心", "愉快", "亲切", "温暖"),
    "sad": ("sad", "sorrow", "低沉", "悲伤"),
    "angry": ("angry", "serious", "严肃", "坚定"),
    "surprised": ("surprised", "excited", "amazed", "惊讶", "兴奋", "激动"),
    "fearful": ("fearful", "worried", "担忧", "紧张"),
    "disgusted": ("disgusted", "反感"),
    "neutral": ("neutral", "plain", "中性"),
}

# 语速档位 → speed 参数值。`normal` 与基准参数一致，属"已支持"；
# 其余档位是"近似"：参数在允许范围内，但该档位未单独试听验证。
PACE_TO_SPEED: Dict[str, float] = {"slow": 0.9, "normal": 1.0, "fast": 1.1}

# 计划里可能出现但不属于 TTS 的控制项：由其他环节消费，不能报成"TTS 不支持"而丢弃
FOREIGN_CONTROLS: Dict[str, str] = {
    "motion": "talking_head",
}

# 疑似表达标记语法。命中只报告，不静默改写原文。
TAG_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"<#\s*[0-9.]+\s*#>", "停顿标记语法"),
    (r"\[(pause|停顿|emphasis|重音|rate|语速|emotion|情绪)\s*[:：]?[^\]]*\]", "方括号表达标记"),
    (r"\{\{?\s*(pause|emotion|rate)\s*[:：][^}]*\}?\}", "花括号表达标记"),
    (r"</?(speak|break|prosody|emphasis)\b[^>]*>", "SSML 标签"),
)

# 段落脚手架标签：策划稿里用来标注段落作用的结构词（"开头：""例子："…）。
# 它们不是要朗读的内容。留在正文里会被逐字念出来，属于 AC-TTS-02 明确要求覆盖的
# 「标签误读」。只报告不改写——静默删掉作者正文比念出一个"例子："更危险。
SCAFFOLD_LABELS = ("开头", "标题", "副标题", "正文", "解释", "例子", "举例",
                   "边界", "收尾", "结尾", "小结", "过渡", "引入", "总结")

_SCAFFOLD_RE = re.compile(r"(?m)^[\s\u3000]*(" + "|".join(SCAFFOLD_LABELS) + r")\s*[:：]")


def scan_scaffold_labels(text: str, where: str) -> List[Dict[str, str]]:
    return [{"where": where, "kind": "段落脚手架标签", "matched": m.group(0).strip(),
             "span": f"{m.start()}-{m.end()}"}
            for m in _SCAFFOLD_RE.finditer(text or "")]


# 本地保守的单请求文本上限。**未按官方上限核验**，因此只用于本地切分与提示，
# 不能当作供应商能力声明对外输出。
LOCAL_TEXT_CHUNK_LIMIT = 3000
LOCAL_LIMIT_NOTE = (f"本地保守上限 {LOCAL_TEXT_CHUNK_LIMIT} 字，用于切分自检；"
                    "官方上限尚未核验，不作为供应商能力声明")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _emotion_to_provider(intent: str) -> Optional[str]:
    key = (intent or "").strip().lower()
    if not key:
        return None
    if key in PROVIDER_EMOTIONS:
        return key
    for target, aliases in EMOTION_INTENT_ALIASES.items():
        if key in aliases:
            return target
    return None


def build_plan_digest(plan: Dict[str, Any]) -> str:
    """复算冻结稿件、分段及全局语音参数摘要；兼容未含参数的旧计划。"""
    from ..agents import contracts as ac

    payload = {
        "segments": [{"segment_id": s.get("segment_id"), "text": s.get("text"),
                      "emotion": s.get("emotion"), "pace": s.get("pace"),
                      "emphasis": list(s.get("emphasis") or []),
                      "motion": s.get("motion")} for s in plan.get("segments", [])],
        "script_digest": plan.get("script_digest", ""),
    }
    # 旧计划摘要保持可读取；新计划同时冻结实际全局参数与修改版本。
    if plan.get("tts_controls_digest"):
        payload.update({"synthesis_profile": plan.get("synthesis_profile") or {},
                        "tts_controls_digest": plan["tts_controls_digest"],
                        "tts_controls_revision": int(plan.get("tts_controls_revision") or 0)})
    return ac.digest(payload)


# ---------- 前置校验：计划必须与稿件、音色、人物一致 ----------

def load_frozen_plan(plan_id: str) -> Dict[str, Any]:
    plan = st_get_plan(plan_id)
    if not plan:
        raise TtsRejected("PLAN_NOT_FOUND", f"PerformancePlan 不存在：{plan_id}")
    if not plan.get("plan_digest"):
        raise TtsRejected("PLAN_NOT_FROZEN",
                          "计划未冻结：缺少 plan_digest，不能作为编译输入")
    recomputed = build_plan_digest(plan)
    if recomputed != plan["plan_digest"]:
        raise TtsRejected(
            "PLAN_DIGEST_MISMATCH",
            "计划摘要与内容不符：冻结后被修改过，必须重新冻结后再编译",
            {"plan_id": plan_id, "registered": plan["plan_digest"], "recomputed": recomputed},
        )
    return plan


def st_get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    from .. import storage as st

    return st.get("performance_plans", plan_id) if plan_id else None


def assert_not_superseded(plan: Dict[str, Any]) -> None:
    """同一作品存在更新的计划时，旧计划不得继续驱动媒体生成。"""
    from .. import storage as st

    rows = st.all_rows("performance_plans", "work_id=?", [plan.get("work_id")])
    newer = [r for r in rows
             if r.get("created_at", "") > plan.get("created_at", "")
             and r.get("id") != plan.get("id")]
    if newer:
        latest = sorted(newer, key=lambda r: r.get("created_at", ""))[-1]
        raise TtsRejected(
            "STALE_PLAN",
            "该计划已被新版本取代：旧候选迟到不得改写当前选择，请改用最新计划",
            {"plan_id": plan.get("id"), "latest_plan_id": latest.get("id")},
        )


def script_text_of(work_id: str, script_version_id: str) -> str:
    from .. import storage as st

    batch = st.get("script_batches", work_id) or {}
    cand = next((c for c in batch.get("candidates", []) if c["id"] == script_version_id), None)
    if not cand:
        raise TtsRejected("STALE_SELECTION",
                          "绑定稿件不存在或已被新版本取代，须重新采用稿件",
                          {"work_id": work_id, "script_version_id": script_version_id})
    return cand.get("text", "")


def assert_segments_match_script(plan: Dict[str, Any], script_text: str) -> Dict[str, Any]:
    """分段拼接必须与稿件一致。缺字/重字在此处而非在听检时才被发现。

    判定分两级，因为"空白"与"字"不是一回事：

    1. 逐字符相同 → 完全一致。
    2. 去掉所有空白后相同 → **仅空白差异**（分段是按段落切分，段间换行不参与朗读），
       记为 `whitespace_only=True`。这不是失败，但必须显式记录，
       不能靠"反正听不出来"把它藏掉。
    3. 其余情况 → 缺字/重字/顺序错乱，硬拒收并给出可直接核对的片段。
    """
    segs = plan.get("segments", [])
    if not segs:
        raise TtsRejected("PLAN_SEGMENTS_EMPTY", "计划没有任何分段，无法编译")
    ids = [s.get("segment_id") for s in segs]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise TtsRejected("PLAN_SEGMENT_ID_DUPLICATED",
                          f"分段 ID 重复：{dup}，无法建立稳定的朗读映射",
                          {"duplicated": dup})

    joined = "".join(s.get("text", "") for s in segs)
    if joined == script_text:
        return {"ok": True, "whitespace_only": False, "missing": [], "extra": [],
                "note": "分段拼接与稿件逐字符相同"}

    expected, actual = _normalize_ws(script_text), _normalize_ws(joined)
    if expected == actual:
        return {"ok": True, "whitespace_only": True, "missing": [], "extra": [],
                "note": "分段拼接与稿件仅空白差异（段间换行不参与朗读）；"
                        "非空白字符逐字一致，无缺字重字"}

    diag = diagnose_text_mismatch(expected, actual)
    raise TtsRejected(
        "PLAN_SCRIPT_TEXT_MISMATCH",
        "分段正文与绑定稿件不一致：存在缺字或重字，媒体提交前必须纠正",
        {"plan_id": plan.get("id"), **diag},
    )


def diagnose_text_mismatch(expected: str, actual: str) -> Dict[str, Any]:
    """用差异块定位缺字/重字，给出可直接核对的片段，而不是只报"不一致"。"""
    sm = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    missing, extra = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            missing.append(expected[i1:i2])
        elif tag == "insert":
            extra.append(actual[j1:j2])
        elif tag == "replace":
            missing.append(expected[i1:i2])
            extra.append(actual[j1:j2])
    return {"missing": [m for m in missing if m], "extra": [e for e in extra if e]}


def assert_voice_binding(plan: Dict[str, Any], voice: Dict[str, Any],
                         work: Optional[Dict[str, Any]] = None) -> None:
    """计划的音色绑定必须指向同一个 VoiceAsset；不可临时换声。

    这是 AC-TTS-03 的资产侧约束：表达需要更强情绪时换 voice_id 是明确负例。
    """
    bound = plan.get("voice_asset_id")
    if bound and bound != voice.get("id"):
        raise TtsRejected(
            "VOICE_SCOPE_MISMATCH",
            "计划绑定的音色与本次请求的音色不一致：为表达情绪改 voice_id 属拒收项",
            {"plan_voice_asset_id": bound, "requested": voice.get("id")},
        )
    if work is not None and work.get("voice_asset_id") and work["voice_asset_id"] != voice.get("id"):
        raise TtsRejected(
            "VOICE_SCOPE_MISMATCH",
            "本次请求的音色不是该作品已选定的音色",
            {"work_voice_asset_id": work.get("voice_asset_id"), "requested": voice.get("id")},
        )


def assert_identity_binding(plan: Dict[str, Any], work: Dict[str, Any]) -> None:
    pid = plan.get("identity_id")
    wid = work.get("identity_id")
    if pid and wid and pid != wid:
        raise TtsRejected(
            "PLAN_IDENTITY_MISMATCH",
            "计划绑定的人物与作品当前人物不一致，须重新生成计划",
            {"plan_identity_id": pid, "work_identity_id": wid},
        )


# ---------- 派生标注视图 ----------

def annotations_from_plan(plan: Dict[str, Any]) -> List[SpeechAnnotation]:
    """从计划派生表达标注（单一来源就是计划本身）。

    停顿标注为**零长度**位置，落在段与段之间；因此"停顿"不会被误当成
    一段可朗读的文本，从而在结构上不可能被读出。
    """
    annotations: List[SpeechAnnotation] = []
    cursor = 0
    for i, seg in enumerate(plan.get("segments", [])):
        text = seg.get("text", "")
        seg_id = seg.get("segment_id", f"seg_{i + 1}")
        if i > 0:
            annotations.append(SpeechAnnotation(type="pause", start_char=cursor,
                                                end_char=cursor, value=seg_id))
        emotion = (seg.get("emotion") or "").strip()
        if emotion and emotion != "calm":
            annotations.append(SpeechAnnotation(type="emotion", start_char=cursor,
                                                end_char=cursor, value=emotion))
        pace = (seg.get("pace") or "").strip()
        if pace and pace != "normal":
            annotations.append(SpeechAnnotation(type="rate", start_char=cursor,
                                                end_char=cursor, value=pace))
        for word in seg.get("emphasis") or []:
            idx = text.find(word)
            if idx >= 0:
                annotations.append(SpeechAnnotation(
                    type="emphasis", start_char=cursor + idx,
                    end_char=cursor + idx + len(word), value=word))
        cursor += len(text)
    return annotations


def scan_suspicious_tags(text: str, where: str) -> List[Dict[str, str]]:
    """检测疑似表达标记语法。只报告，不改写——静默改写正文比读出标记更危险。"""
    hits: List[Dict[str, str]] = []
    for pattern, label in TAG_PATTERNS:
        for m in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            hits.append({"where": where, "kind": label, "matched": m.group(0),
                         "span": f"{m.start()}-{m.end()}"})
    return hits


# ---------- 编译 ----------

def compile_speech(plan_id: str, voice_id: str) -> Dict[str, Any]:
    """编译入口。返回派生视图；不写库、不维护第二份可编辑稿。"""
    from .. import storage as st

    plan = load_frozen_plan(plan_id)
    assert_not_superseded(plan)
    voice = vc.get_voice(voice_id)
    if not voice:
        raise TtsRejected("VOICE_NOT_FOUND", f"音色不存在：{voice_id}")
    scope = vc.assert_scope_complete(voice)
    vc.assert_model_allowed(voice.get("provider", ""), voice.get("model", ""))
    vc.assert_region_verified(voice.get("provider", ""), voice.get("region", ""))

    work = st.get("works", plan.get("work_id", "")) or {}
    assert_voice_binding(plan, voice, work)
    assert_identity_binding(plan, work)

    script_text = script_text_of(plan["work_id"], plan["script_version_id"])
    integrity = assert_segments_match_script(plan, script_text)
    from ..agents import contracts as ac

    if plan.get("script_digest") and plan["script_digest"] != ac.digest(script_text):
        raise TtsRejected(
            "PLAN_SCRIPT_DIGEST_MISMATCH",
            "计划声明的稿件摘要与当前稿件不符：计划绑定的是另一版正文",
            {"plan_script_digest": plan["script_digest"], "actual": ac.digest(script_text)},
        )

    # 标注视图在循环外算一次。注意变量名不能叫 annotations——
    # 本模块 `from __future__ import annotations` 已经在模块层占用了这个名字，
    # 一旦写错就会得到 "'_Feature' object is not iterable" 这种完全跑偏的报错。
    annotations_list = annotations_from_plan(plan)
    pause_segments = {a.value for a in annotations_list if a.type == "pause"}

    profile = vc.effective_profile(work, voice)
    profile_digest = vc.controls_digest(voice, profile)
    if ((plan.get("tts_controls_digest") and plan["tts_controls_digest"] != profile_digest)
            or int(plan.get("tts_controls_revision") or 0) != int(work.get("tts_controls_revision") or 0)):
        raise TtsRejected("PLAN_CONTROLS_MISMATCH", "语音参数已变化，请重新生成冻结计划")
    global_override = bool(work.get("tts_controls"))
    supported_emotions = set(voice.get("supported_emotions") or [])
    default_emotion = str(profile.get("emotion") or "calm")
    default_speed = float(profile.get("speed", 1.0))

    controls: Dict[str, List[Dict[str, Any]]] = {"supported": [], "approximate": [],
                                                 "unsupported": []}
    segments_out: List[Dict[str, Any]] = []
    cursor = 0
    for i, seg in enumerate(plan.get("segments", [])):
        text = seg.get("text", "")
        seg_id = seg.get("segment_id") or f"seg_{i + 1}"
        note: List[str] = []

        # --- 情绪：请求 → 供应商枚举 → 本合同音色是否已试听通过 ---
        intent = default_emotion if global_override else (seg.get("emotion") or "calm").strip()
        provider_emotion = _emotion_to_provider(intent)
        applied_emotion = default_emotion
        if global_override:
            bucket = "supported" if default_emotion in supported_emotions else "approximate"
            controls[bucket].append({
                "control": "emotion", "segment_id": seg_id, "requested": default_emotion,
                "applied": default_emotion, "applied_as": "global",
                "reason": "采用作品全局情绪；实际效果由试听确认",
            })
        elif provider_emotion is None:
            controls["unsupported"].append({
                "control": "emotion", "segment_id": seg_id, "requested": intent,
                "applied": default_emotion, "applied_as": "downgrade",
                "reason": f"{intent} 不在供应商情绪枚举内，降级为 {default_emotion} 并记录",
            })
            note.append(f"情绪 {intent} 不支持，已降级")
        elif provider_emotion != default_emotion:
            controls["unsupported"].append({
                "control": "emotion", "segment_id": seg_id, "requested": intent,
                "resolved_to": provider_emotion, "applied": default_emotion,
                "applied_as": "not_applied",
                "reason": "当前接口一次请求仅采用全局情绪，分段意图未应用；请设置作品全局情绪",
            })
            note.append(f"分段情绪 {provider_emotion} 未应用，实际采用全局 {default_emotion}")
        else:
            bucket = "supported" if default_emotion in supported_emotions else "approximate"
            controls[bucket].append({
                "control": "emotion", "segment_id": seg_id, "requested": intent,
                "applied": applied_emotion, "applied_as": "exact",
                "reason": "采用全局情绪；实测状态以音色试听证据为准",
            })

        # --- 语速：档位 → speed；normal 等于基准参数 ---
        pace = (seg.get("pace") or "normal").strip()
        if global_override:
            controls["supported"].append({
                "control": "pace", "segment_id": seg_id, "requested": default_speed,
                "applied": default_speed, "applied_as": "global", "reason": "采用作品全局语速",
            })
        elif pace not in PACE_TO_SPEED:
            controls["unsupported"].append({
                "control": "pace", "segment_id": seg_id, "requested": pace,
                "applied": default_speed, "applied_as": "downgrade",
                "reason": f"未知语速档位 {pace}，回落基准 speed={default_speed} 并记录",
            })
            note.append(f"语速档位 {pace} 未知，已回落基准")
        elif PACE_TO_SPEED[pace] == default_speed:
            controls["supported"].append({
                "control": "pace", "segment_id": seg_id, "requested": pace,
                "applied": default_speed, "applied_as": "exact",
                "reason": "与基准参数一致",
            })
        else:
            controls["unsupported"].append({
                "control": "pace", "segment_id": seg_id, "requested": pace,
                "applied": default_speed, "applied_as": "not_applied",
                "reason": "当前接口一次请求仅采用全局语速，分段语速未应用；请设置作品全局语速",
            })
            note.append(f"分段语速 {pace} 未应用，实际采用全局 {default_speed}")

        # --- 重音：无内联能力，不靠 SSML 绕过 ---
        for word in seg.get("emphasis") or []:
            controls["unsupported"].append({
                "control": "emphasis", "segment_id": seg_id, "requested": word,
                "applied": None, "applied_as": "not_applied",
                "reason": "本项目不发送任意 SSML；该音色的内联重音能力未经核验，"
                          "调用前明示不支持而非假装生效",
            })
            note.append("重音标记未生效，仅保留在标注视图")

        # --- 停顿：由标点自然产生；显式停顿控制未经核验 ---
        if seg_id in pause_segments:
            controls["unsupported"].append({
                "control": "pause", "segment_id": seg_id, "requested": "segment_boundary",
                "applied": None, "applied_as": "not_applied",
                "reason": "段间停顿依赖标点自然产生；显式停顿标记能力未核验，不注入标记",
            })

        # --- 外部控制：不属于 TTS，交由对应环节消费，不丢弃也不冒充 ---
        motion = (seg.get("motion") or "").strip()
        if motion and motion != "neutral":
            owner = FOREIGN_CONTROLS.get("motion", "talking_head")
            controls["approximate"].append({
                "control": "motion", "segment_id": seg_id, "requested": motion,
                "applied": None, "applied_as": "delegated", "owner": owner,
                "reason": f"motion 不是 TTS 控制项，由 {owner} 环节消费；"
                          "不伪称由文本精确生成动作",
            })

        segments_out.append({
            "segment_id": seg_id, "text": text,
            "char_start": cursor, "char_end": cursor + len(text),
            "emotion_intent": intent, "emotion_applied": applied_emotion,
            "pace": pace, "speed": default_speed,
            "notes": note,
        })
        cursor += len(text)

    # 朗读文本与原文逐字符相等：编译器不注入任何标记
    spoken_text = "".join(s["text"] for s in segments_out)
    tag_hits = scan_suspicious_tags(spoken_text, "spoken_text")
    tag_hits += scan_suspicious_tags(script_text, "script_text")
    tag_hits += scan_scaffold_labels(spoken_text, "spoken_text")

    provider_params = {
        "model": voice.get("model"),
        "voice_id": voice.get("remote_voice_id"),
        "speed": default_speed,
        "vol": profile.get("vol", 1.0),
        "pitch": profile.get("pitch", 0),
        "emotion": default_emotion,
        "language_boost": profile.get("language_boost", "Chinese"),
    }
    if profile.get("voice_modify"):
        provider_params["voice_modify"] = profile["voice_modify"]

    warnings: List[str] = []
    if tag_hits:
        kinds = sorted({h["kind"] for h in tag_hits})
        warnings.append(f"正文含疑似{'、'.join(kinds)}（{len(tag_hits)} 处）："
                        "已报告未改写，须人工确认这些字符是否应当被朗读")
    if len(_normalize_ws(spoken_text)) > LOCAL_TEXT_CHUNK_LIMIT:
        warnings.append(LOCAL_LIMIT_NOTE)

    return {
        "plan_id": plan["id"], "plan_digest": plan["plan_digest"],
        "work_id": plan["work_id"], "script_version_id": plan["script_version_id"],
        "script_digest": plan.get("script_digest", ""),
        "voice_asset_id": voice["id"], "voice_scope": scope,
        "provider_params": provider_params,
        "synthesis_profile": profile,
        "tts_controls_digest": profile_digest,
        "tts_controls_revision": int(work.get("tts_controls_revision") or 0),
        "segments": segments_out,
        "source_text": script_text,
        "spoken_text": spoken_text,
        "char_count": len(_normalize_ws(spoken_text)),
        "controls": controls,
        "control_summary": {k: len(v) for k, v in controls.items()},
        "annotations": [a.model_dump() for a in annotations_list],
        "integrity": integrity,
        "suspicious_tags": tag_hits,
        "warnings": warnings,
        "provider_text_limit": {"local_chunk_limit": LOCAL_TEXT_CHUNK_LIMIT,
                                "official_limit_verified": False,
                                "note": LOCAL_LIMIT_NOTE},
        "note": "派生视图：分段与表达的唯一来源是冻结计划，本产物不可另行编辑",
    }
