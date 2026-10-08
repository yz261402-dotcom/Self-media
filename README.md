# Self-media 自媒体视频生成

数字人口播工作台：从主题到成片再到抖音投稿的完整链路。

支持固定人物口播和短视频分镜两条制作路线。口播路线采用稿件后合成语音与数字人口播；分镜路线经模型检查和人工审核采用剧本，逐镜头生成图片及 Wan 原声视频，再拼接、补字幕和生成发布包。已选人物及服装基准贯穿分镜制作。

## 能力现状

工作台保留七个入口，前四项构成制作链路：

| 入口 | 状态 |
|---|---|
| 人物形象 | 可用，含人物 / 变体两级管理与权属审核 |
| 内容创作 | 口播 / 分镜候选、独立审核、编辑与定向重写；分镜另有人工评分 |
| TTS 语音 | 可用，MiniMax 试听与全文确认 |
| 视频合成 | M2 / OmniHuman 口播，以及 Wan 分镜生成、本地拼接与字幕 |
| 发布 | 首版抖音浏览器 Adapter，单账号单条即时投稿 |
| 指标监控 | 未实现 |
| 领域模型选择 | 未实现 |

发布功能当前正在真实账号上做**只上传、不发布**的验收，尚不能宣称真实发布全链路已通过。下载包不代表已发布。

## 技术栈

- **前端**：Vue 3 + TypeScript + Pinia + Vite
- **后端**：FastAPI + Pydantic，PostgreSQL 持久化（psycopg 3）
- **编排**：DeepAgents 运行时，总控维护依赖与人工选择门；业务门禁、预算、幂等由代码复算
- **模型**：文本 GPT-6.1、图片 GPT Image 2.5、语音 MiniMax `speech-2.8-hd`、字幕 Qwen3-ASR + ForcedAligner、画面分析 Qwen3-VL
- **数字人**：M2（LivePortrait + MuseTalk 1.5，需云端 GPU Worker）/ OmniHuman 1.5（供应商接口）
- **后期**：FFmpeg 裁切、规格统一、降噪锐化、响度处理、字幕与封面输出
- **发布**：Playwright Adapter 操作抖音创作者中心（非官方 OpenAPI）

写稿及返工默认使用 `gpt-6.1-sol`，策划和独立审核默认使用 `gpt-6-astra`，供应商、地址和模型均从配置读取。DeepAgents 不可用时可走独立直接生成流程；模型不可用时，分镜支持人工导入或手写，并如实记录未进行模型审核。

分镜人工审核支持五项或八项 1-5 分评分，全部启用项平均分严格大于 4 才通过。未通过可在审核页人工修改后确认采用，或提交意见定向重写并重新经过模型、人工审核。生成及审核异常时仍保留草稿展示和恢复入口，不自动重发结果未知的收费请求。

业务 Skill 入口： [选题策划](production/skills/topic-planning/SKILL.md)、[内容写作](production/skills/content-writing/SKILL.md)、[内容审核](production/skills/content-review/SKILL.md)。框架先显示元数据，再按需读取正文；实际模型调用按阶段、口播或分镜路线加载同一套规则。详见 [Skill 改造记录](specs/业务Skill渐进加载改造-20261008.md)。

成片规格：竖屏 1080 × 1920、30 fps、MP4 / H.264 / AAC。

## 快速开始

```powershell
cd "C:\Users\22346\Desktop\自媒体视频生成"
powershell -ExecutionPolicy Bypass -File ".\production\start.ps1"
```

默认打开 <http://127.0.0.1:5173>，后端监听 `127.0.0.1:8000`。脚本会检查环境、端口与健康状态，已运行的服务直接复用。

后端依赖 PostgreSQL。本机开发使用 Docker 容器 `avatar-studio-pg`（映射端口 `15432`），启动前需先确保容器运行，连接串由 `AVATAR_STUDIO_DATABASE_URL` 指定。

完整启动流程、云端 Worker 恢复、SSH 隧道建立和问题定位见 **[production/README.md](production/README.md)**；五步操作细节见 [production/使用说明.md](production/使用说明.md)；部署方案见 [production/部署方案.md](production/部署方案.md)。

## 配置

后端读取 `production/backend/.env`，环境变量优先。仓库中只提供模板 `production/backend/.env.example`，**真实密钥不入库**。

关键变量：

- `AVATAR_STUDIO_MODE`：`mock` / `integration` / `production`
- `AVATAR_STUDIO_DATABASE_URL`：PostgreSQL 业务库连接串（本机开发用 Docker 容器 `avatar-studio-pg`，端口 `15432`）
- `AVATAR_STUDIO_M2_WORKER_URL` / `AVATAR_STUDIO_M2_WORKER_TOKEN`：云端口播 Worker
- `AVATAR_STUDIO_ASR_WORKER_URL` / `AVATAR_STUDIO_ASR_WORKER_TOKEN`：字幕识别与对齐
- `AVATAR_STUDIO_DEEPSEEK_*`、`AVATAR_STUDIO_GPTIMAGE_*`、`AVATAR_STUDIO_MINIMAX_*`：供应商凭据
- `AVATAR_STUDIO_OPENAI_API_KEY` / `AVATAR_STUDIO_OPENAI_BASE_URL`：默认策划、写稿和审核共用的兼容接口配置
- `AVATAR_STUDIO_PUBLISH_ADAPTER`：`playwright`（真实发布）/ `mock`（隔离测试）

未配置预算时默认不允许可计费提交。

## 仓库结构

```text
.
├── production/                 项目主体
│   ├── start.ps1               Windows 启动与健康检查
│   ├── README.md               详细说明（启动 / 链路 / 配置 / 运维）
│   ├── 使用说明.md              五步操作与问题定位
│   ├── 部署方案.md              部署与依赖
│   ├── 隧道使用说明.md          SSH 隧道接入
│   ├── Worker运维说明.md        云端 GPU Worker 运维
│   ├── backend/                FastAPI 后端与回归测试
│   ├── frontend/               Vue 3 前端与状态回归测试
│   ├── workers/m2_gpu/         云端 GPU Worker
│   ├── skills/                 选题、写作、独立审核 Skill 与路线参考
│   └── scripts/               隧道、诊断、预览构建等工具脚本
├── specs/                      产品规格与技术方案
│   └── 0001-douyin-ai-studio/  PRD、技术选型、接口契约、实施计划、验收标准
├── assets/                     人物与场景素材
│   ├── 固定主播/               定版主播形象
│   ├── avatar-references/      参考集
│   ├── 动作参考/               动作模板
│   ├── 背景/                   场景背景
│   ├── 遮罩/                   换背合成蒙版
│   └── 生成候选/               待审生成结果
└── content-media/              作品素材
```

### 关键代码位置

```text
production/backend/app/
  agents/             总控、角色、DeepAgents、受控工具
  content/            双路线创作、独立审核、人工评分、直接生成与人工导入
  short_video/        分镜图片 / Wan 视频作业、本地拼接与字幕
  skill_library.py    Skill 元数据、路线规则、摘要与受控文件快照
  avatar/             人物、变体、权属、身份准入
  tts/                MiniMax 音轨、试听与确认
  digital_human/      M2 / OmniHuman、底片与原片审核
  video/              ASR、时间轴、字幕、FFmpeg、技术检查
  publishing/         抖音浏览器 Adapter 与发布确认
  delivery.py         成片确认、版本校验、ZIP 下载
  engine.py           调度、等待选择、取消、恢复
  jobs.py             外部作业台账、幂等、租约与预算

production/frontend/src/
  views/              七个入口页面
  components/         创作向导、进度、成片交付
  stores/             作品与任务状态、刷新恢复
  api/                接口类型与 HTTP 适配
```

## 开发验证

```powershell
.\production\backend\.venv\Scripts\python.exe -m pytest production\backend\tests -q
npm --prefix production/frontend test
npm --prefix production/frontend run typecheck
npm --prefix production/frontend run build
```

后端测试默认使用独立数据库。大规模测试可将 `AVATAR_STUDIO_TEST_ROOT` 与 pytest `--basetemp` 指向空间充足的磁盘，避免媒体测试文件写满系统盘。

## 说明

本仓库不含 `.env`、运行产物（`runtime/`）、依赖目录（`node_modules/`、`.venv/`）与过程文档。抖音登录态保存在 `production/backend/runtime/publish/douyin-profile/`，该目录不应上传或分享。
