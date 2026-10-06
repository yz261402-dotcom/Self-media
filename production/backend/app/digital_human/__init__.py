"""数字人口播包（T05）。

- `routes`：路线能力声明与提交前的前置门禁；
- `providers`：OmniHuman 1.5（火山视觉）与 LivePortrait / MuseTalk 适配器；
- `videos`：口播视频资产（底片与原片）登记与门禁字段；
- `plates`：M2 动态底片的生成与审核；
- `segments`：OmniHuman 约 60 秒分段（句子边界、全局偏移、禁加速截字）；
- `cost`：估价、价格快照与费用确认；
- `queue`：单 GPU 重型作业串行队列；
- `gates`：原片门禁、音轨保持核对与最终视频确认；
- `submission`：两路线的提交编排与失败处理。
"""
from __future__ import annotations

from . import (  # noqa: F401
    cost,
    gates,
    plates,
    providers,
    queue,
    routes,
    segments,
    submission,
    videos,
)
