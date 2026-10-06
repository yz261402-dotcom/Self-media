# EXP-001：LivePortrait 官方部署核验

核验日期：2026-09-22。已完成官方源码、依赖、权重元数据和 CLI 输出逻辑核对；本机只保存小型源码与清单，未下载模型权重、未远程执行、未运行 GPU 推理。以下步骤待主代理在远端执行，不能据此宣称已部署或效果通过。

## 固定版本与隔离目录

- 官方仓库原 `KwaiVGI/LivePortrait`、`KlingTeam/LivePortrait` 路径现转向 [KlingAIResearch/LivePortrait](https://github.com/KlingAIResearch/LivePortrait)。核验固定 commit：`9b294b3d0536135442ea73cb01e6cb3ca7029dd3`，提交日期 2026-06-01。
- 官方权重：[KlingTeam/LivePortrait](https://huggingface.co/KlingTeam/LivePortrait)，固定 revision：`82a4fa6735ca58432b6ce39301b4b9ee066dea47`。
- 实验根：`/root/autodl-tmp/avatar-motion/EXP-001`；源码：其下 `LivePortrait`；独立环境：其下 `env-liveportrait`；输入：`inputs`；输出：`outputs/liveportrait`。
- 使用 Python 3.10、torch 2.3.0 / torchvision 0.18.0 / torchaudio 2.3.0 的 CUDA 11.8 wheels，与官方当前 README 示例一致。已有 MuseTalk 的 Python 3.10 / torch 2.0.1 cu118 环境不改动。原环境可能支持未编译的人像推理，但本次没有验证其兼容性。
- `requirements.txt` 固定 `onnxruntime-gpu==1.18.0`、`transformers==4.38.0`；基础依赖包含 numpy 1.26.4、opencv-python 4.10.0.84、scipy 1.13.1、tyro 0.8.5、gradio 5.1.0、pykalman 0.9.7。安装脚本另固定 `huggingface_hub==0.25.2`，兼容此代 transformers/gradio，并使用 Python 下载接口避免 CLI 改名差异。完整要求保存在 `LivePortrait准备/official_source/requirements*.txt`。
- 系统需已有 git、conda、ffmpeg、ffprobe 和可用 NVIDIA 驱动。人像路径不需要编译动物模式的 X-Pose；本次不启用 `torch.compile`。

## 可执行安装准备

将 `LivePortrait准备/安装.sh` 与同目录 `机器清单.json` 一起上传到远端准备目录，然后执行：

```bash
bash /path/to/LivePortrait准备/安装.sh
```

脚本默认使用上面的实验根，只创建隔离环境；固定源码 commit；安装依赖；下载 8 个指定人像权重；逐个核对字节数与 SHA-256；运行 `pip check`、CLI 帮助和 CUDA 可用性检查，并记录日志和 `pip freeze`。已有源码目录必须处于指定 commit 且无已跟踪文件改动，否则中止，不清理、不重置目录。安装成功仍不等于真实模型推理通过；ONNX Provider 列表也不能代替真实 Session 推理验收。

## 最小人像权重

8 个文件合计 **658,941,445 bytes = 658.94 MB = 628.42 MiB**，不含 Python/torch 环境磁盘占用。HF 全仓页面约 2.14 GB，包含本次不用的动物权重。文件、大小、LFS SHA-256 与官方固定 revision 的下载地址完整保存在 `机器清单.json`。

```text
pretrained_weights/
  insightface/models/buffalo_l/2d106det.onnx                 5,030,888 bytes
  insightface/models/buffalo_l/det_10g.onnx                 16,923,827 bytes
  liveportrait/base_models/appearance_feature_extractor.pth 3,387,959 bytes
  liveportrait/base_models/motion_extractor.pth            112,545,506 bytes
  liveportrait/base_models/spade_generator.pth             221,813,590 bytes
  liveportrait/base_models/warping_module.pth              182,180,086 bytes
  liveportrait/landmark.onnx                              114,666,491 bytes
  liveportrait/retargeting_models/stitching_retargeting_module.pth 2,393,098 bytes
```

## 原速驱动、人物 B 与准确输出

人物 B 原图必须是 `source`。此前约 9.13 秒的 Ali 视频人物不是 B，只提供 `driving` 的动作。LivePortrait 本身是视频驱动的动作迁移；固定音色 3 的 15.218844 秒音频由后续 MuseTalk 处理。驱动不携带旧声音。

为保持眨眼和动作原速，**循环原驱动一次，截取 381 帧**，不把 9 秒拉慢到 15 秒。25 fps 下 `ceil(15.218844 × 25) = 381`，视频为 15.24 秒，多出 21.156 ms；保留原音频不拉伸。循环约在 9.13 秒处产生一次接缝，准确位置随原文件时间戳/帧率归一化而定，必须记录并查看接缝前后。此路径仅用于同一长度的技术对比，不代表自然连续 15 秒动作已生成。

在首次执行前，将同一份人物 B 原图以 `person_B.png` 存放于 `inputs`（若实际为 JPEG，改用 `.jpg`，不要仅改扩展名），并把 Ali 原视频路径替换到命令。先检查第一帧正脸、表情接近中性、头部清晰、肩膀运动较小；不满足时需记录限制，不能默认满足。

```bash
LP_EXP_ROOT=/root/autodl-tmp/avatar-motion/EXP-001
LP_PYTHON="$LP_EXP_ROOT/env-liveportrait/bin/python"
LP_DRIVER_ORIGINAL=/absolute/path/to/existing_ali_video.mp4

ffprobe -v error -show_entries format=duration:stream=index,codec_type,r_frame_rate,avg_frame_rate,nb_frames \
  -of json "$LP_DRIVER_ORIGINAL" > "$LP_EXP_ROOT/logs/ali-original-probe.json"

ffmpeg -nostdin -n -stream_loop 1 -i "$LP_DRIVER_ORIGINAL" \
  -map 0:v:0 -an -vf fps=25 -frames:v 381 \
  -c:v libx264 -crf 18 -pix_fmt yuv420p -movflags +faststart \
  "$LP_EXP_ROOT/inputs/ali_motion_loop25_381.mp4"

cd "$LP_EXP_ROOT/LivePortrait"
"$LP_PYTHON" inference.py \
  -s "$LP_EXP_ROOT/inputs/person_B.png" \
  -d "$LP_EXP_ROOT/inputs/ali_motion_loop25_381.mp4" \
  -o "$LP_EXP_ROOT/outputs/liveportrait" \
  --flag_crop_driving_video --animation_region all \
  --driving_option expression-friendly --driving_multiplier 1.0 \
  > "$LP_EXP_ROOT/logs/liveportrait-inference.log" 2>&1
```

上述命令是一次实际 LivePortrait 模型推理。动作来自驱动视频经模型提取、迁移和生成，不使用静图平移充当动作。重复帧和循环接缝须如实记入评估。主代理应在新实验输出中执行一次；若输出已经存在，不直接覆盖，另建输出目录并记录原因。

准确输出由 `src/live_portrait_pipeline.py:470,486` 决定：

- **供 MuseTalk 使用**：`/root/autodl-tmp/avatar-motion/EXP-001/outputs/liveportrait/person_B--ali_motion_loop25_381.mp4`，是带原图背景回贴的完整生成画面。
- 三联可视化：同目录 `person_B--ali_motion_loop25_381_concat.mp4`，含 driving/source/result；不能把这个三联图送给 MuseTalk。
- 驱动动作缓存：`/root/autodl-tmp/avatar-motion/EXP-001/inputs/ali_motion_loop25_381.pkl`，在驱动视频旁自动生成。

当前 CLI 默认 `flag_relative_motion=True`、`flag_stitching=True`、`flag_pasteback=True`、`flag_do_crop=True`、FP16 开启；不打开仍标为 WIP 的 eye/lip retargeting。CLI 的 `flag_normalize_lip` 默认 **False**，虽 `InferenceConfig` 内写 True，入口会被 `ArgumentConfig` 覆盖。`source_max_dim` 默认 1280，大图会缩至最长边 1280；如对比要求保留更大输出，需显式调整并记入条件。

**没有 `--output_fps` CLI 参数**：源码以 `int(get_fps(driving))` 决定静图驱动输出帧率，所以必须先归一为 CFR 25。生成后核验：

```bash
ffprobe -v error -count_frames -show_entries \
  stream=codec_type,width,height,r_frame_rate,avg_frame_rate,nb_read_frames,duration:format=duration \
  -of json "$LP_EXP_ROOT/outputs/liveportrait/person_B--ali_motion_loop25_381.mp4"
```

验收至少包括 381 帧/25 fps、能正常解码、人物 B 身份稳定、眨眼自然、头部动作、面部/背景回贴边缘及循环接缝；首个未循环片段与循环段分别看。人脸检测失败可能使源码截短帧数，不能只凭退出码为 0 判为通过。之后才将此视频交给 MuseTalk 加固定音色 3，核对口型、原音频和最终时长。

## 身份与许可边界

跨人物驱动会迁移表情/姿态，也可能把驱动者的张嘴幅度、眼睑形态和动作习惯映射到 B；大转头、首帧非中性或裁剪漂移容易带来牙齿/脸型变化、边缘拉伸。相对运动和 stitching 能减轻部分问题，不能保证身份不漂移。后续 MuseTalk 再生成嘴部会增加一层变化；三路对比必须使用同一张 B 图、同一原始音频并保留 LP 中间视频。

固定 commit 的 [LICENSE](https://github.com/KlingAIResearch/LivePortrait/blob/9b294b3d0536135442ea73cb01e6cb3ca7029dd3/LICENSE) 明确：LivePortrait 代码为 MIT，但默认 InsightFace 模型仅限**非商业研究**；商用应替换其检测模型或取得适用授权。本轮按技术评估记录，不把当前默认组合标成可直接商用。

## 官方证据

- [固定 README：安装、权重和人像 CLI](https://github.com/KlingAIResearch/LivePortrait/blob/9b294b3d0536135442ea73cb01e6cb3ca7029dd3/readme.md)
- [参数真实默认值](https://github.com/KlingAIResearch/LivePortrait/blob/9b294b3d0536135442ea73cb01e6cb3ca7029dd3/src/config/argument_config.py)
- [输出帧率、缓存路径、真实结果/三联图命名](https://github.com/KlingAIResearch/LivePortrait/blob/9b294b3d0536135442ea73cb01e6cb3ca7029dd3/src/live_portrait_pipeline.py)
- [官方权重固定版本](https://huggingface.co/KlingTeam/LivePortrait/tree/82a4fa6735ca58432b6ce39301b4b9ee066dea47)
- [InsightFace 官方许可说明](https://github.com/deepinsight/insightface#license)

保存的 15 个源码文件、HF 原始元数据和 SHA-256 清单位于 `LivePortrait准备`；HF 页面中的 Python 3.9 README 是旧版本，本说明采用当前 GitHub 固定源码的 Python 3.10 说明。
