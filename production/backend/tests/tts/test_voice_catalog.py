"""T04 官方预置音色目录（AC-TTS-01 的"可选音色"侧）。

三组断言，各自防一类失真：

1. **目录本身干净**——无重复 voice_id、分组取值合法、刻意排除的类别
   （童声/卡通/机器人角色音、`*-jingpin` 同源重复项）确实不在表里。
   目录是"能选什么"的唯一定义，脏一条就会以候选身份一路走到合成。
2. **引导幂等且不越权**——重复引导不新增行；既有行的作用域/合成参数/审批/
   基准证据一律不被改写。"同一 ID 不得被改写（换声必须新版本）"这条约束，
   不能因为一次描述字段补全而破例。
3. **基准证据只能来自真实试听**——没试听过、只做过占位试听、试听换了句子，
   三种情形都必须拒收。伪造一个哈希填进去，等于把"验证过"变成一句空话。

本文件用一个随机后缀的探针音色（`_probe`）做状态类用例，而不是拿目录里的
真实条目：`tests/conftest.py` 是"一个 pytest 进程一个库"，拿真实条目会把
审批/基准状态写到别的用例也在读的行上。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "tts"))

from app import storage as st  # noqa: E402
from app.tts import audio as au  # noqa: E402
from app.tts import catalog  # noqa: E402
from app.tts import plan as tts_plan  # noqa: E402
from app.tts import preview as pv  # noqa: E402
from app.tts import voices as vc  # noqa: E402
from app.tts.errors import TtsRejected  # noqa: E402
from tts_helpers import allow_billable, make_transport, ok_body  # noqa: E402

_MANDARIN_PREFIX = "Chinese (Mandarin)_"


def _probe() -> tuple:
    """一个本次进程内唯一的探针音色：(remote_voice_id, voice_asset_id)。"""
    remote = f"{_MANDARIN_PREFIX}Probe_{st.new_id('p')}"
    return remote, catalog.voice_id_for(remote)


def _bootstrap_probe(monkeypatch, remote: str, *, tone: str = "探针音色描述",
                     group: str = catalog.GROUP_MANDARIN) -> dict:
    """只引导这一条探针，避免把整批 45 条写进共享库。"""
    monkeypatch.setattr(catalog, "CATALOG", (
        (remote, "探针音色", tone, group),
    ))
    return catalog.bootstrap_catalog()


# ---------- ID 推导（纯函数） ----------

def test_voice_id_for_is_deterministic_and_strips_mandarin_prefix():
    """确定性是硬要求：不确定的话每次重启都会多出一份重复候选。"""
    a = catalog.voice_id_for("Chinese (Mandarin)_Warm_Bestie")
    b = catalog.voice_id_for("Chinese (Mandarin)_Warm_Bestie")
    assert a == b == "voice_minimax_warm_bestie_v1"


def test_voice_id_for_handles_legacy_and_odd_ids():
    assert catalog.voice_id_for("Arrogant_Miss") == "voice_minimax_arrogant_miss_v1"
    assert catalog.voice_id_for("danya_xuejie") == "voice_minimax_danya_xuejie_v1"
    # 连字符/空格等非字母数字统一收敛成下划线，且不产生连续/首尾下划线
    slug = catalog.voice_id_for("male-qn-jingying")
    assert slug == "voice_minimax_male_qn_jingying_v1"
    assert "__" not in slug


def test_voice_id_for_agrees_with_existing_preset_id():
    """目录里的 Warm_Bestie 必须落到 storage 既有的预设音色 3 上。

    落到别的 ID 就会变成"同一条声音登记两遍"，引导时也不会被跳过。
    """
    assert catalog.voice_id_for("Chinese (Mandarin)_Warm_Bestie") == vc.PRESET_VOICE_ID


# ---------- 目录形状 ----------

def test_catalog_has_no_duplicate_remote_ids():
    remotes = [c[0] for c in catalog.CATALOG]
    dupes = {r for r in remotes if remotes.count(r) > 1}
    assert not dupes, f"目录存在重复 voice_id：{sorted(dupes)}"


def test_catalog_entries_are_well_formed():
    groups = {catalog.GROUP_MANDARIN, catalog.GROUP_LEGACY}
    for remote, name, tone, group in catalog.CATALOG:
        assert remote.strip(), "voice_id 不能为空"
        assert name.strip(), f"{remote} 缺少中文名"
        assert tone.strip(), f"{remote} 缺少口播风格描述"
        assert group in groups, f"{remote} 分组非法：{group}"
    # 每条 remote 都推得出唯一 ID，且与其它条不撞
    ids = [catalog.voice_id_for(c[0]) for c in catalog.CATALOG]
    assert len(set(ids)) == len(ids)


def test_catalog_covers_both_groups_with_expected_sizes():
    """分组数量是前端分堆展示的依据，变了要显式知道。"""
    assert catalog.bootstrap_catalog()["groups"] == {
        catalog.GROUP_MANDARIN: 26, catalog.GROUP_LEGACY: 19}


def test_catalog_excludes_non_broadcast_categories():
    """童声/卡通/机器人角色音与 beta 重复项不入口播目录。"""
    remotes = [c[0].lower() for c in catalog.CATALOG]
    for banned in ("jingpin", "beta", "cartoon", "robot", "child"):
        assert not any(banned in r for r in remotes), f"目录混入了 {banned} 条目"
    assert "童声" in catalog.EXCLUDED_NOTE and "粤语" in catalog.EXCLUDED_NOTE


# ---------- 引导：幂等与不越权 ----------

def test_bootstrap_registers_whole_catalog():
    out = catalog.bootstrap_catalog()
    assert out["ok"] is True and out["failed"] == []
    assert out["catalog_size"] == len(catalog.CATALOG)
    # 首次与重复调用都成立：要么新增、要么跳过，总数守恒
    assert len(out["added"]) + len(out["skipped"]) == out["catalog_size"]
    status = catalog.catalog_status()
    assert status["missing"] == []
    assert status["registered"] == status["catalog_size"]


def test_bootstrap_is_idempotent():
    catalog.bootstrap_catalog()
    again = catalog.bootstrap_catalog()
    assert again["added"] == [], "重复引导不得新增行"
    assert again["failed"] == []


def test_bootstrap_does_not_overwrite_existing_row(monkeypatch):
    """既有行只补空描述字段；身份字段（作用域/参数/审批/基准）一个都不许动。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote, tone="首轮描述")

    # 人为把这条推成"用户已选定且已有基准证据"的状态
    vc.approve_voice(vid, reviewer="user")
    before = dict(vc.get_voice(vid))
    before["baseline_audio_asset_id"] = "audio_keep_me"
    before["baseline_audio_hash"] = "sha256:" + "c" * 64
    st.put("voices", vid, before)

    # 换一个不同的描述与分组再引导一次：描述已非空，不该被改写
    out = _bootstrap_probe(monkeypatch, remote, tone="不该覆盖的描述",
                           group=catalog.GROUP_LEGACY)
    assert vid in out["skipped"] and vid not in out["added"]

    after = vc.get_voice(vid)
    assert after["approval_state"] == "approved", "引导不得回退审批状态"
    assert after["baseline_audio_asset_id"] == "audio_keep_me"
    assert after["baseline_audio_hash"] == "sha256:" + "c" * 64
    assert after["catalog_group"] == catalog.GROUP_MANDARIN, "非空分组不得被改写"
    assert after["description"] == "首轮描述", "非空描述不得被改写"
    assert after["scope_key"] == before["scope_key"]


def test_backfill_fills_only_empty_fields(monkeypatch):
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    row = vc.get_voice(vid)
    # 抹掉描述性字段，模拟历史遗留行
    row["description"] = ""
    row["catalog_group"] = ""
    st.put("voices", vid, row)

    out = _bootstrap_probe(monkeypatch, remote, tone="补回来的描述",
                           group=catalog.GROUP_LEGACY)
    assert vid in out["backfilled"]
    fixed = vc.get_voice(vid)
    assert fixed["description"] == "补回来的描述"
    assert fixed["catalog_group"] == catalog.GROUP_LEGACY

    # 再补一次：已经不为空，不该出现在 backfilled 里
    again = _bootstrap_probe(monkeypatch, remote, tone="第三次", group=catalog.GROUP_MANDARIN)
    assert again["backfilled"] == []


def test_bootstrap_records_failure_without_aborting_batch(monkeypatch):
    """单条登记失败要如实写进返回值，不能静默吞掉，也不能拖垮整批。"""
    good_remote, good_id = _probe()
    bad_remote, bad_id = _probe()
    # 把坏条目做成"来源记录为空"的非法形状：region 缺失即作用域不完整
    monkeypatch.setattr(catalog, "CATALOG", (
        (good_remote, "好条目", "描述", catalog.GROUP_MANDARIN),
        (bad_remote, "坏条目", "描述", catalog.GROUP_MANDARIN),
    ))
    monkeypatch.setattr(catalog, "_ensure_source",
                        lambda voice_id, remote: {"id": ""} if voice_id == bad_id
                        else {"id": st.new_id("src")})
    out = catalog.bootstrap_catalog()
    assert good_id in out["added"]
    assert out["ok"] is False
    assert [f["voice_id"] for f in out["failed"]] == [bad_id]


# ---------- 候选态：可试听，但不等于可用 ----------

def test_catalog_license_stays_unverified_by_default():
    """目录共用权属默认必须停在 unverified——判定权在人，代码不得自行放行。"""
    catalog.bootstrap_catalog()
    from app.avatar import licensing

    rec = next((r for r in licensing.list_records()
                if r.get("subject") == catalog.LICENSE_SUBJECT), None)
    assert rec is not None, "目录必须有一条共用权属记录"
    assert rec["scope"] == "unverified"
    assert "未取得书面法律意见" in rec["note"]


def test_catalog_candidates_are_traceable_but_not_production_ready(monkeypatch):
    """候选音色：来源链齐全（可试听），但生产就绪必须被阻塞。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    voice = vc.get_voice(vid)

    assert voice["approval_state"] == "pending", "引导不得代替用户批准"
    assert voice["source_record_id"], "来源链不能缺"
    assert voice["license_id"], "权属记录不能缺"
    assert vc.assert_scope_complete(voice)["complete"] is True

    ok, reasons = vc.voice_ready(vid)
    assert ok is False
    joined = "；".join(reasons)
    assert "未审批" in joined
    assert "权属未核验" in joined
    assert "基准合成证据" in joined


# ---------- 基准证据：只认真实试听 ----------

def test_baseline_requires_real_audition(monkeypatch, work):
    """没试听过就登记基准 = 凭空造一个"已验证"。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    with pytest.raises(TtsRejected) as e:
        vc.attach_baseline_from_preview(vid, work["id"])
    assert e.value.code == "VOICE_BASELINE_NO_AUDITION"


def test_baseline_rejects_mock_audition(monkeypatch, work):
    """占位试听不能冒充基准：它没有真实字节，也没有可核验的哈希。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    out = pv.create_preview(work_id=work["id"], voice_id=vid, dry_run=True)
    assert out["audio"]["is_mock"] is True

    with pytest.raises(TtsRejected) as e:
        vc.attach_baseline_from_preview(vid, work["id"])
    assert e.value.code == "VOICE_BASELINE_NO_AUDITION"


def test_baseline_rejects_audition_with_different_sentence(monkeypatch, work):
    """换了句子就不是同一条基准：跨音色比较的锚点会失效。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=800))
    pv.create_preview(work_id=work["id"], voice_id=vid,
                      sample_text="一句与音色登记不同的试听文本。", transport=transport)

    with pytest.raises(TtsRejected) as e:
        vc.attach_baseline_from_preview(vid, work["id"])
    assert e.value.code == "VOICE_BASELINE_SAMPLE_TEXT_MISMATCH"
    assert e.value.detail["voice_sample_text"] == catalog.DEFAULT_SAMPLE_TEXT


def test_baseline_from_preview_attaches_evidence_then_only_license_blocks(monkeypatch, work):
    """走完"选定 → 登记基准"后，只剩权属一条阻塞——正是真实卡点的形状。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    vc.approve_voice(vid, reviewer="user")           # 用户选定
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=1200, sample_rate=32000))
    pv.create_preview(work_id=work["id"], voice_id=vid,
                      sample_text=catalog.DEFAULT_SAMPLE_TEXT, transport=transport)

    out = vc.attach_baseline_from_preview(vid, work["id"])
    sample = out["sample"]
    assert out["production_ready"] is False
    assert out["production_blockers"] == [
        "权属未核验为可商用（当前 unverified）：" + catalog.LICENSE_SUBJECT]
    assert out["note"].startswith("已登记基准合成证据")

    # 基准不是文字转述：规格与哈希都来自真实容器
    assert sample["duration_ms"] == 1200
    assert sample["sample_rate"] == 32000
    assert sample["channels"] == 1
    assert sample["file_hash"].startswith("sha256:")
    assert out["voice"]["baseline_audio_asset_id"] == sample["id"]
    assert out["voice"]["baseline_audio_hash"] == sample["file_hash"]
    assert out["source_audio_id"]
    assert os.path.isfile(au.runtime_path(sample["storage_uri"]))


def test_baseline_from_preview_is_idempotent(monkeypatch, work):
    """同一音色同一份真实试听重复登记，不得产生第二份样本。"""
    remote, vid = _probe()
    _bootstrap_probe(monkeypatch, remote)
    allow_billable(monkeypatch)
    transport, _ = make_transport(body=ok_body(duration_ms=600))
    pv.create_preview(work_id=work["id"], voice_id=vid,
                      sample_text=catalog.DEFAULT_SAMPLE_TEXT, transport=transport)

    first = vc.attach_baseline_from_preview(vid, work["id"])
    second = vc.attach_baseline_from_preview(vid, work["id"])
    assert second["sample"]["id"] == first["sample"]["id"]
    from app.tts import samples as vs

    assert len(vs.list_samples(vid)) == 1


def test_baseline_unknown_voice_rejected(monkeypatch):
    with pytest.raises(TtsRejected) as e:
        vc.attach_baseline_from_preview("voice_does_not_exist_v1", "w_any")
    assert e.value.code == "VOICE_NOT_FOUND"


def test_baseline_does_not_borrow_another_voices_sample(monkeypatch, work):
    """样本是"某条音色的基准"，不能因为哈希相同就挂到别的音色名下。"""
    remote_a, vid_a = _probe()
    remote_b, vid_b = _probe()
    monkeypatch.setattr(catalog, "CATALOG", (
        (remote_a, "探针A", "描述", catalog.GROUP_MANDARIN),
        (remote_b, "探针B", "描述", catalog.GROUP_MANDARIN),
    ))
    catalog.bootstrap_catalog()
    allow_billable(monkeypatch)

    # 两个作品各自试听：同一句同一参数会命中缓存，因此第二首用 force_new
    w_a, w_b = work, st.get("works", work["id"])
    transport, _ = make_transport(body=ok_body(duration_ms=500))
    pv.create_preview(work_id=w_a["id"], voice_id=vid_a,
                      sample_text=catalog.DEFAULT_SAMPLE_TEXT, transport=transport)
    out_a = vc.attach_baseline_from_preview(vid_a, w_a["id"])

    from app.tts import samples as vs

    assert len(vs.list_samples(vid_b)) == 0, "B 未试听，不该凭空有样本"
    assert out_a["voice"]["baseline_audio_hash"] == out_a["sample"]["file_hash"]


# ---------- 音频路径守卫 ----------

def test_runtime_path_rejects_absolute_path():
    """storage_uri 被改成绝对路径，就等于让"登记基准"去读本机任意文件。"""
    with pytest.raises(TtsRejected) as e:
        au.runtime_path(r"C:\Windows\win.ini")
    assert e.value.code == "AUDIO_PATH_INVALID"


def test_runtime_path_rejects_traversal_and_empty():
    for bad in ("", "audio/../../../etc/passwd", "..\\..\\secret.wav",
                "audio/../../runtime2/x.wav"):
        with pytest.raises(TtsRejected) as e:
            au.runtime_path(bad)
        assert e.value.code == "AUDIO_PATH_INVALID", bad


def test_runtime_path_accepts_relative_path_inside_runtime():
    path = au.runtime_path("audio/w_x/audio_y.wav")
    root = os.path.realpath(au.runtime_root())
    assert os.path.realpath(path).startswith(root)
    assert path.endswith(os.path.join("audio", "w_x", "audio_y.wav"))


# ---------- 计划与当前选择的绑定 ----------

def test_plan_binding_detects_voice_and_identity_change():
    """换音色/换形象后，旧计划不得被复用——否则合成会报作用域不一致。"""
    work = {"voice_asset_id": "voice_a", "identity_id": "id_a"}
    plan = {"voice_asset_id": "voice_a", "identity_id": "id_a"}
    assert tts_plan.binds_current_choice(plan, work) is True
    assert tts_plan.binds_current_choice(plan, {"voice_asset_id": "voice_b",
                                                "identity_id": "id_a"}) is False
    assert tts_plan.binds_current_choice(plan, {"voice_asset_id": "voice_a",
                                                "identity_id": "id_b"}) is False
    # 空计划不可复用（没有可复用的东西），但"没绑字段"的非空历史行不算冲突：
    # 它没有绑上一版的声音/人物，其余条件交给 is_usable 判断。
    assert tts_plan.binds_current_choice({}, work) is False
    assert tts_plan.binds_current_choice(None, work) is False
    assert tts_plan.binds_current_choice({"id": "plan_legacy"}, work) is True
