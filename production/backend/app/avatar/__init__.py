"""T03 人物库、人物候选与衣景动作资产。

本包只做登记、门禁与映射。未经验证的图像编辑接口不标为已支持，
Mock 资产不进入生产，权属未确认的方案保持阻塞。
"""
from . import identity_gate, licensing, motion, registry

__all__ = ["registry", "identity_gate", "motion", "licensing"]
