"""后期媒体处理的显式失败。

约定与 TtsBlocked/TtsRejected 同一取向：block 是"当前不可执行，等条件"，
不是异常崩溃；code 会成为任务 block_reason，必须自解释。
"""
from __future__ import annotations


class VideoBlocked(Exception):
    """后期阶段被显式阻断；code 稳定、message 面向人。"""

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


class VideoRejected(Exception):
    """输入本身不成立（改输入才能继续），用于参数/时间轴非法。"""

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}
