"""T07 统一时间线与 Worker 门禁（AC-MEDIA-04 处理逻辑）。

覆盖：裁剪、片头偏移、分段拼接、字幕越界、正文漏字、音频变更后旧时序
不沿用、依赖未通过不得执行、重复调度不重复产物登记、Worker 失败/恢复。
"""
from __future__ import annotations

import os
import threading

import pytest

from app import storage as st
from app import postprocess_worker as pw
from app.agents import supervisor
from app.video import asr as v_asr
from app.video import subtitles as v_sub
from app.video import timeline as v_tl
from app.video.errors import VideoBlocked, VideoRejected

SCRIPT = "大家好，今天聊聊效率工具。第一，先把事情排进日程。第二，用对工具省时间。"


@pytest.fixture(autouse=True)
def isolate_worker_files_and_records(monkeypatch, tmp_path):
    monkeypatch.setattr(pw, "_runtime_root", lambda: str(tmp_path / "runtime"))
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setattr(st, "_local", threading.local())
    st.init_db()


# ---------- 时间线：单段 / 裁剪 / 片头偏移 ----------

def test_single_timeline_identity_mapping():
    tl = v_tl.EditTimeline.single(10000)
    tl.validate()
    assert tl.final_duration_ms == 10000
    assert tl.map_source_to_final(0) == 0
    assert tl.map_source_to_final(9999) == 9999


def test_head_offset_and_tail_trim_shift_map():
    tl = v_tl.EditTimeline.single(10000, head_offset_ms=500, tail_trim_ms=200)
    tl.validate()
    assert tl.final_duration_ms == 9300
    assert tl.map_source_to_final(500) == 0      # 片头被裁掉
    assert tl.map_source_to_final(1000) == 500   # 映射整体左移
    with pytest.raises(VideoRejected):
        tl.map_source_to_final(100)              # 裁剪区外必须拒绝而非错位


def test_trim_exceeding_source_rejected():
    with pytest.raises(VideoRejected):
        v_tl.EditTimeline.single(1000, head_offset_ms=800, tail_trim_ms=300)


# ---------- 分段拼接 ----------

def test_concat_segments_are_contiguous_and_monotonic():
    tl = v_tl.EditTimeline.concat([3000, 2500, 1500])
    tl.validate()
    assert tl.final_duration_ms == 7000
    assert tl.map_source_to_final(2999) == 2999
    assert tl.map_source_to_final(3000) == 3000   # 第 2 段起点
    assert tl.map_source_to_final(5500) == 5500
    with pytest.raises(VideoRejected):
        v_tl.EditTimeline.concat([3000, 0, 1500])
    with pytest.raises(VideoRejected):
        v_tl.EditTimeline.concat([])


# ---------- 字幕映射与硬检查 ----------

def _cue(start, end, text):
    return {"start_ms": start, "end_ms": end, "text": text}


def test_map_cues_follows_timeline_not_source_time():
    tl = v_tl.EditTimeline.single(10000, head_offset_ms=500)
    cues = v_sub.map_cues([_cue(500, 2000, "大家好"), _cue(2000, 4000, "今天聊聊")], tl)
    assert cues[0].start_ms == 0 and cues[0].end_ms == 1500
    assert cues[1].start_ms == 1500


def test_check_cues_rejects_missing_script_chars():
    tl = v_tl.EditTimeline.single(6000)
    cues = v_sub.map_cues([_cue(0, 3000, "大家好，今天聊聊效率工具")], tl)  # 漏了后两句
    with pytest.raises(VideoRejected) as e:
        v_sub.check_cues(cues, final_duration_ms=tl.final_duration_ms, script_text=SCRIPT)
    assert e.value.code == "SCRIPT_COVERAGE_MISMATCH"


def test_check_cues_rejects_out_of_range_and_last_cue_truncation():
    tl = v_tl.EditTimeline.single(6000)
    full = "大家好，今天聊聊效率工具。第一，先把事情排进日程。第二，用对工具省时间。"
    with pytest.raises(VideoRejected) as e:  # 源端越界：映射层即拒（宁可拒不可错位）
        v_sub.map_cues([_cue(0, 6100, full)], tl)
    assert e.value.code == "TIME_OUT_OF_SOURCE"
    cues2 = v_sub.map_cues([_cue(0, 6000, full)], tl)  # 末句贴死终点=截断
    with pytest.raises(VideoRejected) as e2:
        v_sub.check_cues(cues2, final_duration_ms=tl.final_duration_ms, script_text=SCRIPT)
    assert e2.value.code == "LAST_CUE_TRUNCATED"


def test_check_cues_rejects_overlap_and_disorder():
    tl = v_tl.EditTimeline.single(9000)
    # 人为构造终轴重叠：直接绕过 map_cues 排序
    from app.video.subtitles import Cue
    c1 = Cue(0, 0, 2000, "大家好，今天聊聊效率工具。")
    c2 = Cue(1, 1000, 3000, "第一，先把事情排进日程。")
    with pytest.raises(VideoRejected) as e:
        v_sub.check_cues([c1, c2], final_duration_ms=9000, script_text=SCRIPT)
    assert e.value.code == "CUE_OVERLAP"
    with pytest.raises(VideoRejected) as e2:
        v_sub.check_cues([c2, c1], final_duration_ms=9000, script_text=SCRIPT)
    assert e2.value.code in ("CUE_OVERLAP", "CUE_ORDER")


def test_ass_wraps_long_lines_but_rejects_over_limit():
    tl = v_tl.EditTimeline.single(4000)
    text28 = "一二三四五六七八九" * 2 + "一二三四五六七八九十"  # 28 字
    cues = v_sub.map_cues([_cue(0, 3000, text28)], tl)
    ass = v_sub.build_ass(cues)
    assert "\\N" in ass and "Dialogue: 0,0:00:00.00,0:00:03.00" in ass
    text40 = "字" * 40  # 3 行，超 2 行上限
    cues2 = v_sub.map_cues([_cue(0, 3000, text40)], tl)
    with pytest.raises(VideoRejected) as e:
        v_sub.build_ass(cues2)
    assert e.value.code == "CUE_TOO_LONG"


# ---------- ASR 能力位：未配置即 blocked，绝不假设能力 ----------

def test_require_adapter_blocks_when_unconfigured(monkeypatch):
    monkeypatch.delenv(v_asr._ENV_PROVIDER, raising=False)
    with pytest.raises(VideoBlocked) as e:
        v_asr.require_adapter()
    assert e.value.code == "ASR_NOT_CONFIGURED"


def test_unknown_provider_is_explicit_error(monkeypatch):
    monkeypatch.setenv(v_asr._ENV_PROVIDER, "ghost_asr")
    with pytest.raises(VideoBlocked) as e:
        v_asr.require_adapter()
    assert e.value.code == "ASR_PROVIDER_UNKNOWN"


def test_uniform_duration_is_not_accepted_as_alignment(monkeypatch):
    """对齐来源白名单：均分时长不算对齐（T04 结论在 T07 同样生效）。"""
    class BadAsr:
        provider_id = "uniform"
        alignment_source = "uniform_split"
        simulated = False
        def transcribe(self, audio_path, script_text):
            return []
    monkeypatch.setenv(v_asr._ENV_PROVIDER, "uniform")
    monkeypatch.setitem(v_asr.KNOWN_PROVIDERS, "uniform",
                        {"adapter": lambda: BadAsr()})
    with pytest.raises(VideoBlocked) as e:
        v_asr.require_adapter()
    assert e.value.code == "ASR_ALIGNMENT_SOURCE_INVALID"


# ---------- Worker 门禁 / 幂等 / 恢复 / 音频变更 ----------

def _seed_production(work_id="w_pp", task_id="t_pp", mode="production"):
    work = {"id": work_id, "name": "pp", "brief": {"topic": "t"},
            "identity_id": "av_base_b_v1", "voice_asset_id": "voice_v1",
            "script_version_id": "script_1", "route": "OMNIHUMAN_1_5",
            "final_audio_id": "audio_fx", "video_asset_id": "video_fx"}
    task = {"id": task_id, "work_id": work_id, "mode": mode, "stage": "postprocess",
            "state": "queued", "route": "OMNIHUMAN_1_5", "route_confirmed": True,
            "pending_selection_ids": []}
    st.put("works", work_id, work)
    st.put("tasks", task_id, task, {"work_id": work_id})
    st.put("script_batches", work_id, {
        "candidates": [{"id": "script_1", "title": "T", "text": SCRIPT}]})
    return task, work


def test_worker_dependency_gate_blocks_without_execution(monkeypatch):
    task, work = _seed_production()
    monkeypatch.setattr(supervisor, "build_model",
                        lambda *a, **k: pytest.fail("Worker invoked an Agent"))
    rec = postprocess_record(task, work)
    assert rec["state"] == "blocked"
    assert rec["error"]["code"] == "DEPENDENCY_NOT_MET"
    assert rec["output_files"] == []


def postprocess_record(task, work):
    from app import postprocess_worker as pw
    return pw.run_stage(task, work, "postprocess")


def test_worker_idempotent_same_inputs_single_record(monkeypatch):
    task, work = _seed_production()
    r1 = postprocess_record(task, work)
    r2 = postprocess_record(task, work)
    assert r1["id"] == r2["id"]
    records = [r for r in st.all_rows("media_jobs", "work_id=?", [task["work_id"]])
               if r.get("executor_kind") == "worker"]
    assert len(records) == 1


def _register_fixture_asr(monkeypatch):
    class FixtureAsr:
        provider_id = "fixture"
        alignment_source = "asr_alignment"
        simulated = True
        def transcribe(self, audio_path, script_text):
            # 测试夹具：按标点切句、按字数比例分配时间（仅证明处理逻辑，simulated=True）
            import re as _re
            parts = [p for p in _re.split(r"(?<=[。！？；])", script_text) if p.strip()]
            total = len(script_text)
            cues, cursor = [], 0
            for i, p in enumerate(parts):
                span = int(round(len(p) / total * 5000))
                cues.append({"start_ms": cursor, "end_ms": cursor + span - 300,
                             "text": p})
                cursor += span
            cues[-1]["end_ms"] = 4700  # 不贴死终点
            return cues
    monkeypatch.setenv(v_asr._ENV_PROVIDER, "fixture")
    monkeypatch.setitem(v_asr.KNOWN_PROVIDERS, "fixture",
                        {"adapter": lambda: FixtureAsr()})
    # 本地渲染组件测试显式替换能力门禁；生产 require_adapter 另有拒绝 simulated 的测试。
    monkeypatch.setattr(v_asr, "require_adapter", lambda: FixtureAsr())


def _add_real_media(task, tmp_media):
    """合成夹具与审核记录只进入当前测试的临时 DB/runtime。"""
    video_path, audio_path = tmp_media
    st.put("audios", "audio_fx", {
        "id": "audio_fx", "work_id": task["work_id"], "kind": "full",
        "approved": True, "confirmed": True, "voice_asset_id": "voice_v1",
        "script_version_id": "script_1", "file_hash": "sha256:" + pw._sha256_file(audio_path),
        "storage_uri": f"audio/{task['work_id']}/audio_fx.wav",
        "duration_ms": 6000}, {"work_id": task["work_id"]})
    dst_audio = os.path.join(pw._runtime_root(), "audio", task["work_id"])
    os.makedirs(dst_audio, exist_ok=True)
    import shutil
    shutil.copy(audio_path, os.path.join(dst_audio, "audio_fx.wav"))
    st.put("videos", "video_fx", {
        "id": "video_fx", "work_id": task["work_id"], "kind": "original",
        "is_mock": False, "storage_uri": f"video/{task['work_id']}/video_fx.mp4",
        "duration_ms": 6000, "file_hash": "sha256:" + pw._sha256_file(video_path),
        "audio_asset_id": "audio_fx", "identity_id": "av_base_b_v1", "route": "OMNIHUMAN_1_5",
        "confirmed": True, "approval_state": "approved", "has_video_track": True},
        {"work_id": task["work_id"]})
    st.put("video_reviews", "vr_fixture", {"id": "vr_fixture", "video_id": "video_fx",
        "video_hash": "sha256:" + pw._sha256_file(video_path), "outcome": "pass",
        "dimensions": {d: "pass" for d in ("lip_sync", "identity", "body_motion", "expression")}},
        {"video_id": "video_fx"})
    st.put("audio_track_checks", "atc_fixture", {"id": "atc_fixture", "video_id": "video_fx",
        "video_hash": "sha256:" + pw._sha256_file(video_path), "audio_asset_id": "audio_fx",
        "outcome": "pass", "measured_offset_ms": 0}, {"video_id": "video_fx"})
    dst_video = os.path.join(pw._runtime_root(), "video", task["work_id"])
    os.makedirs(dst_video, exist_ok=True)
    shutil.copy(video_path, os.path.join(dst_video, "video_fx.mp4"))


@pytest.fixture(scope="module")
def tmp_media(tmp_path_factory):
    """ffmpeg 合成夹具：6s 270x480 测试原片 + 6s 正弦 wav，成片验收为 1080x1920。

    合成夹具只证明处理逻辑（specs 05-T07 退出条款）。
    """
    from app.video import ffmpeg as v_ff
    try:
        v_ff.ffmpeg_bin()
    except VideoBlocked:
        pytest.skip("本机未找到 ffmpeg，跳过媒体夹具用例")
    d = tmp_path_factory.mktemp("media")
    video = str(d / "src.mp4")
    audio = str(d / "src.wav")
    cp = v_ff.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                          "testsrc2=size=270x480:rate=30:duration=6",
                          "-c:v", "libx264", "-threads:v", "2", "-pix_fmt", "yuv420p", video],
                         timeout=180)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-400:]
    cp = v_ff.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                          "sine=frequency=440:duration=6",
                          "-ar", "32000", "-ac", "1", audio], timeout=120)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[-400:]
    return video, audio


def test_worker_audio_change_never_reuses_old_timeline(monkeypatch, tmp_media):
    """音频变更 → 旧确认音频失效 + 新音频登记 → 新执行记录、重新对齐；旧记录保留。"""
    task, work = _seed_production()
    _register_fixture_asr(monkeypatch)
    _add_real_media(task, tmp_media)
    rec1 = postprocess_record(task, work)
    assert rec1["state"] == "succeeded", rec1.get("error")
    assert rec1["asr"]["reused"] is False
    # 音频变更：旧音频不再确认，新音频成为唯一确认全文音频（同文件、不同 id）
    old = st.get("audios", "audio_fx")
    st.put("audios", "audio_fx", {**old, "confirmed": False},
           {"work_id": task["work_id"]})
    st.put("audios", "audio_fx2", {
        "id": "audio_fx2", "work_id": task["work_id"], "kind": "full",
        "approved": True, "confirmed": True, "voice_asset_id": "voice_v1",
        "script_version_id": "script_1", "file_hash": old["file_hash"],
        "storage_uri": f"audio/{task['work_id']}/audio_fx.wav",
        "duration_ms": 6000}, {"work_id": task["work_id"]})
    work["final_audio_id"] = "audio_fx2"
    stale = postprocess_record(task, work)
    assert stale["state"] == "blocked", "换音频后旧原片不得继续后期"
    video = st.get("videos", "video_fx")
    video["audio_asset_id"] = "audio_fx2"
    st.put("videos", video["id"], video, {"work_id": task["work_id"]})
    track = st.get("audio_track_checks", "atc_fixture")
    track["audio_asset_id"] = "audio_fx2"
    st.put("audio_track_checks", track["id"], track, {"video_id": "video_fx"})
    rec2 = postprocess_record(task, work)
    assert rec2["state"] == "succeeded", rec2.get("error")
    assert rec2["id"] != rec1["id"]
    assert rec2["asr"]["audio_id"] == "audio_fx2"
    assert rec2["asr"]["reused"] is False   # 旧时序绝不沿用
    assert rec1["asr"]["audio_id"] == "audio_fx"  # 旧记录证据保留
    assert rec1["output_files"][0]["storage_uri"] != rec2["output_files"][0]["storage_uri"]
    for record in (rec1, rec2):
        assert {f["kind"] for f in record["output_files"]} == {"master", "subtitles_ass", "subtitles_srt", "cover"}
        for output in record["output_files"]:
            assert pw._sha256_file(pw._runtime_path(output["storage_uri"])) == output["sha256"]
    # audio_id 是独立列（storage.py:97），按生产代码同口径用过滤查询断言
    for aid in ("audio_fx", "audio_fx2"):
        assert st.all_rows("alignment_records", "audio_id=?", [aid]), \
            f"音频 {aid} 应留下独立的对齐记录"


def test_worker_resume_recovers_when_capability_arrives(monkeypatch, tmp_media):
    """Worker 失败/恢复：缺 ASR 时 blocked；配置到位（新摘要）→ 新记录执行成功。"""
    task, work = _seed_production()
    _add_real_media(task, tmp_media)
    rec_blocked = postprocess_record(task, work)
    assert rec_blocked["state"] == "blocked"
    assert rec_blocked["error"]["code"] == "ASR_NOT_CONFIGURED"
    _register_fixture_asr(monkeypatch)   # 能力到位 → 快照摘要变化
    rec_ok = postprocess_record(task, work)
    assert rec_ok["state"] == "succeeded", rec_ok.get("error")
    assert rec_ok["id"] != rec_blocked["id"]
    # 失败原件保留，不得被成功记录覆盖
    assert st.get("media_jobs", rec_blocked["id"])["state"] == "blocked"
