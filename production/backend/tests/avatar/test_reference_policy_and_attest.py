"""AI 生成人物"看不到第二张脸"这件事：受限路径、人工权属确认与防冒充变体。

这组用例守的是三件事，任一件松了，整条链的证据基础就变成自说自话：

1. **参考依据口径只能由人扳**（`set_reference_policy`）。系统不得从"参考图不足 4 张"
   自动推断某个身份该走受限路径——推断等于让代码替人决定可以拿什么当证据。
   受限路径必须写清谁、为什么这么定，并逐条确认三条限制。
2. **商用范围只能由人确认**（`licensing.attest_commercial`）。这是确认，不是核验、
   更不是法律意见：原有 evidence 里"未取得书面法律意见"那句必须原样保留，
   `verified_by` 记的是人，不是算法。版本化的确认标记会随措辞变化而失效。
3. **受限身份的变体只能从原图派生**（`VARIANT_DERIVATION_MISMATCH`）。
   拿另一张生成图来登记就是换脸，那正是这条路径要挡的事。

背景：整张脸由生成服务凭空产出的人，不存在"同一人的第二张真值"，
因此"至少 4 张参考图"在逻辑上无从取得。这是旧口径没覆盖到的情形，
不是降低标准，而是换成一套说得清、可追责的证据口径。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import licensing, registry  # noqa: E402
from app.avatar.licensing import LicenseBlocked  # noqa: E402
from app.avatar.registry import AssetRejected  # noqa: E402
from app.contracts import HERO_ONLY_LIMITATIONS  # noqa: E402

HASH = "sha256:" + "a" * 64
OTHER_HASH = "sha256:" + "b" * 64
DIGEST = "sha256:" + "c" * 64


# ---------- 夹具 ----------

def _identity() -> dict:
    return registry.create_identity_candidate(name="AI 候选", persona="生成服务产出",
                                              keywords={"gender": "女"})


def _hero(identity_id: str, license_id: str = "") -> dict:
    hero = registry.register_asset(
        identity_id=identity_id, kind="hero", storage_uri="assets/g/hero.png",
        file_hash=HASH, source_evidence="生成服务产出", license_id=license_id,
    )
    registry.record_review(hero["id"], reviewer="user:operator", outcome="pass",
                           identity_digest=DIGEST, method="人工目检：与期望人设一致")
    return hero


def _limited(identity_id: str) -> dict:
    return registry.set_reference_policy(
        identity_id=identity_id, mode="hero_only", operator="user:operator",
        reason="整张脸由文生图服务产出，不存在同一人的第二张真值",
        acknowledged=list(HERO_ONLY_LIMITATIONS),
    )


def _commercial_license(subject: str = "AI 生成人物") -> dict:
    return licensing.register(subject=subject, kind="image_source", scope="commercial_ok",
                              evidence="本项目自有素材", verified_by="user:operator")


def _freeze_limited(identity_id: str, license_id: str = "") -> dict:
    hero = _hero(identity_id, license_id)
    _limited(identity_id)
    return registry.freeze_identity(identity_id, hero["id"], DIGEST,
                                    reviewer="user:operator", license_id=license_id)


# ---------- 1) 参考集政策：开关在人手里 ----------

def test_policy_by_non_user_is_rejected():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.set_reference_policy(ident["id"], "hero_only", "system:auto")
    assert e.value.code == "ACTOR_NOT_USER"


def test_hero_only_without_full_acknowledgement_is_rejected():
    ident = _identity()
    partial = list(HERO_ONLY_LIMITATIONS)[:-1]
    with pytest.raises(AssetRejected) as e:
        registry.set_reference_policy(ident["id"], "hero_only", "user:op", acknowledged=partial)
    assert e.value.code == "REFERENCE_POLICY_NOT_ACKNOWLEDGED"
    assert HERO_ONLY_LIMITATIONS[-1] in e.value.message


def test_hero_only_without_reason_is_rejected():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.set_reference_policy(ident["id"], "hero_only", "user:op",
                                      acknowledged=list(HERO_ONLY_LIMITATIONS))
    assert e.value.code == "REFERENCE_POLICY_REASON_MISSING"


def test_unknown_mode_is_rejected():
    ident = _identity()
    with pytest.raises(AssetRejected) as e:
        registry.set_reference_policy(ident["id"], "auto_decide", "user:op")
    assert e.value.code == "REFERENCE_POLICY_INVALID"


def test_multi_view_needs_no_acknowledgement():
    ident = registry.set_reference_policy(_identity()["id"], "multi_view", "user:op")
    assert ident["reference_policy"]["mode"] == "multi_view"
    assert ident["reference_policy"]["declared_by"] == "user:op"


def test_policy_cannot_be_changed_after_freeze():
    ident = _identity()
    ident = _freeze_limited(ident["id"])
    with pytest.raises(AssetRejected) as e:
        registry.set_reference_policy(ident["id"], "multi_view", "user:op", reason="改口径")
    assert e.value.code == "IDENTITY_FROZEN"


def test_freeze_without_policy_is_rejected():
    """定版前必须先说清拿什么当"是同一个人"的依据，事后不许再改口径。"""
    ident = _identity()
    hero = _hero(ident["id"])
    with pytest.raises(AssetRejected) as e:
        registry.freeze_identity(ident["id"], hero["id"], DIGEST, reviewer="user:op")
    assert e.value.code == "REFERENCE_POLICY_MISSING"


# ---------- 2) 生产准入：两条路各管各的证据 ----------

def test_hero_only_identity_is_ready_without_reference_photos():
    lic = _commercial_license()
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is True, f"受限路径不应再要求 4 张参考图：{reasons}"
    assert not any("参考图" in r for r in reasons)


def test_multi_view_still_requires_four_references():
    """受限路径不能顺带把常规路径的门槛也降下来。"""
    lic = _commercial_license()
    ident = _identity()
    hero = _hero(ident["id"])
    registry.set_reference_policy(ident["id"], "multi_view", "user:op")
    ident = registry.freeze_identity(ident["id"], hero["id"], DIGEST,
                                     reviewer="user:op", license_id=lic["id"])
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("参考图不足 4 张" in r for r in reasons)


def test_system_declared_policy_is_not_honored():
    """绕过端点直接写库也算数核验：不是人声明的受限路径不认。"""
    lic = _commercial_license()
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    row = st.get("identities", ident["id"])
    row["reference_policy"]["declared_by"] = "system:auto"
    st.put("identities", ident["id"], row)
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("须由人声明" in r for r in reasons)


def test_partially_acknowledged_policy_blocks_production():
    lic = _commercial_license()
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    row = st.get("identities", ident["id"])
    row["reference_policy"]["acknowledged"] = [HERO_ONLY_LIMITATIONS[0]]
    st.put("identities", ident["id"], row)
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("限制未逐条确认" in r for r in reasons)


def test_existing_references_must_all_be_reviewed_even_on_hero_only():
    """受限路径不禁止补齐参考图，但一旦有就必须都审过，不许留一堆没结论的混着。"""
    lic = _commercial_license()
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    registry.register_asset(identity_id=ident["id"], kind="reference",
                            storage_uri="assets/g/ref1.png", file_hash=OTHER_HASH,
                            source_evidence="生成服务产出")
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("未通过审核的参考图" in r for r in reasons)


def test_missing_policy_blocks_production():
    lic = _commercial_license()
    ident = _identity()
    hero = _hero(ident["id"])
    row = st.get("identities", ident["id"])
    st.put("identities", ident["id"], row)
    # 跳过政策声明直接把身份标成已定版（模拟老数据），不得被默认放行
    row["approval_state"] = "approved"
    row["frozen"] = True
    row["hero_asset_id"] = hero["id"]
    row["identity_digest"] = DIGEST
    row["license_id"] = lic["id"]
    st.put("identities", ident["id"], row)
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("政策未声明" in r for r in reasons)


# ---------- 3) 商用范围：人确认，不是系统核验 ----------

def test_attest_by_non_user_is_rejected():
    lic = licensing.register(subject="AI 生成人物", kind="image_source")
    with pytest.raises(LicenseBlocked) as e:
        licensing.attest_commercial(lic["id"], "system:auto", "服务条款允许商用",
                                    licensing.ATTEST_TOKEN)
    assert e.value.code == "ACTOR_NOT_USER"


def test_attest_with_stale_token_is_rejected():
    lic = licensing.register(subject="AI 生成人物", kind="image_source")
    with pytest.raises(LicenseBlocked) as e:
        licensing.attest_commercial(lic["id"], "user:op", "服务条款允许商用",
                                    "COMMERCIAL_ATTEST_V0")
    assert e.value.code == "LICENSE_ATTEST_NOT_ACKNOWLEDGED"


def test_attest_without_basis_is_rejected():
    lic = licensing.register(subject="AI 生成人物", kind="image_source")
    with pytest.raises(LicenseBlocked) as e:
        licensing.attest_commercial(lic["id"], "user:op", "   ", licensing.ATTEST_TOKEN)
    assert e.value.code == "LICENSE_ATTEST_BASIS_MISSING"


def test_attest_cannot_overturn_a_blocked_license():
    lic = licensing.register(subject="禁用素材", kind="image_source", scope="blocked",
                             verified_by="user:op", evidence="来源不明")
    with pytest.raises(LicenseBlocked) as e:
        licensing.attest_commercial(lic["id"], "user:op", "我觉得可以", licensing.ATTEST_TOKEN)
    assert e.value.code == "LICENSE_BLOCKED"


def test_attest_unblocks_production_and_keeps_the_original_wording():
    original = "依生成服务条款使用，未取得书面法律意见；商用范围待核验"
    lic = licensing.register(subject="AI 生成人物", kind="image_source", evidence=original)
    with pytest.raises(LicenseBlocked):
        licensing.require(lic["id"], subject="AI 生成人物")

    rec = licensing.attest_commercial(lic["id"], "user:operator",
                                      "该生成服务条款允许商用", licensing.ATTEST_TOKEN)

    # 人在 -> 放行；但"核验"这件事没有发生，原句一个字都不能少
    assert rec["scope"] == "commercial_ok"
    assert rec["verified_by"] == "user:operator"
    assert original in rec["evidence"], "原证据被改写等于伪造核验"
    assert "user:operator 于" in rec["evidence"]
    assert "由人确认，非法律意见" in rec["evidence"]
    assert "不代表已取得书面法律意见" in rec["evidence"]
    assert licensing.require(lic["id"], subject="AI 生成人物")["id"] == lic["id"]


def test_revoke_returns_to_unverified_and_keeps_history():
    lic = licensing.register(subject="AI 生成人物", kind="image_source", evidence="原始记载")
    licensing.attest_commercial(lic["id"], "user:op", "条款允许", licensing.ATTEST_TOKEN)
    rec = licensing.revoke_commercial(lic["id"], "user:op", "条款有变化")
    assert rec["scope"] == "unverified"
    assert rec["verified_by"] == ""
    assert "原始记载" in rec["evidence"], "撤销不得倒回去抹历史"
    assert "撤销上述人工确认" in rec["evidence"]
    with pytest.raises(LicenseBlocked) as e:
        licensing.require(lic["id"], subject="AI 生成人物")
    assert e.value.code == "LICENSE_UNVERIFIED"


def test_revoke_by_non_user_is_rejected():
    lic = licensing.register(subject="AI 生成人物", kind="image_source")
    licensing.attest_commercial(lic["id"], "user:op", "条款允许", licensing.ATTEST_TOKEN)
    with pytest.raises(LicenseBlocked) as e:
        licensing.revoke_commercial(lic["id"], "system:auto")
    assert e.value.code == "ACTOR_NOT_USER"


def test_generated_license_still_blocks_before_attest():
    """AI 生成图默认登记为 unverified：这一步没有被这条新路径绕过。"""
    lic = licensing.register(subject="AI 生成人物", kind="image_source", scope="unverified")
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    ok, reasons = registry.production_ready(ident["id"])
    assert ok is False
    assert any("权属未核验" in r for r in reasons)


# ---------- 4) HTTP 层：受限身份的变体不能另起一张脸 ----------

@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _work_with_identity(identity_id: str) -> str:
    wid = st.new_id("work")
    st.put("works", wid, {"id": wid, "name": "测试作品", "brief": {},
                          "identity_id": identity_id, "route": "M2"})
    return wid


def test_variant_of_limited_identity_must_derive_from_hero(http):
    lic = _commercial_license()
    ident = _identity()
    ident = _freeze_limited(ident["id"], lic["id"])
    wid = _work_with_identity(ident["id"])

    another = registry.register_asset(identity_id=ident["id"], kind="variant",
                                      storage_uri="assets/g/another_face.png",
                                      file_hash=OTHER_HASH,
                                      source_evidence="另一次文生图产出")
    r = http.post(f"/api/v1/works/{wid}/variants", json={
        "outfit": "news_suit", "background": "studio",
        "storage_uri": "assets/g/var.png", "file_hash": OTHER_HASH,
        "source_evidence": "确定性合成派生", "derived_from": another["id"],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VARIANT_DERIVATION_MISMATCH"


def test_variant_of_limited_identity_derived_from_hero_is_accepted(http):
    lic = _commercial_license()
    ident = _identity()
    frozen = _freeze_limited(ident["id"], lic["id"])
    wid = _work_with_identity(frozen["id"])

    r = http.post(f"/api/v1/works/{wid}/variants", json={
        "outfit": "news_suit", "background": "studio",
        "storage_uri": "assets/g/var.png", "file_hash": OTHER_HASH,
        "source_evidence": "由定妆原图确定性合成派生",
        "derived_from": frozen["hero_asset_id"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["identity_id"] == frozen["id"]


def test_reference_policy_endpoint_rejects_system_actor(http):
    ident = _identity()
    r = http.post(f"/api/v1/identities/{ident['id']}/reference-policy",
                  json={"mode": "hero_only", "reviewer": "system:auto",
                        "reason": "x", "acknowledged": list(HERO_ONLY_LIMITATIONS)})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ACTOR_NOT_USER"


def test_attest_endpoint_states_it_is_not_a_legal_opinion(http):
    """返回值必须照抄"非法律意见"的措辞，前端不许改写成"已核验"。"""
    lic = licensing.register(subject="AI 生成人物", kind="image_source",
                             evidence="依生成服务条款使用，未取得书面法律意见")
    r = http.post(f"/api/v1/avatar-licenses/{lic['id']}/attest",
                  json={"reviewer": "user:operator", "basis": "生成服务条款允许商用",
                        "ack_token": licensing.ATTEST_TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["license"]["scope"] == "commercial_ok"
    assert "非法律意见" in body["attest_note"]
    assert "未取得书面法律意见" in body["license"]["evidence"]


def test_readiness_endpoint_reports_the_reference_policy(http):
    """走受限路径的身份，缺参考图是已知代价而不是待办，必须能被前端识别出来。"""
    lic = _commercial_license()
    ident = _identity()
    frozen = _freeze_limited(ident["id"], lic["id"])
    r = http.get(f"/api/v1/identities/{frozen['id']}/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["limited_reference"] is True
    assert body["reference_policy"]["mode"] == "hero_only"
    assert body["reference_policy"]["declared_by"] == "user:operator"
    assert not any("参考图" in x for x in body["reasons"])


def test_readiness_of_multi_view_identity_is_not_marked_limited(http):
    ident = _identity()
    registry.set_reference_policy(ident["id"], "multi_view", "user:op")
    r = http.get(f"/api/v1/identities/{ident['id']}/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["limited_reference"] is False
    assert body["reference_policy"]["mode"] == "multi_view"


# ---------- 5) HTTP 层：作品绑定的变体必须出现在变体列表里 ----------

def test_bound_variant_appears_in_work_variant_list(http):
    """底片真正会用的那条变体，不能因为 work_id 列没回填就从列表里消失。

    这是最坏的一种界面：列表里只有两条用不了的占位，真正在用的那条看不见，
    报错（VARIANT_NOT_APPROVED / IDENTITY_GATE_REJECTED）时用户无从对照。
    """
    lic = _commercial_license()
    ident = _freeze_limited(_identity()["id"], lic["id"])
    wid_a = _work_with_identity(ident["id"])
    wid_b = _work_with_identity(ident["id"])

    created = http.post(f"/api/v1/works/{wid_a}/variants", json={
        "outfit": "news_suit", "background": "studio",
        "storage_uri": "assets/g/var.png", "file_hash": OTHER_HASH,
        "source_evidence": "由定妆原图确定性合成派生",
        "derived_from": ident["hero_asset_id"],
    })
    assert created.status_code == 200, created.text
    vid = created.json()["id"]

    # 把作品 B 绑到该变体上（变体自身的 work_id 仍指向作品 A —— 历史登记就是这样）
    work_b = st.get("works", wid_b)
    work_b["variant_id"] = vid
    st.put("works", wid_b, work_b)

    rows = http.get(f"/api/v1/works/{wid_b}/variants").json()
    assert any(r["id"] == vid for r in rows), "绑定中的变体必须出现在列表里"
    # 有真实变体时不该再塞占位预览
    assert not any((r.get("note") or "").startswith("占位") for r in rows)


# ---------- 5) HTTP 层：作品绑定的变体必须出现在变体列表里 ----------

def test_bound_variant_appears_in_work_variant_list(http):
    """底片真正会用的那条变体，不能因为 work_id 列没回填就从列表里消失。

    这是最坏的一种界面：列表里只有两条用不了的占位，真正在用的那条看不见，
    报错（VARIANT_NOT_APPROVED / IDENTITY_GATE_REJECTED）时用户无从对照。
    """
    lic = _commercial_license()
    ident = _freeze_limited(_identity()["id"], lic["id"])
    wid_a = _work_with_identity(ident["id"])
    wid_b = _work_with_identity(ident["id"])

    created = http.post(f"/api/v1/works/{wid_a}/variants", json={
        "outfit": "news_suit", "background": "studio",
        "storage_uri": "assets/g/var.png", "file_hash": OTHER_HASH,
        "source_evidence": "由定妆原图确定性合成派生",
        "derived_from": ident["hero_asset_id"],
    })
    assert created.status_code == 200, created.text
    vid = created.json()["id"]

    # 把作品 B 绑到该变体上（变体自身的 work_id 仍指向作品 A —— 历史登记就是这样）
    work_b = st.get("works", wid_b)
    work_b["variant_id"] = vid
    st.put("works", wid_b, work_b)

    rows = http.get(f"/api/v1/works/{wid_b}/variants").json()
    assert any(r["id"] == vid for r in rows), "绑定中的变体必须出现在列表里"
    # 有真实变体时不该再塞占位预览
    assert not any((r.get("note") or "").startswith("占位") for r in rows)
