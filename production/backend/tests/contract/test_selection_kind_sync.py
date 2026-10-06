"""选择决定（T01）的写入方必须用契约里的 kind —— 线上 500 回归。

故障现象（2026-09-25，用户在前端点人物卡片上的「选用」）：
  接口返回 500 `Internal Server Error`（纯文本），前端 `res.json()` 于是抛
  `Unexpected token 'I', "Internal S"... is not valid JSON`。界面看到的是"解析失败"，
  跟真正的原因（后端契约校验失败）毫无字面联系。

根因：`select_identity` 调 `_record_selection(work_id, "avatar", ident["id"])`，
而 `SelectionKind = Literal["script","identity","variant","voice","audio","route"]`
里**没有 "avatar"**（选人物对应的字面量是 "identity"）。`SelectionOption` 校验失败抛
`ValidationError`，FastAPI 未捕获 → 500。

更麻烦的是写入顺序：当时是先改 `work.identity_id` 再写选择决定，
所以 500 之后**作品已经换人、却没有任何"谁选的、选了什么"的记录**——
而这张表存在的全部意义就是那条记录。现在改成先落决定、再改作品。

这组用例钉三件事：
1. 静态守门：`main.py` 里所有 `_record_selection(...)` 的 kind 字面量都必须在契约枚举内
   （这样"再写一个近义字面量"会在测试阶段就暴露，而不是等用户点出来）；
2. 端点回归：选已定版人物返回 200，并真的留下 kind="identity"、actor="user" 的决定；
3. 不留半成品：人物未定版时拒收，且作品不被改动、也不产生任何选择记录。
"""
from __future__ import annotations

import ast
import os
import sys
import typing

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import storage as st  # noqa: E402
from app.avatar import registry  # noqa: E402
from app.contracts import SelectionKind  # noqa: E402

MAIN_PY = os.path.join(ROOT, "app", "main.py")
HASH = "sha256:" + "a" * 64
DIGEST = "sha256:" + "c" * 64


@pytest.fixture()
def http():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _err(resp) -> str:
    body = resp.json()
    assert "error" in body, f"错误响应必须是 {{error: ...}} 结构：{body}"
    return body["error"]["code"]


# ---------------------------------------------------------------- 静态守门

def test_all_record_selection_call_sites_use_contract_kinds():
    """调用点写死的 kind 必须落在 `SelectionKind` 枚举里。

    用 AST 而不是正则：正则会把注释和字符串里的同名文本也算进去，
    而这条断言的语义是"传给函数的实参值"。
    """
    allowed = set(typing.get_args(SelectionKind))
    tree = ast.parse(open(MAIN_PY, encoding="utf-8").read(), filename=MAIN_PY)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "_record_selection":
            continue
        assert len(node.args) >= 3, "_record_selection 至少要有 work_id / kind / ref 三个实参"
        kind_arg = node.args[1]
        assert isinstance(kind_arg, ast.Constant) and isinstance(kind_arg.value, str), (
            f"第 {getattr(kind_arg, 'lineno', '?')} 行的 kind 必须是字面量常量，"
            "动态拼出来的 kind 无法被这条守门覆盖")
        found.append((kind_arg.value, kind_arg.lineno))

    assert found, "没找到任何 _record_selection 调用点：函数被改名或删除了？"
    bad = [(k, ln) for k, ln in found if k not in allowed]
    assert not bad, (
        f"kind 不在契约枚举 {sorted(allowed)} 内：{bad}。"
        "加字面量前先想清楚：契约枚举是审计口径，近义字面量会让同一件事有两个名字。")

    # 选人物这条路径必须真的存在（否则本用例会在"函数还在但没人调"时静默通过）
    assert "identity" in {k for k, _ in found}, "选人物不再写选择决定了？"


# ---------------------------------------------------------------- 端点回归

def _approved_identity() -> str:
    """走真实路径拿到一个已定版人物：原图 → 审核 pass → 声明口径 → 冻结。"""
    ident = registry.create_identity_candidate(name="选择回归人物", persona="测试用途")
    hero = registry.register_asset(
        ident["id"], kind="hero", storage_uri="runtime/assets/hero.png",
        file_hash=HASH, source_evidence="夹具构造：本项目自有素材")
    registry.record_review(hero["id"], reviewer="tester", outcome="pass",
                           identity_digest=DIGEST, method="人工对照")
    registry.set_reference_policy(ident["id"], "multi_view", "user:tester",
                                  reason="夹具构造：按多角度参考集路径")
    registry.freeze_identity(ident["id"], hero["id"], DIGEST, reviewer="tester")
    return ident["id"]


def _work(http) -> dict:
    resp = http.post("/api/v1/works", json={"topic": "选择决定回归"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_select_identity_returns_200_and_records_user_decision(http):
    """这条就是线上故障的复现：以前这里 500。"""
    ident_id = _approved_identity()
    work = _work(http)

    resp = http.post(f"/api/v1/works/{work['id']}/identity", json={"identity_id": ident_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["identity_id"] == ident_id

    decisions = [d for d in st.all_rows("selections") if d.get("work_id") == work["id"]]
    selected = [d for d in decisions if d.get("state") == "selected"]
    assert len(selected) == 1, f"应恰好留下 1 条选择决定，实际 {len(selected)}"
    assert selected[0]["kind"] == "identity"
    assert selected[0]["selected_ref"] == ident_id
    # 红线：选择只能由人做出，actor 恒为 user
    assert selected[0]["actor"] == "user"


def test_rejected_selection_leaves_no_half_state(http):
    """人物未定版时：拒收，且作品与选择记录都不许被改动。

    这条比"返回 4xx"更重要——真正伤人的是"作品已经换了、记录却没写"。
    """
    pending = registry.create_identity_candidate(name="未定版人物", persona="测试用途")
    work = _work(http)
    before_work = st.get("works", work["id"])
    before_decisions = len(st.all_rows("selections"))

    resp = http.post(f"/api/v1/works/{work['id']}/identity",
                     json={"identity_id": pending["id"]})
    assert resp.status_code == 422, resp.text
    assert _err(resp) == "ASSET_NOT_APPROVED"

    after_work = st.get("works", work["id"])
    assert after_work["identity_id"] == before_work["identity_id"], "拒收后作品不得被改动"
    assert after_work["version"] == before_work["version"], "拒收不得推进作品版本"
    assert len(st.all_rows("selections")) == before_decisions, "拒收不得留下选择记录"
