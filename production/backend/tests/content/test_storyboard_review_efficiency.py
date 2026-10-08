"""减少分镜输入重复，不裁剪事实依据、镜头字段或用户创作要求。"""
from app.content import prompts


SHOT = {"shot_index": 1, "visual": "画面唯一片段：主角望向门口", "dialogue": "对白唯一片段：先把话说清楚",
        "sound_effects": "声音唯一片段：敲门", "duration_seconds": 6, "prompt": "提示唯一片段：固定机位含原声"}
TEXT = f"镜头1：画面{SHOT['visual']}；台词/旁白{SHOT['dialogue']}；音效{SHOT['sound_effects']}"


def reviewer(candidate, **extra):
    return prompts.reviewer_messages(brief={"content_route": "short_video_storyboard", "topic": "剧情"},
        reference=None, style=None, materials=[], candidates=[candidate], review_ids=["s"], **extra)


def candidate(text=TEXT):
    return {"candidate_id": "s", "slot": 1, "angle": "角度", "title": "标题", "text": text, "storyboard": [SHOT]}


def test_full_structured_storyboard_sent_once_when_body_is_its_generated_mirror():
    _, user = reviewer(candidate())
    for field in ("visual", "dialogue", "sound_effects", "prompt"):
        assert user.count(SHOT[field]) == 1
    assert '"duration_seconds": 6' in user and '"shot_index": 1' in user
    assert "candidate_id=s" in user


def test_extra_body_text_and_complete_evidence_are_never_silently_dropped():
    _, user = reviewer(candidate(TEXT + "\n额外旁白必须保留。"))
    assert "额外旁白必须保留。" in user
    assert SHOT["prompt"] in user


def test_repair_only_sends_one_copy_of_mirrored_storyboard_fields():
    _, user = prompts.repair_messages(brief={"content_route": "short_video_storyboard"},
        prompt={"prompt_text": "保持六秒"}, reference=None, style=None, materials=[],
        targets=[{"slot": 1, "angle": "角度", "title": "标题", "text": TEXT, "storyboard": [SHOT], "problems": ["简化动作"]}])
    for field in ("visual", "dialogue", "sound_effects", "prompt"):
        assert user.count(SHOT[field]) == 1
    assert "保持六秒" in user and "简化动作" in user


def test_storyboard_reviewer_receives_same_creation_requirements_as_writer():
    _, user = reviewer(candidate(), prompt={"prompt_text": "严格十二镜头，总长七十七秒"})
    assert "严格十二镜头，总长七十七秒" in user
    assert "<creation_requirements>" in user


def test_talking_script_body_still_sent_without_structured_mirror():
    _, user = prompts.reviewer_messages(brief={"content_route": "talking_script"}, reference=None,
        style=None, materials=[], candidates=[{"candidate_id": "s", "slot": 1, "angle": "a", "title": "t", "text": TEXT}],
        review_ids=["s"])
    assert TEXT in user
