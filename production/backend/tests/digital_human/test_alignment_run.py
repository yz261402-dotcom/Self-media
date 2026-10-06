"""约 60 秒以上的音频走 OmniHuman 必须分段，而分段必须基于真实字词时间戳。

规格 T07 说 Postprocess Worker **可在音频确认后并行对齐字幕**；实现里后期阶段被
"必须先有已审原片"整体挡住，于是长音频会卡进死循环。这里验证补上的那条通路：
音频确认后即可跑真实对齐 → 登记对齐产物 → 分段计划可建立。

对齐本身用注入的替身适配器（真实适配器要占用算力云 GPU），
但登记、时间戳校验与分段全部走真实代码路径。

另有一项断言值得单独说：对齐必须把**已登记音频的 storage_uri + file_hash** 交给适配器，
而不是先把音频发到公开对象存储再给 URL。公开存储那条路要额外一套凭据，
缺了它整条字幕链路会直接断掉（实测报 TOS SignatureDoesNotMatch）。
"""
import os

import pytest
from fastapi.testclient import TestClient

from app import alignment, main, storage as st
from app.digital_human import segments as dh_segments
from app.digital_human.errors import TalkingHeadBlocked
from app.tts import audio as tts_audio
from app.tts import gate as tts_gate
from app.video import asr as v_asr
from dh_helpers import prepare_work

PUNCT = "，。！？、；：,.?!;:"

# 每句都足够长（>40 字），这样按句边界切分时每句都能真正产生一个切点。
TEXT = (
    "第一句话需要足够长，这样按句子边界切分的时候才会真正产生一个切点，而不是被合并进相邻的句子。"
    "第二句话同样保持这个长度，用来保证每一段的时长都严格小于单次调用的上限，不需要加速也不需要截字。"
    "第三句话继续维持长度，让分段计划里至少存在两段，并且相邻两段的全局偏移彼此连续、不留空隙。"
    "第四句话用来收尾，确保全文都被对齐时间轴覆盖，不留下任何没有被覆盖到的字符位置。"
)


def _units_for(body: str, duration_ms: int):
    """按正文逐字造一份字级 unit：`text + tail` 拼接必须等于正文（去空白）。"""
    units = []
    prefix = ""
    step = max(2, duration_ms // (len(body) + 2))
    t = 0
    for ch in body:
        if ch.isspace():
            continue
        if ch in PUNCT:
            if units:
                units[-1]["tail"] += ch
            else:
                prefix += ch
            continue
        start = t
        t = start + step
        units.append({"text": prefix + ch, "tail": "", "start_ms": start, "end_ms": t})
        prefix = ""
    return units


class _FakeAsr:
    """替身适配器：只替换"远端模型"这一层，其余全是真实代码。"""

    provider_id = "qwen3_cloud"
    alignment_source = "forced_alignment"
    simulated = False

    def __init__(self, body: str, duration_ms: int):
        self.body = body
        self.duration_ms = duration_ms
        self.calls = 0
        self.last_transcript = ""
        self.last_audio_ref = None

    def align_units(self, audio_path: str, script_text: str, audio_ref=None):
        assert script_text == self.body, "对齐必须使用冻结计划正文，否则字符位置对不上"
        self.calls += 1
        self.last_audio_ref = audio_ref
        return {"units": _units_for(self.body, self.duration_ms),
                "alignment_text": self.body}


def _prepared(tmp_path, monkeypatch, *, audio_ms: int = 61000, text: str = TEXT):
    work = prepare_work(route="OMNIHUMAN_1_5", audio_ms=audio_ms, text=text)
    body = dh_segments.frozen_body(work)
    # 本目录的 tmp_path 夹具给的是**字符串**（见 tests/digital_human/conftest.py）
    audio_file = os.path.join(str(tmp_path), "final.wav")
    with open(audio_file, "wb") as fh:
        fh.write(b"RIFF-fake")  # 替身适配器不读字节
    monkeypatch.setattr(tts_audio, "runtime_path", lambda uri: audio_file)
    adapter = _FakeAsr(body, audio_ms)
    monkeypatch.setattr(v_asr, "require_adapter", lambda: adapter)
    return work, body, adapter


# ---------- units_to_words：字符位置映射 ----------

def test_units_to_words_maps_to_frozen_body_char_positions():
    script = "你好，世界。再见。"
    units = [
        {"text": "你", "tail": "", "start_ms": 0, "end_ms": 300},
        {"text": "好", "tail": "，", "start_ms": 300, "end_ms": 600},
        {"text": "世", "tail": "", "start_ms": 700, "end_ms": 1000},
        {"text": "界", "tail": "。", "start_ms": 1000, "end_ms": 1300},
        {"text": "再", "tail": "", "start_ms": 1500, "end_ms": 1800},
        {"text": "见", "tail": "。", "start_ms": 1800, "end_ms": 2100},
    ]
    words = v_asr.units_to_words(units, script, script)
    assert [(w["start_char"], w["end_char"]) for w in words] == [
        (0, 1), (1, 2), (3, 4), (4, 5), (6, 7), (7, 8)]
    assert [(w["start_ms"], w["end_ms"]) for w in words][0] == (0, 300)


def test_units_to_words_skips_whitespace_when_mapping_back():
    script = "你好，\n世界。"
    units = [
        {"text": "你", "tail": "", "start_ms": 0, "end_ms": 100},
        {"text": "好", "tail": "，", "start_ms": 100, "end_ms": 200},
        {"text": "世", "tail": "", "start_ms": 300, "end_ms": 400},
        {"text": "界", "tail": "。", "start_ms": 400, "end_ms": 500},
    ]
    words = v_asr.units_to_words(units, script, script)
    assert [w["start_char"] for w in words] == [0, 1, 4, 5], "换行不得被算进字符位置"


def test_units_to_words_rejects_alignment_of_a_different_text():
    with pytest.raises(v_asr.VideoBlocked) as error:
        v_asr.units_to_words([{"text": "你", "tail": "", "start_ms": 0, "end_ms": 100}],
                             "你", "他")
    assert error.value.code == "ALIGN_TEXT_MISMATCH"


def test_units_to_words_rejects_incomplete_coverage():
    with pytest.raises(v_asr.VideoBlocked) as error:
        v_asr.units_to_words([{"text": "你", "tail": "", "start_ms": 0, "end_ms": 100}],
                             "你好", "你好")
    assert error.value.code == "ALIGN_COVERAGE_INCOMPLETE"


# ---------- run_for_work：从"不可分段"到"可分段" ----------

def test_alignment_unlocks_segmentation_for_long_audio(tmp_path, monkeypatch):
    work, body, adapter = _prepared(tmp_path, monkeypatch)

    with pytest.raises(TalkingHeadBlocked) as blocked:
        dh_segments.create_plan(work["id"])
    assert blocked.value.code == "SEGMENTATION_REQUIRES_ALIGNMENT"

    out = alignment.run_for_work(work["id"])
    assert out["ok"] is True and out["reused"] is False
    assert out["word_count"] > 0
    assert out["source"] == "forced_alignment"
    assert adapter.calls == 1
    # 音频按登记身份直传给 worker：storage_uri 供路径白名单判定，
    # file_hash 是版本身份（worker 会再核一次字节，对不上就拒收）。
    # 路径白名单本身（audio/ 前缀、runtime/audio 目录）由 test_m2_registered_inputs 覆盖。
    audio_row = tts_audio.final_audio(work["id"])
    assert adapter.last_audio_ref == (audio_row["storage_uri"], audio_row["file_hash"])
    assert audio_row["file_hash"].startswith("sha256:")

    status = tts_gate.alignment_status(work["id"])
    assert status["aligned"] is True
    audio = tts_audio.final_audio(work["id"])
    assert st.get("audios", audio["id"])["timing_source"] == "forced_alignment"

    plan = dh_segments.create_plan(work["id"])
    assert plan["segments"], "对齐登记后必须能建立分段计划"
    assert plan["summary"]["speedup_applied"] is False
    assert plan["summary"]["truncation_applied"] is False
    limit = plan["summary"]["limit_ms"]
    assert all(s["duration_ms"] < limit for s in plan["segments"])
    assert plan["segments"][0]["start_char"] == 0
    assert plan["segments"][-1]["end_char"] == len(body)


def test_alignment_is_reused_without_calling_the_model_twice(tmp_path, monkeypatch):
    work, _body, adapter = _prepared(tmp_path, monkeypatch)
    first = alignment.run_for_work(work["id"])
    second = alignment.run_for_work(work["id"])
    assert first["alignment_id"] == second["alignment_id"]
    assert second["reused"] is True
    assert adapter.calls == 1, "已有可追溯的对齐产物时不得重复调用远端模型"


def test_alignment_requires_confirmed_final_audio(tmp_path, monkeypatch):
    work, _body, _adapter = _prepared(tmp_path, monkeypatch)
    audio = tts_audio.final_audio(work["id"])
    audio["confirmed"] = False
    audio["approved"] = False
    st.put("audios", audio["id"], audio, {"work_id": work["id"]})
    with pytest.raises(TalkingHeadBlocked) as blocked:
        alignment.run_for_work(work["id"])
    assert blocked.value.code == "FINAL_AUDIO_REQUIRED"


# ---------- HTTP 入口 ----------

def test_alignment_run_endpoint_reports_status_and_errors(tmp_path, monkeypatch):
    work, _body, adapter = _prepared(tmp_path, monkeypatch)
    client = TestClient(main.app)

    response = client.post(f"/api/v1/works/{work['id']}/alignment/run")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True and payload["reused"] is False
    assert adapter.calls == 1

    # OmniHuman 分段入口已退休；对齐仍可独立完成并供字幕链路使用。
    response = client.post(f"/api/v1/works/{work['id']}/talking-head/segments",
                           json={})
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "OMNIHUMAN_SEGMENT_SUBMISSION_RETIRED"


def test_alignment_run_endpoint_maps_blocked_to_409(tmp_path, monkeypatch):
    work, _body, _adapter = _prepared(tmp_path, monkeypatch)

    def blocked(*_args, **_kwargs):
        raise v_asr.VideoBlocked("ASR_NOT_CONFIGURED", "未配置 ASR worker")

    monkeypatch.setattr(v_asr, "require_adapter", blocked)
    response = TestClient(main.app).post(f"/api/v1/works/{work['id']}/alignment/run")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASR_NOT_CONFIGURED"
