"""稿件质量与事实风险审核（确定性，不调用模型）。

核心红线：**模型自审不算事实核验**。本模块完全不发起 LLM 请求；
断言（Claim）的 verdict 只由用户资料 / 已审知识支撑，缺依据一律 `unverified`
并转人工复核，绝不因为"模型说它对"就放行，也绝不编造引用。

门禁共三项硬门禁：`FACT_EVIDENCE`（断言缺依据）、`CLAIM_COVERAGE`（正文有事实性
表述而清单没覆盖——防"清空清单"绕过上一项）、`TIMELINESS`（强时效表述无依据）。
硬门禁未过的稿子不可采用，且不被其他项的高分抵消；出稿方（`content/generate.py`）
负责在交付前把稿子改到过硬门禁，而不是把修稿转嫁给用户。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from ..contracts import Claim, QualityCheck, QualityReport
from .dedup import TEXT_THRESHOLD as TEXT_DUP_THRESHOLD
from .dedup import TOPIC_THRESHOLD, VIEWPOINT_THRESHOLD

TIMELINESS_MARKERS = (
    "价格", "最新", "今天", "今年", "本月", "目前", "现在", "即将", "新版",
    "降价", "上涨", "排行", "首发", "刚发布", "昨天", "本周", "本月起",
)
# 「现在/目前」只是口语指示词，本身不构成可核验的时效事实（"现在很多人用 AI 查资料"
# 不是一条时效断言）。把它们和真正的时效说法混在一起，会让任何口语稿在无资料时
# 都被 TIMELINESS 挡住——门禁变成措辞检查，反而逼人去改稿。只留**强时效**表述。
STRONG_TIMELINESS_MARKERS = tuple(m for m in TIMELINESS_MARKERS if m not in ("现在", "目前"))
# 断言被资料支撑的判定：资料里出现该断言的关键片段
_MIN_OVERLAP = 8

# 事实性表述的**字面信号**：数字带量纲/货币、年份、归因词、型号。
# 它存在的唯一理由是堵一个贯通性缺口——断言清单是模型自报的，模型只要交回空清单，
# FACT_EVIDENCE 就会判"无关键断言"而放行，等于用模型自审换核验。这里的检测是确定性的，
# 不依赖模型：正文里出现这些字面信号，就必须有断言覆盖它，否则一律 review。
# 明确的能力边界：本项只覆盖**字面可识别的**事实表述；纯叙述性断言是否列全仍由
# 断言清单负责，系统不声称已穷尽（见 CLAIM_COVERAGE 的 message）。
_FACT_SIGNAL_PATTERNS = (
    ("数字量纲", re.compile(
        r"\d+(?:\.\d+)?\s*(?:%|％|元|块钱?|万|亿|秒|分钟|小时|天|个月|年|岁|人|次|倍|台|款"
        r"|GB|MB|KB|TB|token|tokens|字|页|条|例|版本)")),
    ("年份", re.compile(r"(?:19|20)\d{2}\s*年")),
    ("归因", re.compile(r"研究表明|数据显示|据统计|调查显示|报告显示|官方|财报|发布会|榜单|白皮书|站内数据")),
    ("型号", re.compile(r"[A-Za-z]{2,}\s?-\s?\d+[A-Za-z0-9.]*")),
)


def detect_fact_signals(text: str) -> List[Dict[str, str]]:
    """从正文里确定性摘出事实性表述的字面信号（不判真假，只标位置）。"""
    body = _norm(text)
    out: List[Dict[str, str]] = []
    seen = set()
    for kind, pat in _FACT_SIGNAL_PATTERNS:
        for m in pat.finditer(body):
            snippet = m.group(0)
            if (kind, snippet) in seen:
                continue
            seen.add((kind, snippet))
            out.append({"kind": kind, "snippet": snippet})
    return out


def _claim_text_of(c: Any) -> str:
    if isinstance(c, dict):
        return str(c.get("text", ""))
    # 调用方传的是 `Claim` 契约对象时取字段本身；不能 str(obj) —— 那会把
    # `id=.../verdict=...` 一起拼进比对面，等于放宽匹配。
    text = getattr(c, "text", None)
    return str(text) if text is not None else ""


def _covered_by_claims(signal: Dict[str, str], claims: Sequence[Any]) -> bool:
    """断言清单里是否覆盖了这个事实性表述（按字面片段比对）。"""
    snip = _norm(signal.get("snippet", ""))
    if not snip:
        return True
    for c in claims:
        ctext = _norm(_claim_text_of(c))
        if snip and snip in ctext:
            return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# 模型会在断言正文上自带结论标记，位置不固定：前缀 `[unverified]`、后缀 `（unverified）`
# 都出现过。verdict 的唯一来源是本模块按资料复算的结果，标记一律剥掉——留着不仅让
# 读者把模型自评当成判定，还会污染"改稿后这条断言是否还在正文里"的比对：
# 标记留在断言文本里就永远匹配不到正文，等于任何一次编辑都把断言全删了。
_MARKER_EDGE = re.compile(r"[\[【(（][^\]】)）]{0,24}[\]】)）]")
_VERDICT_WORDS = {
    "supported", "refuted", "unverified", "verified",
    "有依据", "无依据", "已核实", "待核", "待复核", "已核验", "未核验", "模型自评",
}


def _is_verdict_marker(token: str) -> bool:
    return token.strip("[]()【】（）").lower() in _VERDICT_WORDS


def claim_text_canonical(text: str) -> str:
    """断言正文的规范写法：剥掉首尾的结论标记，再归一空白。

    落库与后续比对共用这一种写法，否则"这条断言还在不在改后正文里"会因标记差异误判。
    """
    s = _norm(text)
    for _ in range(4):
        stripped = False
        for m in list(_MARKER_EDGE.finditer(s)):
            if m.start() != 0 and m.end() != len(s):
                continue  # 只剥首尾；正文中间的括号属于正文
            if _is_verdict_marker(m.group(0)):
                s = s[:m.start()] + s[m.end():]
                stripped = True
                break
        if not stripped:
            break
    return s


def _materials_text(materials: Optional[Sequence[Any]]) -> str:
    out = []
    for m in materials or []:
        if isinstance(m, dict):
            out.append(_norm(str(m.get("text", ""))))
        else:
            out.append(_norm(str(m)))
    return "".join(out)


def _supported(claim_text: str, materials_blob: str) -> bool:
    """资料里出现断言的关键连续片段才算支撑；数字类断言要求数字一致。"""
    if not materials_blob:
        return False
    c = _norm(claim_text)
    if len(c) < _MIN_OVERLAP:
        return c in materials_blob
    # 滑动窗口找最长公共片段
    best = 0
    i = 0
    while i + _MIN_OVERLAP <= len(c):
        if c[i:i + _MIN_OVERLAP] in materials_blob:
            j = i + _MIN_OVERLAP
            while j <= len(c) and c[i:j] in materials_blob:
                j += 1
            best = max(best, j - i - 1)
            i = j
        else:
            i += 1
    if best >= _MIN_OVERLAP:
        # 数字必须一致：资料里缺数字而断言有数字，不算支撑
        nums_c = re.findall(r"\d+(?:\.\d+)?", c)
        nums_m = re.findall(r"\d+(?:\.\d+)?", materials_blob)
        if nums_c and not all(n in nums_m for n in nums_c):
            return False
        return True
    return False


def extract_claims(llm_claims: Optional[Sequence[Any]] = None,
                   materials: Optional[Sequence[Any]] = None) -> List[Claim]:
    """把模型给出的断言转成 Claim；**模型自带的 verdict 一律丢弃**。"""
    from .. import storage as st

    blob = _materials_text(materials)
    out: List[Claim] = []
    for raw in llm_claims or []:
        text = claim_text_canonical(str(raw.get("text", "") if isinstance(raw, dict) else raw))
        if not text:
            continue
        verdict = "supported" if _supported(text, blob) else "unverified"
        out.append(Claim(id=st.new_id("claim"), text=text, verdict=verdict))
    return out


def review_candidate(*, text: str, title: str = "", claims: Optional[List[Claim]] = None,
                     dedup: Optional[Dict[str, Any]] = None,
                     materials: Optional[Sequence[Any]] = None,
                     target_duration_ms: int = 0,
                     duration_estimate_ms: int = 0,
                     require_hook: bool = True) -> QualityReport:
    """逐项检查并给出 QualityReport。硬门禁失败不能被评分抵消。"""
    checks: List[QualityCheck] = []
    claims = claims or []
    review_required = False

    # 1) 事实依据（硬）：关键断言缺依据 → review，不是 pass
    unverified = [c for c in claims if c.verdict == "unverified"]
    refuted = [c for c in claims if c.verdict == "refuted"]
    if refuted:
        checks.append(QualityCheck(code="FACT_EVIDENCE", outcome="fail", hard_gate=True,
                                   message=f"{len(refuted)} 条断言与资料冲突"))
    elif unverified:
        review_required = True
        checks.append(QualityCheck(
            code="FACT_EVIDENCE", outcome="review", hard_gate=True,
            message=(f"{len(unverified)} 条断言缺依据，已标 unverified；"
                     "需补资料/人工核验或删改后重审，模型自审不算核验")))
    else:
        checks.append(QualityCheck(code="FACT_EVIDENCE", outcome="pass", hard_gate=True,
                                   message="断言均有资料支撑或无关键断言"))

    # 1b) 断言覆盖（硬）：正文里的事实性表述必须被断言清单覆盖。
    #     这一项是**确定性**的、不看模型怎么说：清单是模型自报的，若它交回空清单，
    #     上一项就会判"无关键断言"而放行——那是拿模型自审换核验。有了本项，
    #     正文里带着数字/归因/年份的稿子无论清单怎么写都过不去，只能真去删改或补资料。
    signals = detect_fact_signals(text) if text else []
    uncovered = [s for s in signals if not _covered_by_claims(s, claims)]
    if uncovered:
        sample = "、".join(f"{s['kind']}「{s['snippet']}」" for s in uncovered[:3])
        review_required = True
        checks.append(QualityCheck(
            code="CLAIM_COVERAGE", outcome="review", hard_gate=True,
            message=(f"正文含 {len(uncovered)} 处事实性表述未被断言清单覆盖（{sample}）："
                     "断言清单由模型自报，系统不接受'清单为空即无断言'；"
                     "本项只按字面信号检测，叙述性断言是否列全仍由清单负责，不声称已穷尽")))
    else:
        checks.append(QualityCheck(
            code="CLAIM_COVERAGE", outcome="pass", hard_gate=True,
            message=("正文无字面事实性表述" if not signals
                     else f"正文 {len(signals)} 处事实性表述均已被断言清单覆盖")))

    # 2) 时效性（硬）：带**强时效**表述且无资料 → review
    timey = [m for m in STRONG_TIMELINESS_MARKERS if m in _norm(text)] if text else []
    if timey:
        supported_timey = any(
            _supported(f"{m}{'' }", _materials_text(materials)) for m in timey)
        if not supported_timey and not _materials_text(materials):
            review_required = True
            checks.append(QualityCheck(
                code="TIMELINESS", outcome="review", hard_gate=True,
                message=f"含时效表述（{'/'.join(timey[:4])}）且无资料依据，转人工复核"))
        else:
            checks.append(QualityCheck(code="TIMELINESS", outcome="pass", hard_gate=True,
                                       message="时效表述有资料或已核验"))
    else:
        checks.append(QualityCheck(code="TIMELINESS", outcome="pass", hard_gate=True,
                                   message="无时效敏感表述"))

    # 3) 重复（硬）：只有**正文重复**才硬失败；主题/观点相同不等于重复，
    #    未标定时高相似只能判 review，不能伪报无重复。
    d = dedup or {}
    max_sim = float(d.get("max_similarity") or 0.0)
    # 旧调用方只给 max_similarity（无分层），按最保守方式当正文相似处理
    text_sim = float(d.get("text_similarity")
                     if d.get("text_similarity") is not None else max_sim)
    calibrated = bool(d.get("embedding_calibrated"))
    if text_sim >= TEXT_DUP_THRESHOLD:
        checks.append(QualityCheck(code="DUPLICATE", outcome="fail", hard_gate=True,
                                   message=f"与内部历史正文相似度 {text_sim:.2f}，疑似重复"))
    elif max_sim >= min(TOPIC_THRESHOLD, VIEWPOINT_THRESHOLD):
        review_required = True
        checks.append(QualityCheck(
            code="DUPLICATE", outcome="review", hard_gate=False,
            message=(f"与内部历史同题材/同观点（相似度 {max_sim:.2f}）但正文不同："
                     "主题相同不等于重复，需人判断是否只是换个说法")))
    elif not calibrated:
        review_required = True
        checks.append(QualityCheck(
            code="DUPLICATE", outcome="review", hard_gate=False,
            message="查重未标定（embedding/重排未选型），不得伪报无重复"))
    else:
        checks.append(QualityCheck(code="DUPLICATE", outcome="pass", hard_gate=False,
                                   message=f"最大相似度 {max_sim:.2f}，未命中"))

    # 4) 可读性 / 口语（软）
    body = _norm(text)
    long_sent = max((len(s) for s in re.split(r"[。！？!?]", body) if s), default=0)
    checks.append(QualityCheck(
        code="READABILITY",
        outcome="fail" if long_sent > 60 else "pass", hard_gate=False,
        message=f"最长句 {long_sent} 字" + ("（过长，不利朗读）" if long_sent > 60 else "")))

    # 5) 开头吸引力（软）。参考驱动的新流程允许多种钩子，不再强制以提问开头。
    if require_hook:
        head = body[:30]
        hooky = ("？" in head or "?" in head or "你" in head or "为什么" in head
                 or "有没有" in head)
        checks.append(QualityCheck(code="HOOK_STRENGTH",
                                   outcome="pass" if hooky else "review",
                                   hard_gate=False,
                                   message="开头提出具体问题" if hooky else "开头未形成明确问题"))
        if not hooky:
            review_required = True

    # 6) 时长拟合（软）
    if duration_estimate_ms and target_duration_ms:
        ratio = duration_estimate_ms / target_duration_ms
        if ratio > 1.3 or ratio < 0.7:
            review_required = True
            checks.append(QualityCheck(
                code="DURATION_FIT", outcome="review", hard_gate=False,
                message=f"估算 {duration_estimate_ms}ms 与目标 {target_duration_ms}ms 偏差过大"))
        else:
            checks.append(QualityCheck(code="DURATION_FIT", outcome="pass", hard_gate=False,
                                       message=f"估算 {duration_estimate_ms}ms，拟合目标"))

    hard_fail = any(c.hard_gate and c.outcome == "fail" for c in checks)
    overall = "fail" if hard_fail else ("review" if review_required else "pass")
    return QualityReport(checks=checks, overall=overall, review_required=review_required)
