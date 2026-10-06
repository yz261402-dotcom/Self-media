# MultiTalk 数字人项目调研

核验日期：2026-09-23。仅查看官方仓库、论文和项目说明；未安装、未下载权重、未运行 GPU 推理。本记录不改变 v2.4 已选方案。

核验源码版本：`c1ad84009b99ed36c97ab18e4517ca5f98692438`。

## 1. 它解决什么问题

MultiTalk 是音频驱动的人物视频生成项目。输入参考人物图片、一个或多个音频文件和表演提示词，输出口型与音频配合、具有面部及身体动作的视频。支持单人，也支持多人对话、唱歌和互动；多人情况下研究重点是让不同音频正确驱动对应人物。[官方说明](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/README.md)、[论文](https://arxiv.org/abs/2505.22647)、[演示页](https://meigen-ai.github.io/multi-talk/)

它不是人物资产管理、文案生成、字幕和发布的完整应用。在本项目里，它适合作为数字人视频生成 Provider 候选。

## 2. 接到我们现有流程里的方式

用户确认的口播稿 → MiniMax 最终 TTS；固定人物 B 的确认衣景图＋最终 TTS＋可编辑动作描述 → MultiTalk → 原片审核 → 代码完成字幕、音轨处理、画面处理和编码 → 成片终审。

这一路线仍由固定图片和确定的音频驱动，提示词用于描述表演。无需先用 LivePortrait 生成动作底片，也无需再接 MuseTalk 对口型；音画效果是否足够好仍需实测。

官方单人示例包含 `prompt`、`cond_image`、`cond_audio.person1`。入口默认 `audio_mode=localfile`，因此可以接外部 MiniMax 音频，不必改用项目自带的 Kokoro TTS。[单人输入示例](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/examples/single_example_1.json)、[推理入口](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/generate_multitalk.py)

注意：当前官方入口会对输入音频进行 16 kHz 重采样和响度处理，再用处理后的音频封装示例视频。不能据此承诺原始 MiniMax 音轨逐字节保留；正式接入时应保留原音频，并核验时间轴、时长和最终封装音轨。

## 3. 涉及哪些模型

- `Wan2.1-I2V-14B-480P`：视频生成基座。
- `MeiGen-MultiTalk`：增加音频条件的模型权重。
- `TencentGameMate/chinese-wav2vec2-base`：提取音频特征；不是生成声音或字幕的模型。
- `Kokoro-82M`：项目提供的可选文本转语音能力；我们的流程继续使用 MiniMax。官方入口仍有 Kokoro 导入，选择外部音频不等于现有代码可直接删除此依赖。
- FusioniX、lightx2v LoRA、INT8、TeaCache 等属于可选加速或内存优化方案，不能将不同配置的效果、速度和显存数字混在一起比较。

来源：[官方模型准备与推理说明](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/README.md)。

## 4. 与现有两条路线的区别

- **M2**：LivePortrait 先从动作参考生成底片，MuseTalk 再结合 TTS 修改口型；动作来源主要是参考视频。
- **MultiTalk**：在同一次视频生成中结合参考图、音频和表演描述，生成身体动作和口型。部署和显存要求需要独立验证。
- **OmniHuman 1.5**：同样是图片＋音频驱动的数字人方向，但使用服务商 API。MultiTalk 可自行部署，成本取决于算力占用与有效成片率。

M2、OmniHuman 的当前项目决定见[模型选型决策与接入方案](../0001-douyin-ai-studio/09-模型选型决策与接入方案.md)。以上是技术路径比较，不是同输入质量测试结论。

## 5. 部署与生产边界

官方说明提供单张 RTX 4090 生成 480P 的低显存配置。社区 Wan2GP 的 8GB 条目不能直接视为官方原版的硬件要求；卸载会把部分负担转到内存和数据传输。现有 MuseTalk 实例能否适用，需要重新核对显存、内存、磁盘和推理时间。

分辨率说明有冲突：Features 写支持 480P/720P，Quick Inference 段仍写当前仅 480P、720P 需要多 GPU；实际代码接受 `multitalk-720`，官方效率图也列出单 GPU 的 720P 条件。不能据此承诺当前实例可以稳定生成 720P 或原生 1080P。[尺寸配置](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/wan/configs/__init__.py)、[官方效率图](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/assets/efficiency.png)

长视频说明同样需要区分：README Features 写最长 15 秒，入口另有 streaming 分块生成，并将 `max_frames_num` 设为 1000。因此既不能认定 15 秒是代码硬上限，也不能承诺它已满足一分钟连续口播。身份、衣景、手部和颜色跨片段稳定性仍需验证。

仓库及模型声明 Apache 2.0；正式分发仍需核对所选基座、附加权重和依赖各自许可。[许可证](https://github.com/MeiGen-AI/MultiTalk/blob/c1ad84009b99ed36c97ab18e4517ca5f98692438/LICENSE.txt)

仓库公告已经指向同团队后续项目 InfiniteTalk 与 LongCat-Video-Avatar 1.5。后续部署选型宜一并比较，不应只因发现 MultiTalk 就立即替换当前方案。

## 6. 建议

可以加入本地数字人口播候选，重点验证它能否改善身体动作。先复用人物 B、同一段已确认 MiniMax 音频做约 10～15 秒对照，评价人物一致性、口型、身体动作和手部异常，同时记录实际生成耗时、峰值显存及有效成片成本；短片通过后再测一分钟。

本次没有生成样片，也没有获得新的速度、成本或质量实测结果。
