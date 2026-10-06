"""T03 资产登记、权属与动作映射。

覆盖：登记缺图/缺哈希/来源不清一律拒绝；权属未确认与权重许可缺失阻塞；
预设 av_base_b_v1 仍可用但不继承生产通过结论；动作意图映射到获准参考，
自由描述在 M2 下拒绝，短循环不转长期母版。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import licensing, motion, registry  # noqa: E402
from app.avatar.licensing import LicenseBlocked  # noqa: E402
from app.avatar.registry import AssetRejected  # noqa: E402

HASH = "sha256:" + "a" * 64
DIGEST = "sha256:" + "c" * 64


def _identity(name: str = "候选人物") -> dict:
    return registry.create_identity_candidate(name=name, persona="测试用途")


# ---------- 登记门禁 ----------

def test_register_requires_file():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.register_asset(ident["id"], "hero", storage_uri="",
                                file_hash=HASH, source_evidence="自有")
    assert e.value.code == "ASSET_NO_FILE"


def test_register_requires_hash():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.register_asset(ident["id"], "hero", storage_uri="runtime/assets/x.png",
                                file_hash="", source_evidence="自有")
    assert e.value.code == "ASSET_HASH_MISSING"


def test_register_rejects_malformed_hash():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.register_asset(ident["id"], "hero", storage_uri="runtime/assets/x.png",
                                file_hash="md5:abc", source_evidence="自有")
    assert e.value.code == "ASSET_HASH_INVALID"


def test_register_requires_source_evidence():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.register_asset(ident["id"], "hero", storage_uri="runtime/assets/x.png",
                                file_hash=HASH, source_evidence="")
    assert e.value.code == "ASSET_SOURCE_UNCLEAR"


def test_register_increments_version_on_supersede():
    ident = _identity()
    a1 = registry.register_asset(ident["id"], "reference", storage_uri="runtime/assets/r1.png",
                                 file_hash=HASH, source_evidence="自有")
    a2 = registry.register_asset(ident["id"], "reference", storage_uri="runtime/assets/r2.png",
                                 file_hash="sha256:" + "e" * 64, source_evidence="自有",
                                 supersedes_id=a1["id"])
    assert a2["version"] == a1["version"] + 1
    assert a2["supersedes_id"] == a1["id"]


def test_asset_reference_ids_recorded_on_identity():
    ident = _identity()
    ref = registry.register_asset(ident["id"], "reference", storage_uri="runtime/assets/r1.png",
                                  file_hash=HASH, source_evidence="自有")
    ident = registry.get_identity(ident["id"])
    assert ref["id"] in ident["reference_asset_ids"]


# ---------- 预设 ----------

def test_preset_identity_exists_and_is_approved():
    preset = registry.get_identity("av_base_b_v1")
    assert preset is not None, "初始预设 av_base_b_v1 必须保留"
    assert preset["approval_state"] == "approved"
    assert preset["is_preset"] is True


def test_preset_does_not_inherit_production_readiness():
    """登记不继承生产质量通过结论：预设缺少已审核资产即不通过生产准入。"""
    ok, reasons = registry.production_ready("av_base_b_v1")
    assert ok is False
    assert reasons, "必须给出可定位的阻断原因"


# ---------- 权属 ----------

def test_license_commercial_claim_without_evidence_falls_back():
    rec = licensing.register(subject="图像来源 A", kind="image_source",
                             scope="commercial_ok", evidence="", verified_by="")
    assert rec["scope"] == "unverified", "声称商用但无证据与核验人，必须回落未核验"


def test_license_missing_blocks():
    with pytest.raises(LicenseBlocked) as e:
        licensing.require(None, subject="未登记对象")
    assert e.value.code == "LICENSE_MISSING"


def test_license_unverified_blocks():
    rec = licensing.register(subject="图像来源 B", kind="image_source")
    with pytest.raises(LicenseBlocked) as e:
        licensing.require(rec["id"], subject="图像来源 B")
    assert e.value.code == "LICENSE_UNVERIFIED"


def test_license_blocked_scope_blocks():
    rec = licensing.register(subject="来源 C", kind="image_source", scope="blocked",
                             evidence="条款禁止商用", verified_by="tester")
    with pytest.raises(LicenseBlocked) as e:
        licensing.require(rec["id"])
    assert e.value.code == "LICENSE_BLOCKED"


def test_algorithm_name_does_not_replace_weights_license():
    rec = licensing.register(subject="检测模型 X", kind="model_weights", scope="commercial_ok",
                             evidence="https://github.com/example/model 论文 arXiv:1234",
                             verified_by="tester", covers_weights=False)
    with pytest.raises(LicenseBlocked) as e:
        licensing.require(rec["id"], need_weights=True)
    assert e.value.code == "WEIGHTS_LICENSE_MISSING"


def test_weights_license_ok_when_covers_weights():
    rec = licensing.register(subject="检测模型 Y", kind="model_weights", scope="commercial_ok",
                             evidence="厂商商用授权书编号 2026-001",
                             verified_by="tester", covers_weights=True)
    assert licensing.require(rec["id"], need_weights=True)["id"] == rec["id"]


def test_register_asset_with_blocked_license_is_rejected():
    ident = _identity()
    rec = licensing.register(subject="来源 D", kind="image_source", scope="blocked",
                             evidence="禁止商用", verified_by="tester")
    with pytest.raises(LicenseBlocked):
        registry.register_asset(ident["id"], "hero", storage_uri="runtime/assets/x.png",
                                file_hash=HASH, source_evidence="来源 D", license_id=rec["id"])


# ---------- 动作映射 ----------

def _approved_motion(identity_id: str, key: str = "nod", loop_seconds: float = 5.0) -> dict:
    asset = registry.register_asset(identity_id, "motion", storage_uri=f"runtime/motion/{key}.mp4",
                                    file_hash="sha256:" + key.encode().hex().ljust(64, "0"),
                                    source_evidence="自有动作素材")
    ref = motion.register_motion_reference(identity_id, key, asset["id"], loop_seconds=loop_seconds)
    return motion.approve_reference(ref["id"])


def test_motion_intent_maps_to_approved_reference():
    ident = _identity()
    ref = _approved_motion(ident["id"], "nod")
    out = motion.resolve_motion_intent(ident["id"], {"motion_key": "nod", "params": {"amplitude": 0.3}})
    assert out["motion_ref_id"] == ref["id"]
    assert out["master_eligible"] is True


def test_motion_freetext_rejected_on_m2():
    ident = _identity()
    _approved_motion(ident["id"], "nod")
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"free_text": "边走边说"}, route="M2")
    assert e.value.code == "MOTION_FREETEXT_UNSUPPORTED"


def test_motion_requires_approved_reference():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"motion_key": "nod"})
    assert e.value.code == "MOTION_NOT_APPROVED"


def test_motion_unsupported_key_rejected():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"motion_key": "dance"})
    assert e.value.code == "MOTION_UNSUPPORTED"


def test_motion_param_out_of_range_rejected():
    ident = _identity()
    _approved_motion(ident["id"], "nod")
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"motion_key": "nod", "params": {"amplitude": 5.0}})
    assert e.value.code == "MOTION_PARAM_OUT_OF_RANGE"


def test_motion_param_unsupported_rejected():
    ident = _identity()
    _approved_motion(ident["id"], "nod")
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"motion_key": "nod", "params": {"spin": 1.0}})
    assert e.value.code == "MOTION_PARAM_UNSUPPORTED"


def test_motion_reference_requires_motion_kind_asset():
    ident = _identity()
    hero = registry.register_asset(ident["id"], "hero", storage_uri="runtime/assets/hero.png",
                                   file_hash=HASH, source_evidence="自有")
    with pytest.raises(AssetRejected) as e:
        motion.register_motion_reference(ident["id"], "nod", hero["id"])
    assert e.value.code == "ASSET_KIND_MISMATCH"


def test_short_loop_not_eligible_as_master():
    ident = _identity()
    _approved_motion(ident["id"], "smile", loop_seconds=1.5)
    out = motion.resolve_motion_intent(ident["id"], {"motion_key": "smile"})
    assert out["master_eligible"] is False
    with pytest.raises(AssetRejected) as e:
        motion.resolve_motion_intent(ident["id"], {"motion_key": "smile"}, as_master=True)
    assert e.value.code == "MOTION_LOOP_TOO_SHORT"
