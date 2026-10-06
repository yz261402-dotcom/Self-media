"""M2 动作意图的**写入方**（回归：门禁在读一个永远为空的作品字段）。

`create_plate` 是从作品上读 `work["motion_intent"]` 的，而在加上这个端点之前
全后端没有任何地方写它——界面上那个"动作模板"下拉框只绑了本地变量，连请求都不发。
夹具则直接往字典里塞（`dh_helpers.prepare_work` 里的 `work["motion_intent"] = {...}`），
所以单元测试全绿、界面却必然卡在 `MOTION_REQUIRED`：**门禁没错，是缺了它的写入方**。

这组用例锁三件事：
1. 端点能落库，并把点名的 `motion_ref_id` 固化（同 key 的另一条参考不得顶替）；
2. 解析不过就拒收，不先存一个将来必然被拒的意图（先存后拒会把问题推迟到更贵的一步）；
3. 端到端：登记完动作意图后，建底片不再报 `MOTION_REQUIRED`。
"""
from __future__ import annotations

import pytest

from dh_helpers import (
    install_final_audio, install_identity, install_motion_ref, install_variant, new_work,
)
from app import storage as st  # noqa: E402


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _err(resp) -> str:
    body = resp.json()
    assert "error" in body, f"错误响应必须是 {{error: ...}} 结构：{body}"
    return body["error"]["code"]


def _work_with_ref(motion_key: str = "gesture_open") -> dict:
    install_identity()
    variant = install_variant()
    ref = install_motion_ref(motion_key=motion_key)
    work = new_work(route="M2")
    return {"work": work, "ref": ref, "variant": variant}


def test_motion_intent_is_persisted_and_pins_the_reference(http):
    ctx = _work_with_ref()
    work_id, ref = ctx["work"]["id"], ctx["ref"]

    resp = http.post(f"/api/v1/works/{work_id}/motion-intent",
                     json={"motion_key": ref["motion_key"], "motion_ref_id": ref["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["motion_intent"]["motion_key"] == "gesture_open"
    assert body["motion_intent"]["motion_ref_id"] == ref["id"]

    # 落库而不只是回显：底片是从库里读的
    stored = st.get("works", work_id)["motion_intent"]
    assert stored["motion_ref_id"] == ref["id"]


def test_intent_is_rejected_before_being_stored(http):
    """先解析再落库：拒收时不得留下"看起来已选好"的作品状态。"""
    ctx = _work_with_ref()
    work_id = ctx["work"]["id"]
    assert (st.get("works", work_id).get("motion_intent") or {}) == {}

    resp = http.post(f"/api/v1/works/{work_id}/motion-intent", json={"motion_key": "nod"})
    assert resp.status_code == 422
    assert _err(resp) == "MOTION_NOT_APPROVED"
    assert (st.get("works", work_id).get("motion_intent") or {}) == {}


def test_m2_rejects_free_text_intent(http):
    ctx = _work_with_ref()
    resp = http.post(f"/api/v1/works/{ctx['work']['id']}/motion-intent",
                     json={"motion_key": "gesture_open", "free_text": "随便挥挥手"})
    assert resp.status_code == 422
    assert _err(resp) == "MOTION_FREETEXT_UNSUPPORTED"


def test_unknown_key_is_rejected(http):
    ctx = _work_with_ref()
    resp = http.post(f"/api/v1/works/{ctx['work']['id']}/motion-intent",
                     json={"motion_key": "随便跳个舞"})
    assert resp.status_code == 422
    assert _err(resp) == "MOTION_UNSUPPORTED"


def test_intent_requires_identity(http):
    work = new_work(route="M2")
    st.put("works", work["id"], {**st.get("works", work["id"]), "identity_id": None})
    resp = http.post(f"/api/v1/works/{work['id']}/motion-intent",
                     json={"motion_key": "gesture_open"})
    assert resp.status_code == 422
    assert _err(resp) == "SELECTION_REQUIRED"


def test_plate_no_longer_reports_motion_required(http):
    """这条才是整件事的目的：门禁读的那个字段，现在有写入方了。"""
    ctx = _work_with_ref()
    work_id = ctx["work"]["id"]
    work = st.get("works", work_id)
    # 用**本次夹具刚建的那条**变体，而不是 `st.all_rows("variants")[0]`：
    # 全量跑时整库是共享的，别的测试文件也会往 variants 里写（且属于别的人物），
    # 取表的"第一条"会随机拿到别人的变体 → VARIANT_IDENTITY_MISMATCH，
    # 表现为"换个运行顺序就红"。夹具返回什么就用什么，别依赖全表顺序。
    work["variant_id"] = ctx["variant"]["id"]
    st.put("works", work_id, work)
    install_final_audio(work_id)

    # 登记动作意图（走的正是界面会打的那个端点）
    ok = http.post(f"/api/v1/works/{work_id}/motion-intent",
                   json={"motion_key": ctx["ref"]["motion_key"]})
    assert ok.status_code == 200, ok.text

    resp = http.post(f"/api/v1/works/{work_id}/talking-head/plate",
                     json={"output_seconds": 9, "dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("motion_ref_id") == ctx["ref"]["id"]
    assert body.get("variant_id") == work["variant_id"]

    # 反例：清掉意图后应回到 MOTION_REQUIRED——证明上面的通过是因为登记了动作，
    # 不是因为门禁被放宽
    work = st.get("works", work_id)
    work["motion_intent"] = {}
    st.put("works", work_id, work)
    again = http.post(f"/api/v1/works/{work_id}/talking-head/plate",
                      json={"output_seconds": 9, "dry_run": True})
    assert again.status_code == 422
    assert _err(again) == "MOTION_REQUIRED"
