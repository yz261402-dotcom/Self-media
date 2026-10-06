"""T04 语音链路的拒绝/阻塞异常。

两类语义必须分开，前端与编排层据此给出不同的下一步：

- `TtsRejected`：**请求本身不成立**（作用域不符、表达越界、版本错配）。
  重试同样输入没有任何意义，必须先修输入或重新选择。
- `TtsBlocked`：**输入成立但当前不能执行**（未配置供应商、预算未授权、
  能力未实测、上游未确认）。输入不变时重试也不会变好，需外部条件变化。

两者都带 `code`，便于前端与测试按码断言，而不是靠匹配中文消息。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class TtsError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class TtsRejected(TtsError):
    """请求不成立：修输入才能继续。"""


class TtsBlocked(TtsError):
    """当前不可执行：等外部条件（配置/预算/实测/上游确认）变化。"""
