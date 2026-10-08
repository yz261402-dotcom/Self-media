"""真实分镜中的音乐调度秒数不应当被误认成需要资料证明的事实。"""
from copy import deepcopy

import pytest

from app.content.review import review_candidate, storyboard_review_texts


def _review(**fields):
    shot = {"visual": "人物看向电脑。", "dialogue": "这次总算可以了。",
            "sound_effects": "安静室内环境声。", **fields}
    original = deepcopy(shot)
    texts = storyboard_review_texts([shot])
    report = review_candidate(text="\n".join(str(v) for v in shot.values()),
                              require_hook=False, **texts)
    assert shot == original
    return {check.code: check.outcome for check in report.checks}


@pytest.mark.parametrize("sound", [
    # work_eb00e30a67f9 的候选 2 / 3 第 12 镜头，按真实原文固定复现。
    "低音量音乐在第6秒前结束；最后两秒只有安静室内环境声。无呼气、喘气或叹气声。",
    "低音量音乐在第6秒前结束；最后两秒只保留安静室内环境声。无呼气、喘气或叹气声。",
    "配乐在第6秒后渐出。",
])
def test_music_schedule_with_before_or_after_is_not_a_factual_claim(sound):
    assert _review(sound_effects=sound)["CLAIM_COVERAGE"] == "pass"


@pytest.mark.parametrize("field,content", [
    ("dialogue", "低音量音乐在第6秒前结束。"),
    ("visual", "屏幕字幕：低音量音乐在第6秒前结束。"),
    ("sound_effects", "新闻播报：低音量音乐在第6秒前结束。"),
    ("sound_effects", "广播播报：低音量音乐在第6秒前结束。"),
    ("visual", "屏幕写着：系统在第6秒前结束响应。"),
    ("sound_effects", "系统在第6秒前结束响应。"),
    ("sound_effects", "音乐识别系统在第6秒前结束响应。"),
    ("sound_effects", "“低音量音乐在第6秒前结束。”"),
    ("sound_effects", "低音量音乐在第6秒前结束；新闻播报：模型推理耗时2秒。"),
])
def test_reported_content_and_device_facts_keep_their_numeric_signal(field, content):
    assert _review(**{field: content})["CLAIM_COVERAGE"] == "review"


@pytest.mark.parametrize("dialogue", [
    "官方今天先不验收。", "新闻播报今天先不验收。", "客户今天不验收。",
    "今天的价格是99元。", "新品今天发布。",
])
def test_today_statements_with_possible_real_world_context_remain_gated(dialogue):
    assert _review(dialogue=dialogue)["TIMELINESS"] == "review"
