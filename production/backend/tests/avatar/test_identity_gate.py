"""T03 身份门禁负例：先测拦截，再谈生成。

覆盖：冻结身份不可覆盖、换脸候选拒收、旧审核摘要失效、Mock 后代拒绝、
动作参考误作人物原片、非写实画风与无法判断、哈希篡改、参考集不足。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import identity_gate, registry  # noqa: E402
from app.avatar.registry import AssetRejected  # noqa: E402

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64


def _new_identity(name: str = "候选人物") -> dict:
    return registry.create_identity_candidate(name=name, persona="测试用途", keywords={"gender": "女"})


def _asset(identity_id: str, kind: str = "hero", is_mock: bool = False,
           file_hash: str = HASH, uri: str = "runtime/assets/hero.png") -> dict:
    return registry.register_asset(
        identity_id=identity_id, kind=kind, storage_uri=uri, file_hash=file_hash,
        source_evidence="本项目自有素材，授权自持", is_mock=is_mock,
    )


def _freeze(identity_id: str, digest: str = DIGEST) -> dict:
    hero = _asset(identity_id, "hero")
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=digest, method="人工对照")
    registry.set_reference_policy(identity_id, "multi_view", "user:tester",
                                  reason="夹具构造：按多角度参考集路径")
    return registry.freeze_identity(identity_id, hero["id"], digest, reviewer="tester")


# ---------- 冻结身份不可覆盖 ----------

def test_frozen_identity_hero_cannot_be_overwritten():
    ident = _new_identity()
    _freeze(ident["id"])
    with pytest.raises(AssetRejected) as e:
        _asset(ident["id"], "hero")
    assert e.value.code == "IDENTITY_FROZEN"


def test_frozen_identity_reference_file_cannot_be_replaced():
    ident = _new_identity()
    _freeze(ident["id"])
    ref = _asset(ident["id"], "reference", uri="runtime/assets/ref1.png")
    registry.record_review(ref["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    ref = registry.get_asset(ref["id"])
    with pytest.raises(AssetRejected) as e:
        registry.replace_asset_file(ref["id"], "runtime/assets/ref1_replaced.png", OTHER_HASH)
    assert e.value.code == "IDENTITY_FROZEN"


def test_reference_set_can_be_completed_after_freeze():
    """冻结后仍可补齐参考集：AC-AV-01 要求至少 4 张审核通过的参考图。"""
    ident = _new_identity()
    _freeze(ident["id"])
    for i in range(4):
        _asset(ident["id"], "reference", uri=f"runtime/assets/ref_{i}.png",
               file_hash=f"sha256:{'%064x' % i}")
    assert len(registry.list_assets(ident["id"], "reference")) == 4


def test_replacing_approved_file_of_frozen_identity_is_rejected():
    ident = _new_identity()
    ident = _freeze(ident["id"])
    hero = registry.get_asset(ident["hero_asset_id"])
    with pytest.raises(AssetRejected) as e:
        registry.replace_asset_file(hero["id"], "runtime/assets/hero_v2.png", OTHER_HASH)
    assert e.value.code == "IDENTITY_FROZEN"


def test_changing_person_bumps_version_and_requires_reapproval():
    ident = _new_identity()
    _freeze(ident["id"])
    v2 = registry.bump_identity_version(ident["id"])
    assert v2["identity_version"] == 2
    assert v2["frozen"] is False
    assert v2["approval_state"] == "pending"
    assert v2["hero_asset_id"] == "" and v2["identity_digest"] == ""
    # 新版本可以自由登记原片，不再被"冻结"挡住
    hero2 = _asset(ident["id"], "hero", uri="runtime/assets/hero_v2.png")
    assert hero2["version"] == 1


# ---------- 换脸候选拒收 ----------

def test_face_swap_candidate_is_rejected():
    ident = _new_identity()
    _freeze(ident["id"])
    variant = registry.register_asset(
        identity_id=ident["id"], kind="variant", storage_uri="runtime/assets/var.png",
        file_hash=OTHER_HASH, source_evidence="图像编辑产出（未验证）",
    )
    registry.record_review(variant["id"], reviewer="tester", outcome="pass",
                           identity_digest=OTHER_DIGEST, method="人工对照")
    asset = registry.get_asset(variant["id"])
    asset["approval_state"] = "approved"
    st.put("avatar_assets", asset["id"], asset, {"identity_id": ident["id"]})

    out = identity_gate.admit(variant["id"], ident["id"], expected_kind="variant")
    assert out["ok"] is False
    assert any("换脸候选" in r for r in out["reasons"])


def test_unreviewed_hero_cannot_be_frozen():
    """选择审批不等于身份审核：没有任何审核记录的原片不得作为定版依据。"""
    ident = _new_identity()
    hero = _asset(ident["id"], "hero")
    registry.set_reference_policy(ident["id"], "multi_view", "user:tester",
                                  reason="夹具构造：本用例考察的是未审核即冻结")
    with pytest.raises(AssetRejected) as e:
        registry.freeze_identity(ident["id"], hero["id"], DIGEST, reviewer="tester")
    assert e.value.code == "ASSET_NOT_REVIEWED"


def test_missing_license_blocks_production_admission():
    """AC-AV-04：权属未登记即阻塞，不得默认放行。"""
    ident = _new_identity()
    ident = _freeze(ident["id"])
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("权属" in r for r in reasons)


# ---------- 旧审核摘要失效 ----------

def test_stale_review_expires_after_file_replacement():
    ident = _new_identity()
    hero = _asset(ident["id"], "hero")
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    assert registry.current_review(hero["id"]) is not None

    registry.replace_asset_file(hero["id"], "runtime/assets/hero_replaced.png", OTHER_HASH)
    assert registry.current_review(hero["id"]) is None, "文件变更后旧审核摘要必须失效"

    out = identity_gate.admit(hero["id"], ident["id"])
    assert out["ok"] is False


def test_review_bound_to_version_and_hash_record():
    ident = _new_identity()
    hero = _asset(ident["id"], "hero")
    rev = registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                                 identity_digest=DIGEST, method="人工对照")
    assert rev["asset_version"] == hero["version"]
    assert rev["file_hash"] == HASH


def test_tampered_hash_is_rejected():
    ident = _new_identity()
    hero = _asset(ident["id"], "hero")
    with pytest.raises(AssetRejected) as e:
        identity_gate.verify_no_tamper(hero["id"], OTHER_HASH)
    assert e.value.code == "ASSET_HASH_TAMPERED"


# ---------- Mock 后代拒绝 ----------

def test_mock_asset_cannot_enter_production():
    ident = _new_identity()
    mock_asset = _asset(ident["id"], "hero", is_mock=True)
    registry.record_review(mock_asset["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    mock_asset = registry.get_asset(mock_asset["id"])
    mock_asset["approval_state"] = "approved"
    st.put("avatar_assets", mock_asset["id"], mock_asset, {"identity_id": ident["id"]})

    out = identity_gate.admit(mock_asset["id"], ident["id"])
    assert out["ok"] is False
    assert any("Mock" in r for r in out["reasons"])


# ---------- 动作参考误作人物原片 ----------

def test_motion_asset_rejected_as_hero():
    ident = _new_identity()
    motion_asset = _asset(ident["id"], "motion", uri="runtime/assets/motion.mp4")
    registry.record_review(motion_asset["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    out = identity_gate.admit(motion_asset["id"], ident["id"], expected_kind="hero")
    assert out["ok"] is False
    assert any("资产种类不符" in r for r in out["reasons"])


def test_guard_kind_raises_for_motion_as_original():
    ident = _new_identity()
    motion_asset = _asset(ident["id"], "motion", uri="runtime/assets/motion.mp4")
    with pytest.raises(AssetRejected) as e:
        identity_gate.guard_kind(motion_asset, "hero")
    assert e.value.code == "ASSET_KIND_MISMATCH"


# ---------- 画风与无法判断 ----------

@pytest.mark.parametrize("style", ["cartoon", "anime", "svg", "css", "geometric", "illustration"])
def test_negative_styles_are_rejected(style):
    out = identity_gate.screen_style(style)
    assert out["outcome"] == "fail"


def test_undeclared_style_is_review_not_pass():
    out = identity_gate.screen_style("")
    assert out["outcome"] == "review"


def test_realistic_style_passes():
    assert identity_gate.screen_style("photoreal")["outcome"] == "pass"


# ---------- 生产准入 ----------

def test_production_ready_blocks_when_assets_incomplete():
    ident = _new_identity()
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("原片" in r or "定版" in r or "冻结" in r for r in reasons)


def _commercial_license() -> str:
    from app.avatar import licensing

    return licensing.register(subject="本项目自有素材", kind="image_source",
                              scope="commercial_ok", evidence="本项目自行生成，权利自持",
                              verified_by="tester")["id"]


def test_production_ready_requires_four_reviewed_references():
    from app.avatar import licensing

    ident = _new_identity()
    lic = _commercial_license()
    hero = registry.register_asset(
        identity_id=ident["id"], kind="hero", storage_uri="runtime/assets/hero.png",
        file_hash=HASH, source_evidence="本项目自有素材，授权自持", license_id=lic,
    )
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    registry.set_reference_policy(ident["id"], "multi_view", "user:tester",
                                  reason="夹具构造：本用例走多角度路径，验证 4 张门槛仍然有效")
    ident = registry.freeze_identity(ident["id"], hero["id"], DIGEST,
                                     reviewer="tester", license_id=lic)

    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("参考图不足" in r for r in reasons)

    for i in range(4):
        ref = registry.register_asset(
            identity_id=ident["id"], kind="reference",
            storage_uri=f"runtime/assets/ref_{i}.png", file_hash=f"sha256:{'%064x' % i}",
            source_evidence="本项目自有素材，授权自持", license_id=lic,
        )
        registry.record_review(ref["id"], reviewer="tester", outcome="pass",
                               identity_digest=DIGEST, method="人工对照")
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is True, reasons
    del licensing


def test_require_production_raises_with_reasons():
    ident = _new_identity()
    with pytest.raises(AssetRejected) as e:
        registry.require_production(ident["id"])
    assert e.value.code == "ASSET_NOT_PRODUCTION_READY"
    assert e.value.detail["reasons"]
