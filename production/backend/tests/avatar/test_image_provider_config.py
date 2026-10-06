"""GPT Image 2.5 默认模型配置契约。"""
from __future__ import annotations

from app import config


def test_gpt_image_provider_defaults_to_image_2_5_sunburst():
    spec = config.PROVIDERS["gpt-image-2"]
    assert spec.default_model == "gpt-image-2.5-sunburst"
