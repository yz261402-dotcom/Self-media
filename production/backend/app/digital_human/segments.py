"""OmniHuman 约 60 秒分段（AC-TH-05）。

分段的唯一合法来源是**同一条已确认音轨的句子边界**：

- 切点来自对齐产物（provider 时间戳/强制对齐/ASR 对齐）的真实毫秒位置，
  不是按字符数平均切、也不是按目标时长反推；
- 每段严格小于当前规范上限（60 秒），超限在提交前就拒绝；
- **禁止加速、禁止截字**：为了让音频塞进上限而改速度或砍掉字词，
  是把约束违约伪装成通过；
- 每段保存相对统一音轨的全局偏移，拼接与字幕映射都靠它。

装箱是这一层的另一半职责，而且**最容易被漏掉**
---------------------------------------------
只按句子切、不装箱，一条 62 秒的音轨会被切成 7 段（6 个接缝），
其中 3 个接缝落在正在说话的地方。而接缝处的画面跳变是 OmniHuman 侧决定的
（它没有"承接上一段末帧"的入参），本地拼接只能遮、不能消——
**少一个接缝，比事后怎么遮都管用**。装到上限后同样这条音轨只有 2 段、1 个接缝。

装箱只决定"装到哪个句子边界为止"，**不制造任何新的切点**：
切点仍然全部来自对齐产物的真实毫秒位置。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import storage as st
from ..contracts import now_iso
from ..tts import audio as tts_audio
from ..tts import gate as tts_gate
from ..video.errors import VideoBlocked
from . import seams
from .errors import TalkingHeadBlocked, TalkingHeadRejected, TalkingHeadError
from .routes import OMNIHUMAN_LIMIT

SEGMENT_PLAN_TABLE = "segment_plans"

# 句子结束标点。切点只允许落在这些字符之后——在句子中间切开会让每段的
# 语调重新起头，听感上"每段都像第一句"。
SENTENCE_ENDINGS = "。！？!?…；;\n"
# 段内最小时长：过短的尾段（比如只剩一个字）会单独产生一次计费调用，先合并
MIN_SEGMENT_MS = 900

# 整轨直出（不切分）的门槛：契约是"严格小于 60 秒"。
# 留 500ms 是为了吸收时长取整与切片误差——曾踩过的坑正是"提交成功、查询永远 50215"，
# 而那时发出去的音频恰好卡在上限附近。余量取小值，保证"不到 1 分钟就不切分"照常成立。
WHOLE_TRACK_MARGIN_MS = 500


def needs_split(duration_ms: int, limit_ms: int) -> bool:
    """这条音轨**是否必须**切成多段。够短就整轨一次调用，一刀不切。"""
    return int(duration_ms) + WHOLE_TRACK_MARGIN_MS >= int(limit_ms)


# 装箱目标 = 上限 × 安全系数。**不贴着上限走**：切片后的 WAV 时长有毫秒级取整，
# 而"是否 ≥60 秒"是云端在自己那一侧判定的；贴边走等于把上限违约伪装成通过，
# 最后只会拿到"提交成功、查询永远 50215"这种最难归因的形态。
PACK_SAFETY = 0.95
# 为了让切点落在更静的句读处，最多愿意往回让多少毫秒。给这个上限是因为
# 回让是在拿"段数变多"换"接缝更静"，不设限就会为了几 dB 切出一堆碎段。
PACK_GIVEBACK_MS = 2500
# 回让的门槛：只有明显更静才值得。1dB 的区别不值一段内容。
PACK_GIVEBACK_MIN_GAIN_DB = 3.0


def _limit_ms() -> int:
    return int(OMNIHUMAN_LIMIT["audio_max_seconds_exclusive"] * 1000)


def _sentence_bounds(text: str) -> List[int]:
    """返回句子结束字符的 index（该字符本身归属前一句）。"""
    return [i for i, ch in enumerate(text) if ch in SENTENCE_ENDINGS]


def _pack_atoms(atoms: List[Dict[str, Any]], limit_ms: int,
                energy: Optional[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """把"句子原子块"贪心装箱到上限，返回每段的 [起, 止] 原子下标。

    贪心是这里唯一诚实的做法：在"切点必须来自句子边界"的硬约束下，
    这就是能装到的最少段数；再优化就要么切在句子中间，要么预知后面句子的时长。
    """
    target = max(MIN_SEGMENT_MS, int(limit_ms * PACK_SAFETY))
    groups: List[Tuple[int, int]] = []
    i, n = 0, len(atoms)
    while i < n:
        start_ms = int(atoms[i]["start_ms"])
        j = i
        while j + 1 < n and int(atoms[j + 1]["end_ms"]) - start_ms <= target:
            j += 1
        # 只有后面还有内容时，回让切点才有意义（最后一段直接装到结尾）。
        if j + 1 < n and energy is not None:
            j = _retreat_to_quiet(atoms, i, j, start_ms, energy)
        groups.append((i, j))
        i = j + 1
    return groups


def _retreat_to_quiet(atoms: List[Dict[str, Any]], i: int, j: int, start_ms: int,
                      energy: Dict[str, Any]) -> int:
    """把切点从"能装到的最远处"往回挪到更静的句读处，代价是这一段变短。"""
    latest = int(atoms[j]["end_ms"])
    floor_ms = latest - PACK_GIVEBACK_MS
    candidates = sorted(
        (int(atoms[k]["end_ms"]) for k in range(i, j + 1)
         if int(atoms[k]["end_ms"]) >= floor_ms
         and int(atoms[k]["end_ms"]) - start_ms >= MIN_SEGMENT_MS),
        reverse=True)  # 从晚到早：默认答案是第一个，只有明显更静才回退
    best = seams.pick_quiet_boundary(candidates, energy=energy,
                                     min_gain_db=PACK_GIVEBACK_MIN_GAIN_DB)
    if best is None:
        return j
    for k in range(j, i - 1, -1):
        if int(atoms[k]["end_ms"]) == best:
            return k
    return j


def _whole_track_segment(text: str, duration_ms: int, limit_ms: int) -> Dict[str, Any]:
    """整轨一段：覆盖全文、覆盖整条音轨，不做任何切分。

    起点取 0 而不是对齐给出的首字时间：这一段要交出去的是**整条音轨**，
    起点若取成 240ms 之类，切片就会从 240ms 开始，凭空丢掉开头那一小截。
    """
    return {"index": 0, "segment_id": "oh_seg_1", "text": text,
            "start_char": 0, "end_char": len(text),
            "start_ms": 0, "end_ms": int(duration_ms),
            "duration_ms": int(duration_ms), "global_offset_ms": 0,
            "over_limit": int(duration_ms) >= limit_ms,
            "sentences": None, "whole_track": True}


def build_segments(text: str, words: List[Dict[str, Any]], duration_ms: int,
                   *, limit_ms: Optional[int] = None,
                   energy: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """按句边界把文本映射到真实音频时间，并装箱到上限。要求词级时间戳覆盖全文。

    `energy` 是当前音轨的能量分析（`seams.load_energy`）。给了它，
    装箱会优先把切点落在更静的句读处；给不了（比如拿不到音频文件）就退化为
    纯装箱——装箱本身不依赖它，少掉的只是"挑更静切点"这一层优化。
    """
    limit = limit_ms or _limit_ms()
    if not text:
        raise TalkingHeadRejected("SEGMENT_TEXT_EMPTY", "稿件正文为空，无法分段")
    # **够短就整轨一次调用，一刀不切。**
    # 这不是优化，是正确性：分段本身会凭空造出接缝，而接缝处的画面跳变是
    # 供应商侧决定的、本地拼不掉的。能不切就不切。
    # 附带效果：单段不需要任何切点，因此**不依赖对齐产物**——
    # 短素材（最常见的情形）不必先跑对齐就能生成。
    if not needs_split(duration_ms, limit):
        return [_whole_track_segment(text, int(duration_ms), limit)]
    if not words:
        raise TalkingHeadBlocked(
            "SEGMENTATION_REQUIRES_ALIGNMENT",
            "分段必须基于真实字词时间戳：均分时长或未验证逐句时长不能作为切分依据；"
            "先完成并登记对齐产物",
        )

    ordered = sorted(words, key=lambda w: (int(w.get("start_char", 0)), int(w.get("start_ms", 0))))
    char_to_ms: Dict[int, int] = {}
    for w in ordered:
        sc, ec = int(w.get("start_char", 0)), int(w.get("end_char", 0))
        for c in range(sc, ec):
            char_to_ms.setdefault(c, int(w.get("start_ms", 0)))
        if ec > 0:
            char_to_ms[ec - 1] = int(w.get("end_ms", w.get("start_ms", 0)))

    ends = _sentence_bounds(text)
    cuts: List[int] = []
    for e in ends:
        if not cuts or e + 1 - cuts[-1] >= 40:
            cuts.append(e + 1)
    if not cuts or cuts[-1] != len(text):
        cuts.append(len(text))

    def ms_at(char_index: int) -> int:
        """字符位置对应的起始时间。

        边界时间必须**按定义**取同一个函数值：第 i 段的结束就是第 i+1 段的开始，
        否则拼起来要么漏字要么重字。文本末尾直接取音频总时长。
        """
        if char_index >= len(text):
            return int(duration_ms)
        for c in range(char_index, -1, -1):
            if c in char_to_ms:
                return char_to_ms[c]
        raise TalkingHeadRejected(
            "SEGMENT_TIMING_GAP",
            f"对齐结果未覆盖字符位置 {char_index}，无法建立可信切点；"
            "不得用估算值补齐",
        )

    raw: List[Dict[str, Any]] = []
    prev_char = 0
    for cut in cuts:
        seg_text = text[prev_char:cut]
        if not seg_text.strip():
            prev_char = cut
            continue
        raw.append({"index": len(raw), "text": seg_text,
                    "start_char": prev_char, "end_char": cut,
                    "start_ms": ms_at(prev_char), "end_ms": ms_at(cut)})
        prev_char = cut

    # 过短的尾段并入前一段：单独一次调用既贵又容易在接缝处出问题。
    # 但合并后若会顶到上限，就不合并——上限优先于省钱，且超限必须暴露而不是被"顺手"绕过。
    merged: List[Dict[str, Any]] = []
    for seg in raw:
        prev = merged[-1] if merged else None
        if prev and (seg["end_ms"] - seg["start_ms"]) < MIN_SEGMENT_MS \
                and (seg["end_ms"] - prev["start_ms"]) < limit:
            prev["text"] += seg["text"]
            prev["end_char"] = seg["end_char"]
            prev["end_ms"] = seg["end_ms"]
            continue
        merged.append(seg)

    out: List[Dict[str, Any]] = []
    for i, (first_atom, last_atom) in enumerate(_pack_atoms(merged, limit, energy)):
        head, tail = merged[first_atom], merged[last_atom]
        duration = int(tail["end_ms"]) - int(head["start_ms"])
        out.append({
            "index": i, "segment_id": f"oh_seg_{i + 1}",
            "text": "".join(merged[k]["text"] for k in range(first_atom, last_atom + 1)),
            "start_char": head["start_char"], "end_char": tail["end_char"],
            "start_ms": int(head["start_ms"]), "end_ms": int(tail["end_ms"]),
            "duration_ms": duration, "global_offset_ms": int(head["start_ms"]),
            "over_limit": duration >= limit,
            "sentences": last_atom - first_atom + 1,
        })
    return out


def assert_segments_submittable(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """提交前拦截：任一超限、拼接不覆盖全文、或偏移不连续都拒绝。"""
    if not segments:
        raise TalkingHeadRejected("SEGMENTS_EMPTY", "没有可提交的分段")
    limit = _limit_ms()
    over = [s for s in segments if s["duration_ms"] >= limit]
    if over:
        raise TalkingHeadRejected(
            "SEGMENT_EXCEEDS_LIMIT",
            f"分段 {[s['segment_id'] for s in over]} 达到或超过 {limit}ms 上限；"
            "规范要求严格小于上限，禁止加速或截字",
            {"over": [{"segment_id": s["segment_id"], "duration_ms": s["duration_ms"]}
                      for s in over]},
        )
    for i, s in enumerate(segments):
        if s["index"] != i:
            raise TalkingHeadRejected("SEGMENT_INDEX_NONCONTIGUOUS", "分段序号不连续")
        if i and s["global_offset_ms"] != segments[i - 1]["end_ms"]:
            raise TalkingHeadRejected(
                "SEGMENT_OFFSET_GAP",
                f"分段 {s['segment_id']} 的全局偏移与上一段结束时间不连续；"
                "拼接后会出现漏字或重字",
            )
    return {
        "segments": len(segments),
        "total_ms": sum(s["duration_ms"] for s in segments),
        "max_segment_ms": max(s["duration_ms"] for s in segments),
        "limit_ms": limit,
        "speedup_applied": False,
        "truncation_applied": False,
        "note": "按同一确认音轨的句子边界切分并装箱到上限（接缝越少越好）；"
                "未加速、未截字，偏移为音频全局时间",
    }


def frozen_body(work: Dict[str, Any]) -> str:
    """冻结 PerformancePlan 的正文——分段、对齐与朗读映射必须同一来源。

    不允许因为"works 里也存了一份正文"就绕过冻结校验：正文来源若不同，
    对齐产出的字符位置与分段使用的字符位置就会指向不同的字符串，
    于是切点看起来"合法"，实际切在别处。
    """
    plan_id = work.get("performance_plan_id")
    if not plan_id:
        raise TalkingHeadRejected("PLAN_REQUIRED", "缺少冻结 PerformancePlan，正文来源不明")
    from ..tts import compiler as tts_compiler

    frozen = tts_compiler.load_frozen_plan(plan_id)
    return tts_compiler.script_text_of(work["id"], frozen["script_version_id"])


def create_plan(work_id: str, *, limit_ms: Optional[int] = None,
                text: Optional[str] = None) -> Dict[str, Any]:
    """为作品建立分段计划。要求已确认最终音频 + 冻结计划正文。

    对齐产物**只在真的要切时才需要**：音频够短就整轨一段，一个切点都用不上，
    此时要求先跑对齐纯属挡路（绝大多数素材是这种情况）。
    """
    work = st.get("works", work_id)
    if not work:
        raise TalkingHeadRejected("WORK_NOT_FOUND", f"作品不存在：{work_id}")
    audio = tts_audio.final_audio(work_id)
    if not audio:
        raise TalkingHeadBlocked("FINAL_AUDIO_REQUIRED", "尚无已确认最终音频，分段无从谈起")
    if audio.get("is_mock"):
        raise TalkingHeadBlocked("FINAL_AUDIO_IS_MOCK", "占位音频不得用于分段与提交")

    body = text if text is not None else frozen_body(work)
    duration_ms = int(audio.get("duration_ms") or 0)
    limit = limit_ms or _limit_ms()

    rows = st.all_rows("alignment_records", "audio_id=?", [audio["id"]])
    latest = sorted(rows, key=lambda r: r.get("created_at", ""))[-1] if rows else None
    words = (latest or {}).get("words", [])
    # 只在"需要切点"时才把对齐当成硬前置。
    if needs_split(duration_ms, limit):
        alignment = tts_gate.alignment_status(work_id)
        if not alignment.get("aligned") or not words:
            raise TalkingHeadBlocked("SEGMENTATION_REQUIRES_ALIGNMENT",
                                     alignment.get("reason") or "对齐未完成，无法取真实切点")

    # 能量分析只用来"挑更静的切点"，装箱本身不依赖它；拿不到就退化并如实记录，
    # 而不是让一个观感优化把建计划这件事卡住。
    energy, silence_preference = None, "skipped:no_local_audio"
    try:
        uri = audio.get("storage_uri") or ""
        if uri:
            energy = seams.load_energy(seams.runtime_path(uri))
            silence_preference = "enabled"
    except (TalkingHeadError, VideoBlocked, OSError, ValueError) as error:
        silence_preference = f"skipped:{getattr(error, 'code', type(error).__name__)}"

    segments = build_segments(body, words, duration_ms, limit_ms=limit_ms,
                              energy=energy)
    summary = assert_segments_submittable(segments)
    summary["silence_preference"] = silence_preference
    summary["whole_track"] = bool(segments and segments[0].get("whole_track"))
    plan = {
        "id": st.new_id("segplan"), "work_id": work_id,
        "audio_asset_id": audio["id"], "audio_digest": audio.get("file_hash", ""),
        "audio_duration_ms": duration_ms,
        "alignment_id": (latest or {}).get("id"),
        "alignment_source": (latest or {}).get("source"),
        "text_digest": (latest or {}).get("audio_digest", ""),
        "limit_ms": _limit_ms(), "segments": segments, "summary": summary,
        "created_at": now_iso(),
        "note": "分段只从冻结计划正文与同一音轨的真实时间派生；不维护第二份可编辑分段稿",
    }
    st.put(SEGMENT_PLAN_TABLE, plan["id"], plan, {"work_id": work_id})
    return plan


def latest_plan(work_id: str) -> Optional[Dict[str, Any]]:
    rows = st.all_rows(SEGMENT_PLAN_TABLE, "work_id=?", [work_id])
    if not rows:
        return None
    return sorted(rows, key=lambda r: r.get("created_at", ""))[-1]


def segment_status(work_id: str) -> Dict[str, Any]:
    plan = latest_plan(work_id)
    if not plan:
        return {"work_id": work_id, "planned": False,
                "reason": "尚未建立分段计划（需要已确认音频与可用对齐）"}
    return {"work_id": work_id, "planned": True, "plan_id": plan["id"],
            **plan["summary"],
            "segments": [{"segment_id": s["segment_id"], "duration_ms": s["duration_ms"],
                          "global_offset_ms": s["global_offset_ms"]}
                         for s in plan["segments"]]}
