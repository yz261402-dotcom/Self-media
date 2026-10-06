"""T04 测试的纯构造工具（不依赖 pytest，可被任意测试模块直接导入）。

单独成文件而不是写在 conftest.py 里：`conftest` 这个模块名在
`tests/` 与 `tests/tts/` 下各有一份，跨目录 `from conftest import ...`
取到哪一份取决于 sys.path 顺序。供应商响应构造是本组测试的核心夹具，
不能靠导入顺序决定它长什么样。
"""
from __future__ import annotations

import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.tts import plan as tts_plan  # noqa: E402
from app.tts import voices as tts_voices  # noqa: E402

PRESET_VOICE = "voice_minimax_warm_bestie_v1"


def make_wav(duration_ms: int = 1000, sample_rate: int = 32000, channels: int = 1,
             bits: int = 16, truncate_bytes: int = 0) -> bytes:
    """构造一个合法的 WAV。

    `truncate_bytes>0` 时 header 声明长度大于实际字节数，用于验证
    「截断音频不得入库」。
    """
    block = bits // 8 * channels
    frames = int(round(sample_rate * duration_ms / 1000.0))
    data = b"\x00" * (frames * block)
    declared = len(data)
    if truncate_bytes:
        data = data[:-truncate_bytes]
    header = (b"RIFF" + struct.pack("<I", 36 + declared) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                      sample_rate * block, block, bits)
              + b"data" + struct.pack("<I", declared))
    return header + data


def wav_hex(**kwargs) -> str:
    return make_wav(**kwargs).hex()


def make_transport(*, status_code: int = 200, body=None, error=None, message="",
                   raw: bytes = b"") -> tuple:
    """返回 (transport, calls)。calls 记录每次请求，用于断言"没有第二次提交"。"""
    calls = []

    def transport(req):
        calls.append(req)
        return {"status_code": status_code, "body": body, "error": error,
                "message": message, "raw": raw}

    return transport, calls


def ok_body(*, duration_ms: int = 1000, sample_rate: int = 32000, channels: int = 1,
            words=None, usage: int = 9, audio: str = None, truncate_bytes: int = 0,
            declared_ms: int = None) -> dict:
    """MiniMax 成功响应形状：hex 音频 + extra_info 元数据（可选字词时间戳）。"""
    payload = audio if audio is not None else wav_hex(
        duration_ms=duration_ms, sample_rate=sample_rate, channels=channels,
        truncate_bytes=truncate_bytes)
    return {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "data": dict({"audio": payload, "status": 2},
                     **({"subtitles": words} if words is not None else {})),
        "extra_info": {"audio_length": duration_ms if declared_ms is None else declared_ms,
                       "audio_sample_rate": sample_rate,
                       "audio_channel": channels, "audio_format": "wav",
                       "usage_characters": usage},
        "trace_id": "trace_test_001",
    }


def new_work(*, voice_id: str = PRESET_VOICE, identity_id: str = "av_base_b_v1") -> dict:
    """一个独立的、已采用稿件且已选音色的作品。

    每个用例一个作品：T04 的用例会往里加计划、音频、对齐记录，
    共用同一个作品会让上一个用例留下的记录静默影响下一个用例。
    """
    from app.mockdata import make_candidates

    wid = st.new_id("wtts")
    w = {"id": wid, "name": "T04 测试作品",
         "brief": {"topic": "RAG 科普", "content_type": "AI 科普",
                   "audience": "年轻受众", "keywords": [], "target_duration_ms": 60000},
         "identity_id": identity_id, "voice_asset_id": voice_id,
         "script_version_id": None, "route": "M2", "current_task_id": None, "version": 1}
    st.put("works", w["id"], w)
    batch = {"id": st.new_id("b"), "work_id": wid, "source_kind": "mock",
             "candidates": [c.model_dump() for c in make_candidates("RAG 科普")]}
    st.put("script_batches", wid, batch)
    w["script_version_id"] = batch["candidates"][0]["id"]
    st.put("works", wid, w)
    return w


def script_text_of(work: dict) -> str:
    batch = st.get("script_batches", work["id"]) or {}
    cand = next(c for c in batch["candidates"] if c["id"] == work["script_version_id"])
    return cand["text"]


def preset_voice() -> dict:
    v = tts_voices.get_voice(PRESET_VOICE)
    assert v, "预设音色应已由 storage._seed 建立"
    return v


def frozen_plan(work: dict) -> dict:
    return tts_plan.ensure_plan(work["id"])


def approved_alt_voice(voice_id: str = "voice_alt_v1") -> dict:
    """登记并批准一个"备选音色"，用于验证换声路径。

    走完整登记链（权属 + 来源 + 基准证据）而不是直接写库：换声必须真的能被
    `on_voice_change` 接受，测试才证明得了"换声会失效下游"。
    """
    from app.avatar import licensing
    from app.tts import sources as src

    record = licensing.register(subject=voice_id, kind="voice_source",
                                scope="unverified", evidence="待核验")
    source = src.register_source(subject=voice_id, source_type="preset_voice_selection",
                                 provider="minimax", model=tts_voices.MINIMAX_TTS_MODEL,
                                 version="v1")
    tts_voices.register_voice(
        voice_id=voice_id, name="备选音色", provider="minimax",
        model=tts_voices.MINIMAX_TTS_MODEL,
        remote_voice_id="Chinese (Mandarin)_Alt_Warm",
        sample_text="同一短句。", source_record_id=source["id"],
        license_id=record["id"], baseline_audio_asset_id="audio_baseline_x",
        baseline_audio_hash="sha256:" + "a" * 64)
    return tts_voices.approve_voice(voice_id, reviewer="user")


def allow_billable(monkeypatch, *, amount_fen: int = 0, limit_fen: int = 100000,
                   submissions: int = 9999) -> None:
    """把预算策略放宽到"允许可计费提交"。

    默认策略是全 0（未授权不得产生计费副作用），因此要验证"请求发出后
    响应如何被解析"，必须先显式授权——而不是让测试顺手改默认值。
    """
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", str(submissions))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", str(limit_fen))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", str(limit_fen))
