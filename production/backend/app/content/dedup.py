"""内部历史查重：主题 / 观点 / 正文三层，只查本地业务库，不联网。

诚实边界：embedding 与重排器尚未选型标定，因此这里只做确定性文本近似，
结果一律标 `approximate`；**未标定时不得伪报"无重复"**，
查重不可用应返回 not_run 而不是放行。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .. import storage as st

# 主题/观点层用较低阈值（更容易命中），正文层用较高阈值（避免误判改写为重复）
TOPIC_THRESHOLD = 0.45
VIEWPOINT_THRESHOLD = 0.50
TEXT_THRESHOLD = 0.62


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _bigrams(s: str) -> set:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def text_similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度。确定性、可离线复算，不依赖任何模型。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def internal_history(work_id: str) -> List[Dict[str, Any]]:
    """读取本工程内部历史：其他作品的选题与既有稿件正文。"""
    rows: List[Dict[str, Any]] = []
    for w in st.all_rows("works"):
        if w.get("id") == work_id:
            continue
        topic = (w.get("brief") or {}).get("topic", "")
        if topic:
            rows.append({"layer": "topic", "source": "work", "id": w["id"],
                         "title": w.get("name", ""), "text": "", "topic": topic})
    for b in st.all_rows("script_batches"):
        if b.get("work_id") == work_id:
            continue
        for c in b.get("candidates", []):
            rows.append({"layer": "text", "source": "script_batch", "id": c.get("id", ""),
                         "title": c.get("title", ""), "text": c.get("text", ""),
                         "topic": (c.get("angle") or "")})
    return rows


def dedup_report(work_id: str, text: str, title: str = "", topic: str = "") -> Dict[str, Any]:
    """三层查重。返回命中与是否标定；未标定时结论只能是近似。"""
    history = internal_history(work_id)
    hits: List[Dict[str, Any]] = []
    for h in history:
        sims = {}
        if topic and h.get("topic"):
            sims["topic"] = round(text_similarity(topic, h["topic"]), 4)
        if title and h.get("title"):
            sims["viewpoint"] = round(text_similarity(title, h["title"]), 4)
        if text and h.get("text"):
            sims["text"] = round(text_similarity(text, h["text"]), 4)
        if not sims:
            continue
        best = max(sims.values())
        if best >= min(TOPIC_THRESHOLD, VIEWPOINT_THRESHOLD, TEXT_THRESHOLD):
            hits.append({"id": h["id"], "source": h["source"], "similarity": best,
                         "layers": sims})
    hits.sort(key=lambda x: x["similarity"], reverse=True)

    def layer_max(layer: str) -> float:
        return max((h["layers"].get(layer, 0.0) for h in hits), default=0.0)

    # embedding/重排未选型 → 不标 calibrated，下游不得据此断言"确认无重复"
    return {
        "hits": hits[:10],
        "max_similarity": hits[0]["similarity"] if hits else 0.0,
        # 分层给最大相似度：**正文重复**才是硬重复；主题/观点相同不等于重复
        # （规格 03：「主题相同并不必然重复；只换标题、措辞而重复观点和案例应被识别」）。
        # 只报一个 max 会把"同题材的不同稿子"误判成重复稿。
        "text_similarity": layer_max("text"),
        "topic_similarity": layer_max("topic"),
        "viewpoint_similarity": layer_max("viewpoint"),
        "thresholds": {"topic": TOPIC_THRESHOLD, "viewpoint": VIEWPOINT_THRESHOLD,
                       "text": TEXT_THRESHOLD},
        "embedding_calibrated": False,
        "method": "char_bigram_jaccard",
        "note": ("embedding/重排器未选型标定，结果为确定性文本近似；"
                 "未标定时不得伪报无重复"),
    }
