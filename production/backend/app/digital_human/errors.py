"""口播环节的两类失败语义（与 T04 保持同一分法）。

- `TalkingHeadRejected`：请求本身不成立，改输入才有意义（HTTP 422）。
  例：路线没选、音频没确认、底片与音频版本不匹配、prompt 超长。
- `TalkingHeadBlocked`：请求成立但当前不可执行，要等外部条件变化（HTTP 409）。
  例：预算未授权、GPU 被占用、对齐尚未完成、供应商未配置。

把两者混成一个"失败"是本项目明确拒绝的做法：前者重试一万次也没用，
后者重试才有意义，前端要让用户看到区别。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class TalkingHeadError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class TalkingHeadRejected(TalkingHeadError):
    """请求不成立：必须修改输入或选择后重新提交。"""


class TalkingHeadBlocked(TalkingHeadError):
    """当前不可执行：等待预算、对齐、GPU 或供应商状态变化。"""
