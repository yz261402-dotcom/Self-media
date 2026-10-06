"""内容链路的两类失败：请求不成立（改输入）与当前不可执行（等条件）。

与 TTS 保持同一语义：Rejected → 调用方应改输入；Blocked → 条件不满足，
不重试计费动作。不把两者合并成一种，避免"看起来能重试其实永远不通"。
"""
from __future__ import annotations


class ContentError(Exception):
    code = "CONTENT_ERROR"

    def __init__(self, message: str, code: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ContentRejected(ContentError):
    """请求本身不成立：改输入才能继续。"""
    code = "CONTENT_REJECTED"


class ContentBlocked(ContentError):
    """当前不可执行：缺凭据/未授权/能力未就绪，等条件而不是改输入。"""
    code = "CONTENT_BLOCKED"

    def __init__(self, message: str, code: str = "", detail: str = "", retryable: bool = True):
        super().__init__(message, code, detail)
        self.retryable = retryable
