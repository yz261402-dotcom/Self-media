"""T07 代码后期 Worker 的媒体能力层。

边界（架构 v2.4）：
- 后期是**确定性代码 Worker**，不是 Agent、不走 MCP、不加载业务 Skill；
- 统一时间线（EditTimeline）是唯一权威：音频、视频、字幕都由同一映射驱动；
- ASR/对齐是**可插拔适配器**：模型未选/未配置前明确 blocked，绝不假设已有能力；
- FFmpeg 只做确定性处理（两遍响度、CFR、烧录、封面），普通缩放不当作真实增强；
- 任何失败保留证据、登记 blocked，不伪造成功产物。

模块：
- `errors`：VideoBlocked（带 code，供上层映射 block_reason）；
- `ffmpeg`：二进制定位、probe、完整解码校验、响度实测、黑帧/冻结检测；
- `timeline`：EditTimeline 源→终映射与校验（单调、连续、范围）；
- `subtitles`：终时间轴字幕行、正文覆盖/越界/错序检查、ASS 生成；
- `asr`：ASR/对齐适配器接口与注册表（默认无 provider → blocked）；
- `render`：真实渲染管线（loudnorm 两遍、CFR 30fps、1080x1920、
  H.264/AAC-LC 48kHz、yuv420p、faststart、烧录字幕、AI 标识、封面）。
"""
from .errors import VideoBlocked

__all__ = ["VideoBlocked"]
