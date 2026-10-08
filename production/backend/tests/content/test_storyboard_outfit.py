"""The same outfit policy reaches every text-model stage, including repair/review."""
import pytest

from app.content import prompts


@pytest.mark.parametrize("stage", ["planner", "writer", "repair", "reviewer"])
def test_storyboard_models_require_reference_outfit_consistency(stage):
    identity = {"identity_id": "person", "name": "主角", "persona": "讲述者", "keywords": {"appearance": "短发"}}
    brief = {"topic": "连续场景", "content_route": "short_video_storyboard"}
    common = dict(brief=brief, reference={}, style=None, materials=[], identity=identity)
    if stage == "planner":
        system, user = prompts.planner_messages(**common)
    elif stage == "writer":
        system, user = prompts.writer_messages(**common, prompt={"prompt_text": "连续场景"})
    elif stage == "repair":
        system, user = prompts.repair_messages(**common, prompt={"prompt_text": "连续场景"}, targets=[])
    else:
        system, user = prompts.reviewer_messages(**common, candidates=[], review_ids=[])
    assert "只允许按镜头调整服装" not in user
    assert "服装" in system
    assert "参考图" in user and "所有镜头" in user
    for detail in ("颜色", "款式", "配饰"):
        assert detail in user
    if stage == "reviewer":
        assert "identity_consistency" in system and "fail" in system
