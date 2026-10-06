"""口播稿生成的两处**必然阻断**（回归）。

现场症状：点「生成三稿」报 `Unexpected token 'I', "Internal S"...`，之后稿子明明写出来了
却一稿也采用不了。根因不在"生成"，都在生成之后的两道关卡上：

1. **契约没跟上写入方**：`t_register_script_candidates` 写 `source_kind`，而 `ScriptBatch`
   是 `extra=forbid`——稿子已经落库，却在返回时被判非法，路由抛未捕获的 ValidationError，
   变成纯文本 500，前端解析 JSON 失败。`RewriteLedger` 往同一行写
   `rewrite_count/rewrite_history`，是同一颗还没踩响的雷。
2. **查重拿自己当历史**：`generate._as_script_version` 用 `dedup_report("", ...)` 查重，
   `internal_history("")` 于是**不排除本作品**——本作品的选题行与自己的主题 100% 相同，
   同主题的作品之间也 100% 相同，DUPLICATE 硬门禁必然 fail，三稿全部不可采用。
   只有真实模型出稿才暴露：Mock 回落路径根本不跑审核。

所以本文件同时锁三件事：契约与写入方同步、查重分层（**正文重复才是硬重复**，
主题相同不等于重复——规格 03）、以及改稿重审的口径（删掉无依据断言可以过关，
但不能靠编辑把"无依据"变成"有依据"）。
"""
from __future__ import annotations

import json

import pytest

from app import storage as st
from app.contracts import ScriptBatch, WorkBrief
from app.content import generate as gen
from app.content import review as rv
from app.content.errors import ContentRejected

TOPIC = "RAG 科普"
TEX_A = "RAG 是把检索结果交给模型作答，答案可以追溯到资料。"
TEX_B = "把切片长度从一千二百字改成四百字之后，召回命中率明显上升。"
TEX_C = "接上知识库不等于不会答错，检索失败时模型仍会给出错的答案。"
CANON_A = TEX_A.replace(" ", "")
# 同一个进程里共享一个库，且 storage 没有删除接口：凡是要落库再比对查重的用例，
# 必须用只属于它自己的正文，否则会被别的用例留下的行当成"历史"。
OWN_OLD = "本作品上一轮的旧稿正文，只有这个用例会写它。"
SAME_TOPIC_OTHER = "同题材但完全不同的一份正文，只有这个用例会写它。"
OTHER_WORK_OLD = "另一个作品写过的正文，只有这个用例会写它。"


class _Resp:
    def __init__(self, payload: str):
        self._b = payload.encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _transport(payload):
    def _op(req, timeout):
        content = json.dumps(payload, ensure_ascii=False)
        return _Resp(json.dumps({"choices": [{"message": {"content": content}}]}))
    return _op


def _cand(angle, title, text):
    return {"angle": angle, "title": title, "description": "一句话说明",
            "tags": ["AI科普"], "text": text, "claims": []}


def _work(work_id: str, topic: str = TOPIC) -> str:
    st.put("works", work_id, {
        "id": work_id, "name": topic, "brief": WorkBrief(topic=topic).model_dump(),
        "identity_id": None, "voice_asset_id": None, "script_version_id": None,
        "route": "M2",
    })
    return work_id


def _stored_batch(batch_id: str, work_id: str, text: str, **extra) -> dict:
    row = {"id": batch_id, "work_id": work_id, "shortfall_note": None,
           "candidates": [{"id": st.new_id("script"), "angle": "旧角度", "title": "旧标题",
                           "description": "旧", "tags": [], "text": text,
                           "duration_estimate_ms": 60000, "claims": [],
                           "quality_report": {"checks": [], "overall": "pass",
                                              "review_required": False}}]}
    row.update(extra)
    st.put("script_batches", work_id, dict(row), {"work_id": work_id})
    return row


def _dup(candidate):
    return next(x for x in candidate.quality_report.checks if x.code == "DUPLICATE")


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "sk-unit-test")


# ---------- ① 契约必须容纳写入方真正写的字段 ----------

def test_batch_contract_covers_every_writer_field():
    """写入方写什么，契约就得认什么——否则"写成功、返回失败"，而且只在返回时才炸。"""
    live = _stored_batch("b_live", "w1", TEX_A, source_kind="llm", rewrite_count=1,
                         rewrite_history=[{"seq": 1, "reason": "事实依据不足"}])
    ScriptBatch(**live)


def test_batch_contract_still_reads_legacy_rows():
    """改动前写入的批次没有 source_kind／改稿计数，不得因此被整条拒绝。"""
    legacy = _stored_batch("b_legacy", "w0", TEX_A)
    assert "source_kind" not in legacy
    ScriptBatch(**legacy)


# ---------- ② 查重不得拿本作品（或其主题）当历史 ----------

def test_regeneration_does_not_flag_its_own_work_as_duplicate(key):
    """点「重新生成」时，上一轮稿子就在库里；它不是"历史重复"。"""
    wid = _work("w_own")
    _stored_batch("b_own_old", wid, OWN_OLD, source_kind="llm")
    out = gen.generate_candidates(
        work_id=wid, topic=TOPIC, target_duration_ms=60000,
        transport=_transport({"candidates": [
            _cand("生活类比", "闭卷考试", OWN_OLD),
            _cand("常见误区", "接了库不等于不胡说", TEX_C),
            _cand("实战案例", "切片决定召回", TEX_B),
        ]}))
    for c in out["candidates"]:
        dup = _dup(c)
        assert dup.outcome != "fail", f"被自己的旧稿判成硬重复：{dup.message}"


def test_same_topic_different_text_is_not_a_hard_duplicate(key):
    """规格 03：「主题相同并不必然重复」——同题材必须由人判断，不能自动判死。"""
    _work("w_same_topic_a", topic=TOPIC)
    wid = _work("w_same_topic_b", topic=TOPIC)
    out = gen.generate_candidates(
        work_id=wid, topic=TOPIC, target_duration_ms=60000,
        transport=_transport({"candidates": [_cand("生活类比", "闭卷考试", SAME_TOPIC_OTHER)]}))
    dup = _dup(out["candidates"][0])
    assert dup.outcome != "fail", f"同主题被直接判死：{dup.message}"
    assert dup.hard_gate is False or dup.outcome == "pass"


def test_other_works_identical_text_is_still_a_hard_duplicate(key):
    """反过来必须钉住：别的作品写了同样的正文，仍要判硬重复，不能顺手把查重改没了。"""
    other = _work("w_other", topic="完全不同的另一个选题")
    _stored_batch("b_other", other, OTHER_WORK_OLD, source_kind="llm")
    wid = _work("w_mine", topic=TOPIC)
    out = gen.generate_candidates(
        work_id=wid, topic=TOPIC, target_duration_ms=60000,
        transport=_transport({"candidates": [_cand("生活类比", "闭卷考试", OTHER_WORK_OLD)]}))
    dup = _dup(out["candidates"][0])
    assert dup.outcome == "fail" and dup.hard_gate is True
    assert "正文" in dup.message, "硬失败必须来自正文层，而不是主题层"


# ---------- ③ 改稿重审：断言清单必须来自改后的正文，且判定不由模型给 ----------

def test_revision_recheck_keeps_unsupported_claim_blocking():
    report, kept = gen.revalidate_revision(
        work_id="w1", text=TEX_A, title="标题", topic=TOPIC,
        claims=[{"id": "c1", "text": TEX_A, "verdict": "unverified"}])
    assert [c.text for c in kept] == [CANON_A]
    fact = next(c for c in report.checks if c.code == "FACT_EVIDENCE")
    assert fact.hard_gate is True and fact.outcome != "pass", "保留无依据断言就不能放行"


def test_revision_passes_only_when_relisted_claims_are_empty():
    """改写删去断言后重审（规格 03）：改后正文里确实没有断言了，才允许通过。"""
    report, kept = gen.revalidate_revision(
        work_id="w1", text="我们今天聊聊怎么把一个问题讲清楚。", title="标题", topic=TOPIC,
        claims=[])
    assert kept == []
    fact = next(c for c in report.checks if c.code == "FACT_EVIDENCE")
    assert fact.outcome == "pass"


def test_revision_never_upgrades_verdict_by_editing():
    """编辑不带来新证据：改后仍在的断言沿用资料复算结果，不得被"重审"洗成 supported。"""
    _, kept = gen.revalidate_revision(
        work_id="w1", text=TEX_A, title="标题", topic=TOPIC,
        claims=[{"id": "c1", "text": TEX_A, "verdict": "unverified"}])
    assert [c.verdict for c in kept] == ["unverified"]


def test_revision_claims_come_from_model_and_are_verdict_free(key):
    """重审用的清单由模型按改后正文给出；模型自己写的判定标记一律不算数。"""
    out = gen.extract_revision_claims(
        text=TEX_A, topic=TOPIC,
        transport=_transport({"claims": [f"{TEX_A}（supported）", "  ", "另一条断言"]}))
    assert [c.text for c in out] == [CANON_A, "另一条断言"]
    assert [c.verdict for c in out] == ["unverified", "unverified"]


def test_revision_claims_never_silently_become_empty(key):
    """模型没给出清单就报错，不能当成"零断言"而让门禁自动通过。"""
    with pytest.raises(ContentRejected) as caught:
        gen.extract_revision_claims(text=TEX_A, topic=TOPIC,
                                    transport=_transport({"nope": []}))
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("marked", [f"[unverified]{TEX_A}", f"{TEX_A}（unverified）",
                                    f"【无依据】{TEX_A}（模型自评）"])
def test_claim_marker_never_leaks_into_stored_text(marked):
    """模型自带的结论标记在前在后都要剥掉：留着会让读者把模型自评当成判定。"""
    claims = rv.extract_claims([marked])
    assert [c.text for c in claims] == [CANON_A]


# ---------- ④ 契约不符与供应商不可用都要报具名错误，不能是裸 500 ----------

def test_scripts_endpoint_reports_contract_mismatch(monkeypatch):
    from fastapi.testclient import TestClient

    from app.agents import supervisor
    from app.main import app

    wid = _work("w_poison")
    _stored_batch("b_poison", wid, TEX_A, source_kind="llm",
                  未来字段="写入方多写了一个契约不认的字段")

    async def _noop(task, work, stage):
        return {"executed": 0, "states": {}, "failures": []}

    monkeypatch.setattr(supervisor, "run_stage", _noop)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(f"/api/v1/works/{wid}/scripts", json={})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "AGENT_RESULT_CONTRACT_MISMATCH"


def test_revision_needs_no_model_and_creates_new_unreviewed_version(monkeypatch):
    """口播稿升级后：手改正文不再暗中调用模型重抽断言（避免隐含计费）。
    改稿产生新版本 ID，规则重审，且新正文没有有效的独立审核 → 不能直接采用。"""
    from fastapi.testclient import TestClient

    from app.main import app

    for name in ("AVATAR_STUDIO_DEEPSEEK_API_KEY", "AVATAR_STUDIO_QWEN_API_KEY", "AVATAR_STUDIO_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    wid = _work("w_no_provider", topic=TOPIC)
    batch = _stored_batch("b_no_provider", wid, TEX_A, source_kind="llm")
    cand_id = batch["candidates"][0]["id"]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(f"/api/v1/works/{wid}/script-revisions",
                           json={"script_id": cand_id, "text": TEX_A + "再补一句自己的话。"})
        assert resp.status_code == 200, resp.text
        new_id = resp.json()["id"]
        assert new_id != cand_id, "改稿必须是新版本 ID，不能原地改正文"
        assert resp.json()["parent_id"] == cand_id
        pick = client.post(f"/api/v1/works/{wid}/script-selection", json={"script_id": new_id})
    assert pick.status_code == 409 and pick.json()["error"]["code"] == "SCRIPT_REVIEW_REQUIRED"


# ---------- ⑤ 「重新生成」必须真的重跑，而不是空转返回旧批次 ----------

def test_regenerate_actually_reruns_the_script_stage(monkeypatch):
    """编排只跑未完成的工作项：已有批次时「重新生成」会空转——返回旧批次、按钮像假的，
    用户已有的卡住作品因此永远出不来。重跑必须把该阶段重新排队。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", raising=False)  # 无凭据 → 走 mock 回落，离线
    wid = _work("w_rerun", topic=TOPIC)
    with TestClient(app) as client:
        first = client.post(f"/api/v1/works/{wid}/scripts", json={})
        assert first.status_code == 200
        second = client.post(f"/api/v1/works/{wid}/scripts", json={})
        assert second.status_code == 200
    assert first.json()["id"] != second.json()["id"], "第二次点击必须真的重跑"
    events = [e.get("type") for e in st.all_rows("task_events")]
    assert "stage.requeued" in events


def test_failed_regeneration_keeps_the_adopted_script(monkeypatch):
    """重跑失败不该有副作用：旧采用只在写出新批次时才失效。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", raising=False)
    wid = _work("w_keep_adopted", topic=TOPIC)
    batch = _stored_batch("b_keep", wid, TEX_A, source_kind="llm")
    adopted = batch["candidates"][0]["id"]
    w = st.get("works", wid)
    w["script_version_id"] = adopted
    st.put("works", wid, w)

    # 让重跑必然失败：写稿工具直接拒绝（改注册表，改函数引用不会生效）
    from app.agents import tools as agent_tools

    def _boom(args, ctx):
        raise agent_tools.ToolDenied("PROVIDER_CALL_FAILED", "模拟供应商故障")

    monkeypatch.setitem(agent_tools.TOOLS, "register_script_candidates", _boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(f"/api/v1/works/{wid}/scripts", json={})
    # 写稿失败就得报错，不能把旧批次当成"生成成功"返回
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROVIDER_CALL_FAILED"
    assert st.get("works", wid)["script_version_id"] == adopted, "重跑失败不得丢掉已采用的稿子"
