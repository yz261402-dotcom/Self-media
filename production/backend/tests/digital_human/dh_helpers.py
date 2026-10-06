"""T05 测试的纯构造工具（不依赖 pytest，可被任意测试模块直接导入）。

与 T04 同一理由单独成文件：`conftest` 这个模块名在 `tests/` 与
`tests/digital_human/` 下各有一份，跨目录导入取到哪一份取决于 sys.path 顺序。
而这里承载的是"供应商响应长什么样""MP4 长什么样"这类关键定义，不能靠导入顺序决定。
"""
from __future__ import annotations

import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import licensing, registry  # noqa: E402
from app.avatar import motion as avatar_motion  # noqa: E402
from app.contracts import now_iso  # noqa: E402
from app.tts import audio as tts_audio  # noqa: E402
from app.tts import gate as tts_gate  # noqa: E402
from app.tts import voices as tts_voices  # noqa: E402

PRESET_VOICE = "voice_minimax_warm_bestie_v1"
# T05 夹具专用音色。**不复用预设音色**：预设音色"权属未核验 → 禁止生产"是 T04 的
# 验收事实（`tests/tts/test_voice_scope.py` 会断言阻塞项恰好只有"权属未核验"一条），
# 而测试会话共用一个 SQLite 库——把预设的 license_id 换成 commercial_ok 等于
# 为了让 T05 通过而改掉 T04 依赖的事实。夹具必须自建音色。
FIXTURE_VOICE = "voice_dh_fixture_t05_v1"
FIXTURE_VOICE_REMOTE = "Fixture_T05_Voice"
FIXTURE_SAMPLE_TEXT = "这是一条仅用于测试的基准短句。"
PRESET_IDENTITY = "av_base_b_v1"

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


# ---------- MP4 构造 ----------

def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + btype + payload


def _hdlr(handler_type: bytes) -> bytes:
    return _box(b"hdlr", struct.pack(">B3s", 0, b"\0\0\0") + struct.pack(">I", 0)
                + handler_type + b"\0" * 12 + b"Handler\0")


def _tkhd(track_id: int, duration: int, width: int = 0, height: int = 0) -> bytes:
    body = (struct.pack(">B3s", 0, b"\0\0\0")
            + struct.pack(">IIIII", 0, 0, track_id, 0, duration)
            + b"\0" * 8
            + struct.pack(">hhhh", 0, 0, 0x0100, 0)
            + b"\0" * 36
            + struct.pack(">II", width << 16, height << 16))
    return _box(b"tkhd", body)


def _mvhd(timescale: int, duration: int) -> bytes:
    body = (struct.pack(">B3s", 0, b"\0\0\0")
            + struct.pack(">IIII", 0, 0, timescale, duration)
            + struct.pack(">I", 0x00010000)
            + struct.pack(">H", 0x0100)
            + b"\0" * 10 + b"\0" * 36 + b"\0" * 24
            + struct.pack(">I", 3))
    return _box(b"mvhd", body)


def make_mp4(duration_ms: int = 1000, *, width: int = 704, height: int = 1248,
             video: bool = True, audio: bool = True, brand: bytes = b"isom",
             with_moov: bool = True, truncate_bytes: int = 0,
             mdat_bytes: int = 512) -> bytes:
    """构造一个结构合法的最小 MP4，用于验证容器解析与假成功拦截。"""
    timescale = 1000
    ftyp = _box(b"ftyp", brand + struct.pack(">I", 512) + brand + b"mp41")
    traks = b""
    if video:
        traks += _box(b"trak", _tkhd(1, int(duration_ms), width, height)
                      + _box(b"mdia", _hdlr(b"vide")))
    if audio:
        traks += _box(b"trak", _tkhd(2, int(duration_ms))
                      + _box(b"mdia", _hdlr(b"soun")))
    moov = _box(b"moov", _mvhd(timescale, int(duration_ms)) + traks)
    data = ftyp + (moov if with_moov else b"") + _box(b"mdat", b"\0" * mdat_bytes)
    if truncate_bytes:
        data = data[:-truncate_bytes]
    return data


def html_error_page() -> bytes:
    """网关/登录页的典型形态：HTTP 200 + HTML 正文。"""
    return (b"<!DOCTYPE html>\n<html><head><title>404</title></head>"
            b"<body>Not Found</body></html>")


# ---------- 传输层替身 ----------

def make_transport(*, responses=None, default=None) -> tuple:
    """返回 (transport, calls)。

    - `responses` 是响应队列，按顺序取；
    - 队列空了用 `default`（可为 dict 或 callable）；
    - `calls` 记录每次请求，用于断言"没有第二次提交""签名 header 长什么样"。
    """
    calls = []
    queue = list(responses or [])

    def transport(req):
        calls.append(req)
        r = queue.pop(0) if queue else default
        if callable(r):
            r = r(req)
        return r if r is not None else {"status_code": 200, "json": {}}

    return transport, calls


def volc_submit_ok(task_id: str = "volc_task_001") -> dict:
    return {"status_code": 200,
            "json": {"ResponseMetadata": {"RequestId": "req-1", "Action": "CVSubmitTask"},
                     "data": {"task_id": task_id}}}


def volc_submit_no_task_id() -> dict:
    """HTTP 200 但没给 task_id：受理与否未知，不得改判。"""
    return {"status_code": 200, "json": {"ResponseMetadata": {"RequestId": "req-2"},
                                         "data": {}}}


def volc_query(status: str = "done", *, video_url: str = "https://cdn.example.com/v.mp4",
               **extra) -> dict:
    data = {"status": status}
    if video_url:
        data["video_url"] = video_url
    data.update(extra)
    return {"status_code": 200,
            "json": {"ResponseMetadata": {"RequestId": "req-3"}, "data": data}}


def binary_response(data: bytes, status_code: int = 200) -> dict:
    return {"status_code": status_code, "body": data}


def m2_ok(output_url: str = "https://gpu.example.com/out.mp4", **extra) -> dict:
    return {"status_code": 200,
            "json": dict({"state": "succeeded", "output_url": output_url}, **extra)}


# ---------- 领域对象安装 ----------

def install_identity(identity_id: str = PRESET_IDENTITY) -> dict:
    """把预设人物补成"可生产"：原图 + 身份审核 + 冻结 + 权属。

    幂等：同一会话里多个用例会各自调用一次，已冻结的身份不能被再次覆盖
    （这正是 registry 的 `IDENTITY_FROZEN` 规则），因此这里先检查再动手。
    """
    ident = registry.get_identity(identity_id)
    assert ident, "预设人物应由 storage._seed 建立"
    if ident.get("frozen") and ident.get("hero_asset_id") \
            and registry.get_asset(ident.get("hero_asset_id") or ""):
        # 已冻结身份不能再登记英雄图（registry 会以 IDENTITY_FROZEN 拒绝）。
        # 这里只把夹具造成的状态偏差复位，保证用例之间互不依赖顺序。
        changed = False
        if ident.get("approval_state") != "approved":
            ident["approval_state"] = "approved"
            changed = True
        if not ident.get("license_id"):
            lic = licensing.register(subject=identity_id, kind="image_source",
                                     scope="commercial_ok",
                                     evidence="测试用授权书（夹具构造）", verified_by="user")
            ident["license_id"] = lic["id"]
            changed = True
        if changed:
            st.put("identities", identity_id, ident)
        return st.get("identities", identity_id)
    lic = licensing.register(subject=identity_id, kind="image_source", scope="commercial_ok",
                             evidence="测试用授权书（夹具构造）", verified_by="user")
    hero = registry.register_asset(identity_id, "hero", storage_uri="https://cdn.example.com/hero.png",
                                   file_hash=HASH_A, source_evidence="预设人物定妆原图",
                                   license_id=lic["id"], width=941, height=1672)
    registry.record_review(hero["id"], reviewer="user", outcome="pass",
                           identity_digest=ident["identity_digest"], method="manual")
    # 定版前必须由人声明参考依据口径。夹具统一走多角度路径：这些用例考察的是
    # 口播链路，不是参考集政策，走受限路径反而会让失败原因含混。
    registry.set_reference_policy(identity_id, "multi_view", "user",
                                  reason="夹具构造：按多角度参考集路径")
    return registry.freeze_identity(identity_id, hero["id"], ident["identity_digest"],
                                    reviewer="user", license_id=lic["id"])


def install_variant(identity_id: str = PRESET_IDENTITY, *, variant_id: str = None,
                    storage_uri: str = "https://cdn.example.com/variant.png",
                    file_hash: str = HASH_B) -> dict:
    variant_id = variant_id or st.new_id("var")
    lic = licensing.register(subject=variant_id, kind="image_source", scope="commercial_ok",
                             evidence="测试用授权书（夹具构造）", verified_by="user")
    asset = registry.register_asset(identity_id, "variant", storage_uri=storage_uri,
                                    file_hash=file_hash, source_evidence="变体合成产物",
                                    license_id=lic["id"], width=941, height=1672)
    ident = registry.get_identity(identity_id)
    rev = registry.record_review(asset["id"], reviewer="user", outcome="pass",
                                 identity_digest=ident["identity_digest"], method="manual")
    row = {"id": variant_id, "identity_id": identity_id, "outfit": "米白西装",
           "background": "浅灰影棚", "approval_state": "approved", "asset_id": asset["id"],
           "identity_version": ident.get("identity_version", 1), "review_id": rev["id"],
           "identity_report": {"outcome": "pass"}, "preview_label": "测试变体",
           "declared_style": "realistic（写实人像，夹具声明）", "motion_ref_id": None}
    st.put("variants", variant_id, row)
    return row


def install_motion_ref(identity_id: str = PRESET_IDENTITY, *, motion_key: str = "gesture_open",
                       loop_seconds: float = 15.2) -> dict:
    lic = licensing.register(subject=f"motion/{motion_key}", kind="image_source",
                             scope="commercial_ok", evidence="测试用授权书（夹具构造）",
                             verified_by="user")
    asset = registry.register_asset(identity_id, "motion",
                                    storage_uri="https://cdn.example.com/motion.mp4",
                                    file_hash=HASH_C, source_evidence="已获准动作参考",
                                    license_id=lic["id"])
    ref = avatar_motion.register_motion_reference(identity_id, motion_key, asset["id"],
                                                 loop_seconds=loop_seconds,
                                                 license_id=lic["id"])
    return avatar_motion.approve_reference(ref["id"])


def install_final_audio(work_id: str, *, duration_ms: int = 9000,
                        storage_uri: str = "https://cdn.example.com/final.wav",
                        file_hash: str = HASH_D) -> dict:
    """登记并确认一条真实（非 Mock）最终音频。"""
    work = st.get("works", work_id)
    audio = tts_audio.register_audio(
        work_id=work_id, kind="full", voice_id=work["voice_asset_id"],
        duration_ms=duration_ms, file_hash=file_hash, storage_uri=storage_uri,
        script_version_id=work["script_version_id"],
        performance_plan_id=work["performance_plan_id"],
        sample_rate=32000, channels=1, timing_source=None,
        note="夹具构造的已确认最终音频",
    )
    tts_gate.confirm_final_audio(work_id, audio["id"], actor_id="user_1", audition_ack=True)
    return tts_audio.get_audio(audio["id"])


def make_words(text: str, duration_ms: int) -> list:
    """按句生成字级时间戳。

    这是**夹具**，不是对齐算法：时间按句子字符数比例分配，仅用于让分段逻辑
    拿到"覆盖全文的确定时间轴"。真实对齐必须来自 provider/强制对齐/ASR 并登记来源。
    """
    import re

    units = [m for m in re.finditer(r"[^。！？!?…\n]+[。！？!?…\n]?", text)]
    if not units:
        return []
    total = sum(len(m.group(0)) for m in units)
    words = []
    t = 0
    for m in units:
        seg = m.group(0)
        span = max(1, int(round(duration_ms * len(seg) / total)))
        words.append({"segment_id": "seg_1", "start_char": m.start(),
                      "end_char": m.end(), "start_ms": t, "end_ms": t + span})
        t += span
    if words:
        words[-1]["end_ms"] = duration_ms
    return words


def install_alignment(audio_id: str, text: str, duration_ms: int, *,
                      source: str = "forced_alignment", method: str = "mfa") -> dict:
    return tts_gate.register_alignment(audio_id, source=source, method=method,
                                       words=make_words(text, duration_ms),
                                       rule_version="t05-test-1",
                                       note="夹具构造的时间戳，不代表真实对齐结果")


def install_task(work_id: str, *, route: str = "OMNIHUMAN_1_5", confirmed: bool = True,
                 with_selection: bool = True, cost_notice_ack: bool = True) -> dict:
    """装一条任务；默认同时补上"高成本路线已确认费用提示"的作品记录。

    默认合规是为了让其它用例聚焦在自己要验的那条规则上；
    要验"没确认费用提示就被拦"的用例显式传 `cost_notice_ack=False`。
    """
    from app.digital_human import routes as rt

    tid = st.new_id("task")
    task = {"id": tid, "work_id": work_id, "mode": "mock", "stage": "talking_head",
            "state": "running", "route": route, "route_confirmed": bool(confirmed),
            "route_selection_id": st.new_id("sel") if (confirmed and with_selection) else None,
            "percent": None, "progress_source": "indeterminate",
            "pending_selection_ids": [], "input_snapshot_digest": "", "stage_rows": []}
    st.put("tasks", tid, task, {"work_id": work_id})
    work = st.get("works", work_id)
    work["current_task_id"] = tid
    work["route"] = route
    if confirmed and cost_notice_ack and rt.route_spec(route).get("cost_notice_required"):
        work["cost_notice_ack"] = {"route": route, "version": rt.COST_NOTICE_VERSION,
                                   "acknowledged_at": now_iso(), "actor": "user_test"}
    else:
        work.pop("cost_notice_ack", None)
    st.put("works", work_id, work)
    return task


def revoke_cost_notice(work_id: str) -> None:
    """撤销费用提示确认：用于验证"高成本路线没有确认就不能选/不能提交"。"""
    work = st.get("works", work_id) or {}
    work.pop("cost_notice_ack", None)
    st.put("works", work_id, work)


def install_plate(work_id: str, *, duration_ms: int = 9000, identity_id: str = PRESET_IDENTITY,
                  variant_id: str = None, motion_ref_id: str = None,
                  approved: bool = True, audio_asset_id: str = "current", is_mock: bool = False,
                  has_video_track: bool = True, file_hash: str = None) -> dict:
    """登记（并按需批准）一条 M2 动态底片。"""
    from app.digital_human import plates as pl
    from app.digital_human import videos as vd

    variant = st.get("variants", variant_id) if variant_id else None
    source = registry.get_asset((variant or {}).get("asset_id")) or {}
    if audio_asset_id == "current":
        audio_asset_id = (tts_audio.final_audio(work_id) or {}).get("id")
    video = vd.register_video(
        work_id=work_id, kind=vd.KIND_PLATE, route="M2", provider="m2_worker",
        model="LivePortrait", duration_ms=duration_ms,
        file_hash=file_hash if file_hash is not None else "sha256:" + "e" * 64,
        storage_uri=f"video/{work_id}/plate.mp4", is_mock=is_mock, identity_id=identity_id,
        variant_id=variant_id, motion_ref_id=motion_ref_id,
        source_asset_id=source.get("id"), source_asset_hash=source.get("file_hash"),
        audio_asset_id=audio_asset_id, has_video_track=has_video_track,
        has_audio_track=False, width=704, height=1248,
        note="夹具构造的动态底片")
    if approved:
        ident = registry.get_identity(identity_id) or {}
        pl.record_plate_review(video["id"], reviewer="user_1", outcome="pass",
                               audio_duration_ms=duration_ms,
                               identity_digest=ident.get("identity_digest", ""),
                               note="夹具审核通过")
    return vd.get_video(video["id"])


def skip_identity_and_use_preset(work_id: str) -> dict:
    """把作品绑定到预设人物（不额外改造身份）。"""
    work = st.get("works", work_id)
    work["identity_id"] = PRESET_IDENTITY
    st.put("works", work_id, work)
    return work


def new_work(*, route: str = "OMNIHUMAN_1_5", voice_id: str = FIXTURE_VOICE,
             make_plan: bool = True) -> dict:
    """一个独立作品：已采用稿件、（可选）已冻结计划、已绑人物与音色。"""
    from app.mockdata import make_candidates
    from app.tts import plan as tts_plan

    wid = st.new_id("wdh")
    w = {"id": wid, "name": "T05 测试作品",
         "brief": {"topic": "RAG 科普", "content_type": "AI 科普",
                   "audience": "年轻受众", "keywords": [], "target_duration_ms": 60000},
         "identity_id": PRESET_IDENTITY, "voice_asset_id": voice_id,
         "script_version_id": None, "route": route, "current_task_id": None, "version": 1}
    st.put("works", wid, w)
    batch = {"id": st.new_id("b"), "work_id": wid,
             "candidates": [c.model_dump() for c in make_candidates("RAG 科普")]}
    st.put("script_batches", wid, batch)
    w["script_version_id"] = batch["candidates"][0]["id"]
    st.put("works", wid, w)
    if make_plan:
        plan = tts_plan.ensure_plan(wid)
        w = st.get("works", wid)
        w["performance_plan_id"] = plan["id"]
        st.put("works", wid, w)
    return st.get("works", wid)


def script_text_of(work: dict) -> str:
    batch = st.get("script_batches", work["id"]) or {}
    cand = next(c for c in batch["candidates"] if c["id"] == work["script_version_id"])
    return cand["text"]


def long_text(*, sentences: int = 10, per_sentence: int = 24) -> str:
    """构造一段足够长（用于触发分段）的稿件正文。"""
    return "".join(f"这是第{i + 1}个要点，需要讲清楚它的来由与边界。" for i in range(sentences))[:sentences * per_sentence]


def _wav_bytes(duration_ms: int = 1200, sample_rate: int = 32000) -> bytes:
    """确定性的合法 WAV 字节。与 T04 的 `tts_helpers.make_wav` 同构。

    刻意在本文件内自带一份：跨目录 `from conftest/tts_helpers import ...`
    取到哪一份取决于 sys.path（见模块 docstring），而基准音频决定了夹具音色能否
    通过生产就绪判定，不能靠导入顺序决定。
    """
    frames = int(round(sample_rate * duration_ms / 1000.0))
    data = b"\x00" * (frames * 2)
    header = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                      sample_rate * 2, 2, 16)
              + b"data" + struct.pack("<I", len(data)))
    return header + data


def install_voice_ready(voice_id: str = FIXTURE_VOICE) -> dict:
    """登记一条**夹具专用**音色并使其通过生产就绪判定。

    为什么自建而不是给预设音色挂权属：预设音色的"权属未核验 → 不得生产"是 T04
    的验收结论，测试会话共用一个库，改写它就等于为了让 T05 的门禁用例跑通而
    抹掉了那条结论。这里改成一条独立音色，并把每一份证据都标明是夹具构造：

    - 来源：`project_test_material`（本项目测试素材，不冒充真实来源）
    - 基准音频：夹具生成的 WAV，不是任何一次真实合成
    - 权属：`commercial_ok`，但 evidence 写明"不代表已核验"，不做法律结论

    这样 T05 验证的是"下游门禁在音色就绪时的行为"，而"音色就绪"这件事本身
    是显式声明的前提，不是被偷偷造出来的事实。
    """
    from app.tts import samples as tts_samples
    from app.tts import sources as tts_sources

    existing = tts_voices.get_voice(voice_id)
    if not existing:
        asset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".tmp_dh", "_fixture_assets")
        os.makedirs(asset_dir, exist_ok=True)
        wav_path = os.path.join(asset_dir, f"{voice_id}_baseline.wav")
        with open(wav_path, "wb") as f:
            f.write(_wav_bytes())
        source = tts_sources.register_source(
            subject=voice_id, source_type="project_test_material",
            note="夹具构造：T05 下游门禁测试用音色，不代表真实来源或真实合成")
        sample = tts_samples.register_sample(
            voice_id=voice_id, audio_path=wav_path, sample_text=FIXTURE_SAMPLE_TEXT,
            source="夹具生成的基准音频（非真实合成）",
            note="仅用于让 T05 下游门禁可测；不得当作真实基准合成证据引用")
        lic0 = licensing.register(
            subject=voice_id, kind="voice_source", scope="commercial_ok",
            evidence="夹具构造：仅用于测试放行下游门禁，不代表已核验",
            verified_by="test-fixture")
        tts_voices.register_voice(
            voice_id=voice_id, name="T05 夹具音色", provider="minimax",
            model=tts_voices.MINIMAX_TTS_MODEL, remote_voice_id=FIXTURE_VOICE_REMOTE,
            region=tts_voices.MINIMAX_REGION, sample_text=FIXTURE_SAMPLE_TEXT,
            source_record_id=source["id"], license_id=lic0["id"],
            baseline_audio_asset_id=sample["id"],
            baseline_audio_hash=sample["file_hash"], approval_state="approved",
            note="夹具音色：不得作为任何生产或商业结论的依据")

    voice = tts_voices.get_voice(voice_id) or {}
    if not voice.get("license_id"):
        lic = licensing.register(
            subject=voice_id, kind="voice_source", scope="commercial_ok",
            evidence="夹具构造：仅用于测试放行下游门禁，不代表已核验",
            verified_by="test-fixture")
        voice["license_id"] = lic["id"]
        st.put("voices", voice_id, voice)
    return tts_voices.get_voice(voice_id)


def prepare_work(*, route: str = "OMNIHUMAN_1_5", audio_ms: int = 9000,
                 with_variant: bool = True, with_plate: bool = False,
                 with_alignment: bool = False, with_task: bool = True,
                 plate_audio: bool = True, text: str = None) -> dict:
    """一把搭好 T05 需要的全部前置：身份、音色、变体、动作参考、计划、音频、（底片）。

    刻意走完整登记链而不是直接写库：如果前置能靠写库绕过，
    那"未审人物/未审底片被拦"这类用例就证明不了门禁真的生效。
    """
    install_identity()
    install_voice_ready()
    work = new_work(route=route)
    if with_variant:
        variant = install_variant()
        work = st.get("works", work["id"])
        work["variant_id"] = variant["id"]
        st.put("works", work["id"], work)
    if route == "M2":
        ref = install_motion_ref()
        work = st.get("works", work["id"])
        work["motion_intent"] = {"motion_key": ref["motion_key"],
                                 "motion_ref_id": ref["id"],
                                 "params": {"amplitude": 0.4}}
        st.put("works", work["id"], work)
    if text:
        batch = st.get("script_batches", work["id"])
        cand = next(c for c in batch["candidates"] if c["id"] == work["script_version_id"])
        cand["text"] = text
        st.put("script_batches", work["id"], batch)
    audio = install_final_audio(work["id"], duration_ms=audio_ms)
    if with_alignment:
        install_alignment(audio["id"], script_text_of(work), audio_ms)
    if with_plate:
        install_plate(work["id"], duration_ms=audio_ms, variant_id=work.get("variant_id"),
                      motion_ref_id=(work.get("motion_intent") or {}).get("motion_ref_id"),
                      audio_asset_id=audio["id"] if plate_audio else None)
    if with_task:
        install_task(work["id"], route=route)
    return st.get("works", work["id"])


def allow_billable(monkeypatch, *, limit_fen: int = 100000, submissions: int = 9999) -> None:
    """显式放宽预算策略。默认全 0 是刻意的：未授权不得产生计费副作用。"""
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS", str(submissions))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_PER_TASK_FEN", str(limit_fen))
    monkeypatch.setenv("AVATAR_STUDIO_BUDGET_DAILY_FEN", str(limit_fen))


def authorize_cost(work_id: str, route: str = "OMNIHUMAN_1_5", *,
                   prompt: str = "", resolution: str = "1080p",
                   actor_id: str = "user_1") -> dict:
    """走真实链路建立费用确认：摘要必须与提交时算出来的一致。

    摘要一律取自 `current_call_list`——提交与费用确认共用同一份推导。
    若这里另算一份，就会出现"用户确认的清单"和"实际提交的清单"并非同一件事，
    而金额可能完全相同，光看金额发现不了。
    """
    from app.digital_human import cost as dh_cost
    from app.digital_human import submission as dh_sub

    listing = dh_sub.current_call_list(work_id, route=route, prompt=prompt,
                                       resolution=resolution)
    audio = tts_audio.final_audio(work_id)
    audio_ms = int(audio.get("duration_ms") or 0)
    est = dh_cost.estimate(route, duration_ms=audio_ms, resolution=resolution,
                           segment_count=listing["segment_count"])
    return dh_cost.confirm_cost(work_id, route=route, request_digest=listing["digest"],
                                estimated_fen=est["estimated_fen"],
                                ceiling_fen=est["ceiling_fen"],
                                segment_count=listing["segment_count"],
                                generated_seconds=audio_ms / 1000.0,
                                resolution=resolution, actor_id=actor_id)


# ---------- 口播原片构造 ----------

HASH_ORIGINAL = "sha256:" + "f" * 64


def install_original(work_id: str, *, duration_ms: int = 9000,
                     audio_asset_id: str = None, route: str = "OMNIHUMAN_1_5",
                     file_hash: str = None, is_mock: bool = False,
                     has_video_track: bool = True, identity_id: str = PRESET_IDENTITY,
                     variant_id: str = None, segment_index=None, segment_count=None,
                     global_offset_ms: int = 0, storage_uri: str = None) -> dict:
    """登记一条口播原片（默认未审核）。"""
    from app.digital_human import videos as vd

    work = st.get("works", work_id) or {}
    video = vd.register_video(
        work_id=work_id, kind=vd.KIND_ORIGINAL, route=route,
        provider="volcengine" if route == "OMNIHUMAN_1_5" else "m2_worker",
        model="omnihuman-1.5" if route == "OMNIHUMAN_1_5" else "MuseTalk-1.5",
        duration_ms=duration_ms,
        file_hash=file_hash if file_hash is not None else HASH_ORIGINAL,
        storage_uri=storage_uri or f"video/{work_id}/original.mp4",
        is_mock=is_mock, identity_id=identity_id,
        variant_id=variant_id if variant_id is not None else work.get("variant_id"),
        audio_asset_id=audio_asset_id, has_video_track=has_video_track,
        has_audio_track=True, width=704, height=1248, fps=25,
        segment_index=segment_index, segment_count=segment_count,
        global_offset_ms=global_offset_ms, note="夹具构造的口播原片",
    )
    return vd.get_video(video["id"])


def approve_original(video_id: str, *, audio_asset_id: str, reviewer: str = "user_1",
                     offset_ms: int = 0, method: str = "manual_frame_step",
                     outcome: str = "pass", dimensions: dict = None,
                     references: dict = None, note: str = "夹具审核通过") -> dict:
    """补齐四维人工审核 + 音轨核对，使原片可以通过门禁。

    默认四项全 pass 且各带引用：门禁要求"每条通过都要有引用"，
    这里给的就是最小合法引用（资产 id + 毫秒区间）。
    """
    from app.digital_human import gates as dh_gates

    dims = dimensions if dimensions is not None else {d: "pass" for d in dh_gates.DIMENSIONS}
    refs = references if references is not None else {
        d: {"asset_id": f"ref_{d}", "start_ms": 0, "end_ms": 1000, "method": method}
        for d in dh_gates.DIMENSIONS}
    review = dh_gates.record_video_review(video_id, reviewer=reviewer, outcome=outcome,
                                          dimensions=dims, references=refs, note=note)
    track = dh_gates.record_audio_track_check(
        video_id, audio_asset_id=audio_asset_id, method="decode_align", outcome="pass",
        measured_offset_ms=offset_ms,
        evidence="夹具实测：解码为 24kHz 单声道后取最佳偏移",
        note="夹具音轨核对，不代表真实测量")
    return {"review": review, "audio_track": track}
