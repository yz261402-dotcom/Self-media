"""把 OmniHuman 的分段原片拼成一条完整原片。

**本机 ffmpeg，全程不提交任何云端任务、不产生费用。**

为什么必须有这一步
-----------------
OmniHuman 单次调用的音频必须 < 60 秒，长音频只能切成多段分别生成。
每段生成完各自登记为一条独立原片（`segment_index` / `global_offset_ms`），
而后期阶段只接受**一条**原片（`EditTimeline.single()` + 单输入渲染），
并且会校验"原片不短于已确认音频"。不拼，多段作品就走不到成片。

两个不能靠"直接首尾相接"绕过去的坑（都是实测数据，不是推测）：

1. **云端每段产出都比计划窗口长**（本例 +40ms / +120ms）。直接相接误差会累积，
   而字幕是按**统一音轨时间轴**映射的 —— 越往后画面越落后于声音。
   → 每段必须按它自己的窗口裁到**精确帧数**。
2. **分段计划不一定从 0 毫秒开始**（本例第一段从 240ms 起，窗口合计 61483ms，
   而音轨 61723ms）。不补齐开头就会因"原片短于音频"被后期拒掉。
   → 开头用首帧定格补齐，结尾补到不短于音轨。

对齐方式：每段在统一音轨上的窗口两端各自**就近取整到帧**，
这样每个接缝的误差 ≤ 半帧，不会像"按段长累加"那样一路漂下去。

接缝怎么办（实测推翻过一次结论，别凭直觉改）
-------------------------------------------
OmniHuman **没有"承接上一段末帧"的入参**，每段都独立从源图的定格表情起，
所以接缝必然跳：实测段内相邻帧 PSNR 24.75 dB，而接缝处只有 12.65 dB —— 一半都不到。
这是供应商侧决定的，本地怎么拼都**只能遮、不能消**。

三种处理的代价（都在本机量化过）：

- **叠加转场（xfade）**：会同时叠出两张脸，且唇形跟声音对不上 —— 宁可跳，也不要鬼影。
- **黑场缓冲（本模块默认）**：在接缝处压黑，**不动任何一帧的位置**。
- **什么都不做**：硬切，跳变原样保留。

黑场不是"全片加黑"，而是**逐接缝、逐侧独立判定**（B' 方案）：

1. 把整条音轨解成单声道 PCM 做平方前缀和，之后任意窗口的 RMS 都是 O(1) 查表。
2. 接缝 T 处，**前一段的尾巴**看 `[T-d, T]`、**后一段的开头**看 `[T, T+d]`，
   两侧**各自**从 240ms 起往下试，取第一个整体低于 `SEAM_SILENCE_DB` 的时长。
3. 只有静默的那一侧才压黑；另一侧保持原样。

为什么必须两侧分开：实测本片 6 个接缝，静默区并不对称——
3920 / 43840 / 53280 三处的静默全在**接缝之后**（段首有换行停顿），
其中 53280 的接缝前 80ms 还是 -37 dB 的说话声。若按"接缝居中"一刀切，
要么会把 53280 判成不合规白白放弃，要么会把黑场压在正在说话的嘴上。

压黑用 `fade`（淡出到黑 / 从黑淡入），**不删帧、不插帧**：
时长与逐帧对齐完全不变，音画同步 0 位移。代价只是接缝附近几百毫秒被压暗，
而这些地方本来就是静默 —— 没有唇形可丢。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..tts import audio as tts_audio
from ..video import ffmpeg as vff
from . import seams
from . import segments as sg
from . import videos as vd
from .errors import TalkingHeadBlocked, TalkingHeadRejected

STITCH_MARK = "stitch:"
# 拼接**方案**的版本。改动任何影响画面结果的处理（淡入淡出归属、压黑方式、
# 补帧策略、编码参数）都必须 +1。
#
# 幂等复用的 key 只覆盖"输入"，不覆盖"怎么拼"。于是修了拼接逻辑之后，
# 同一组输入会一直命中旧产物、永远看不到修复结果——而旧产物是**错得很安静**的
# （黑场压在错误的接缝上），界面上完全看不出来。版本号就是用来切断这种复用的。
STITCH_SCHEMA = 2


# ---------------------------------------------------------------- 时间轴换算

def _frames(ms: float, fps: float) -> int:
    """毫秒 → 帧数（就近取整）。用于把窗口边界钉在帧格上。"""
    return int(round(float(ms) * fps / 1000.0))


def _seconds(frames: int, fps: float) -> float:
    return frames / fps


# ---------------------------------------------------------------- 前置校验

def _runtime_path(rel_uri: str) -> str:
    """storage_uri（如 `video/<work>/<id>.mp4`）→ 本机绝对路径。

    与 `seams.runtime_path` 同一实现：拼接要读分段原片和音轨，
    路径判定若与分段/能量分析那套不一致，就会出现"这里能读、那里读不到"的鬼故事。
    """
    try:
        return seams.runtime_path(rel_uri)
    except TalkingHeadRejected as error:
        raise TalkingHeadRejected("STITCH_PATH_INVALID", error.message) from error


def _plan_and_windows(work_id: str) -> List[Dict[str, Any]]:
    plan = sg.latest_plan(work_id)
    segs = list((plan or {}).get("segments") or [])
    if not segs:
        raise TalkingHeadRejected("STITCH_PLAN_MISSING",
                                  "没有分段计划：拼接只能依据计划里的窗口，不能现猜")
    for i, s in enumerate(segs):
        start = s.get("start_ms")
        end = s.get("end_ms")
        if start is None or end is None:
            raise TalkingHeadRejected(
                "STITCH_PLAN_INCOMPLETE",
                f"第 {i + 1} 段缺少窗口时间戳：没有窗口就无法按帧对齐")
        if int(end) <= int(start):
            raise TalkingHeadRejected(
                "STITCH_PLAN_INCOMPLETE", f"第 {i + 1} 段窗口非正：{start}-{end}ms")
    return segs


def _segment_videos(work_id: str, runs: List[Dict[str, Any]],
                    total: int) -> List[Dict[str, Any]]:
    """按段号取出对应的已生成原片。缺一段都不拼。"""
    by_index = {int((r.get("provider_params") or {}).get("segment_index") or 0): r
                for r in runs}
    missing, failed, unknown = [], [], []
    for i in range(total):
        run = by_index.get(i)
        if run is None:
            missing.append(i + 1)
            continue
        if run.get("state") == "failed":
            failed.append(i + 1)
        elif run.get("state") == "unknown":
            unknown.append(i + 1)

    if unknown:
        raise TalkingHeadBlocked(
            "STITCH_SEGMENTS_UNKNOWN",
            f"第 {'、'.join(str(x) for x in unknown)} 段结果未知，可能已计费：先对账，不拼",
            {"unknown_segments": unknown})
    if failed:
        raise TalkingHeadBlocked(
            "STITCH_SEGMENTS_FAILED",
            f"第 {'、'.join(str(x) for x in failed)} 段失败：重做要重新确认费用，不拼",
            {"failed_segments": failed})
    if missing:
        raise TalkingHeadBlocked(
            "STITCH_SEGMENTS_PENDING",
            f"第 {'、'.join(str(x) for x in missing)} 段还没提交或还在生成：等全部完成再拼",
            {"pending_segments": missing})

    out: List[Dict[str, Any]] = []
    for i in range(total):
        run = by_index[i]
        asset_ids = list(run.get("result_asset_ids") or [])
        video = vd.get_video(asset_ids[0]) if asset_ids else None
        if video is None:
            raise TalkingHeadRejected("STITCH_SEGMENT_ASSET_MISSING",
                                      f"第 {i + 1} 段没有登记原片产物，无法拼接")
        if video.get("approval_state") == "revoked":
            raise TalkingHeadRejected(
                "STITCH_SEGMENT_ASSET_REVOKED",
                f"第 {i + 1} 段的原片已作废（输入已变化），不能作为拼接来源")
        if not video.get("has_video_track"):
            raise TalkingHeadRejected("STITCH_SEGMENT_NO_VIDEO",
                                      f"第 {i + 1} 段没有视频轨，不是视频")
        out.append(video)
    return out


def _probe_segments(paths: List[str]) -> List[Dict[str, Any]]:
    infos = []
    for i, p in enumerate(paths):
        if not os.path.isfile(p):
            raise TalkingHeadBlocked("STITCH_FILE_MISSING",
                                     f"第 {i + 1} 段的视频文件不在本机：{p}")
        info = vff.probe(p)
        stream = next((s for s in info.get("streams") or []
                       if s.get("codec_type") == "video"), None)
        if stream is None:
            raise TalkingHeadRejected("STITCH_SEGMENT_NO_VIDEO",
                                      f"第 {i + 1} 段探测不到视频轨")
        infos.append(info)
    return infos


def _common_fps_size(infos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """拼接目标规格：取第一段的帧率与尺寸，其余段统一缩放过去。"""
    first = infos[0]
    stream = next(s for s in first["streams"] if s.get("codec_type") == "video")
    raw = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "25/1")
    num, _, den = raw.partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    if fps <= 0:
        fps = 25.0
    return {"fps": fps, "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0)}


# ---------------------------------------------------------------- 拼接

def _build_clip_filter(index: int, frames: int, fps: float, width: int, height: int,
                       head_frames: int, tail_frames: int,
                       fade_in_ms: int = 0, fade_out_ms: int = 0) -> str:
    """单段的滤镜链：统一规格 → 按窗口精确裁帧 → 接缝压黑 → 首尾定格补齐。

    压黑用 `fade`（淡出到黑 / 从黑淡入），**不删帧、不插帧**：
    每一帧落在统一音轨上的位置严格不变，音画同步 0 位移。
    这一点是硬要求——一旦为了"让黑场有点时长"去偷帧或补帧，
    后面所有画面的时间轴就会整体错位，而错位不会报错，只会让嘴慢慢对不上声音。
    """
    parts = [f"fps={fps:g}", "setsar=1"]
    if width and height:
        parts.append(f"scale={width}:{height}")
    parts.append("format=yuv420p")
    # trim 的 end_frame 是"不含"的，因此 end_frame=N 恰好保留 N 帧。
    parts.append(f"trim=start_frame=0:end_frame={max(1, int(frames))}")
    parts.append("setpts=PTS-STARTPTS")
    clip_sec = max(1, int(frames)) / fps
    # 淡入从段首起算：段首若是静默（段首带换行停顿很常见），压黑不吃任何唇形。
    if fade_in_ms > 0:
        d = min(fade_in_ms / 1000.0, max(0.0, clip_sec - 1.0 / fps))
        if d > 0:
            parts.append(f"fade=t=in:st=0:d={d:.6f}")
    # 淡出贴到段尾：接缝处两侧同时到黑，跳变就被藏在黑里了。
    if fade_out_ms > 0:
        d = min(fade_out_ms / 1000.0, max(0.0, clip_sec - 1.0 / fps))
        if d > 0:
            parts.append(f"fade=t=out:st={max(0.0, clip_sec - d):.6f}:d={d:.6f}")
    if head_frames > 0:
        parts.append(f"tpad=start_mode=clone:start_duration={_seconds(head_frames, fps):.6f}")
    if tail_frames > 0:
        parts.append(f"tpad=stop_mode=clone:stop_duration={_seconds(tail_frames, fps):.6f}")
    return f"[{index}:v]" + ",".join(parts) + f"[v{index}]"


def _stitch_token(work_id: str, attempt: str, seg_videos: List[Dict[str, Any]],
                  audio: Dict[str, Any], spec: Dict[str, Any],
                  windows: List[Dict[str, Any]], total_frames: int,
                  seam_plan: List[Dict[str, Any]]) -> str:
    payload = {
        "schema": STITCH_SCHEMA,
        "work_id": work_id, "attempt": attempt, "fps": spec["fps"],
        "width": spec["width"], "height": spec["height"],
        "audio_id": audio.get("id"), "audio_hash": audio.get("file_hash"),
        "windows": [[s.get("start_ms"), s.get("end_ms")] for s in windows],
        "total_frames": total_frames,
        "seams": [[s.get("at_ms"), s.get("fade_out_ms"), s.get("fade_in_ms")]
                  for s in seam_plan],
        "segments": [[v.get("id"), v.get("file_hash")] for v in seg_videos],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _existing_stitch(work_id: str, token: str) -> Optional[Dict[str, Any]]:
    mark = f"{STITCH_MARK}{token}"
    for v in vd.list_videos(work_id, vd.KIND_ORIGINAL):
        if mark in (v.get("note") or ""):
            return v
    return None


def stitch_segments(work_id: str, *, keep_temp: bool = False,
                    soften_seams: bool = True) -> Dict[str, Any]:
    """把本作品**最近一次尝试**的全部分段原片拼成一条完整原片。

    幂等：同一组输入（段视频哈希 + 音轨哈希 + 窗口 + 规格 + 接缝方案）只登记一次，
    重复调用直接复用已拼好的那条，不会又多出一个文件。

    `soften_seams=False` 是显式放弃接缝压黑（退回全硬切）的开关。
    压黑只是观感优化，若有人要逐帧比对原始分段，得有办法拿到没动过的版本。
    """
    from . import omni_driver  # 局部导入：omni_driver 不需要知道 stitch 的存在

    group = omni_driver._active_group(work_id)
    if group is None:
        raise TalkingHeadRejected("STITCH_NO_RUNS",
                                  "这个作品没有 OmniHuman 运行记录：先生成，才有东西可拼")
    windows = _plan_and_windows(work_id)
    total = len(windows)
    if total < 2:
        # 整轨一段的作品本来就没有接缝可拼。再来一次"拼接"只会多出一个重复文件，
        # 而且会让人误以为做过什么处理。
        raise TalkingHeadRejected(
            "STITCH_NOTHING_TO_STITCH",
            "这个作品只有 1 段（整轨一次生成），没有可拼的接缝；"
            "分段原片本身就是完整原片")

    audio = tts_audio.final_audio(work_id)
    if not audio or not audio.get("confirmed"):
        raise TalkingHeadRejected("STITCH_AUDIO_UNCONFIRMED",
                                  "没有已确认的全文音频：拼接要以它作为时长基准")
    audio_path = _runtime_path(audio.get("storage_uri") or "")
    if not os.path.isfile(audio_path):
        raise TalkingHeadBlocked("STITCH_FILE_MISSING",
                                 f"最终音频文件不在本机：{audio_path}")
    audio_ms = int(audio.get("duration_ms") or
                   round(float(vff.probe(audio_path).get("format", {}).get("duration") or 0) * 1000))
    if audio_ms <= 0:
        raise TalkingHeadRejected("STITCH_AUDIO_DURATION_INVALID",
                                  "最终音频时长无效，无法作为拼接基准")

    seg_videos = _segment_videos(work_id, group["runs"], total)
    paths = [_runtime_path(v.get("storage_uri") or "") for v in seg_videos]
    infos = _probe_segments(paths)
    spec = _common_fps_size(infos)
    fps = spec["fps"]

    # 把窗口两端各自就近钉到帧格：每个接缝误差 ≤ 半帧，不会累加。
    bounds = [(_frames(int(w["start_ms"]), fps), _frames(int(w["end_ms"]), fps))
              for w in windows]
    head_frames = max(0, bounds[0][0])
    # 结尾至少补到"不短于音轨"：不必 -shortest 也能让后期通过覆盖校验。
    tail_target = max(bounds[-1][1], math.ceil(audio_ms * fps / 1000.0))
    tail_frames = max(0, tail_target - bounds[-1][1])
    clip_frames = [e - s for s, e in bounds]
    if any(n <= 0 for n in clip_frames):
        raise TalkingHeadRejected(
            "STITCH_PLAN_TOO_SHORT",
            "有分段窗口短于一帧：按帧对齐后没有可保留的画面，请重新建计划")

    # 接缝方案：阈值从这条音轨自身的能量分布来，逐接缝、逐侧独立判定。
    seam_plan: List[Dict[str, Any]] = []
    threshold_db: Optional[float] = None
    if soften_seams:
        energy = seams.load_energy(audio_path)
        threshold_db = seams.silence_threshold(energy)
        seam_plan = seams.seam_decisions(
            energy, [int(w["end_ms"]) for w in windows[:-1]], threshold_db=threshold_db)
    if not seam_plan:
        seam_plan = [{"index": i, "at_ms": int(w["end_ms"]),
                      "fade_out_ms": 0, "fade_in_ms": 0,
                      "tail_db": None, "head_db": None, "softened": False}
                     for i, w in enumerate(windows[:-1])]

    token = _stitch_token(work_id, group["attempt"], seg_videos, audio, spec,
                          windows, tail_target, seam_plan)
    reused = _existing_stitch(work_id, token)
    if reused:
        return {"video": reused, "reused": True, "total_segments": total,
                "duration_ms": reused.get("duration_ms"),
                "note": "同一组分段已经拼过，直接复用，不重复登记"}

    work = st.get("works", work_id) or {}
    anchor_params = group["anchor"].get("provider_params") or {}
    snapshot = anchor_params.get("input_snapshot") or {}
    work_dir = os.path.join(seams.runtime_root(), "omnihuman_stitch", work_id, token)
    os.makedirs(work_dir, exist_ok=True)
    out_path = os.path.join(work_dir, "original.mp4")

    filters = []
    for i, n in enumerate(clip_frames):
        filters.append(_build_clip_filter(
            index=i, frames=n, fps=fps, width=spec["width"], height=spec["height"],
            head_frames=head_frames if i == 0 else 0,
            tail_frames=tail_frames if i == total - 1 else 0,
            # seam[k] 位于第 k 段与第 k+1 段之间：
            # 它的 fade_out 压的是**第 k 段的尾巴**，fade_in 压的是**第 k+1 段的开头**。
            # 所以对第 i 段而言，淡入来自它前面那条缝（i-1），淡出来自它后面那条缝（i）。
            fade_in_ms=seam_plan[i - 1]["fade_in_ms"] if i > 0 else 0,
            fade_out_ms=seam_plan[i]["fade_out_ms"] if i < total - 1 else 0))
    chains = ";".join(filters)
    # 必须接滤镜链的**输出** [v0..vN]，不是原始输入 [0:v..N:v]：
    # 接原始输入会让上面每条链（裁帧/压黑/补帧）整体悬空，
    # ffmpeg 的报错是 "output 0 (v0) unconnected"，看不出是这里接错。
    inputs = "".join(f"[v{i}]" for i in range(total))
    chains += f";{inputs}concat=n={total}:v=1:a=0[vout]"

    args: List[str] = ["-y", "-threads", "4", "-filter_threads", "2"]
    for p in paths:
        args += ["-i", p]
    args += ["-i", audio_path,
             "-filter_complex", chains,
             "-map", "[vout]", "-map", f"{total}:a:0",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", f"{fps:g}",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", out_path]
    cp = vff.run_ffmpeg(args, timeout=3600)
    if cp.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise TalkingHeadBlocked(
            "STITCH_FAILED", "ffmpeg 拼接失败",
            {"stderr_tail": (cp.stderr or b"").decode("utf-8", "replace")[-4000:],
             "temp_dir": work_dir})

    probe = vff.probe(out_path)
    if not any(s.get("codec_type") == "video" for s in probe.get("streams") or []):
        raise TalkingHeadBlocked("STITCH_NO_VIDEO_TRACK",
                                 "拼接结果没有视频轨，不能作为原片")
    duration_ms = int(round(float(probe.get("format", {}).get("duration") or 0) * 1000))
    tolerance = int(round(1000.0 / fps))
    if duration_ms + tolerance < audio_ms:
        raise TalkingHeadBlocked(
            "STITCH_COVERAGE_SHORT",
            f"拼接结果 {duration_ms}ms 短于已确认音频 {audio_ms}ms：正文会被截断，不得登记",
            {"duration_ms": duration_ms, "audio_ms": audio_ms})

    data = open(out_path, "rb").read()
    file_hash = hashlib.sha256(data).hexdigest()
    softened = [s for s in seam_plan if s.get("softened")]
    seam_note = (
        f"{len(softened)}/{len(seam_plan)} 个接缝落在静默并压黑"
        f"（判静线 {threshold_db:.1f}dB）"
        if threshold_db is not None
        else f"{len(seam_plan)} 个接缝全为硬切（本音轨分不出动静，不能硬套阈值）")
    note = (f"由 {total} 段拼接而成：每段按计划窗口精确到帧，"
            f"首帧定格补 {int(round(head_frames * 1000 / fps))}ms，"
            f"尾帧补 {int(round(tail_frames * 1000 / fps))}ms；"
            f"{seam_note}；压黑不删帧不插帧，音画同步 0 位移 "
            f"（音轨 {audio_ms}ms，成品 {duration_ms}ms）· {STITCH_MARK}{token}")
    video = vd.register_video(
        work_id=work_id, kind=vd.KIND_ORIGINAL,
        route=group["anchor"].get("route") or "OMNIHUMAN_1_5",
        provider=group["anchor"].get("provider") or "omnihuman",
        model=group["anchor"].get("model") or "",
        duration_ms=duration_ms, file_hash=file_hash,
        is_mock=False, identity_id=work.get("identity_id") or snapshot.get("identity_id"),
        variant_id=work.get("variant_id") or snapshot.get("variant_id"),
        audio_asset_id=audio.get("id"),
        source_asset_id=snapshot.get("source_asset_id"),
        source_asset_hash=snapshot.get("source_asset_hash"),
        cost_reservation_id=group["anchor"].get("cost_reservation_id"),
        has_video_track=True, has_audio_track=True,
        width=spec["width"], height=spec["height"], fps=fps,
        checks=[], note=note,
    )
    video["storage_uri"] = vd.store_video_bytes(work_id, video["id"], data)
    st.put(vd.VIDEO_TABLE, video["id"], video, {"work_id": work_id})

    if not keep_temp:
        shutil.rmtree(work_dir, ignore_errors=True)

    return {"video": video, "reused": False, "total_segments": total,
            "duration_ms": duration_ms, "audio_ms": audio_ms,
            "head_pad_ms": int(round(head_frames * 1000 / fps)),
            "tail_pad_ms": int(round(tail_frames * 1000 / fps)),
            "fps": fps, "token": token,
            "silence_threshold_db": threshold_db,
            "seams": seam_plan,
            "seams_softened": len(softened),
            "note": "已把全部分段拼成一条完整原片；请观看审核后再确认"}
