"""写入方与契约必须同步（2026-09-25 线上故障回归）。

故障：`freeze_identity` 会写 `reviewed_by`，但 `AvatarIdentity` 契约里没有该字段。
StrictModel 的 extra_forbidden 于是拒绝整行 → `/api/v1/avatar-identities` 返回 500 →
前端 bootstrap 在这一步中断，`apiReady` 永远为 false，页面一直显示"正在连接后端"。

根因不是脏数据，而是**写入方写了契约没声明的字段**。这类问题只有当某个端点对
整表做反序列化时才暴露，平时测不到。这里钉住两处已知写入点：身份定版、音源权属审核。

同理暴露另一个隐患：`/api/v1/*` 的全表反序列化端点对**任何一行**脏/缺字段都整体失败，
而不是跳过问题行。测试因此直接走端点级校验，而不是只验模型能构造。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app.contracts import AvatarIdentity, SourceRecord  # noqa: E402
from app import storage as st  # noqa: E402
from app.avatar import registry  # noqa: E402

HASH = "sha256:" + "a" * 64
DIGEST = "sha256:" + "c" * 64


def _new_identity(name: str = "契约回归人物") -> dict:
    return registry.create_identity_candidate(name=name, persona="测试用途")


def test_frozen_identity_row_is_loadable_by_contract():
    """定版写入的字段必须都在 AvatarIdentity 契约里，且审核人留痕不被丢弃。"""
    ident = _new_identity()
    hero = registry.register_asset(
        ident["id"], kind="hero", storage_uri="runtime/assets/hero.png",
        file_hash=HASH, source_evidence="本项目自有素材，授权自持")
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    registry.set_reference_policy(ident["id"], "multi_view", "user:tester",
                                  reason="夹具构造：按多角度参考集路径")
    frozen = registry.freeze_identity(ident["id"], hero["id"], DIGEST, reviewer="tester")
    row = st.get("identities", frozen["id"])
    AvatarIdentity(**row)  # 政策写入后身份行必须仍能被契约反序列化

    loaded = AvatarIdentity(**st.get("identities", frozen["id"]))
    assert loaded.reviewed_by == "tester", "定版审核人必须能被契约读出，不能静默丢弃"
    assert loaded.frozen is True


def test_all_identity_rows_loadable_by_contract():
    """`/avatar-identities` 是整表反序列化：任何一行不通，端点就整体 500。"""
    ident = _new_identity()
    hero = registry.register_asset(
        ident["id"], kind="hero", storage_uri="runtime/assets/hero.png",
        file_hash=HASH, source_evidence="本项目自有素材，授权自持")
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    registry.set_reference_policy(ident["id"], "multi_view", "user:tester",
                                  reason="夹具构造：按多角度参考集路径")
    registry.freeze_identity(ident["id"], hero["id"], DIGEST, reviewer="tester")

    rows = st.all_rows("identities")
    assert rows, "夹具未写入任何身份"
    # 不捕获任何异常：这里一旦抛 ValidationError，就是端点 500 的那个故障
    loaded = [AvatarIdentity(**r) for r in rows]
    assert {i.id for i in loaded} >= {ident["id"]}


def test_source_review_fields_loadable_by_contract():
    """音源权属审核写入 reviewed_by/-reviewed_at，契约同样必须容纳。"""
    rec = SourceRecord(id="src_contract_sync", subject="voice_test",
                       source_type="project_generated")
    st.put("source_records", rec.id, rec.model_dump(), {"subject": rec.subject})
    row = st.get("source_records", rec.id)
    row["reviewed_by"] = "tester"
    row["reviewed_at"] = "2026-09-25T00:00:00+00:00"
    row["license_review_state"] = "reviewed"
    st.put("source_records", rec.id, row, {"subject": row["subject"]})

    loaded = SourceRecord(**st.get("source_records", rec.id))
    assert loaded.reviewed_by == "tester", "音源审核人必须能被契约读出"
