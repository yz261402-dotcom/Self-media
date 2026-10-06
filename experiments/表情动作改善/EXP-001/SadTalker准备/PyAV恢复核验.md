# PyAV 恢复核验

2026-09-22。首次安装日志已确认：独立环境的 torch 2.0.1+cu118、torchvision 0.15.2+cu118、torchaudio 2.0.2+cu118、numpy 1.23.4 已安装；后续依赖阶段从阿里镜像取得 `av-10.0.0.tar.gz`，在准备 metadata 时因缺 Cython 失败。该失败尚未进入 SadTalker 权重下载和推理。

恢复只改 PyAV 兼容约束为 12.3.0，复用已有 venv、源码、torch 和 numpy；不重建环境、不修改首次脚本/旧日志，不补 Cython 编译 FFmpeg。

## 官方 wheel

- 文件：`av-12.3.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`
- 适用：CPython 3.10、Linux x86_64、glibc >= 2.17。
- 字节数：33,490,936。
- PyPI SHA-256：`0220fce2a62d71cc5e89617419b6224ddb43f1753b00f68b5c9af8b5f41d38c9`。
- 上传时间：2024-07-21T04:00:33.782411Z；未被 yanked。
- 官方元数据：<https://pypi.org/pypi/av/12.3.0/json>。

核心恢复命令（已包含到恢复安装1.sh）：

```bash
/root/autodl-tmp/avatar-motion/EXP-001/env-sadtalker/bin/python -m pip install \
  --retries 0 --no-deps --only-binary=:all: \
  'https://files.pythonhosted.org/packages/0a/5f/5ab859d8770ac1203d492e418cf949cfcac5c25994e9754c536fb37578fc/av-12.3.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl#sha256=0220fce2a62d71cc5e89617419b6224ddb43f1753b00f68b5c9af8b5f41d38c9'
```

直接使用已核实并带哈希的官方 wheel URL，避免镜像再次选择源码包。剩余 requirements 继续使用原镜像缓存，且新 constraints 固定 `av==12.3.0`、安装命令使用 `--only-binary=av`；不会允许 PyAV 自动升级至 14 或回退源码编译。

## torchvision 接口核验

torchvision v0.15.2 的 `write_video` 对每帧执行 `frame.pict_type = "NONE"`。PyAV v12.3.0 的 `pict_type` setter 实现仍为 `self.ptr.pict_type = PictureType[value].value`，其 `PictureType` 枚举包含 `NONE`，因此该关键接口在源码层面匹配。torchvision 的异常处理还引用 `av.AVError`，恢复脚本也检查该属性存在。

- <https://github.com/pytorch/vision/blob/v0.15.2/torchvision/io/video.py>
- <https://github.com/PyAV-Org/PyAV/blob/v12.3.0/av/video/frame.pyx>

源码匹配不等于服务器已验证。`恢复安装1.sh` 在依赖安装完成后先实际调用 torchvision 写出 25 帧 64×64 H.264 样例，再用 PyAV 解码并断言帧数和 fps 均为 25；MP4 和 JSON 结果保留到 `evidence/sadtalker/torchvision-write-video-recovery1-*.{mp4,json}`。通过后才继续 SadTalker 导入检查与四个所选 profile 权重下载。

执行入口：把恢复脚本与原 `权重清单.json` 放在远端同一目录，再 `bash 恢复安装1.sh`。默认保持初始脚本的 256/crop；若主代理本次选择 512/full，显式设置 `SAD_SIZE=512 SAD_PREPROCESS=full`。实际口播推理仍由主代理单独执行并保留日志。

本次仅核验官方元数据/源码并生成恢复文件；没有 SSH、没有在本机安装 PyAV、没有执行恢复脚本或权重下载。
