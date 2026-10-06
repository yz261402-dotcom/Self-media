"""T06 内容策划：提示词直接生成三稿、内部查重与事实风险审核。

红线（对应 03/10 文档 v2.4）：
- **不联网**：稿件只来自用户提示词、关键词、受众、时长与可选用户资料；
  没有任何搜索/抓取工具入口，也不接受模型自称"已检索"。
- **不编造证据**：断言（Claim）的 verdict 只由用户资料/已审知识支撑；
  模型自审不算事实核验，缺依据一律 `unverified` 并转人工复核。
- **不伪造生成**：凭据缺失或未授权时显式 blocked，不返回伪装成模型输出的文本。
"""
from .errors import ContentBlocked, ContentRejected
from .llm import deepseek_json, llm_status
from .dedup import dedup_report, internal_history
from .review import review_candidate, extract_claims
from .generate import generate_candidates, build_generation_messages
from .rewrite import RewriteLedger

__all__ = [
    "ContentBlocked", "ContentRejected",
    "deepseek_json", "llm_status",
    "dedup_report", "internal_history",
    "review_candidate", "extract_claims",
    "generate_candidates", "build_generation_messages",
    "RewriteLedger",
]
