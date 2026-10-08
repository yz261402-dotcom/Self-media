"""Storyboard timing and presentation requirements follow the stored schema."""
import copy
import json

import pytest

from app.content import prompts, semantic_review, service
from app.content.errors import ContentRejected
from tests.content._helpers import review_json


SHOTS = [
    {"shot_index": 1, "visual": "场景A，固定机位：人物放下杯子。", "dialogue": "先看看。",
     "sound_effects": "杯底轻响。", "duration_seconds": 6, "prompt": "工作室，固定机位，原声。"},
    {"shot_index": 2, "visual": "场景A，人物在屏幕前停下。", "dialogue": "无",
     "sound_effects": "安静的环境声。", "duration_seconds": 4, "prompt": "中景，人物停下，无人声。"},
    {"shot_index": 3, "visual": "场景B，门口侧拍人物离开。", "dialogue": "走吧。",
     "sound_effects": "脚步声。", "duration_seconds": 8, "prompt": "门口，侧拍，原声。"},
]
REQUIREMENTS = "必须十八秒、三个镜头；最后人物离开。逐镜标注场景编号、景别运镜及起止时间，列总时长。"


@pytest.mark.parametrize("stage", ["review", "repair"])
def test_saved_shot_durations_produce_complete_timeline_without_mutating_script(stage):
    shots = copy.deepcopy(SHOTS)
    # Unsupported presentation fields are dropped by the real persistence normalizer.
    raw = [dict(s, scene_id=f"scene_{i}", start_seconds=999) for i, s in enumerate(shots)]
    stored, text, total_ms = service._storyboard(raw, 0)
    before = copy.deepcopy(stored)
    common = {"brief": {"content_route": "short_video_storyboard"}, "prompt": {"prompt_text": REQUIREMENTS},
              "reference": None, "style": None, "materials": []}
    if stage == "review":
        _, user = prompts.reviewer_messages(**common, candidates=[{
            "candidate_id": "s", "slot": 1, "angle": "视角", "title": "标题", "text": text,
            "storyboard": stored}], review_ids=["s"])
    else:
        _, user = prompts.repair_messages(**common, targets=[{
            "slot": 1, "angle": "视角", "title": "标题", "text": text, "storyboard": stored,
            "problems": ["结尾动作要落实"]}])
    timing = json.loads(user.split("<storyboard_timing_data>\n", 1)[1].split("\n</storyboard_timing_data>", 1)[0])
    assert timing == {"shot_count": 3, "total_duration_seconds": 18, "shots": [
        {"shot_index": 1, "start_seconds": 0, "end_seconds": 6},
        {"shot_index": 2, "start_seconds": 6, "end_seconds": 10},
        {"shot_index": 3, "start_seconds": 10, "end_seconds": 18}]}
    assert total_ms == 18000
    assert stored == before
    assert all("scene_id" not in shot and "start_seconds" not in shot for shot in stored)
    assert REQUIREMENTS in user
    for shot in stored:
        for field in ("visual", "dialogue", "sound_effects", "prompt"):
            if shot[field] != "无":
                assert user.count(shot[field]) == 1


@pytest.mark.parametrize("stage", ["plan", "write", "repair", "review"])
def test_storyboard_stages_share_supported_field_mapping_and_keep_story_requirements(stage):
    common = {"brief": {"content_route": "short_video_storyboard"}, "reference": None,
              "style": None, "materials": []}
    if stage == "plan":
        system, _ = prompts.planner_messages(**common)
    elif stage == "write":
        system, _ = prompts.writer_messages(**common, prompt={"prompt_text": REQUIREMENTS})
    elif stage == "repair":
        system, _ = prompts.repair_messages(**common, prompt={"prompt_text": REQUIREMENTS}, targets=[])
    else:
        system, user = prompts.reviewer_messages(**common, prompt={"prompt_text": REQUIREMENTS},
                                                 candidates=[], review_ids=[])
        assert REQUIREMENTS in user
    assert "起止时间与总时长由 duration_seconds 累加派生" in system
    assert "场景编号、景别与运镜写入 visual 或 prompt" in system
    assert "不能仅因缺少独立的展示字段判失败" in system
    assert "用户明示的剧情、镜头数、总时长和连续性仍须逐项核对" in system


def test_talking_script_has_no_storyboard_timeline_or_format_rules():
    system, user = prompts.reviewer_messages(brief={"content_route": "talking_script"},
        reference=None, style=None, materials=[], candidates=[{
            "candidate_id": "s", "slot": 1, "angle": "视角", "title": "标题", "text": "真实口播正文。"}], review_ids=["s"])
    assert "storyboard_timing_data" not in user
    assert "起止时间与总时长由 duration_seconds" not in system
    assert "真实口播正文。" in user


def test_derived_timing_metadata_cannot_be_used_as_a_quote_from_the_script():
    stored, text, _ = service._storyboard(copy.deepcopy(SHOTS), 0)
    version = {"id": "s", "slot": 1, "text": text, "storyboard": stored,
               "content_route": "short_video_storyboard"}
    invalid = review_json(["s"], fail=("s",), quote="start_seconds", storyboard=True)
    with pytest.raises(ContentRejected) as rejected:
        semantic_review.parse_review(invalid, review_ids=["s"], versions=[version])
    assert rejected.value.code == "REVIEW_QUOTE_INVALID"
