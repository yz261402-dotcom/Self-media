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


@pytest.mark.parametrize("field,content", [
    ("sound_effects", "室内底声；第4秒鼠标单击；第8秒一次待批准提示音。无配乐。"),
    ("sound_effects", "室内底声；第5秒鼠标单击；第8秒待批准提示音。"),
    ("sound_effects", "第3秒待批准提示音；第8秒鼠标单击。"),
    ("sound_effects", "第6秒轻微鼠标滑动声。"),
    ("sound_effects", "第10秒鼠标单击。"),
    ("sound_effects", "第5秒自然呼气；第6秒轻微布料声。"),
    ("visual", "前4秒保持一次安静停顿；主要动作是肩膀随着呼气慢慢松下来。"),
    ("visual", "第3秒转头；第5秒微笑。"),
    ("sound_effects", "第8秒画外鼠标单击；第2秒轻微手指触杯声；第5秒画外点击。"),
    ("sound_effects", "第1秒杯底离桌轻响；第4、7、10秒各一次提示音。"),
    ("sound_effects", "第7秒手掌落桌轻响；第14秒低调状态提示音。"),
    ("sound_effects", "第9秒单击；第14秒一次轻柔确认音。"),
    ("sound_effects", "第3秒触杯声；第10秒轻微饮用声；第9秒杯底离桌声。"),
    ("sound_effects", "前3秒预览室内底声与轻微呼气；第4秒轻柔结束提示音。"),
    ("sound_effects", "第5秒袖口与桌面轻微摩擦；第6秒细微布料声。"),
    ("visual", "主要动作在前3秒完成：她迅速而合理地放松肩部并展开微笑。"),
    ("visual", "前4秒她保持有意识的安静停顿；主要动作是缓缓呼气。"),
])
def test_standalone_action_and_sound_offsets_are_production_instructions(field, content):
    assert _review(**{field: content})["CLAIM_COVERAGE"] == "pass"


@pytest.mark.parametrize("field,content", [
    ("dialogue", "第4秒鼠标单击；第8秒一次待批准提示音。"),
    ("dialogue", "前4秒保持一次安静停顿。"),
    ("visual", "屏幕字幕：第4秒鼠标单击。"),
    ("sound_effects", "新闻播报：第6秒轻微布料声。"),
    ("sound_effects", "“第5秒自然呼气；第6秒轻微布料声。”"),
    ("visual", "设备在第4秒响应完成。"),
    ("sound_effects", "第4秒鼠标单击；官方数据显示性能提升20%。"),
    ("visual", "前4秒保持一次安静停顿；屏幕展示价格99元。"),
    ("sound_effects", "第4秒声音识别系统响应完成。"),
    ("sound_effects", "第3秒设备发声后完成响应。"),
    ("sound_effects", "第2秒产品录音功能完成检测。"),
    ("sound_effects", "4秒音频延迟。"),
    ("sound_effects", "4秒鼠标点击延迟。"),
    ("sound_effects", "第4秒鼠标单击响应完成。"),
])
def test_production_offset_filter_does_not_erase_reported_or_real_facts(field, content):
    assert _review(**{field: content})["CLAIM_COVERAGE"] == "review"


@pytest.mark.parametrize("visual", [
    "预览全镜静止，人物消失是镜头间出现的新版本结果，不是镜内变化。",
    "镜头切到编辑器的新版本画面。",
])
def test_local_draft_version_description_is_not_product_release_information(visual):
    assert _review(visual=visual)["TIMELINESS"] == "pass"


@pytest.mark.parametrize("field,content", [
    ("dialogue", "这是新版本结果。"),
    ("visual", "屏幕字幕：这是新版本结果。"),
    ("visual", "展示最新发布的新版本手机。"),
    ("visual", "新版本模型今天上线。"),
    ("sound_effects", "新闻播报：新版产品刚发布。"),
    ("visual", "人物打开新版本画面；屏幕显示新品本月发布。"),
    ("visual", "展示软件产品的新版本画面。"),
    ("visual", "屏幕展示新版本画面。"),
    ("visual", "镜头预览软件产品的新版本画面。"),
    ("visual", "屏幕展示文字：编辑器的新版本画面。"),
    ("visual", "屏幕展示编辑器的新版本画面。"),
])
def test_product_version_and_release_claims_remain_subject_to_evidence(field, content):
    assert _review(**{field: content})["TIMELINESS"] == "review"
