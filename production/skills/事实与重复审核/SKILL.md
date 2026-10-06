# S03 事实与重复审核

版本：0.3.0 · 使用者：内容审核（quality）· 验证范围：离线契约

## 1. 触发条件
`ScriptBatch` 已产生候选稿，需要逐稿形成审核结论并给出返工目标时。

## 2. 输入引用
`ScriptVersion.text` 与其 `claims`、用户提供或已有已核资料的 `ContentSource` 摘录、业务库历史命中、`QualityDecision` 既有结论。

## 3. 工作步骤
1. 抽取断言（数据、价格、版本、政策、时间点），对照用户资料、已有已核知识或人工证据。禁止网上搜索/抓取；关键断言无依据转 review，补充材料或删改后重审，模型自审不构成事实核验。
2. 做历史主题/观点/文本相似检查；可用时给相似度，不可用时明确记 `not_run`。
3. 口播稿检查自然表达、可朗读性、观点和标题一致性；分镜剧本检查镜头可制作性、声画一致性、镜头时长和叙事衔接。
4. 输出 `QualityReport.checks`，每项标注 `hard_gate`；硬门禁失败必须给出 `rework_target`。
5. 结论只能是 `pass/fail/review/not_run`；缺证据时写 `review`，不写 `pass`。
6. 使用与写作方独立的 Qwen 上下文审核，绑定正文、完整分镜、参考、资料、风格、提示和版本；再检查三个候选的角度差异。只通过一两篇不能对用户展示或允许采用。

## 4. 允许调用的工具
`search_content_history`、`submit_quality_report`、`propose_memory`

## 5. 输出契约
`QualityDecision`（`subject_id/subject_digest/ruleset_version/recommendation/issues[]`），
`AgentResult.output_refs` 含 `quality_decision_id(s)`。

## 6. 拒收 / 返工条件
- 无证据却判事实正确 → 拒收。
- 查重不可用仍放行 → 拒收。
- 试图改写已有检查项结论或清除硬门禁失败 → 业务层拒绝（`QUALITY_REPORT_TAMPERING`）。

## 7. 正反例
正例："『2025 年市场规模 300 亿』缺乏可定位来源，判 `review` 并要求补证据"。
反例："未查证但语气肯定，直接判 `pass`"。

## 8. 版本
0.3.0（2026-09-29 仅审核内容，完整三稿及集合审核门禁；语义审核质量尚未真实验收）
