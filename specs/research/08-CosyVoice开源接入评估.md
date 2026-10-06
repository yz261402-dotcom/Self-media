# CosyVoice 开源接入评估

调研日期：2026-09-28  
上游仓库：[`QwenAudio/CosyVoice`](https://github.com/QwenAudio/CosyVoice)  
核对的 `main` 提交：[`074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`](https://github.com/QwenAudio/CosyVoice/commit/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc)

## 结论

CosyVoice 3 值得作为可切换 TTS 引擎做隔离试音。它提供指令式语气、情绪、语速和音量控制，也可用参考音频保持目标说话人；这些能力与本项目“声音太像 AI、缺少语气控制”的诉求相关。但官方同时说明，指令控制范围有限，不能假设任意中文描述都会稳定生效。它也不能单靠换模型保证长文段间的语气与音色连续。

更直接的发现是：当前问题不全在 MiniMax。项目自动生成的表演计划把每段固定成 `calm / normal`，语音页没有语气编辑入口；编译器虽检查局部表达意图，最终合成仍只提交一组全局参数和整篇文本。即使现在只换成 CosyVoice，默认请求也没有表达信息可供它执行。因此建议先做一条隔离的试听验证，再决定是否正式接入；并把“语气入口和参数传递”视为接入的必要条件。

## 对项目的匹配度

| 需求 | CosyVoice 能力与限制 | 对本项目的判断 |
|---|---|---|
| 设置语气、情绪、语速 | README 声称支持语言、方言、情绪、速度和音量指令；官方示例通过 `inference_instruct2(text, instruct_text, prompt_wav)` 传入独立指令。 | 能提供当前 MiniMax 链路没有的自然语言指令接口。先验证少量稳定指令，例如“自然、亲切，像知识类短视频主播，语速稍快”，不能宣称任意风格都可控。 |
| 降低 AI 感 | CosyVoice 3 README 说相较 2.0 改善韵律自然度、内容一致性和说话人相似度。 | 这是上游自述，不等于在本项目音色、文案和听众上的实测结论；需要与现有音轨做盲听 A/B。 |
| 保持固定主播 | 支持零样本参考音频；`inference_instruct2` 接受提示音频。 | 可用授权的主播参考音频固定声音身份。参考提示音频有最长 30 秒的限制；这是输入参考音频限制，不是生成总时长限制。 |
| 生成长口播 | 推理前端会把文本按句规范化、拆分后逐段合成；支持流式输入/输出。没有看到总输出音频只能小于 60 秒的接口限制。 | 有机会绕开 OmniHuman 的单段音频限制来生成较长 TTS；不能据此推断 OmniHuman 接受长音频。长文本耗时、内存、失败率和重试边界仍需实测。 |
| 长文连贯 | 官方 issue 中有流式推理音色变化、切段情绪不连续的报告；维护者解释训练按整段语音进行，流式推理可能有性能退化，并建议 `stream=False`。 | 避免应用层大量并行、独立调用后再拼接。第一阶段尽量整稿调用模型的非流式路径，让模型自己做句级前处理；最终仍须试听跨句、跨段连续性。 |
| 部署 | 官方建议 Python 3.10，提供 FastAPI/gRPC 示例和 NVIDIA GPU Docker 部署路径。 | 不适合塞进现有后端环境：本机项目 venv 是 Python 3.13，后端没有 PyTorch/CosyVoice 依赖；应独立部署在云 GPU 服务或独立容器，并纳入现有 GPU 排队策略。 |

## 当前链路的实际阻塞点

- [`plan.py`](../../production/backend/app/tts/plan.py) 的 `derive_segments()` 将每段写为 `emotion="calm"`、`pace="normal"`、`emphasis=[]`，没有用户可编辑的计划入口。
- [`TtsView.vue`](../../production/frontend/src/views/TtsView.vue) 提供音色筛选、试听、全文合成和确认，没有语气或情绪控件。
- [`voices.py`](../../production/backend/app/tts/voices.py) 只白名单 `minimax / speech-2.8-hd`，预设音色的验证情绪仅为 `calm`；[`compiler.py`](../../production/backend/app/tts/compiler.py) 会把未经当前音色验证的情绪降级为默认值。
- [`synthesis.py`](../../production/backend/app/tts/synthesis.py) 把 `compiled["spoken_text"]` 和一组 `provider_params` 传给一次合成调用。当前编译器生成的逐段情绪/语速报告并没有转成逐段调用参数；MiniMax 适配器也只构造 MiniMax 请求格式。
- 项目现有 [`04-TTS音色与内容生成质检.md`](04-TTS音色与内容生成质检.md) 中提到的阿里云百炼 CosyVoice 是托管 API 路线，不等同于本次评估的开源自托管模型，二者的价格、请求格式、运维方式和实测证据不能混用。

## 接入风险与约束

1. **语气控制不是任意指令跟随。** CosyVoice 维护者在 [issue #1308](https://github.com/QwenAudio/CosyVoice/issues/1308) 表示“不是任意 instruct 都支持”，支持方式有限。另一个情绪控制问题中，维护者建议优先尝试情绪参考音频，而不是只依赖文字指令：[issue #257](https://github.com/QwenAudio/CosyVoice/issues/257)。
2. **流式输出不代表长文无缝。** [issue #369](https://github.com/QwenAudio/CosyVoice/issues/369) 讨论了切段后语调/情绪不一致；[issue #428](https://github.com/QwenAudio/CosyVoice/issues/428) 中维护者指出训练与流式推理方式存在差异，并建议设为 `stream=False`。
3. **需要独立模型服务与 GPU 调度。** 现有 M2/ASR Worker 使用单 GPU 串行队列，项目文档记录的算力云 GPU 为 RTX 4080；是否能在同一实例上稳定与数字人、字幕任务共存，需要实际测显存峰值和等待时间。不要让 TTS 服务绕过现有 GPU 互斥。
4. **音频协议要适配。** 上游 FastAPI 示例通过 HTTP 流返回 int16 音频字节，响应没有 WAV 容器头。项目现有 TTS 解析和下游验收依赖真实容器时长及确定的采样率/声道。接入层需要明确采样规格并转换为项目约定格式，不能把原始字节误认成 WAV。
5. **模型权重许可需单独确认。** GitHub 仓库代码许可为 Apache-2.0；Fun-CosyVoice3 权重位于独立模型仓库。此次读取的 ModelScope 模型元数据没有明确的许可证名称，不能仅凭代码许可证认定权重可商用。正式用于商业发布前要核对所选权重版本的模型卡和适用条款。
6. **不能修复上游数字人时长门禁。** CosyVoice 解决的是 TTS 生成，不会改变 OmniHuman 的输入要求。本项目仍须遵守当前既定规则：OmniHuman 输入音频 `>= 60 秒` 阻断，不切片、不拼接、不默认切换 M2。

## 建议的验证步骤

1. 在独立云 GPU 服务部署 CosyVoice 3，不改现有 MiniMax 默认路由，不把推理依赖装入项目后端 venv。
2. 选一段约 30–60 秒、有陈述、转折和强调内容的真实口播稿；固定同一授权参考音频，分别生成 MiniMax 当前基线、CosyVoice 3 普通指令，以及 2–3 种明确语气指令。先用 `stream=False`。
3. 盲听比较自然度、语气指令命中、发音/漏字、主播一致性、跨句连续性；记录首包/总耗时、显存峰值、音频规格和失败情况。语气控制只有在不同稿件与重复生成中也能听出稳定差异，才算通过。
4. 通过后再实现可选 Provider 适配器、语音页语气入口、请求摘要/缓存与音频规格归一；先做全稿统一风格控制，避免第一版就把每段拆成独立请求。之后再评估是否需要逐段表达计划。

## 上游来源

- [CosyVoice 官方仓库 README](https://github.com/QwenAudio/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/README.md)：CosyVoice 3 能力、模型版本、推荐型号、安装和部署方式。
- [官方示例](https://github.com/QwenAudio/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/example.py)：`inference_instruct2`、零样本参考音频和非流式调用示例。
- [模型推理 API](https://github.com/QwenAudio/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/cosyvoice/cli/cosyvoice.py)：`inference_zero_shot`、`inference_instruct2` 与文本规范化/切分路径。
- [FastAPI 服务端](https://github.com/QwenAudio/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/runtime/python/fastapi/server.py)：支持的路由、提示音频载入和流式原始音频响应。
- [维护者对 instruct 范围的说明](https://github.com/QwenAudio/CosyVoice/issues/1308)、[情绪参考建议](https://github.com/QwenAudio/CosyVoice/issues/257)、[分段情绪连续性讨论](https://github.com/QwenAudio/CosyVoice/issues/369)、[流式推理限制说明](https://github.com/QwenAudio/CosyVoice/issues/428)。
- [CosyVoice 3 ModelScope 模型页](https://www.modelscope.cn/models/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)：权重版本与模型信息。
- [CosyVoice 仓库 LICENSE](https://github.com/QwenAudio/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/LICENSE)：代码仓库许可证。

