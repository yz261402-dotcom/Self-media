"""Mock 内容生成：不调用任何付费模型、不产生真实媒体字节。

所有 mock 产物必须携带来源与模式标记，界面与报告显示“MOCK / 结构测试”，
不得冒充真实 Provider 结果，也不得进入生产验收统计。
"""
from __future__ import annotations

from typing import List

from .contracts import Claim, QualityCheck, QualityReport, ScriptVersion
from .storage import new_id

MOCK_NOTE = "MOCK：未调用真实服务，不能作为质量验收证据"


def _checks(extra_fail: str | None = None) -> QualityReport:
    base = [
        QualityCheck(code="FACT_SUPPORT", outcome="pass", hard_gate=True,
                     message="关键断言均可定位到已存档来源（MOCK 结论）"),
        QualityCheck(code="DEDUP_TOPIC", outcome="pass", hard_gate=True, message="主题层未命中重复"),
        QualityCheck(code="DEDUP_VIEWPOINT", outcome="pass", hard_gate=True, message="观点层未命中重复"),
        QualityCheck(code="DEDUP_TEXT", outcome="pass", hard_gate=True, message="文本层相似度低于阈值"),
        QualityCheck(code="TYPO", outcome="pass", hard_gate=True, message="未发现错别字与术语误写"),
        QualityCheck(code="ORAL_FLUENCY", outcome="pass", hard_gate=False, message="口语流畅度良好"),
    ]
    if extra_fail:
        base.append(QualityCheck(code=extra_fail, outcome="fail", hard_gate=True,
                                 message="存在硬门禁失败，需要重写或人工复核"))
        return QualityReport(checks=base, overall="fail", review_required=True)
    base.append(QualityCheck(code="HOOK_STRENGTH", outcome="pass", hard_gate=False,
                             message="开头 3 秒提出了具体问题"))
    return QualityReport(checks=base, overall="pass", review_required=False)


def make_candidates(topic: str) -> List[ScriptVersion]:
    base = topic or "RAG 如何补充模型回答所需的信息"
    items = [
        ("生活类比", "为什么 AI 读了你的文件，还是答错了",
         "开头：你有没有遇到过，把资料丢给 AI，它却答非所问？\n\n"
         "解释：问题往往不在模型，而在检索。RAG 先去找资料，再让模型读资料作答。找错了资料，后面再聪明也没用。\n\n"
         "例子：同一份文档，切片太大，答案就被无关段落稀释；切片太小，又丢了上下文。\n\n"
         "边界：RAG 不能凭空补全文档里没有的信息。\n\n"
         "收尾：下次遇到答错，先问一句：它到底检索到了什么。", None),
        ("常见误区", "三个最常见的 RAG 误区",
         "开头：很多人以为接了向量库就是 RAG，其实只完成了一半。\n\n"
         "解释：误区一是只看相似度分数；误区二是切片一刀切；误区三是忽略时效性。\n\n"
         "例子：把一年前的价格当成今天的报价，就是时效没管好。\n\n"
         "边界：本文只讲检索环节，不讨论模型微调。\n\n"
         "收尾：把这三件事做对，效果往往比换模型更明显。", None),
        ("实战案例", "一次把回答准确率救回来的排查过程",
         "开头：一条问答准确率只有四成，我们花了两小时定位到根因。\n\n"
         "解释：先查召回，再查切片，最后查提示词，顺序不能反。\n\n"
         "例子：召回阶段命中率只有 55%，把切片从 1200 字改成 400 字后提升到 88%。\n\n"
         "边界：这个数字只属于当时的语料，不能外推。\n\n"
         "收尾：先量再改，别凭感觉调参数。", "HOOK_STRENGTH"),
    ]
    out = []
    for angle, title, text, fail in items:
        out.append(
            ScriptVersion(
                id=new_id("script"),
                angle=angle,
                title=title,
                description=f"{title}｜围绕“{base}”的一个独立切入角度",
                tags=["AI科普", "RAG", "实用技巧"],
                text=text,
                duration_estimate_ms=58000,
                quality_report=_checks(fail),
                claims=[
                    Claim(id=new_id("claim"), text="RAG 先检索外部资料再生成答案", verdict="supported"),
                    Claim(id=new_id("claim"), text="检索质量决定最终回答质量", verdict="supported"),
                ],
            )
        )
    return out


PACKAGE_FILES = [
    "video.mp4",
    "cover.jpg",
    "subtitles.srt",
    "title.txt",
    "description.txt",
    "tags.txt",
    "ai_label.txt",
    "manifest.json",
]
