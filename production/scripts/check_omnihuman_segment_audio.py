#!/usr/bin/env python3
"""核对"真正发给 OmniHuman 的音频"到底是多长（只读 + 免费）。

为什么需要它
------------
OmniHuman 的 `audio_url` 是**整段音频**，官方契约写明：

    音频时长必须小于 60 秒……过长情况下**提交任务正常，查询任务会报错**：
    {"code": 50215, "message": "Input invalid for this service."}

也就是说，把整条音轨发给云端，会拿到一个看起来正常的 `task_id`，
然后**无论查多少次都是 50215**。这个症状极易被误判成"云端故障/账号没开通"，
实际病因却是"我们发的音频超长"（2026-09-28 真实踩到）。

本脚本把**每一段实际会被云端抓取的 URL** 匿名读回来，量一遍真实时长，
于是"分段是不是真的分段了"不需要靠推断，可以直接看数。

用法（在 production/backend 下）
--------------------------------
    ./.venv/Scripts/python.exe ../scripts/check_omnihuman_segment_audio.py work_xxx

本脚本不提交任何生成任务、不产生任何生成费用；只做切片与对象存储读写
（对象存储读写免费），并且对象按内容寻址，重复运行会直接复用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

# 本机常有 127.0.0.1:7897 代理；匿名读回和 TOS 上传都不该走它。
for _key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
             "ALL_PROXY", "all_proxy"):
    os.environ.pop(_key, None)

from app import config, storage as st  # noqa: E402


def _probe(data: bytes, suffix: str = ".wav"):
    """把字节落盘后量真实时长（秒）。量的是"云端会抓到的东西"，不是我们的推算。"""
    from app.video import ffmpeg as vff

    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(data)
        handle.close()
        cp = subprocess.run(
            [vff.ffprobe_bin(), "-v", "error", "-print_format", "json",
             "-show_format", str(handle.name)],
            capture_output=True, timeout=60)
        return float(json.loads(cp.stdout)["format"]["duration"])
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"匿名读取未返回 200：{response.status}")
        return response.read()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    work_id = args[0]

    config.bootstrap_secrets()
    from app.digital_human import omni_media, segments as sg, submission as sub
    from app.digital_human.plate_source import resolve_plate_source
    from app.tts import audio as tts_audio

    work = st.get("works", work_id)
    if not work:
        print(f"作品不存在：{work_id}")
        return 2
    audio = tts_audio.final_audio(work_id) or {}
    if not audio:
        print("该作品没有已确认的最终音频，无从核对。")
        return 2
    ak = os.environ.get("AVATAR_STUDIO_VOLC_ACCESS_KEY", "")
    sk = os.environ.get("AVATAR_STUDIO_VOLC_SECRET_KEY", "")
    if not ak or not sk:
        print("缺凭据：请在密钥清单里补 volc-ak / volc-sk 两行。")
        return 2

    plan = sg.latest_plan(work_id)
    total_ms = int(audio.get("duration_ms") or 0)
    limit_ms = int(plan["limit_ms"]) if plan else None
    print(f"作品      : {work_id}")
    print(f"最终音频  : {audio['id']}  时长 {total_ms / 1000:.3f}s")
    print(f"单次上限  : {limit_ms / 1000:.0f}s（严格小于）" if limit_ms else "单次上限  : 未知")
    print(f"超限      : {'是 —— 必须分段，否则查询阶段必报 50215' if limit_ms and total_ms >= limit_ms else '否 —— 整轨可单次提交'}\n")

    segments = (plan or {}).get("segments") or [{
        "segment_id": "oh_seg_1", "index": 0, "start_ms": 0, "end_ms": total_ms,
        "duration_ms": total_ms, "global_offset_ms": 0, "single_call": True}]

    media = omni_media.prepare_media(resolve_plate_source(work), audio, ak=ak, sk=sk)
    print(f"人物图 URL: {media['image_url']}")
    print(f"整轨   URL: {media['audio_url']}")
    print(f"整轨实测  : {_probe(_fetch(media['audio_url'])):.3f}s\n")

    urls = omni_media.prepare_segment_audio(
        audio, segments, audio_url=media["audio_url"],
        audio_path=media.get("audio_path"), ak=ak, sk=sk)

    print(f"{'段':<10}{'窗口(ms)':<18}{'预期(s)':>9}{'云端实测(s)':>13}  判定")
    print("-" * 68)
    bad = 0
    for seg in segments:
        sid = seg["segment_id"]
        start_ms, end_ms = omni_media._segment_window(seg)
        want = (end_ms - start_ms) / 1000.0
        url = urls.get(sid, "")
        got = _probe(_fetch(url)) if url else 0.0
        if not url:
            verdict, bad = "缺 URL —— 不会提交", bad + 1
        elif abs(got - want) > 0.05:
            verdict, bad = "时长不符 —— 切片有问题", bad + 1
        elif limit_ms and got * 1000 >= limit_ms:
            verdict, bad = "**超限 —— 查询必报 50215**", bad + 1
        else:
            verdict = "ok"
        print(f"{sid:<10}{f'{start_ms}-{end_ms}':<18}{want:>9.3f}{got:>13.3f}  {verdict}")

    print()
    if bad:
        print(f"有 {bad} 段不合格：**不要提交**，先修好音频，否则会得到"
              "『提交成功、查询永远 50215』这种最难归因的形态。")
        return 1
    print("全部合格：每一段都带着自己的、未超限的音频。可以提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
