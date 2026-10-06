"""统一编辑时间线（AC-MEDIA-04 的"同一编辑时间轴"）。

一条 EditTimeline 描述"源素材 → 终片"的确定性映射：
- 裁剪（片头偏移 head_offset_ms / 片尾尾裁 tail_trim_ms）；
- 分段拼接（多段原片按序拼接，段内连续映射）；
- map_source_to_final / map_final_to_source 供字幕与验证双向使用。

校验是硬的：段内正时长、final 侧单调连续无空隙重叠、总时长落在终片范围内。
"字幕不能停留在 TTS 原始时间轴"——所有 cue 必须经 EditTimeline 映射后才可使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .errors import VideoRejected

_EPS_MS = 1


@dataclass(frozen=True)
class EditSegment:
    source_in_ms: int
    source_out_ms: int
    final_in_ms: int


@dataclass(frozen=True)
class EditTimeline:
    segments: List[EditSegment] = field(default_factory=list)

    # ---- 构造 ----

    @staticmethod
    def single(source_duration_ms: int, *, head_offset_ms: int = 0,
               tail_trim_ms: int = 0) -> "EditTimeline":
        """单段全片映射：终片 = 源片 [head_offset, source_duration - tail_trim)。"""
        if head_offset_ms < 0 or tail_trim_ms < 0:
            raise VideoRejected("TRIM_NEGATIVE", "片头/片尾裁剪不能为负")
        if head_offset_ms + tail_trim_ms >= source_duration_ms:
            raise VideoRejected("TRIM_EXCEEDS_SOURCE",
                                f"裁剪总量 {head_offset_ms + tail_trim_ms}ms 不少于源片长 "
                                f"{source_duration_ms}ms")
        return EditTimeline(segments=[EditSegment(
            source_in_ms=head_offset_ms,
            source_out_ms=source_duration_ms - tail_trim_ms,
            final_in_ms=0)])

    @staticmethod
    def concat(segment_lengths_ms: List[int]) -> "EditTimeline":
        """多段拼接：源段按给定时长顺序首尾相接，终侧连续无空隙。"""
        if not segment_lengths_ms:
            raise VideoRejected("SEGMENTS_EMPTY", "分段拼接至少需要一段")
        segs: List[EditSegment] = []
        src_cursor = 0
        final_cursor = 0
        for i, length in enumerate(segment_lengths_ms):
            if length <= 0:
                raise VideoRejected("SEGMENT_LENGTH_INVALID",
                                    f"第 {i} 段时长 {length}ms 非正")
            segs.append(EditSegment(source_in_ms=src_cursor,
                                    source_out_ms=src_cursor + length,
                                    final_in_ms=final_cursor))
            src_cursor += length
            final_cursor += length
        return EditTimeline(segments=segs)

    # ---- 只读派生 ----

    @property
    def final_duration_ms(self) -> int:
        return sum(s.source_out_ms - s.source_in_ms for s in self.segments)

    @property
    def source_span_ms(self) -> int:
        if not self.segments:
            return 0
        return self.segments[-1].source_out_ms - self.segments[0].source_in_ms

    def validate(self) -> None:
        if not self.segments:
            raise VideoRejected("TIMELINE_EMPTY", "时间线没有任何段")
        prev_out = 0
        prev_src_out: int | None = None
        for i, s in enumerate(self.segments):
            if s.source_out_ms - s.source_in_ms <= 0:
                raise VideoRejected("SEGMENT_EMPTY", f"第 {i} 段源区间为空")
            if s.final_in_ms != prev_out:
                raise VideoRejected(
                    "SEGMENTS_NOT_CONTIGUOUS",
                    f"第 {i} 段 final_in={s.final_in_ms} 与前段终点 {prev_out} 不连续")
            if prev_src_out is not None and s.source_in_ms != prev_src_out:
                raise VideoRejected(
                    "SEGMENTS_SOURCE_GAP",
                    f"第 {i} 段源起点 {s.source_in_ms} 与前段源终点 {prev_src_out} 不接续")
            prev_out = s.final_in_ms + (s.source_out_ms - s.source_in_ms)
            prev_src_out = s.source_out_ms

    # ---- 映射 ----

    def map_source_to_final(self, t_ms: int) -> int:
        """源时间 → 终时间；落在任何段的裁剪区外即拒绝（宁可拒不可错位）。"""
        for s in self.segments:
            if s.source_in_ms <= t_ms < s.source_out_ms:
                return s.final_in_ms + (t_ms - s.source_in_ms)
            if t_ms == s.source_out_ms:
                # 右端闭点：归到段尾（与下一段起点连续）
                return s.final_in_ms + (s.source_out_ms - s.source_in_ms)
        raise VideoRejected("TIME_OUT_OF_SOURCE",
                            f"源时间 {t_ms}ms 不在任何段的保留区间内")

    def map_final_to_source(self, t_ms: int) -> int:
        if t_ms < 0 or t_ms > self.final_duration_ms:
            raise VideoRejected("TIME_OUT_OF_FINAL",
                                f"终时间 {t_ms}ms 超出终片长 {self.final_duration_ms}ms")
        for s in self.segments:
            seg_len = s.source_out_ms - s.source_in_ms
            if s.final_in_ms <= t_ms < s.final_in_ms + seg_len:
                return s.source_in_ms + (t_ms - s.final_in_ms)
        return self.segments[-1].source_out_ms
