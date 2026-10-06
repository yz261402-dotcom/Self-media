"""定版四步的进度口径（`registry.qualify_steps`）。

为什么需要这组用例：
  界面上「选用」与「审核 / 定版」是两个并排的按钮、四步是四块同质的表单，
  用户既看不出二者的**前提 / 引用**关系，也看不出自己停在哪一步、下一步点哪个。
  修法不是再补一段说明文字，而是把顺序、完成状态与"这一步为什么必须由人做"
  统一由后端给出，前端只渲染。

  因此这里钉的不是文案好不好看，而是三类会直接导致"点了必被拒"的问题：

  ① 步骤顺序固定，每一步都带非空 `why` —— 界面直接渲染该字段，留空就等于
     用户眼前一片空白，"没提示"的老问题原地复发；
  ② 进度必须来自**真实留痕**：审核结论要 pass、受限路径的限制要逐条确认、
     原图要挂回身份。任一项缺失都要体现在 `done_count` / `current` 上，
     否则界面会把"还没做的事"显示成已完成，比不显示更危险；
  ③ **冻结即链路闭合**：冻结后口径不可再声明、不再接受新的基准，未完成项
     此后不可能补齐，因此必须给出 `current = None`，否则界面会提示一个点了
     必定被拒的动作。早期直接落库冻结的身份（无逐条留痕）正是这种情况，
     如实标记 `closed_incomplete`，让人自己决定要不要新建版本重走。

另有一条与界面直接相关的断言：**已定版 ≠ 可生产**。播种的预设
`av_base_b_v1` 的 `approval_state` 就是 approved，但就绪清单不为空。
界面若按 `approval_state` 单独判定并显示"可用于生产"，用户会以为点了选用就能出片，
实际一提交生产就被拒——这正是"界面能用、实际不能用"的来源。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import licensing, registry  # noqa: E402
from app.contracts import HERO_ONLY_LIMITATIONS  # noqa: E402

HASH = "sha256:" + "d" * 64
DIGEST = "sha256:" + "e" * 64
OPERATOR = "user:qualify-reviewer"

STEP_ORDER = ["review", "reference_policy", "license_attest", "freeze"]
PRESET_APPROVED = "av_base_b_v1"  # storage 播种的已定版预设


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


# ---------- 夹具 ----------

def _link_hero(identity_id: str, asset_id: str) -> None:
    """把原图挂回身份。

    这一步不是可选的：`qualify_steps` 与 `production_ready` 都从身份上的
    `hero_asset_id` 找原图。只登记资产而不挂回，资产就是孤儿——资产表里有图、
    身份上却是空的，就绪清单永远报"缺少定妆原片"。真实写入方见
    `person_image.py` 出图后的 `ident["hero_asset_id"] = asset_id`。
    """
    ident = dict(registry.get_identity(identity_id) or {})
    ident["hero_asset_id"] = asset_id
    st.put("identities", identity_id, ident)


def _candidate_with_license() -> tuple:
    """建一个"生成服务产出"形态的候选：身份自带一条待核验权属记录。

    这条记录由 `person_image.register_generated_license` 在出图时写入，
    这里按同样的形态直接落库，避免测试真的去调文生图。
    """
    ident = registry.create_identity_candidate(name="定版进度夹具", persona="生成服务产出",
                                               keywords={"gender": "女"})
    lic = licensing.register(subject=ident["name"], kind="image_source", scope="unverified",
                             evidence="生成服务产出，尚待人工确认商用范围")
    ident = dict(ident)
    ident["license_id"] = lic["id"]
    st.put("identities", ident["id"], ident)
    return ident, lic


def _hero(identity_id: str, license_id: str = "", outcome: str = "pass") -> dict:
    hero = registry.register_asset(
        identity_id=identity_id, kind="hero", storage_uri="assets/q/hero.png",
        file_hash=HASH, source_evidence="生成服务产出", license_id=license_id,
    )
    registry.record_review(hero["id"], reviewer=OPERATOR, outcome=outcome,
                           identity_digest=DIGEST, method="人工目检：与期望人设一致")
    _link_hero(identity_id, hero["id"])
    return hero


def _declare_limited(identity_id: str, acknowledged: list | None = None) -> dict:
    return registry.set_reference_policy(
        identity_id=identity_id, mode="hero_only", operator=OPERATOR,
        reason="整张脸由文生图服务产出，不存在同一人的第二张真值",
        acknowledged=list(HERO_ONLY_LIMITATIONS) if acknowledged is None else acknowledged,
    )


def _attest(license_id: str) -> dict:
    return licensing.attest_commercial(
        license_id=license_id, attestor=OPERATOR,
        basis="生成服务条款允许商用", ack_token=licensing.ATTEST_TOKEN,
    )


def _early_frozen_without_chain() -> str:
    """构造"四步流程建立之前就被直接冻结"的身份。

    `freeze_identity` 强制要求口径与审核留痕，所以这种状态**只能来自直接落库的
    历史写入**（运行库里的 `av_base_b_v1` 就是这样：frozen 为真、没有
    reference_policy、原图审核结论是 review 而非 pass）。正因为无法事后补齐，
    才必须如实标记而不是替它补记录。
    """
    ident = registry.create_identity_candidate(name="早期冻结预设", persona="流程建立前登记",
                                               keywords={"gender": "女"})
    ident = dict(ident)
    ident["frozen"] = True
    ident["approval_state"] = "approved"
    st.put("identities", ident["id"], ident)
    return ident["id"]


# ---------- 1) 顺序与"为什么" ----------

def test_step_order_is_fixed():
    ident, _ = _candidate_with_license()
    q = registry.qualify_steps(ident["id"])
    assert [s["key"] for s in q["steps"]] == STEP_ORDER
    assert q["total"] == len(STEP_ORDER)


def test_every_step_carries_a_non_empty_reason():
    """`why` 是界面上唯一解释"这一步在干什么"的文字，留空 = 没提示。"""
    ident, _ = _candidate_with_license()
    q = registry.qualify_steps(ident["id"])
    for s in q["steps"]:
        assert s["label"].strip(), f"步骤 {s['key']} 缺少名称"
        assert len(s["why"].strip()) >= 20, f"步骤 {s['key']} 的说明过短，等于没解释"


def test_unknown_identity_reports_zero_progress_without_raising():
    """不存在的人物不能让整个就绪清单接口炸掉（列表页会整屏 500）。"""
    q = registry.qualify_steps("av_does_not_exist")
    assert q["done_count"] == 0
    assert q["current"] == "review"


# ---------- 2) 进度来自真实留痕 ----------

def test_progress_advances_only_with_real_evidence():
    ident, lic = _candidate_with_license()
    iid = ident["id"]

    q = registry.qualify_steps(iid)
    assert (q["done_count"], q["current"]) == (0, "review")

    hero = _hero(iid, lic["id"])
    q = registry.qualify_steps(iid)
    assert (q["done_count"], q["current"]) == (1, "reference_policy"), "审核记录未生效"

    _declare_limited(iid)
    q = registry.qualify_steps(iid)
    assert (q["done_count"], q["current"]) == (2, "license_attest"), "口径声明未生效"

    _attest(lic["id"])
    q = registry.qualify_steps(iid)
    assert (q["done_count"], q["current"]) == (3, "freeze"), "权属确认未生效"

    registry.freeze_identity(iid, hero["id"], DIGEST, reviewer=OPERATOR,
                             license_id=lic["id"])
    q = registry.qualify_steps(iid)
    assert q["done_count"] == 4
    assert q["frozen"] is True
    assert q["closed_incomplete"] is False


def test_hero_not_linked_to_identity_does_not_count():
    """只登记资产、没挂回身份：资产是孤儿，第 1 步不能算完成。

    这条钉住的是"资产表里有图，身份上却是空的"这种半完成状态——运行库里
    确实出现过（因此另有一个补挂脚本）。若把它当成已完成，界面会显示
    "审核已过"，而生产时会报"缺少定妆原片资产"。
    """
    ident, lic = _candidate_with_license()
    hero = registry.register_asset(
        identity_id=ident["id"], kind="hero", storage_uri="assets/q/hero.png",
        file_hash=HASH, source_evidence="生成服务产出", license_id=lic["id"],
    )
    registry.record_review(hero["id"], reviewer=OPERATOR, outcome="pass",
                           identity_digest=DIGEST, method="人工目检")
    q = registry.qualify_steps(ident["id"])
    assert q["done_count"] == 0
    assert q["current"] == "review"


def test_failed_review_does_not_count_as_done():
    """outcome 不是 pass 的审核结论是"没通过"，不能算完成第 1 步。"""
    ident, lic = _candidate_with_license()
    _hero(ident["id"], lic["id"], outcome="fail")
    q = registry.qualify_steps(ident["id"])
    assert q["done_count"] == 0
    assert q["current"] == "review"


def test_limited_path_needs_full_acknowledgement_to_count():
    """受限路径的限制必须逐条确认：部分确认被直接拒收，不落成"半完成"状态。

    挡住而不是记成"还没做完"，是因为一条只认了三分之二的依据口径会永久留在库里，
    后面没人分得清那三分之一是"当时没勾"还是"勾了没存"。所以第 2 步要么完整成立、
    要么根本不存在——`qualify_steps` 因此不会看到中间态。
    """
    ident, lic = _candidate_with_license()
    iid = ident["id"]
    _hero(iid, lic["id"])

    with pytest.raises(registry.AssetRejected) as e:
        _declare_limited(iid, acknowledged=list(HERO_ONLY_LIMITATIONS)[:-1])
    assert e.value.code == "REFERENCE_POLICY_NOT_ACKNOWLEDGED"

    q = registry.qualify_steps(iid)
    assert q["done_count"] == 1
    assert q["current"] == "reference_policy"


# ---------- 3) 冻结即链路闭合 ----------

def test_frozen_identity_offers_no_next_step():
    """冻结后不再给"下一步"：未完成项此后不可能补齐，提示了也只会被拒。"""
    ident, lic = _candidate_with_license()
    iid = ident["id"]
    hero = _hero(iid, lic["id"])
    _declare_limited(iid)
    _attest(lic["id"])
    registry.freeze_identity(iid, hero["id"], DIGEST, reviewer=OPERATOR, license_id=lic["id"])

    q = registry.qualify_steps(iid)
    assert q["frozen"] is True
    assert q["current"] is None
    assert q["closed_incomplete"] is False


def test_frozen_without_chain_is_flagged_instead_of_silently_padded():
    """早期冻结、留痕不全：如实标记，不替它补记录，也不给无法执行的"下一步"。"""
    iid = _early_frozen_without_chain()
    q = registry.qualify_steps(iid)
    assert q["frozen"] is True
    assert q["closed_incomplete"] is True
    assert q["done_count"] < q["total"]
    assert q["current"] is None, "链路已闭合，不能提示一个无法执行的动作"

    # 两份结论不能互相打架：既然留痕不全，就同样进不了生产
    ok, reasons = registry.production_ready(iid)
    assert ok is False and reasons


# ---------- 4) 已定版 ≠ 可生产 ----------

def test_approved_preset_is_not_automatically_production_ready():
    """播种预设的 approval_state 就是 approved，但就绪清单不为空。

    界面按 approval_state 单独判定并写"可用于生产"，就会让用户以为点了选用
    就能出片；实际生产前置检查会逐条拒。定版（可选用）与就绪（可生产）
    必须分列显示。
    """
    ident = registry.get_identity(PRESET_APPROVED)
    assert ident and ident["approval_state"] == "approved"

    ok, reasons = registry.production_ready(PRESET_APPROVED)
    assert ok is False, "预设若真的全部就绪，这条用例需要重新评估界面分组"
    assert reasons


def test_unfrozen_identity_cannot_be_selected_by_a_work(http):
    """「选用」的门槛是已定版：未定版的候选连选用都进不去。"""
    ident, _ = _candidate_with_license()
    work = http.post("/api/v1/works", json={"name": "定版前提验证", "topic": "定版是选用的前提"})
    assert work.status_code == 200, work.text
    wid = work.json()["id"]

    r = http.post(f"/api/v1/works/{wid}/identity", json={"identity_id": ident["id"]})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ASSET_NOT_APPROVED"


def test_readiness_endpoints_expose_the_same_progress(http):
    """单条与批量两个端点必须给出同一份进度，否则列表与面板会各说各话。"""
    ident, _ = _candidate_with_license()
    iid = ident["id"]

    single = http.get(f"/api/v1/identities/{iid}/readiness")
    assert single.status_code == 200, single.text
    assert single.json()["qualify"]["done_count"] == 0

    batch = http.get("/api/v1/avatar-identities/readiness")
    assert batch.status_code == 200, batch.text
    assert batch.json()[iid]["qualify"]["done_count"] == 0

    # 推进一步后两边要同时变，不能一个动一个不动
    _hero(iid)
    assert http.get(f"/api/v1/identities/{iid}/readiness").json()["qualify"]["done_count"] == 1
    assert http.get("/api/v1/avatar-identities/readiness").json()[iid]["qualify"]["done_count"] == 1
