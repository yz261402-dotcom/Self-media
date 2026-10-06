"""T04 音色试听、音频确认与统一表达编译。

本包只做语音链路：音色资产作用域、表达编译、试听与全文合成、音频门禁与对齐登记。
不声称做过未验证的事——未实测的能力一律标为未核验，
未接入的检测能力一律记 `not_run` 而不是"通过"。
"""
from . import audio, compiler, gate, plan, preview, provider, sources, voices

__all__ = ["voices", "sources", "plan", "compiler", "provider", "preview",
           "audio", "gate"]
