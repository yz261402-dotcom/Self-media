"""分段装箱与接缝压黑的通用性测试。

这批用例刻意**不引用任何一个具体作品的观测值**。之前踩过的坑是：
从一条音轨量到"接缝 X 是静默的""−50dB 算静默"，然后把这些数字写进代码；
换个音色、换个底噪、换个语速，这些数字全部失效，而且失效时不报错，
只会静悄悄地把黑场压在正在说话的嘴上。

所以这里验的是**推导过程而不是推导结果**：
同一套代码喂进不同底噪、不同动静差的音轨，都要能自己得出可用的判据；
没有可辨识静默时要如实说"分不出来"，而不是硬套一个数字。
"""
from __future__ import annotations

import array
import math
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from dh_helpers import make_words  # noqa: E402

from app.digital_human import seams, segments as sg, stitch  # noqa: E402

SAMPLE_RATE = 32000


# ---------------------------------------------------------------- 合成音轨

def _write_wav(path: str, *, total_ms: int, bursts=(), noise_amp: float = 0.0) -> str:
    """写一条**动静位置完全可控**的 WAV。

    `bursts` 是 (start_ms, end_ms, freq, amp)；未覆盖的区间是 `noise_amp` 的底噪
    （0 即数字静音）。合成音轨而不是拿真实录音，是为了让"哪里有声、哪里静默"
    成为用例里的已知条件——用真实录音就只能反过来猜阈值对不对。
    """
    n = int(SAMPLE_RATE * total_ms / 1000.0)
    buf = array.array("h", bytes(2 * n))
    if noise_amp:
        for i in range(n):
            buf[i] = int(max(-1.0, min(1.0, noise_amp * math.sin(2 * math.pi * 110 * i / SAMPLE_RATE))) * 32767)
    for start, end, freq, amp in bursts:
        i0 = int(SAMPLE_RATE * start / 1000.0)
        i1 = min(n, int(SAMPLE_RATE * end / 1000.0))
        for i in range(i0, i1):
            t = (i - i0) / SAMPLE_RATE
            buf[i] = int(max(-1.0, min(1.0, amp * math.sin(2 * math.pi * freq * t))) * 32767)
    header = (b"RIFF" + struct.pack("<I", 36 + len(buf) * 2) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
                                      SAMPLE_RATE * 2, 2, 16)
              + b"data" + struct.pack("<I", len(buf) * 2))
    with open(path, "wb") as f:
        f.write(header + buf.tobytes())
    return path


# 有声 0-3800 / 静默 3800-4500 / 有声 4500-8000
BURSTS = ((0, 3800, 300, 0.5), (4500, 8000, 300, 0.5))


@pytest.fixture()
def track(tmp_path):
    return seams.load_energy(_write_wav(os.path.join(tmp_path, "a.wav"),
                                        total_ms=8000, bursts=BURSTS))


# ---------------------------------------------------------------- 阈值推导

def test_threshold_separates_this_tracks_own_loud_and_quiet(track):
    """判静线必须落在**这条音轨自己**的说话音量与静默之间。"""
    threshold = seams.silence_threshold(track)
    assert threshold is not None
    loud = seams.window_db(track, 1000, 3000)
    quiet = seams.window_db(track, 3900, 4400)
    assert quiet <= threshold, f"静默区 {quiet:.1f}dB 应低于判静线 {threshold:.1f}dB"
    assert loud > threshold, f"说话区 {loud:.1f}dB 应高于判静线 {threshold:.1f}dB"


def test_threshold_follows_a_higher_noise_floor(tmp_path):
    """底噪抬高一档，判静线必须跟着抬——否则底噪会被当成说话，静默全部落空。"""
    clean = seams.load_energy(_write_wav(os.path.join(tmp_path, "clean.wav"),
                                         total_ms=8000, bursts=BURSTS))
    noisy = seams.load_energy(_write_wav(os.path.join(tmp_path, "noisy.wav"),
                                         total_ms=8000, bursts=BURSTS, noise_amp=0.02))
    t_clean, t_noisy = seams.silence_threshold(clean), seams.silence_threshold(noisy)
    assert t_noisy > t_clean
    # 且对各自那条音轨都仍然成立：底噪区判静、说话区判动。
    assert seams.window_db(noisy, 3900, 4400) <= t_noisy
    assert seams.window_db(noisy, 1000, 3000) > t_noisy


def test_track_without_dynamic_range_reports_no_usable_silence(tmp_path):
    """全程等能量 = 分不出动静。**必须返回 None**，而不是硬套一个阈值继续判。

    硬套的后果是把说话判成静默，然后在正在说话的地方压黑——那会吞掉内容，
    比画面跳一下严重得多。
    """
    flat = seams.load_energy(_write_wav(os.path.join(tmp_path, "flat.wav"),
                                        total_ms=4000, bursts=((0, 4000, 300, 0.4),)))
    assert seams.silence_threshold(flat) is None
    decisions = seams.seam_decisions(flat, [2000], threshold_db=None)
    assert decisions[0]["fade_out_ms"] == 0 and decisions[0]["fade_in_ms"] == 0
    assert decisions[0]["softened"] is False


# ---------------------------------------------------------------- 接缝两侧独立

def test_seam_sides_are_judged_independently(track):
    """接缝前是上一段的尾巴、接缝后是下一段的开头，两者没有理由相同。"""
    threshold = seams.silence_threshold(track)
    # 4480：前面大段静默，后面 240ms 内已经开口说话（静默区止于 4500）。
    after_silence = seams.seam_decisions(track, [4480], threshold_db=threshold)[0]
    assert after_silence["fade_out_ms"] > 0
    assert after_silence["fade_in_ms"] == 0
    # 3800：正好是说话结束处——前面还在说话，后面才停。
    before_silence = seams.seam_decisions(track, [3800], threshold_db=threshold)[0]
    assert before_silence["fade_out_ms"] == 0
    assert before_silence["fade_in_ms"] > 0
    # 如果按"接缝居中"一刀切，上面两处会同时被判成不可用，白丢两个接缝。


def test_seam_in_the_middle_of_speech_is_left_alone(track):
    """正在说话的接缝：宁可硬切，也不能压黑。压黑会吞字，那是内容错误。"""
    threshold = seams.silence_threshold(track)
    d = seams.seam_decisions(track, [2000], threshold_db=threshold)[0]
    assert d["fade_out_ms"] == 0 and d["fade_in_ms"] == 0


# ---------------------------------------------------------------- 切点选择

def test_quiet_boundary_is_preferred_only_when_clearly_quieter(track):
    """回让切点要拿时长换，所以只有**明显**更静才值得。"""
    # 4100 两侧都在静默里；3850 前面还在说话；4400 后面已经开口。
    assert seams.pick_quiet_boundary([4400, 4100, 3850], energy=track) == 4100
    # 门槛抬到不可能达到：一律保留最晚那个，不做无谓牺牲。
    assert seams.pick_quiet_boundary([4400, 4100, 3850], energy=track,
                                     min_gain_db=200.0) == 4400
    # 两个候选都落在静默里、差别不到门槛：保留更晚的那个（不白丢时长）。
    assert seams.pick_quiet_boundary([4120, 4100], energy=track) == 4120


def test_no_energy_means_no_opinion(track):
    """拿不到能量分析就不该有主张：返回 None，让调用方保留默认切点。"""
    assert seams.pick_quiet_boundary([4400, 4100], energy=None) is None


# ---------------------------------------------------------------- 装箱

def _words_for(text: str, duration_ms: int):
    return make_words(text, duration_ms)


def test_packing_is_maximal_under_the_limit():
    """装箱的判据：任意相邻两段都**塞不进同一个上限**——否则就是没装满。

    漏掉装箱的代价是接缝数量：75 秒按句子切会切出一堆段，
    而每一个接缝的画面跳变都是供应商侧决定的、本地拼不掉的。
    """
    text = "".join(f"这是第{i + 1}个要点，需要讲清楚它的来由与边界。" for i in range(10))
    duration = 75000
    limit = 60000
    segs = sg.build_segments(text, _words_for(text, duration), duration, limit_ms=limit)
    assert len(segs) > 1, "75 秒必然要分多段"
    assert all(s["duration_ms"] < limit for s in segs)
    target = limit * sg.PACK_SAFETY
    for a, b in zip(segs, segs[1:]):
        assert a["duration_ms"] + b["duration_ms"] > target, \
            "相邻两段还能合并却没合并 = 装箱没装满"
    assert len(segs) <= math.ceil(duration / target)


def test_packing_keeps_coverage_and_contiguity():
    """装箱只决定装到哪个边界为止，不得漏字、重字或挪动切点来源。"""
    text = "".join(f"要点{i + 1}要说清楚它的边界和来由。" for i in range(12))
    duration = 130000
    segs = sg.build_segments(text, _words_for(text, duration), duration, limit_ms=60000)
    assert segs[0]["start_char"] == 0
    assert segs[-1]["end_char"] == len(text)
    for i, s in enumerate(segs):
        if i:
            assert s["start_ms"] == segs[i - 1]["end_ms"], "段间不得有缝隙或重叠"
            assert s["start_char"] == segs[i - 1]["end_char"]
        assert s["end_ms"] > s["start_ms"]


def test_short_audio_is_never_split():
    """够短就整轨一次调用：分段会凭空造出接缝，而接缝跳变是拼不掉的。"""
    text = "第一句话完整地说完。第二句话也完整地说完。第三句话同样完整。"
    duration = 30000
    segs = sg.build_segments(text, _words_for(text, duration), duration, limit_ms=60000)
    assert len(segs) == 1
    assert segs[0]["start_ms"] == 0 and segs[0]["end_ms"] == duration
    assert segs[0]["start_char"] == 0 and segs[0]["end_char"] == len(text)
    assert segs[0]["whole_track"] is True


def test_under_one_minute_goes_whole_even_when_close_to_the_limit():
    """不到 1 分钟就不切分：59 秒也必须整轨一次调用，不能为了保险切两段。"""
    text = "第一句话完整地说完。第二句话也完整地说完。"
    segs = sg.build_segments(text, _words_for(text, 59000), 59000, limit_ms=60000)
    assert len(segs) == 1 and segs[0]["end_ms"] == 59000
    # 反过来：够到上限就必须切，整轨发出去会得到"提交成功、查询 50215"。
    long_segs = sg.build_segments(text, _words_for(text, 61000), 61000, limit_ms=60000)
    assert len(long_segs) > 1


def test_short_audio_needs_no_alignment():
    """单段用不上任何切点，因此**不依赖对齐产物**——短素材不必先跑对齐。"""
    text = "这是一段很短的稿子，整轨一次就能生成。"
    segs = sg.build_segments(text, [], 12000, limit_ms=60000)
    assert len(segs) == 1 and segs[0]["end_ms"] == 12000


def test_long_audio_still_requires_real_timestamps():
    """反过来：要切就必须有真实字词时间戳，均分时长不能当切分依据。"""
    text = "".join(f"第{i + 1}个要点讲清楚。" for i in range(12))
    with pytest.raises(sg.TalkingHeadBlocked) as e:
        sg.build_segments(text, [], 120000, limit_ms=60000)
    assert e.value.code == "SEGMENTATION_REQUIRES_ALIGNMENT"


def test_packing_does_not_split_mid_sentence():
    """切点仍然只能来自句子边界：装箱不得为了凑时长切在句子中间。"""
    text = "第一句话完整地说完。第二句话也完整地说完。第三句话同样完整。"
    duration = 120000
    segs = sg.build_segments(text, _words_for(text, duration), duration, limit_ms=60000)
    joined = "".join(s["text"] for s in segs)
    assert joined == text
    for s in segs[:-1]:
        assert s["text"].rstrip()[-1] in sg.SENTENCE_ENDINGS, \
            "除最后一段外，每段必须以句读结尾"


# ---------------------------------------------------------------- 压黑不删帧

def test_fade_never_changes_the_trimmed_frame_count():
    """压黑是**观感**手段，绝不能动帧数：动一帧，后面全部画面就相对声音错位。"""
    plain = stitch._build_clip_filter(0, 250, 25.0, 1280, 720, 0, 0)
    faded = stitch._build_clip_filter(0, 250, 25.0, 1280, 720, 0, 0,
                                      fade_in_ms=240, fade_out_ms=120)
    assert "trim=start_frame=0:end_frame=250" in plain
    assert "trim=start_frame=0:end_frame=250" in faded
    assert "fade=t=in:st=0:d=0.240000" in faded
    # 10 秒素材，淡出 120ms 应当贴到 9.88s 起
    assert "fade=t=out:st=9.880000:d=0.120000" in faded


def test_concat_with_fades_keeps_exact_frame_count(tmp_path):
    """真跑一次 ffmpeg：加了压黑之后总帧数必须与不加时**完全一致**。"""
    from app.video import ffmpeg as vff

    fps, w, h, frames = 25.0, 320, 240, 100
    seg_paths = []
    for i in range(2):
        p = os.path.join(tmp_path, f"seg{i}.mp4")
        cp = vff.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                             f"testsrc=size={w}x{h}:rate=25:duration=4",
                             "-pix_fmt", "yuv420p", p], timeout=300)
        assert cp.returncode == 0
        seg_paths.append(p)

    def build(with_fade: bool):
        chains = [stitch._build_clip_filter(
            i, frames, fps, w, h, 0, 0,
            fade_in_ms=(240 if with_fade and i == 1 else 0),
            fade_out_ms=(120 if with_fade and i == 0 else 0))
            for i in range(2)]
        chains.append("[v0][v1]concat=n=2:v=1:a=0[vout]")
        out = os.path.join(tmp_path, f"out_{int(with_fade)}.mp4")
        cp = vff.run_ffmpeg(["-y", *sum([["-i", p] for p in seg_paths], []),
                             "-filter_complex", ";".join(chains), "-map", "[vout]",
                             "-c:v", "libx264", "-crf", "32", "-pix_fmt", "yuv420p",
                             "-r", "25", out], timeout=600)
        assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-800:]
        return out

    def frame_count(path: str) -> int:
        cp = vff._run([vff.ffprobe_bin(), "-v", "error", "-count_frames",
                       "-select_streams", "v:0", "-show_entries",
                       "stream=nb_read_frames", "-of", "csv=p=0", path], timeout=300)
        return int(cp.stdout.decode().strip())

    assert frame_count(build(False)) == frame_count(build(True)) == 200
