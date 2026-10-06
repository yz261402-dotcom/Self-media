# SadTalker 部署核验（EXP-001）

核验日期：2026-09-22。范围：公开官方仓库、固定版本源码及 Release 元数据；未 SSH、未修改远端、未下载权重、未运行推理。以下安装与推理命令交由主代理在独立实验目录执行，不构成已部署或效果已通过的结论。

本地交付检查：`安装.sh` 已通过 Git Bash `bash -n`；内嵌 Python 下载器已通过编译语法检查；`权重清单.json` 解析通过，两个尺寸 × 五种预处理共 10 种配置均准确选出四个权重，默认字节总量复核一致。未执行安装脚本，远端依赖兼容性、下载及推理仍待实测。

输入约束：继续使用同一人物 B 原图及 MiniMax speech-2.8-hd 声音 3 的现有 WAV（15.218844 秒），不重新生成声音、不使用 T2V、不默认使用 GFPGAN 修脸。

## 1. 可优先执行的动作配置

```bash
# 在独立 SadTalker checkout 内执行；替换为远端已确认的绝对路径。
SAD_PY='/root/autodl-tmp/avatar-motion/EXP-001/env-sadtalker/bin/python'
SAD_IMAGE='/absolute/EXP-001/inputs/B.png'
SAD_AUDIO='/absolute/EXP-001/inputs/voice3.wav'
SAD_OUTPUT='/root/autodl-tmp/avatar-motion/EXP-001/outputs/sadtalker_crop_motion'
mkdir -p "$SAD_OUTPUT"
"$SAD_PY" -c 'import random, numpy as np, torch, runpy; random.seed(20260922); np.random.seed(20260922); torch.manual_seed(20260922); torch.cuda.manual_seed_all(20260922); runpy.run_path("inference.py", run_name="__main__")' \
  --source_image "$SAD_IMAGE" \
  --driven_audio "$SAD_AUDIO" \
  --result_dir "$SAD_OUTPUT" \
  --preprocess crop --size 256 --pose_style 0 \
  --expression_scale 1.0 --batch_size 2 --verbose
```

- 故意不传 `--still`、`--enhancer`、`--background_enhancer`、`--ref_pose`、`--ref_eyeblink` 或 `--input_yaw/pitch/roll`。保留原生音频预测头姿和默认眨眼；不人为固定方向，也不加人脸增强变量。
- `pose_style=0` 是官方默认风格，不是“静止”。CLI 范围是 0–45；风格编号不是动作强度。`expression_scale=1.0` 是默认比例，先不人为放大表情。
- `crop` 是用于检验自然头姿/表情能力的首选原生配置，输出是裁剪后的脸部画幅，不能将它与全身画幅的清晰度直接混为一项比较。若三方案必须保持原图画幅，可另用 `--preprocess full` 且仍不加 `--still`，但须下载 `mapping_00109-model.pth.tar`；这会切换另一 MappingNet，而不是简单保存格式变化。全图贴回接缝应单独记录。
- 256 是最低下载量、最快检查管线的推荐规格，不代表最终清晰度上限。512 使用同一命令的 `--size 512`，同时换成 `SadTalker_V0.0.2_512.safetensors`，不能仅改输出尺寸。
- `--verbose` 保留中间系数、裁剪图片等证据。最终文件是 `$SAD_OUTPUT/YYYY_MM_DD_HH.MM.SS.mp4`，中间目录是同名无扩展名目录；不是固定名 `result.mp4`。默认不加 verbose 会删中间目录。
- 固定随机种子覆盖 Python 随机眨眼及 PyTorch 姿态采样，但不承诺跨 GPU 完全位级一致。

## 2. 固定官方代码版本与最小隔离安装

官方仓库：<https://github.com/OpenTalker/SadTalker>。

本次 `gh api repos/OpenTalker/SadTalker/commits/main` 实时取得的固定 commit：

```text
cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8
commit time: 2023-10-10T16:10:21Z
message: update colab
```

官方 README/cog.yaml 原始环境为 Python 3.8、torch 1.12.1 / torchvision 0.13.1 / torchaudio 0.12.1、CUDA 11.3。为了适配现有服务器 Python 3.10 / CUDA 11.8，以下使用**单独 venv** 与官方 PyTorch 配套的 2.0.1 / 0.15.2 / 2.0.2；属于需实际验证的兼容部署方案，不是 SadTalker 已承诺支持的全新基线。禁止在 MuseTalk 环境里直接安装此 requirements，禁止 `--system-site-packages`。

主代理指定的实际隔离路径已固化到 `SadTalker准备/安装.sh`，该文件尚未执行：

- root：`/root/autodl-tmp/avatar-motion/EXP-001`
- repo：`/root/autodl-tmp/avatar-motion/EXP-001/SadTalker`
- env：`/root/autodl-tmp/avatar-motion/EXP-001/env-sadtalker`
- 日志：root 下 `logs/sadtalker-install-时间戳-PID.log`
- 依赖与权重证据：root 下 `evidence/sadtalker/`

```bash
# 将 安装.sh 与 权重清单.json 放在远端同一目录；确认 Python 路径后执行。
SAD_BOOTSTRAP_PY='/absolute/existing/python3.10' bash '/absolute/preparation/安装.sh'
# 若选正式 512/full 样片，首次安装时改为：
# SAD_BOOTSTRAP_PY='/absolute/existing/python3.10' SAD_SIZE=512 SAD_PREPROCESS=full bash '/absolute/preparation/安装.sh'
```

脚本创建完整新 venv、固定上述 commit 和 PyTorch 配套，再按 `权重清单.json` 只下载所选 profile 的四个权重；安装目录已有内容时拒绝覆盖。未把导入通过等同于推理通过。后面的四文件手工命令用于审查/参考，若执行了安装脚本则不要重复下载。脚本中的 constraints 是本次兼容约束，完整的实际解析结果应以远端 `pip-freeze` 为准。

约束版本的依据与边界：

- PyTorch 官方配套 CUDA 11.8 命令明确是 torch 2.0.1 / torchvision 0.15.2 / torchaudio 2.0.2。
- 官方 `numpy==1.23.4`、`joblib==1.1.0` 等旧约束不能被新版依赖悄悄升级。这里额外约束 numba/llvmlite、scikit-learn 和绘图库，降低旧依赖与最新版解析冲突。
- 固定源码 `src/utils/croper.py` 使用 `Image.ANTIALIAS`，Pillow 10 及以后已删除此 API，故约束 Pillow 9.5.0。
- BasicSR 1.4.2 依赖旧 torchvision 模块，因此不应任由安装器升级 torchvision。`--no-build-isolation` 允许其构建看见刚安装的 torch；发生安装错误应保留完整日志，不能覆盖 MuseTalk 解决。
- GFPGAN 包虽然需要安装（`face_enhancer.py` 顶层导入），但本次不传 enhancer，所以不启用修脸，也不需要 GFPGANv1.4 权重。
- FFmpeg 及 Linux 共享库 libGL/glib 应先只读检查现有系统。若缺失，优先在本实验隔离环境补充；上面没有修改系统包或原环境。

## 3. 最小权重清单、大小与哈希状态

均为官方 `scripts/download_models.sh` 所列原始发布地址。本次通过 GitHub Release API 核实大小；Release 的 `digest` 全为 `null`，未发布可从该接口验证的 SHA-256。未下载大文件，故**本地实得 SHA-256 未知**；不要把文件长度、Git blob ID 或下载地址冒充哈希。主代理下载后必须记录 `sha256sum`，这能固定本次实验文件，但不等同于与官方签名哈希比对成功。

最小 crop + 256 清单（总计 1,183,756,176 字节，约 1.102 GiB）：

- `checkpoints/SadTalker_V0.0.2_256.safetensors`：725,066,984 字节。<https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_256.safetensors>
- `checkpoints/mapping_00229-model.pth.tar`：155,521,183 字节。<https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00229-model.pth.tar>
- `gfpgan/weights/alignment_WFLW_4HG.pth`：193,670,248 字节。<https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth>
- `gfpgan/weights/detection_Resnet50_Final.pth`：109,497,761 字节。<https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth>

可选替换/补充：

- `--size 512`：替换为 `SadTalker_V0.0.2_512.safetensors`，725,066,984 字节。<https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_512.safetensors>
- `--preprocess full` / `extfull`：改用 `mapping_00109-model.pth.tar`，155,779,231 字节。<https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00109-model.pth.tar>
- 不下载 `GFPGANv1.4.pth`、`parsing_parsenet.pth`，不调用整个 `download_models.sh` 造成两种分辨率与增强模型全量下载。
- detection/alignment 虽放在 `gfpgan/weights/`，实际为裁脸与关键点必需，不表示启用人脸增强。使用 safetensors 普通 2D 推理不需要另行下载旧式全部 checkpoint 或 BFM 大包；所需 `src/config/similarity_Lm3D_all.mat`（994 字节）已在固定仓库中。

仅供主代理在远端执行的四文件下载与校验命令：

```bash
mkdir -p checkpoints gfpgan/weights
curl -fL --retry 0 'https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/SadTalker_V0.0.2_256.safetensors' -o checkpoints/SadTalker_V0.0.2_256.safetensors
curl -fL --retry 0 'https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc/mapping_00229-model.pth.tar' -o checkpoints/mapping_00229-model.pth.tar
curl -fL --retry 0 'https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth' -o gfpgan/weights/alignment_WFLW_4HG.pth
curl -fL --retry 0 'https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth' -o gfpgan/weights/detection_Resnet50_Final.pth
"$SAD_PY" - <<'PY'
from pathlib import Path
expected = {
    'checkpoints/SadTalker_V0.0.2_256.safetensors': 725066984,
    'checkpoints/mapping_00229-model.pth.tar': 155521183,
    'gfpgan/weights/alignment_WFLW_4HG.pth': 193670248,
    'gfpgan/weights/detection_Resnet50_Final.pth': 109497761,
}
for name, size in expected.items():
    actual = Path(name).stat().st_size
    assert actual == size, (name, actual, size)
    print(name, actual)
PY
sha256sum checkpoints/SadTalker_V0.0.2_256.safetensors checkpoints/mapping_00229-model.pth.tar gfpgan/weights/alignment_WFLW_4HG.pth gfpgan/weights/detection_Resnet50_Final.pth > "$SAD_ROOT/weights.sha256"
```

这些下载命令应只在新的实验 checkout 内首次执行；若某文件已经下载或校验失败，先保留失败文件和日志，再由主代理决定后续动作，不覆盖旧样片与已有权重证据。

## 4. full / still 的真实逻辑与公平比较

以下均基于固定 commit，而不是只复述 README：

1. `src/generate_facerender_batch.py`：前 64 维为表达系数；64–69 为头姿/平移。`still_mode=True` 执行 `generated_3dmm[:, 64:] = repeat(source_semantics[:,64:])`，把头姿等替换为原图常量，故不能用 `full + still` 样片代表自然动作上限。
2. `full` 单独只附加并固定原图的 70–72 维裁剪变换，不直接清空 64–69 维预测姿态。`init_path.py` 同时为 full 选择 `mapping_00109` 与 `facerender_still.yaml`（73 维输入），其他模式选择 `mapping_00229` / `facerender.yaml`（70 维）。文件名带 still 不等于自动执行 CLI 的 `--still` 冻结逻辑。
3. `src/generate_batch.py` 接收 `still` 参数但函数体未使用它；默认仍调用 `generate_blink_seq_randomly()`，且 `use_blink=True`。因此旧 `docs/best_practice.md` 的“still 会停止眨眼和头动”只可确认后半句；当前源码不能据此认定 still 一定禁用眨眼。
4. 默认眨眼是程序随机时序控制，不是根据台词语义精确安排。Audio2Pose 由音频预测姿态、Audio2Exp 预测表达系数，不能表述为能理解一句话何时该挑眉或情感表演一定自然。
5. `crop` 原生动作结果与 `full` 原图贴回结果用途不同；如果保留 crop 作为能力对照，要在展示中说明画幅改变。若只选择一次正式全图样片，使用 `full`、不加 still，并把贴回瑕疵如实计入结果。

## 5. 输出验收与时长边界

- 固定图像和现有 WAV 的 SHA-256，记录完整命令、种子、代码 commit、pip freeze、GPU 型号、依赖安装与推理日志。
- 验证口型、身份一致性、眨眼、头动、表情自然度和脖颈/背景边缘；“有动作”不等于“动作自然”，检测到 GPU 也不等于整片成功。
- 官方固定 25 fps，帧数使用 `int(audio_length / (16000/25))` 向下取整。15.218844 秒音频理论得到 380 帧，即 **15.20 秒**（实际以音频重采样长度和 ffprobe 为准），尾部约 18.844 毫秒不会完整保留在官方默认输出中。禁止把帧率量化的微小差异误判为截掉长段台词；若后续统一重封装原 WAV，须单独记录。
- 最终 `ffprobe` 应记录帧数、fps、视频尺寸、音视频时长；`--verbose` 的 `.mat/.txt` 可以检查姿态系数是否随帧变化，避免把参数关闭导致的静止归因于模型。

## 6. 官方证据链接

- 固定提交：<https://github.com/OpenTalker/SadTalker/commit/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8>
- 推理参数、输出命名：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/inference.py>
- still 冻结、full 附加系数：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/src/generate_facerender_batch.py>
- 眨眼与帧数：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/src/generate_batch.py>
- full/crop 权重选择：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/src/utils/init_path.py>
- 音频驱动姿态/表达：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/src/test_audio2coeff.py>
- 检测/对齐必需权重：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/src/face3d/extract_kp_videos_safe.py>
- 官方下载脚本：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/scripts/download_models.sh>
- 主权重元数据：<https://api.github.com/repos/OpenTalker/SadTalker/releases/tags/v0.0.2-rc>
- 检测/对齐元数据：<https://api.github.com/repos/xinntao/facexlib/releases/tags/v0.1.0>
- 依赖清单：<https://github.com/OpenTalker/SadTalker/blob/cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8/requirements.txt>
- PyTorch 官方旧版安装配套：<https://docs.pytorch.org/get-started/previous-versions/>

