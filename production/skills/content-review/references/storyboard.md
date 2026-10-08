# 分镜审核与人工流程

用于 `content_route=short_video_storyboard`。读取完整结构化分镜、当前创作要求、人物基准及证据，不能只依据派生阅读文本判断。

## 逐稿标准

检查 `originality/reference_reuse/evidence_support/style_fit/no_fabrication`，另检查：

- `shot_feasibility`：镜头编号从 1 连续递增，每镜 2 到 30 秒整数时长；画面、对白、声音和提示完整且一致。遵循用户指定剧情、镜头数及总时长，清楚首帧加一个主要动作，不在单镜头安排多次场景硬切或不可能的机位关系；对白长度匹配时长。
- `identity_consistency`：沿用已选数字人物的面部、发型、年龄感、身材比例与身份；服装颜色、款式、层次、材质、图案和配饰跨镜一致。擅自换装、变色、增减衣物或逐镜另造服装必须失败。相同场景的布局、道具和状态连续，合理场景切换可改变背景。

Wan 原生生成声音，不要求额外配音、口型同步或尚未具备的屏幕跟踪合成；不能因未使用这些流程判失败。具体制作问题须在给定时长与镜头数内提供可行建议，个人润色偏好或一般生成风险不单独判失败。

参考画面观察不是事实来源；原视频动作与时段须对应已有 `visual.scenes`，无口播时不要求或编造原台词。原创分镜可以设计新动作。制作计时及虚构日常话语不等于现实新闻事实，现实价格、发布信息、数据和机构结论仍须证据。

每稿将同类失败合并，最多三条 finding；`quote` 只摘该镜头 `visual/dialogue/sound_effects` 原文，不摘 JSON 字段名或仅在 `prompt` 中出现的生成说明。全部标准通过时 findings 为空。三篇可以共享人物和设定；集合差异看叙事视角、笑点推进和落点。

## 模型输出契约

```json
{
  "candidates": [
    {
      "candidate_id": "给定 ID",
      "criteria": {
        "originality": "pass",
        "reference_reuse": "pass",
        "evidence_support": "pass",
        "style_fit": "pass",
        "shot_feasibility": "pass",
        "identity_consistency": "pass",
        "no_fabrication": "pass"
      },
      "findings": []
    }
  ],
  "set": {"distinct": true, "rewrite_slots": [], "findings": []}
}
```

每项为 `pass/fail`，仅审核 `review_ids`。模型审核失败仍展示当前草稿，供人工编辑、保存新版本或重新审核。当前单篇模型审核通过即可进入人工审核，不要求整组三篇全部通过，不自动发起返工。

## 人工评分与采用

模型检查证据与可制作性，人工表判断吸引力和主观创作品质。现有 `core5` 检查 `hook/momentum/interaction/empathy/structure`，`extended8` 另含 `value/delivery/execution`。所选表各项为 1 到 5 的整数，全部填写；平均分必须严格大于 4 才能评分通过，恰好 4 不通过。

模型审核通过后的候选有三种人工动作：

- `adopt`：评分通过后采用原稿，记录 `scored_pass`。
- `edit_adopt`：人工修改完整分镜后直接确认采用，记录 `human_edit` 与新版本、原稿和评分绑定；评分未通过也可走此路径，不能伪装成新的独立模型通过。
- `rewrite`：输入具体意见，仅对所选稿定向重写，保存反馈，重写版本重新接受独立模型审核，再进入人工流程；不因评分失败自动调用模型。

审核和采用绑定当前稿件、展示信息、批次、创作修订及输入摘要；内容变更后不沿用旧结论。人工编辑直接采用仍由业务层校验完整结构与当前绑定，Skill 不改写旧审核或绕过业务接口。

人工导入来源记录为 `manual`，`ai_review_status=not_performed`，保留导入来源、接管原因与人工事实确认；不生成虚假的模型、独立审核或三篇集合审核。评分通过的原稿接管记录 `manual_takeover`，人工修改采用记录 `human_edit`。人工导入没有原三稿模型批次，意见重写需返回生成流程建立新批次，不伪装成原批次返工。
