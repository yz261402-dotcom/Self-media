"""人物草稿候选的「丢弃 / 恢复」（HTTP 级）。

为什么需要这组用例：
  界面上人物库把候选平铺出来，既没有出口、也分不清哪个能用，用户被一堆
  "待审"候选困住。但规格明确要求资产/授权/审核记录长期保存（"不删除失败证据"、
  "资产/授权/发布回执长期保存"），**所以不做物理删除**；能力做成
  "移出工作列表 + 记录谁/何时/为什么"，并且可恢复。

这组用例钉住三件事：
  ① 丢弃只改可见性与丢弃元数据，**原有字段一字不改**；
  ② 已定版、或已被作品 / 变体 / 动作参考引用的候选**拒绝丢弃**，并逐条说明原因
     （宁可挡住，也不做会静默改变别人已确认内容的级联清理）；
  ③ 只有人能丢弃：actor 须以 user 开头，且必须写明理由。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402

FIXTURE_USER = "user:fixture-reviewer"
PRESET_APPROVED = "av_base_b_v1"  # storage 播种的已定版预设，用于"不能丢弃"的反例


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _new_candidate(http, appearance: str = "测试用描述") -> dict:
    r = http.post("/api/v1/avatar-identities/candidates",
                  json={"gender": "女", "age": "22 岁左右", "appearance": appearance})
    assert r.status_code == 200, r.text
    return r.json()[0]


def _discard(http, identity_id: str, reason: str = "试跑出来的草稿，不要了",
             actor: str = FIXTURE_USER):
    return http.post(f"/api/v1/avatar-identities/{identity_id}/discard",
                     json={"actor": actor, "reason": reason})


def _visible_ids(http, include_discarded: bool = False) -> list:
    q = "?include_discarded=true" if include_discarded else ""
    r = http.get(f"/api/v1/avatar-identities{q}")
    assert r.status_code == 200, r.text
    return [i["id"] for i in r.json()]


# ---------------------------------------------------------------- 丢弃

def test_discard_hides_candidate_from_default_list(http):
    """丢弃 = 移出工作列表，不是删除记录。"""
    cand = _new_candidate(http)
    assert cand["id"] in _visible_ids(http)

    r = _discard(http, cand["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discarded_at"]
    assert body["discarded_by"] == FIXTURE_USER
    assert body["discard_reason"] == "试跑出来的草稿，不要了"

    # 默认列表看不到；显式索取仍在列——记录没被删掉
    assert cand["id"] not in _visible_ids(http)
    assert cand["id"] in _visible_ids(http, include_discarded=True)


def test_discard_leaves_original_fields_untouched(http):
    """丢弃只追加丢弃元数据。人设、备注、来源标记都必须原样。"""
    cand = _new_candidate(http, appearance="蓝色毛衣")
    before = st.get("identities", cand["id"])
    assert _discard(http, cand["id"]).status_code == 200
    after = st.get("identities", cand["id"])

    touched = {"discarded_at", "discarded_by", "discard_reason", "discard_history"}
    for k, v in before.items():
        if k in touched:
            continue  # 这四项本身就是丢弃要写的东西
        assert after[k] == v, f"丢弃不应改动原字段 {k}"
    assert set(after) - set(before) <= touched, "丢弃只允许新增丢弃元数据"


def test_discard_records_history_event(http):
    cand = _new_candidate(http)
    assert _discard(http, cand["id"]).status_code == 200
    hist = st.get("identities", cand["id"])["discard_history"]
    assert len(hist) == 1
    assert hist[0]["action"] == "discard"
    assert hist[0]["actor"] == FIXTURE_USER
    assert hist[0]["reason"] == "试跑出来的草稿，不要了"
    assert hist[0]["at"]


def test_discard_is_idempotent(http):
    """重复丢弃不算错，也不重复记事件（否则人会因为"多点了一下"而困惑）。"""
    cand = _new_candidate(http)
    assert _discard(http, cand["id"]).status_code == 200
    r2 = _discard(http, cand["id"])
    assert r2.status_code == 200, r2.text
    assert len(st.get("identities", cand["id"])["discard_history"]) == 1


# ---------------------------------------------------------------- 拒绝路径

def test_discard_requires_human_actor(http):
    """丢弃是"这个候选我不要了"的判断，系统不得替人做。"""
    cand = _new_candidate(http)
    r = _discard(http, cand["id"], actor="agent:avatar-skill")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ACTOR_NOT_HUMAN"
    assert cand["id"] in _visible_ids(http)


def test_discard_requires_reason(http):
    """理由必填：日后回看时"为什么丢的"比"丢了"更重要。"""
    cand = _new_candidate(http)
    r = _discard(http, cand["id"], reason="   ")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "REASON_REQUIRED"


def test_discard_rejected_for_approved_identity(http):
    """已定版是对外承诺，不能靠丢弃来"换人"。"""
    r = _discard(http, PRESET_APPROVED)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "IDENTITY_NOT_DISCARDABLE"
    assert "已定版" in err["message"]


def test_discard_rejected_when_referenced_by_work(http):
    """丢弃会让作品指向一个空人物，所以挡住并说清是哪个作品。"""
    cand = _new_candidate(http)
    r = http.post("/api/v1/works", json={"topic": "丢弃规则测试", "name": "丢弃规则测试"})
    assert r.status_code == 200, r.text
    work_id = r.json()["id"]
    # 夹具直接建立引用关系（选用端点只接受已定版人物，这里要构造的正是"被引用"状态）
    w = st.get("works", work_id)
    w["identity_id"] = cand["id"]
    st.put("works", work_id, w)

    r = _discard(http, cand["id"])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "IDENTITY_NOT_DISCARDABLE"
    assert "作品引用" in err["message"] and work_id in err["message"]
    assert cand["id"] in _visible_ids(http)


def test_discard_rejected_when_variant_exists(http):
    """变体建立在身份之上，丢身份会让变体失去归属。"""
    cand = _new_candidate(http)
    vid = st.new_id("var")
    st.put("variants", vid,
           {"id": vid, "identity_id": cand["id"], "work_id": "", "outfit": "x",
            "background": "y", "approval_state": "pending"},
           extra={"work_id": ""})

    r = _discard(http, cand["id"])
    assert r.status_code == 422, r.text
    assert "变体" in r.json()["error"]["message"]


def test_discard_unknown_identity_reports_not_found(http):
    r = _discard(http, "av_does_not_exist")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "IDENTITY_NOT_FOUND"


# ---------------------------------------------------------------- 恢复

def test_restore_returns_candidate_to_list(http):
    cand = _new_candidate(http)
    assert _discard(http, cand["id"]).status_code == 200

    r = http.post(f"/api/v1/avatar-identities/{cand['id']}/restore",
                  json={"actor": FIXTURE_USER})
    assert r.status_code == 200, r.text
    assert r.json()["discarded_at"] is None
    assert cand["id"] in _visible_ids(http)

    # 历史只追加：原丢弃理由仍然查得到
    hist = st.get("identities", cand["id"])["discard_history"]
    assert [h["action"] for h in hist] == ["discard", "restore"]
    assert hist[0]["reason"] == "试跑出来的草稿，不要了"


def test_restore_requires_human_actor(http):
    cand = _new_candidate(http)
    assert _discard(http, cand["id"]).status_code == 200
    r = http.post(f"/api/v1/avatar-identities/{cand['id']}/restore",
                  json={"actor": "agent:avatar-skill"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "ACTOR_NOT_HUMAN"


# ---------------------------------------------------------------- 就绪度

def test_readiness_batch_matches_single_endpoint(http):
    """批量就绪度必须与单条同一口径，否则界面显示的"还缺什么"会与门禁不一致。"""
    cand = _new_candidate(http)
    batch = http.get("/api/v1/avatar-identities/readiness")
    assert batch.status_code == 200, batch.text
    single = http.get(f"/api/v1/identities/{cand['id']}/readiness")
    assert single.status_code == 200, single.text

    assert cand["id"] in batch.json()
    assert batch.json()[cand["id"]]["reasons"] == single.json()["reasons"]
    assert batch.json()[cand["id"]]["ready"] == single.json()["ready"]


def test_readiness_batch_skips_discarded_by_default(http):
    cand = _new_candidate(http)
    assert _discard(http, cand["id"]).status_code == 200
    assert cand["id"] not in http.get("/api/v1/avatar-identities/readiness").json()
    with_disc = http.get("/api/v1/avatar-identities/readiness?include_discarded=true").json()
    assert cand["id"] in with_disc
