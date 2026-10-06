"""出稿方自修复 + 资料端接通（回归）。

现场症状：点「生成三稿」后三稿全部「1 项硬门禁未过」，采用按钮点不动，界面只能提示
"删掉没有依据的断言"——**用户被要求替出稿方返工**。

根因是结构性的，不是模型不听话：`materials` 在全后端**没有任何写入方**
（`t_register_script_candidates` 读的 `args["materials"]` 永远是空列表），于是
`review._supported` 一律返回 False，每条断言必然 `unverified`，
`FACT_EVIDENCE` 硬门禁必然挡住。**写得多好的稿子都不可能被采用。**

所以本文件锁三件事：
1. 资料端接通（`WorkBrief.materials` + 工具层读 brief），让"断言有依据"这条路不再是死的；
2. 生成侧自修复：出稿后由**模型自己**按硬门禁结论重写，断言清单从**改后正文**重抽，
   交付稿必须可采用，用户只负责挑一稿；
3. 不许用"清空断言清单"绕过门禁——`CLAIM_COVERAGE` 是确定性检查，不看模型怎么说。
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app import storage as st
from app.contracts import MaterialEntry, ScriptBatch, WorkBrief
from app.content import generate as gen
from app.content import review as rv
from app.content.errors import ContentRejected

TOPIC = "RAG 科普"

# 带数字/归因的正文与断言：无资料时必然过不了门禁，正是自修复要处理的对象。
RAW_TEXT = ("很多人说 RAG 能治好模型胡说。据官方数据显示，2025 年有 78% 的企业在用它，"
            "检索准确率提升到 92%。")
GROUNDED_MATERIAL = "2026 年 3 月官方发布：标准版定价 199 元，切片长度默认 400 字。"
GROUNDED_TEXT = ("价格这事我先说清楚：2026 年 3 月官方发布，标准版定价 199 元。"
                 "你别急着比价，先想想自己到底要解决什么问题。")
CLEAN_TEXT = (
    "你有没有发现，把资料丢给 AI，它照样答非所问？先别急着怪它笨。"
    "我的体会是，问题往往不在它聪不聪明，而在于它到底挑中了哪几段内容。"
    "换个说法，这就像你让朋友帮你查一本书里的某句话。"
    "他没从头读到尾，只是翻了翻目录，挑几页念给你听，然后回答你。"
    "翻对了，答案就准；翻漏了，他也只能硬着头皮往下编。"
    "所以我现在养成了一个习惯：答案离谱的时候先不换工具。"
    "先回头看看自己是怎么问的，再看看它可能翻到了什么。"
    "下一期我们接着说，怎么问才能让它翻对页。"
)
# 另一份干净正文：用于需要"两稿都干净"的用例，长度同样对齐 60 秒目标
CLEAN_TEXT_B = (
    "先问你一个问题：同一份文件，你看到的和它看到的，是同一样东西吗？"
    "我自己的体会是，多半不是。你在屏幕上看到的标题层级、对齐整齐的表格，"
    "到了它那边，可能就成了一串顺序打乱的文字。"
    "比如一张表格，表头在一行，数据在下面几行，人一看就懂。"
    "可要是拆开去看，某个数字到底属于哪一列，就没那么确定了。"
    "所以我现在的做法很朴素：先把话说清楚，再看看它懂了没有。"
    "很多时候要改的不是它，是你喂给它的东西。"
    "下一期我们接着说，怎么把一份文件整理成它读得进去的样子。"
)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reply(payload):
    return _Resp({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})


def _scripted_transport(*payloads, fail_at: int = 0, exc: Exception | None = None):
    """按调用顺序依次返回给定 payload；`fail_at` 指定第几次调用抛错。"""
    calls = []

    def _op(req, timeout):
        calls.append(json.loads(req.data.decode("utf-8")))
        n = len(calls)
        if fail_at and n == fail_at:
            raise exc or urllib.error.HTTPError("https://api.deepseek.com", 429, "rate limited", {}, None)
        idx = min(n - 1, len(payloads) - 1)
        return _reply(payloads[idx])

    _op.calls = calls
    return _op


def _cand(angle, title, text, claims=()):
    return {"angle": angle, "title": title, "description": "一句话说明",
            "tags": ["AI科普"], "text": text, "claims": list(claims)}


def _raw_batch(*cands):
    return {"candidates": list(cands)}


def _work(work_id: str, topic: str = TOPIC, materials=None) -> str:
    st.put("works", work_id, {
        "id": work_id, "name": topic,
        "brief": WorkBrief(topic=topic, materials=materials or []).model_dump(),
        "identity_id": None, "voice_asset_id": None, "script_version_id": None,
        "route": "M2",
    })
    return work_id


def _check(cand, code):
    return next(x for x in cand.quality_report.checks if x.code == code)


def _hard(cand):
    return [c for c in cand.quality_report.checks if c.hard_gate and c.outcome != "pass"]


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("AVATAR_STUDIO_DEEPSEEK_API_KEY", "sk-unit-test")


# ---------- ① 资料端必须真的有写入方 ----------

def test_brief_carries_materials_and_defaults_to_empty():
    """资料是断言判定的唯一依据来源。没有这个字段，"有依据"就是永远走不到的分支。"""
    assert WorkBrief(topic="t").materials == []
    brief = WorkBrief(topic="t", materials=[MaterialEntry(text=GROUNDED_MATERIAL, source="官方")])
    assert brief.materials[0].text == GROUNDED_MATERIAL


def test_material_traceability_fields_exist_for_versioning():
    """规格 06 AC-CT-02 要求资料的提交时间与版本可追溯，断言才能回溯到"哪一版资料"。"""
    e = MaterialEntry(text=GROUNDED_MATERIAL, source="官方")
    assert e.content_hash == "" and e.added_at == ""


def test_materials_endpoint_stamps_hash_and_time(monkeypatch):
    """哈希与时间必须由服务端复算：客户端填的值一律被覆盖，否则追溯是假的。"""
    import hashlib

    from fastapi.testclient import TestClient

    from app import storage as st
    from app.agents import supervisor
    from app.main import app

    async def _noop(task, work, stage):
        """不出稿：本用例只验资料落库，绝不发起真实调用。"""
        return {}

    monkeypatch.setattr(supervisor, "run_stage", _noop)
    st.put("works", "w_mat_stamp", {
        "id": "w_mat_stamp", "name": "t",
        "brief": WorkBrief(topic=TOPIC).model_dump(),
        "identity_id": None, "voice_asset_id": None, "script_version_id": None, "route": "M2",
    })
    client = TestClient(app)
    client.post("/api/v1/works/w_mat_stamp/scripts", json={
        "topic": TOPIC,
        "materials": [{"text": GROUNDED_MATERIAL, "source": "官方",
                       "content_hash": "假装是哈希", "added_at": "1970-01-01T00:00:00Z"}],
    })
    saved = (st.get("works", "w_mat_stamp") or {}).get("brief", {}).get("materials") or []
    assert saved, "资料必须落库"
    assert saved[0]["content_hash"] == hashlib.sha256(GROUNDED_MATERIAL.encode("utf-8")).hexdigest()
    assert saved[0]["content_hash"] != "假装是哈希"
    assert saved[0]["added_at"] and saved[0]["added_at"] != "1970-01-01T00:00:00Z"


def test_batch_contract_covers_repair_fields():
    """写入方写什么契约就得认什么——否则稿子落库成功、返回时整条 500。"""
    batch = {"id": "b1", "work_id": "w1", "shortfall_note": None,
             "candidates": [], "source_kind": "llm", "materials_count": 0,
             "repair_note": "已自修复 1 次", "repair_history": [{"round": 1, "candidate_id": "s1"}]}
    ScriptBatch(**batch)


def test_batch_contract_still_reads_rows_written_before_repair():
    legacy = {"id": "b0", "work_id": "w0", "shortfall_note": None, "candidates": []}
    assert ScriptBatch(**legacy).repair_note is None


# ---------- ② 无资料时：模型必须自己把稿子改到可采用 ----------

def test_unfounded_draft_is_repaired_by_the_model_before_delivery(key):
    """无资料出稿 → 首轮带数字/归因的稿子过不了门禁 → 模型自己重写 → 交付即可采用。"""
    wid = _work("w_repair_basic")
    transport = _scripted_transport(
        _raw_batch(_cand("常见误区", "接了库不等于不胡说", RAW_TEXT, [RAW_TEXT[:20]])),
        # 自修复返回：去掉数字与归因后的正文，断言清空
        {"text": CLEAN_TEXT, "claims": [], "repair_note": "删掉了没有资料支撑的数字与归因"},
    )
    out = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport)
    cand = out["candidates"][0]
    assert _hard(cand) == [], f"交付稿仍有硬门禁未过：{[c.code for c in _hard(cand)]}"
    assert cand.text == CLEAN_TEXT
    assert out["repair_note"] and "自修复" in out["repair_note"]
    rounds = [h for h in out["repair_history"] if h["outcome"] == "repaired"]
    assert rounds and rounds[0]["before"] and rounds[0]["after"] == []


def test_repair_keeps_the_same_candidate_slot(key):
    """自修复是"这稿变好了"，不是凭空多出候选——三稿不能被修成五稿。"""
    wid = _work("w_repair_slot")
    transport = _scripted_transport(
        _raw_batch(_cand("常见误区", "接了库不等于不胡说", RAW_TEXT, [RAW_TEXT[:20]])),
        {"text": CLEAN_TEXT, "claims": [], "repair_note": "删数字"},
    )
    out = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport)
    assert len(out["candidates"]) == 1
    # 断言清单来自**改后的正文**：改后没断言，交付稿的断言就必须是空的
    assert out["candidates"][0].claims == []


def test_repair_gives_up_honestly_instead_of_faking_a_pass(key):
    """模型改了两轮还带着无依据断言？如实标为不可采用，绝不伪报通过。"""
    wid = _work("w_repair_fail")
    stubborn = {"text": RAW_TEXT, "claims": ["据官方数据显示 2025 年有 78% 的企业在用它"],
                "repair_note": "没改"}
    transport = _scripted_transport(
        _raw_batch(_cand("常见误区", "接了库不等于不胡说", RAW_TEXT, ["据官方数据显示"])),
        stubborn, stubborn,
    )
    out = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport,
                                  max_repair_rounds=2)
    cand = out["candidates"][0]
    assert _hard(cand), "改不动就应当仍是不可采用"
    assert out["repair_note"] and "不可采用" in out["repair_note"]
    # 两条渠道各说各的事：shortfall_note 只说"稿数够不够"，不掺"哪稿不能用"
    assert "未过硬门禁" not in (out["shortfall_note"] or "")


def test_shortfall_note_counts_and_repair_note_explains(key):
    """三稿都产出了，但其中一稿自修复后仍没过门禁：缺额说明保持沉默，
    由自修复留痕说明"哪一稿、因为哪项、为什么不可采用"。"""
    wid = _work("w_two_channels")
    payload = _raw_batch(
        _cand("生活类比", "闭卷考试", CLEAN_TEXT),
        _cand("常见误区", "接了库不等于不胡说", RAW_TEXT, ["据官方数据显示"]),
        _cand("实战案例", "先量再改", CLEAN_TEXT_B),
    )
    stubborn = {"text": RAW_TEXT, "claims": ["据官方数据显示 2025 年有 78% 的企业在用它"]}
    out = gen.generate_candidates(work_id=wid, topic=TOPIC,
                                  transport=_scripted_transport(payload, stubborn))
    assert out["produced"] == 3
    assert out["shortfall_note"] is None
    assert len(out["candidates"]) == 3, "自修复不得凭空增删候选位"
    assert [c.angle for c in out["candidates"] if _hard(c)] == ["常见误区"]
    assert "FACT_EVIDENCE" in out["repair_note"] and "不可采用" in out["repair_note"]


def test_repair_stops_when_provider_is_unavailable(key):
    """自修复期间供应商挂掉：停止循环、如实记账，不把"没修成"说成"修好了"。"""
    wid = _work("w_repair_blocked")
    transport = _scripted_transport(
        _raw_batch(_cand("常见误区", "接了库不等于不胡说", RAW_TEXT, ["据官方数据显示"])),
        fail_at=2,
    )
    out = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport)
    assert any(h.get("outcome") == "provider_blocked" for h in out["repair_history"])
    assert out["repair_note"] and "未完成" in out["repair_note"]
    assert _hard(out["candidates"][0]), "没修成的稿子不得被当成可用"


def test_repairing_a_draft_without_blockers_is_rejected_not_silently_ok(key):
    """没有要修的东西时明确拒绝，而不是假装改过一遍。"""
    wid = _work("w_repair_noop")
    transport = _scripted_transport(_raw_batch(_cand("生活类比", "闭卷考试", CLEAN_TEXT)))
    sv = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport)["candidates"][0]
    assert gen.hard_blockers(sv) == []
    with pytest.raises(ContentRejected) as ei:
        gen.repair_one(version=sv, work_id=wid, topic=TOPIC, transport=transport)
    assert ei.value.code == "NOTHING_TO_REPAIR"


# ---------- ③ 清空断言清单不能绕过事实门禁 ----------

def test_empty_claim_list_cannot_hide_numeric_facts():
    """断言清单是模型自报的：交回空清单就等于把 FACT_EVIDENCE 变成摆设。
    带数字/年份/归因的正文必须由确定性检查兜住，与模型怎么说无关。"""
    report = rv.review_candidate(text=RAW_TEXT, title="t", claims=[], materials=[])
    cov = next(c for c in report.checks if c.code == "CLAIM_COVERAGE")
    assert cov.hard_gate and cov.outcome == "review"
    assert "未被断言清单覆盖" in cov.message


def test_claim_coverage_passes_when_claim_list_covers_the_facts():
    text = "标准版定价 199 元。"
    claims = rv.extract_claims([{"text": "标准版定价 199 元"}], materials=[])
    report = rv.review_candidate(text=text, title="t", claims=claims, materials=[])
    assert next(c for c in report.checks if c.code == "CLAIM_COVERAGE").outcome == "pass"
    # 覆盖了也不代表有依据：断言照样是 unverified，FACT_EVIDENCE 仍然挡着
    assert next(c for c in report.checks if c.code == "FACT_EVIDENCE").outcome != "pass"


def test_prose_without_literal_fact_signals_passes_coverage():
    report = rv.review_candidate(text=CLEAN_TEXT, title="t", claims=[], materials=[])
    assert next(c for c in report.checks if c.code == "CLAIM_COVERAGE").outcome == "pass"


# ---------- ④ 有资料时：照抄原句才算有依据 ----------

def test_quoting_materials_verbatim_makes_the_claim_supported(key):
    """资料接通后，"断言有依据"这条路才真的走得通；但必须**照抄原句**。"""
    wid = _work("w_grounded", materials=[{"text": GROUNDED_MATERIAL, "source": "官方"}])
    transport = _scripted_transport(
        _raw_batch(_cand("价格澄清", "先说清楚价格", GROUNDED_TEXT,
                         ["2026 年 3 月官方发布：标准版定价 199 元"])))
    cand = gen.generate_candidates(work_id=wid, topic=TOPIC,
                                   materials=[{"text": GROUNDED_MATERIAL, "source": "官方"}],
                                   transport=transport)["candidates"][0]
    assert [c.verdict for c in cand.claims] == ["supported"]
    assert next(c for c in cand.quality_report.checks if c.code == "FACT_EVIDENCE").outcome == "pass"
    assert _hard(cand) == []


def test_paraphrasing_the_material_is_still_unverified(key):
    """把资料改成同义句会被判无依据——这就是生成侧被要求照抄原句的原因，
    也说明门禁没有被"给了资料"这件事放宽。"""
    claims = rv.extract_claims(
        [{"text": "标准版的定价大约两百块上下，切片长度默认是四百个字符左右"}],
        materials=[{"text": GROUNDED_MATERIAL}])
    assert claims[0].verdict == "unverified"


# ---------- ⑤ 时效门禁不能被口语指示词误触发 ----------

def test_casual_indicator_words_do_not_trip_the_timeliness_gate():
    """「现在/目前」是口语指示词，不是时效事实。混进来会把所有口语稿无资料时全挡掉。"""
    assert "现在" not in rv.STRONG_TIMELINESS_MARKERS
    report = rv.review_candidate(text="现在很多人用 AI 查资料，目前常见的做法是先检索。",
                                 title="t", claims=[], materials=[])
    assert next(c for c in report.checks if c.code == "TIMELINESS").outcome == "pass"


def test_real_timeliness_claims_still_gated_without_materials():
    report = rv.review_candidate(text="最新版把价格降到了 199 元，本周首发。",
                                 title="t", claims=[], materials=[])
    assert next(c for c in report.checks if c.code == "TIMELINESS").outcome != "pass"


# ---------- ⑦ 软缺陷也要返工，但软项绝不阻断采用 ----------

LONG_SENTENCE_TEXT = (
    "你有没有想过，为什么同一份文件你看着清清楚楚，"
    "可是当你把它丢给一个只会按关键词挑几段内容然后照着这几段回答问题、"
    "既不会主动承认自己没找到也不打算从头到尾读一遍的工具的时候，"
    "它给出的答案就会离谱得让你怀疑自己的眼睛？"
)


def test_soft_defects_are_repaired_but_never_block_adoption(key):
    """最长句过长是**可判定**的软缺陷：出稿方要改，但它不构成"不能采用"。"""
    wid = _work("w_soft_repair")
    transport = _scripted_transport(
        _raw_batch(_cand("生活类比", "为什么读错了", LONG_SENTENCE_TEXT)),
        {"text": CLEAN_TEXT, "claims": [], "repair_note": "拆短了过长句"},
    )
    out = gen.generate_candidates(work_id=wid, topic=TOPIC, transport=transport)
    cand = out["candidates"][0]
    assert cand.text == CLEAN_TEXT, "软缺陷也应由模型自己改掉"
    assert _hard(cand) == [], "软缺陷不得被当成「不能采用」的理由"
    assert out["soft_left"] == []


def test_soft_defect_left_over_is_reported_as_advisory_not_as_blocking(key):
    """改不动的软项写进留痕当建议项，且不得影响可采用性。"""
    wid = _work("w_soft_left")
    stubborn = {"text": LONG_SENTENCE_TEXT, "claims": []}
    out = gen.generate_candidates(work_id=wid, topic=TOPIC,
                                  transport=_scripted_transport(
                                      _raw_batch(_cand("生活类比", "为什么读错了", LONG_SENTENCE_TEXT)),
                                      stubborn))
    cand = out["candidates"][0]
    assert _hard(cand) == []
    assert out["soft_left"] == [cand.id]
    assert "建议项" in (out["repair_note"] or "") and "不影响采用" in (out["repair_note"] or "")
    # 采用判定只看硬门禁——这里必须仍然可用
    assert gen.hard_blockers(cand) == []


# ---------- ⑥ 三段硬门禁在同一份报告里共存且互不抵消 ----------

def test_hard_gates_do_not_cancel_out():
    """事实依据缺 + 覆盖缺 + 强时效，三项都要各自记账，不能被彼此或软项的高分抵消。"""
    report = rv.review_candidate(text=RAW_TEXT, title="t",
                                 claims=rv.extract_claims([{"text": "RAG 能治好模型胡说"}], materials=[]),
                                 materials=[])
    codes = {c.code: c for c in report.checks}
    assert codes["FACT_EVIDENCE"].outcome == "review"
    assert codes["CLAIM_COVERAGE"].outcome == "review"
    assert report.overall == "review" and report.review_required
