"""字幕行（终时间轴）与检查，以及 ASS 生成（AC-MEDIA-04/05）。

- cue 一律以**终时间轴**表达：源 cue 必须先经 EditTimeline 映射；
- 硬检查：正文覆盖（漏字即拒）、重叠、错序、越界（含末句截断）；
- 样式冻结参数集中在此（字号/每行字数/安全区 MarginV），烧录用 libass；
- 中文逐字无空格：覆盖比对按"去空白后逐字符包含"判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import VideoRejected
from .timeline import EditTimeline

_EPS_MS = 1  # 末句不截断判定余量（毫秒）

# ---- 冻结样式参数（AC-MEDIA-05：字号/换行/安全区满足冻结参数）----
STYLE = {
    "font_name": "Microsoft YaHei",
    "font_size": 64,          # PlayResY=1920 下的字号
    "max_chars_per_line": 16,  # 超长自动折行（\\N）
    "max_lines": 2,
    "margin_v": 260,           # 距底安全区：不遮嘴/关键人物区域
    "margin_h": 60,
    "outline": 3,
    "primary_colour": "&H00FFFFFF",
    "outline_colour": "&H00101010",
}


@dataclass(frozen=True)
class Cue:
    """终时间轴字幕行。text 不含换行；start/end 毫秒。"""
    index: int
    start_ms: int
    end_ms: int
    text: str


def normalize_text(t: str) -> str:
    return "".join(t.split())


def map_cues(source_cues: List[dict], tl: EditTimeline) -> List[Cue]:
    """源时间轴 cue → 终时间轴。源 cue: {start_ms, end_ms, text}。"""
    out: List[Cue] = []
    for i, c in enumerate(sorted(source_cues, key=lambda x: x["start_ms"])):
        start = tl.map_source_to_final(int(c["start_ms"]))
        end = tl.map_source_to_final(int(c["end_ms"]))
        if end <= start:
            raise VideoRejected("CUE_EMPTY_SPAN",
                                f"第 {i} 条 cue 映射后区间为空（{start}≥{end}）")
        out.append(Cue(index=i, start_ms=start, end_ms=end, text=str(c["text"])))
    return out


def check_cues(cues: List[Cue], *, final_duration_ms: int,
               script_text: str) -> Dict[str, str]:
    """AC-MEDIA-04 硬检查；任何一条不过即 VideoRejected（宁可阻断不出错片）。"""
    if not cues:
        raise VideoRejected("CUES_EMPTY", "字幕行为空：有声正文必须有字幕")
    prev: Optional[Cue] = None
    for c in cues:
        if not normalize_text(c.text):
            raise VideoRejected("CUE_TEXT_BLANK", f"第 {c.index} 条 cue 文本为空")
        if c.start_ms < 0 or c.end_ms > final_duration_ms:
            raise VideoRejected("CUE_OUT_OF_RANGE",
                                f"第 {c.index} 条 cue 越界 [{c.start_ms},{c.end_ms}] "
                                f"超出终片 {final_duration_ms}ms")
        if prev is not None:
            if c.start_ms < prev.start_ms:
                raise VideoRejected("CUE_ORDER", f"第 {c.index} 条 cue 起点倒退")
            if c.start_ms < prev.end_ms:
                raise VideoRejected("CUE_OVERLAP",
                                    f"第 {c.index} 条与第 {prev.index} 条重叠")
        prev = c
    last = cues[-1]
    if last.end_ms > final_duration_ms - _EPS_MS:
        raise VideoRejected("LAST_CUE_TRUNCATED",
                            f"末句终点 {last.end_ms}ms 贴住/超出终片终点，疑似截断")
    # 正文覆盖：去空白拼接必须逐字符一致（漏字/多字都拒）
    want = normalize_text(script_text)
    got = normalize_text("".join(c.text for c in cues))
    if want != got:
        raise VideoRejected("SCRIPT_COVERAGE_MISMATCH",
                            f"字幕与冻结正文不一致（正文 {len(want)} 字 / 字幕 {len(got)} 字）",
                            detail={"script_head": want[:40], "cues_head": got[:40]})
    return {"cue_count": str(len(cues)), "coverage_chars": str(len(want))}


def _wrap(text: str) -> str:
    max_len = STYLE["max_chars_per_line"]
    max_lines = STYLE["max_lines"]
    if len(text) <= max_len:
        return text
    lines = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    if len(lines) > max_lines:
        raise VideoRejected("CUE_TOO_LONG",
                            f"cue {len(text)} 字超过 {max_lines} 行上限，需在 ASR/切分行层拆分")
    return "\\N".join(lines)


def _ts(ms: int) -> str:
    h, rem = divmod(max(0, ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms2 // 10:02d}"


def build_srt(cues: List[Cue]) -> str:
    """SRT 使用真实毫秒时间戳，与烧录 ASS 同一组已验证字幕。"""
    def timestamp(ms: int) -> str:
        seconds, milliseconds = divmod(ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
    return "\n".join(f"{i}\n{timestamp(c.start_ms)} --> {timestamp(c.end_ms)}\n{c.text}\n"
                     for i, c in enumerate(cues, 1))


def build_ass(cues: List[Cue], *, play_res_x: int = 1080,
              play_res_y: int = 1920, title: str = "") -> str:
    """生成烧录用 ASS。样式来自冻结 STYLE；时间已是终时间轴。"""
    s = STYLE
    header = (
        "[Script Info]\n"
        f"; {title}\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        f"MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{s['font_name']},{s['font_size']},{s['primary_colour']},"
        f"&H000000FF,{s['outline_colour']},&H00000000,0,0,0,0,100,100,0,0,1,"
        f"{s['outline']},0,2,{s['margin_h']},{s['margin_h']},{s['margin_v']},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n")
    events = "".join(
        f"Dialogue: 0,{_ts(c.start_ms)},{_ts(c.end_ms)},Default,,0,0,0,,{_wrap(c.text)}\n"
        for c in cues)
    return header + events
