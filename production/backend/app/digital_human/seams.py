"""静默与接缝分析：只依赖本机 ffmpeg，与具体作品、具体音色无关。

**这里禁止出现任何从某一条音轨观测来的阈值。** 原因不是洁癖：
同一套代码要处理不同音色、不同底噪、不同语速的稿件。拿一篇稿子量到的
"-50 dB 算静默"写死在这里，换一篇底噪高的就会把说话判成静默，
换一篇底噪低的又会白白放弃本来可用的停顿——而这两种错都不会报错，
只会静悄悄地把黑场压在正在动的嘴上，或者让该被遮住的接缝裸奔。

所以阈值一律**从当前音轨自身的能量分布推导**：

    静默阈值 = 低分位 + 混合系数 × (高分位 − 低分位)

低分位代表这条音轨的底噪，高分位代表这条音轨的说话音量，
两者之间取一小段作为"既不是说话、也不必是绝对无声"的判静线。
再套一层绝对区间做兜底，防止极端分布把阈值推到荒谬的位置。

若一条音轨**压根没有可辨识的动静差异**（比如全程满电平的音乐垫底），
本模块返回"无可用静默"，调用方必须退化为硬切而不是硬套一个阈值。

提供的两件事：

- `seam_decisions()`：给拼接用——逐接缝、逐侧判定能不能压黑。
- `pick_quiet_boundary()`：给分段用——在一组合法切点里挑最安静的那个。
"""
from __future__ import annotations

import array
import math
import os
from itertools import accumulate
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..video import ffmpeg as vff
from .errors import TalkingHeadBlocked, TalkingHeadRejected

# 能量分析用的采样率：判断"有没有声音"不需要高保真，16k 足够且省一半内存。
ENERGY_RATE = 16000
# 统计块长。80ms 比一个字短、比一次换气长，正好落在"能区分说话与停顿"的尺度上。
BLOCK_MS = 80
# 计算 dB 时的下限：全零采样会让 log10 发散，必须夹住。
FLOOR_DB = -120.0

# 阈值推导：低分位取底噪、高分位取说话音量，中间取 SILENCE_MIX 处。
SILENCE_LOW_Q = 0.05
SILENCE_HIGH_Q = 0.75
SILENCE_MIX = 0.35
# 动静差不到这个量级，说明这条音轨没有可辨识的静默（别硬判）。
MIN_DYNAMIC_RANGE_DB = 12.0
# 绝对兜底区间：防止极端分布把阈值推到荒谬位置。
THRESHOLD_MIN_DB = -75.0
THRESHOLD_MAX_DB = -40.0
# 硬约束：阈值必须**严格落在**这条音轨的底噪与说话音量之间，两侧各留这么多余量。
# 这条比上面的绝对区间优先——曾踩过的坑是：底噪 −37dB 的音轨按混合系数算出 −27dB，
# 再被绝对上限夹到 −40dB，结果阈值低于底噪本身，底噪再也判不成静默。
# 绝对区间只是防荒谬的兜底，不能反过来破坏"对这条音轨可分"这件正事。
MARGIN_DB = 3.0

# 压黑时长候补，从长到短试：第一个"整窗都低于阈值"的就采用。
# 240ms 起是因为再长就像刻意的转场了；60ms 收尾是因为再短就压不住跳变。
FADE_CANDIDATES_MS: tuple = (240, 200, 160, 120, 80, 60)
# 判定切点静默时，向两侧各看多长。
BOUNDARY_PROBE_MS = 120


# ---------------------------------------------------------------- 路径

def runtime_root() -> str:
    """运行时目录（`backend/runtime`）的绝对路径——所有落盘位置都必须以它为根。"""
    return os.path.realpath(str(Path(__file__).resolve().parents[2] / "runtime"))


def runtime_path(rel_uri: str) -> str:
    """storage_uri（如 `audio/<work>/<id>.wav`）→ 本机绝对路径，并挡住越界。"""
    if not rel_uri or os.path.isabs(rel_uri):
        raise TalkingHeadRejected("MEDIA_PATH_INVALID", f"存储路径不是相对路径：{rel_uri}")
    root = runtime_root()
    path = os.path.realpath(os.path.join(root, rel_uri.replace("/", os.sep)))
    if os.path.commonpath([root, path]) != root:
        raise TalkingHeadRejected("MEDIA_PATH_INVALID", f"存储路径越出运行时目录：{rel_uri}")
    return path


# ---------------------------------------------------------------- 能量

def load_energy(path: str, *, rate: int = ENERGY_RATE) -> Dict[str, Any]:
    """把整条音轨解成单声道 PCM，做平方前缀和。

    一次性解完，之后任意窗口的 RMS 都是 O(1) 查表：
    分段要试几十个切点、拼接要试每接缝两侧 × 6 个时长，
    逐个窗口去调 ffmpeg 会产生上百次进程启动。
    """
    if not os.path.isfile(path):
        raise TalkingHeadBlocked("AUDIO_ENERGY_FILE_MISSING", f"音频文件不在本机：{path}")
    cp = vff.run_ffmpeg(["-v", "error", "-i", path, "-ac", "1", "-ar", str(rate),
                         "-f", "s16le", "-"], timeout=900)
    if cp.returncode != 0:
        raise TalkingHeadBlocked(
            "AUDIO_ENERGY_DECODE_FAILED", "音频能量分析失败：解不出 PCM",
            {"stderr_tail": (cp.stderr or b"").decode("utf-8", "replace")[-2000:]})
    raw = bytes(cp.stdout or b"")
    n = len(raw) // 2
    if n == 0:
        raise TalkingHeadBlocked("AUDIO_ENERGY_DECODE_FAILED",
                                 "音频能量分析失败：解出 0 个采样点")
    samples = array.array("h")
    samples.frombytes(raw[:n * 2])
    scale = 1.0 / (32768.0 * 32768.0)
    prefix = array.array("d", accumulate((s * s * scale for s in samples), initial=0.0))
    return {"rate": int(rate), "prefix": prefix,
            "samples": n, "total_ms": n * 1000.0 / rate}


def window_db(energy: Dict[str, Any], start_ms: float, end_ms: float) -> Optional[float]:
    """任意时间窗的 RMS（dBFS）。窗口为空或越界返回 None。"""
    if not energy:
        return None
    rate = int(energy["rate"])
    n = len(energy["prefix"]) - 1
    i0 = max(0, min(n, int(round(start_ms * rate / 1000.0))))
    i1 = max(0, min(n, int(round(end_ms * rate / 1000.0))))
    if i1 <= i0:
        return None
    mean = (energy["prefix"][i1] - energy["prefix"][i0]) / (i1 - i0)
    if mean <= 1e-20:
        return FLOOR_DB
    return max(FLOOR_DB, 10.0 * math.log10(mean))


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] * (1 - (pos - lo)) + ordered[hi] * (pos - lo)


def block_db_series(energy: Dict[str, Any], *, block_ms: int = BLOCK_MS) -> List[float]:
    """全片按块切开的能量序列——阈值推导与诊断都基于它。"""
    total = float(energy.get("total_ms") or 0.0)
    count = max(1, int(total // max(1, block_ms)))
    out: List[float] = []
    for i in range(count):
        db = window_db(energy, i * block_ms, (i + 1) * block_ms)
        out.append(FLOOR_DB if db is None else db)
    return out


def silence_threshold(energy: Dict[str, Any], *, block_ms: int = BLOCK_MS,
                      mix: float = SILENCE_MIX) -> Optional[float]:
    """从这条音轨自身的能量分布推导静默阈值；没有可辨识静默时返回 None。

    返回 None 不是失败，而是一个**必须被尊重的结论**：这条音轨分不出动静，
    调用方应该退化成硬切，而不该自己拍一个数字继续判。
    """
    blocks = block_db_series(energy, block_ms=block_ms)
    if not blocks:
        return None
    low = _percentile(blocks, SILENCE_LOW_Q)
    high = _percentile(blocks, SILENCE_HIGH_Q)
    if low is None or high is None or high - low < MIN_DYNAMIC_RANGE_DB:
        return None
    level = max(THRESHOLD_MIN_DB, min(THRESHOLD_MAX_DB, low + mix * (high - low)))
    return min(max(level, low + MARGIN_DB), high - MARGIN_DB)


def energy_profile(energy: Dict[str, Any], *, block_ms: int = BLOCK_MS) -> Dict[str, Any]:
    """诊断用：把阈值是怎么来的摆出来，便于事后对账"为什么这个接缝没压黑"。"""
    blocks = block_db_series(energy, block_ms=block_ms)
    return {"block_ms": block_ms, "blocks": len(blocks),
            "low_db": _percentile(blocks, SILENCE_LOW_Q),
            "high_db": _percentile(blocks, SILENCE_HIGH_Q),
            "threshold_db": silence_threshold(energy, block_ms=block_ms)}


# ---------------------------------------------------------------- 接缝判定

def seam_decisions(energy: Dict[str, Any], seam_times_ms: Sequence[int], *,
                   threshold_db: Optional[float],
                   fade_candidates_ms: Sequence[int] = FADE_CANDIDATES_MS
                   ) -> List[Dict[str, Any]]:
    """逐接缝判定能不能压黑，且**两侧独立**。

    为什么必须两侧分开：接缝 T 的前一侧是"上一段的尾巴"，后一侧是"下一段的开头"，
    这两处的动静**没有任何理由相同**。实测里最常见的形态就是静默全在接缝之后
    （段首带换行停顿），此时若按"接缝居中"一刀切，要么白白放弃这个接缝，
    要么把黑场压在接缝前正在说话的嘴上。

    判定口径：只在**黑场真正覆盖的那段窗口**整体低于阈值时才压黑。
    宁可保持硬切，也不能在说话处压黑——前者的代价是画面跳一下，
    后者会把一个字吞掉，那是内容错误，不是观感问题。
    """
    out: List[Dict[str, Any]] = []
    for i, t in enumerate(seam_times_ms):
        t = int(t)
        tail_ms, head_ms = 0, 0
        tail_db = head_db = None
        if threshold_db is not None and energy:
            for d in fade_candidates_ms:
                db = window_db(energy, t - d, t)
                if db is not None and db <= threshold_db:
                    tail_ms, tail_db = int(d), round(db, 1)
                    break
            for d in fade_candidates_ms:
                db = window_db(energy, t, t + d)
                if db is not None and db <= threshold_db:
                    head_ms, head_db = int(d), round(db, 1)
                    break
        out.append({"index": i, "at_ms": t,
                    "fade_out_ms": tail_ms, "fade_in_ms": head_ms,
                    "tail_db": tail_db, "head_db": head_db,
                    "softened": bool(tail_ms or head_ms)})
    return out


# ---------------------------------------------------------------- 切点选择

def boundary_db(energy: Dict[str, Any], at_ms: int, *,
                probe_ms: int = BOUNDARY_PROBE_MS) -> Optional[float]:
    """切点的"安静程度"：取两侧里**较吵**的那一侧。

    取 max 而不是 min，是在刻意保守：一个切点要算得上"落在静默里"，
    得前后都安静。只看安静的那一半，会选出"前半句在说话、后半段恰好停了"的切点。
    """
    if not energy:
        return None
    before = window_db(energy, at_ms - probe_ms, at_ms)
    after = window_db(energy, at_ms, at_ms + probe_ms)
    if before is None or after is None:
        return before if after is None else after
    return max(before, after)


def pick_quiet_boundary(candidates_ms: Sequence[int], *,
                        energy: Optional[Dict[str, Any]],
                        probe_ms: int = BOUNDARY_PROBE_MS,
                        min_gain_db: float = 3.0) -> Optional[int]:
    """在一组候选切点里挑最安静的；返回 None 表示无法判断（调用方保留默认）。

    `candidates_ms` 必须**按从晚到早**排列：默认答案是第一个（最晚的，
    也就是不牺牲时长的那个），只有明显更安静才回退。
    `min_gain_db` 就是这个"明显"——回退是要拿时长换的，
    区区 1dB 的区别不值得少装一段内容。
    """
    if not candidates_ms or not energy:
        return None
    best: Optional[int] = None
    best_db: Optional[float] = None
    for candidate in candidates_ms:
        db = boundary_db(energy, int(candidate), probe_ms=probe_ms)
        if db is None:
            continue
        if best_db is None or db < best_db - min_gain_db:
            # 先到先得：并列或不够明显时，保留更早被遍历到的那个（也就是更晚的切点）。
            best, best_db = int(candidate), db
    return best
