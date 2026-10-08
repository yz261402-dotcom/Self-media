# 数字人口播工作台

单用户工作台：支持固定人物口播及短视频分镜。口播稿采用后进入语音与数字人制作；分镜剧本经模型检查、人工审核和采用后进入逐镜头图片 / Wan 原声视频生成、本地拼接、字幕及发布包流程。

界面保留七个入口：人物形象、口播稿生成、TTS 语音、视频合成、发布、指标监控、领域模型选择。前四项提供制作链路；发布页现接入首版抖音创作者中心浏览器 Adapter，准备完成后须由用户明确确认发布。其它发布平台、定时发布、自动指标监控和领域模型选择仍未实现。下载包不代表已发布。

操作步骤见 [使用说明](使用说明.md)。产品方案以 `../specs/0001-douyin-ai-studio/` 为准；本文件描述当前代码能力。

2026-09-26 前端已重做：紧凑进度导航、单一人物库、三稿全文比较、播放器优先、分步视频制作；审核与诊断明细按需展开。桌面及手机已复验，前端 29 项回归、类型检查和构建通过。[重做验收与截图](../experiments/MVP重构/20260926-前端重做/重做结果与验收.md)。

## 启动

项目分为本机工作台、本地分镜拼接 Worker、云端 GPU Worker 和连接云端的 SSH 隧道。`start.ps1` 启动本机前后端及分镜拼接 Worker；若已配置云端 Worker 服务和隧道，也会检查并尝试恢复连接。它不会替你开机算力云。

- **只关闭了网页**：服务仍在运行时，直接打开 <http://127.0.0.1:5173>。
- **电脑重启了**：执行下面第 1、3 步；云端 Worker 仍运行时无需重复启动。
- **算力云也重启了**：先在控制台开机，按第 2 步恢复 Worker，再执行第 3 步连接。
- **只写稿、生成图片或 TTS**：本机工作台与对应模型 API 可用即可。M2 动作底片、对口型和云端字幕需要 Worker；OmniHuman 原片由供应商生成，但后续字幕仍使用云端 Worker。
- **只发布已有成片**：本机前后端、发布依赖和 Chrome 可用即可，无需启动 GPU Worker。抖音登录与投稿使用单独的专用浏览器。

### 1. 启动本机工作台

打开本机 PowerShell，执行：

```powershell
cd "C:\Users\22346\Desktop\自媒体视频生成"
powershell -ExecutionPolicy Bypass -File ".\production\start.ps1"
```

默认自动打开 <http://127.0.0.1:5173>，后端监听 `127.0.0.1:8000`。脚本检查环境、端口与健康状态，已运行的服务会被复用；本地分镜拼接 Worker 使用 `backend/.venv` 中的 Python，核对 PID 和命令行后复用或启动。作品和素材保留，无需重新安装依赖。首次部署缺少依赖时，按 [使用说明](使用说明.md) 安装。

前后端在后台运行，启动完成后可以关闭这个 PowerShell 窗口。后端代码或 `backend/.env` 更新后需重启对应后端进程；刷新网页或重新执行启动脚本不会替换已运行的健康后端。结束进程前先核对命令行属于本项目，不要直接使用旧 PID 文件中的进程号。

首次使用抖音发布时，确认本机已安装 Google Chrome，再在项目根目录安装可选发布依赖：

```powershell
.\production\backend\.venv\Scripts\python.exe -m pip install -r production/backend/requirements-publish.txt
```

发布 Adapter 默认使用本机 Chrome，不需要导入日常浏览器的 Cookie。打开“发布”页，点击“打开专用浏览器登录”，由用户扫码并完成可能出现的验证。专用登录状态保存在 `production/backend/runtime/publish/douyin-profile/`，不应上传或分享该目录。即使尚未有成片，也可以先登录；首次安装依赖或更新后端发布代码后，需要重启本项目后端。

### 2. 算力云重启后恢复 Worker

目前云端 Worker 尚未配置开机自启。先在算力云控制台开机，再从本机 PowerShell 登录：

```powershell
ssh -p 20064 root@connect.westd.seetacloud.com
```

上述地址和端口为 2026-09-26 使用的实例连接信息；如果控制台显示已变化，此处和第 3 步都要使用新地址、端口。SSH 密码按终端提示输入。

登录后，在**云端终端**先检查服务：

```bash
curl -fsS --max-time 5 http://127.0.0.1:8080/v1/healthz
```

返回 `state: ok`，且 `runner_stages` 包含 `align、asr、lipsync、motion` 四项时，直接进入第 3 步。只有两项代表字幕服务未启用。健康检查失败不代表 Worker 已退出，先检查进程与端口：

```bash
pgrep -f '[w]orker_server.py'
ss -ltnp 'sport = :8080'
```

仅在确认没有 Worker 进程、8080 也没有监听时执行以下启动命令。已有进程但健康检查失败时先查看日志，不要重复启动或在生成任务进行中重启。

```bash
read -rsp '输入已有 Worker 令牌: ' M2_WORKER_TOKEN
export M2_WORKER_TOKEN
nohup bash /root/autodl-tmp/avatar-m2worker/start-all-stages.sh \
  >> /root/autodl-tmp/avatar-m2worker/worker-all-stages.log 2>&1 < /dev/null &
```

这里输入的是本机 `production/backend/.env` 中 `AVATAR_STUDIO_M2_WORKER_TOKEN` 的现有值，**不是 SSH 密码**。只输入等号后的值，不含变量名或外围引号，不要重新生成令牌，也不要把令牌写入文档。

启动后再次执行上面的 `curl`，确认四个阶段可用。`nohup` 让服务在退出 SSH 后继续运行，但不能让它在实例重启后自动恢复。启动失败时在云端查看：

```bash
tail -n 60 /root/autodl-tmp/avatar-m2worker/worker-all-stages.log
```

### 3. 建立本机到算力云的连接

另开一个**本机 PowerShell**，执行：

```powershell
cd "C:\Users\22346\Desktop\自媒体视频生成"
D:\anaconda3\python.exe production/scripts/m2_worker_tunnel.py --ssh-host connect.westd.seetacloud.com --ssh-port 20064 --remote-port 8080 --local-port 18080
```

按提示输入 SSH 密码；出现“隧道已就绪”后，**保持这个窗口打开**。它将本机 `127.0.0.1:18080` 转发到云端 `127.0.0.1:8080`。当前电脑的 Anaconda Python 已安装隧道依赖 `paramiko`，不要直接换成后端虚拟环境的 Python。

在另一个本机 PowerShell 中检查连接：

```powershell
Invoke-RestMethod http://127.0.0.1:18080/v1/healthz
```

返回 `state: ok` 和四个阶段后，再在页面生成 M2 视频或字幕。隧道已正常运行时不用再启动；端口占用先检查已有隧道。关掉隧道窗口、电脑重启或网络断开后，需要重新建立连接，当前没有自动重连。

后端现有配置中，M2 和 ASR Worker 地址均为 `http://127.0.0.1:18080`，字幕 Provider 为 `qwen3_cloud`；保留已有配置即可。修改配置后必须重启后端。

### 检查状态和定位问题

- 工作台：<http://127.0.0.1:5173>。
- 后端健康检查：<http://127.0.0.1:8000/api/v1/health>，应返回 `ok: true`。
- 云端连接检查：<http://127.0.0.1:18080/v1/healthz>，应包含四个阶段。页面能打开不代表云端已连接。
- 本机启动日志与 PID：`production/runtime/`；重点查看 `backend-8000.err.log`、`frontend-5173.err.log` 和 `storyboard-worker.err.log`。本地分镜 Worker 的 PID 保存在 `storyboard-worker.pid`；结束进程前仍须核对实际命令行。
- 云端 Worker 启动细节：[Worker 说明](workers/m2_gpu/README.md)。

### 开发时使用独立端口

在项目根目录执行：

```powershell
.\production\start.ps1 -BackendPort 8001 -FrontendPort 5174 -NoBrowser
```

更换端口不会自动隔离数据库。验收与正式使用不得仅靠端口区分数据。

## 当前制作链路

1. **创建作品**：填写主题、受众、时长和要求。顶部五步向导显示当前缺项，选择会持久保存。
2. **确认人物**：选择已就绪主播；新人物完成原图审核、参考依据、权属确认、冻结定版。人物身份与背景变体分开管理。
3. **内容创作与采用**：选择口播或分镜路线，由写稿模型生成不同角度候选、独立审核模型形成检查结论；失败或审核异常的草稿仍可展示、编辑和重审。口播通过后可采用；分镜单篇模型通过后进入人工审核，评分均值严格大于 4 才可评分通过。人工修改确认采用记录为 `human_edit`，用户意见定向重写后重新经过模型及人工审核。此环节不联网搜索或爬取。
4. **试听与确认**：MiniMax 生成试听和全文语音。用户听检并确认当前音轨。相同输入已有有效音频时优先复用；显式重新合成可能再次收费。
5. **合成与下载**：确认生成条件和调用清单，生成口播原片；审核画面与音轨并确认原片；代码 Worker 生成字幕、封面与成片；观看成片后确认并下载。
6. **抖音发布**：选择已确认的交付包，编辑标题、简介和话题，开始准备上传；核对专用浏览器内的账号、视频、封面、文案和 AI 内容声明后，勾选并点击“确认并发布”。准备阶段不执行最终发布。

M2 路线需要已审人物变体和获准动作参考：LivePortrait 生成表情、头部运动底片，MuseTalk 使用已确认 TTS 对口型。M2 不提供任意身体动作生成。OmniHuman 1.5 接收同一人物图片与音频，生成包含动作的口播，按云端接口规则收费。两条路线都保留固定人物和已确认音轨，不使用独立文生视频替代口播。

分镜路线采用完整结构化剧本后，先确认图片预算、生成并检查镜头图，再确认 Wan 费用并生成镜头原声视频。本机 Worker 拼接成片，云端 ASR / 对齐可生成并烧录字幕。单镜头图片或视频明确失败时提供重新生成；请求结果未知时只核对原作业，不自动重发。

DeepAgents 不可用时，“直接按主题生成三稿”走独立持久化内容任务；模型服务不可用时，可通过“导入 / 手写分镜”人工接管。人工稿记录 `manual`、`ai_review_status=not_performed`，不伪造模型通过或三篇批次；确认事实及使用权后，走人工审核和采用流程。

人物页现支持自定义动作视频上传：本机检查与规范化后预览、命名并保存为个人模板，可直接用于本期或跨作品复用。云端 Worker v2 支持通过鉴权接口接收已登记的本地图片、视频、音频；SSH 隧道会完整发送大文件数据块。具体限制与操作见使用说明。

## 实际技术实现

- **界面**：Vue 3、TypeScript、Pinia、Vite；七入口与常驻创作向导。
- **服务与持久化**：FastAPI、Pydantic、PostgreSQL（psycopg 3）；作品、选择、审核、任务、成本与资产血缘持久化。本机开发使用 Docker 容器 `avatar-studio-pg`（端口 `15432`），连接串见 `backend/.env` 的 `AVATAR_STUDIO_DATABASE_URL`。pgvector 未部署。
- **智能体**：supervisor 总控维护工作项依赖和人工选择门，content / quality 角色执行受控内容工具；媒体制作由业务服务及 Worker 执行。默认由 DeepAgents 执行工作项，业务门禁、预算与幂等由代码复算；直接内容流程可独立运行。
- **模型调度边界**：默认 `AVATAR_STUDIO_MODEL_BACKEND=deterministic`，表示框架调度使用确定性决策，不代表所有生成 Provider 都是 Mock。真正策划、写稿、返工及审核由内容服务调用配置的模型；默认写稿 / 返工为 `gpt-6.1-sol`，策划 / 审核为 `gpt-6-astra`。`deepseek` 自主调度适配器作为可选路径保留，使用时需满足依赖与预算配置。
- **模型与媒体**：口播稿与返工默认使用 GPT（`AVATAR_STUDIO_CONTENT_WRITER_PROVIDER=openai`，model `gpt-6.1-sol`）；画面分析使用 Qwen3-VL；图片使用 GPT Image 2.5 Sunburst（请求模型 ID `gpt-image-2.5-sunburst`，兼容保留原有图片 Provider 配置键）；语音 MiniMax `speech-2.8-hd`；M2 为 LivePortrait + MuseTalk 1.5；云路线为 OmniHuman 1.5。`deepseek` 作为可选写稿/调度 Provider 保留。
- **字幕与后期**：Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B 提供真实识别/对齐时间轴。FFmpeg 完成裁切、规格统一、降噪/锐化、响度处理、字幕与封面输出，并复验成片。当前清晰度处理是常规滤镜与缩放，不是模型超分。
- **发布**：独立 Playwright Adapter 操作抖音创作者中心，发布任务、确认快照与提交状态存入业务库。它不是官方 OpenAPI；首版只支持单账号、单条、即时投稿。
- **Skill / MCP / 记忆**：S01 选题、S02 写作、S03 独立审核使用标准 `name/description` 元数据。框架仅展示角色白名单目录，正文及参考通过受限只读内存快照按需读取；实际内容调用按阶段和口播 / 分镜路线读取同一规则，降级流程同样可用。版本及摘要包含参考文件。记录工具审计及 checkpoint、persona、history、experience；无联网搜索工具及 editor Agent。

## 已接通的交付能力

后期已实现真实文件输出，不再只有依赖检查入口。最终规格为竖屏 `1080 × 1920`、30 fps、MP4 / H.264 / AAC。下载 ZIP 包含：

- `成片.mp4`、`字幕.ass`、`字幕.srt`、`封面.jpg`；
- `作品信息.json`：标题、简介、标签、正文和 AI 标识；
- `质量报告.json`：技术复验、字幕检查与人工成片确认；
- `manifest.json`：当前作品、任务、文件大小与 SHA256。

下载前校验上游版本、实际媒体文件与审核记录。更换正文、音色、人物或原片后，旧下游产物会失效；不会通过沿用旧包把新版本标为完成。Mock 媒体不能进入真实交付，进度未知时只显示阶段与等待状态。

## 抖音发布首版

发布页提供专用 Chrome 登录、已确认成片预览、可编辑投稿文案、准备上传、用户确认、取消准备与任务状态查询。标题要求 1–30 字；准备后文案锁定为本次快照，需要修改时先取消准备再重做。最终确认绑定交付包、账号与文案版本，不能沿用旧页面确认新内容。

“已提交”表示提交给平台，不表示审核通过或作品已公开。结果未知时必须先到创作者中心核验，系统禁止重复发布。扫码、验证码和人工内容核对均由用户完成；页面变化或验证阻断会显示具体错误。

当前正在进行真实账号的**只上传、不发布**验收，尚不能宣称真实发布全链路已经验收通过。其它平台、定时发布、多账号与自动数据监控未实现。最新证据见 [抖音发布实施与验收](../experiments/MVP重构/20260927-抖音发布/实施与验收.md)，具体操作见 [使用说明](使用说明.md)。

## 当前验收边界

2026-09-25/26 本轮重构已使用既有真实 M2 视频、TTS 和已验证的字幕时间轴，执行本地后期复验：23.17 秒、1080 × 1920、30 fps、H.264 / AAC，输出字幕与封面，并检查解码、响度及黑帧/冻结情况。证据位于 `../experiments/MVP重构/20260925/真实后期验收/`。

这证明本地后期处理能力，不等于本轮新作品已从模型生成一路完成验收。本轮没有新增收费模型调用或新 GPU 生成；算力云连接仍需恢复后复验，人物身份、口型、表情等主观质量仍需要人工观看确认。当前也未验收日产 5 条、全模型自主调度或官方自动发布。最终测试结果以本轮验收记录为准。

人物 B 和历史资产会保留。历史“已冻结”不自动补出缺失审核；若冻结人物缺项无法修改，可复制为新草稿，复用同一原图重新完善审核。换背景的确定性合成需已确认蒙版和背景资产；当前图片中转的参考编辑一致性未通过验证，不承诺任意换衣都能保持同一张脸。

## 配置与运行模式

后端读取 `backend/.env`，环境变量优先。不要将密钥写入前端、说明文档或提交到仓库。模型选择页面暂不提供密钥编辑；当前从服务端配置管理。

- `AVATAR_STUDIO_MODE`：`mock` / `integration` / `production`；真实模式还要求对应授权和预算配置。
- `AVATAR_STUDIO_ORCHESTRATOR=agents`、`AVATAR_STUDIO_AGENT_RUNTIME=deepagents`：默认编排路径。
- `AVATAR_STUDIO_MODEL_BACKEND`：`deterministic` 为受控确定性调度，`deepseek` 为真实聊天模型调度。
- `AVATAR_STUDIO_DEEPSEEK_*`、`AVATAR_STUDIO_GPTIMAGE_*`、`AVATAR_STUDIO_MINIMAX_*`：供应商配置；具体受支持字段见实现，不以配置文本齐全判断模型可用。
- `AVATAR_STUDIO_M2_WORKER_URL` / `AVATAR_STUDIO_M2_WORKER_TOKEN`：算力云口播 Worker。
- `AVATAR_STUDIO_ASR_PROVIDER=qwen3_cloud`、`AVATAR_STUDIO_ASR_WORKER_URL` / `AVATAR_STUDIO_ASR_WORKER_TOKEN`：字幕识别和对齐 Worker。
- `AVATAR_STUDIO_VOLC_ACCESS_KEY` / `AVATAR_STUDIO_VOLC_SECRET_KEY`：火山视觉签名；素材传输还需相应 TOS 配置。
- `AVATAR_STUDIO_FFMPEG_BIN`：包含 `ffmpeg` 和 `ffprobe` 的目录，也可从 PATH 查找。
- `AVATAR_STUDIO_PUBLISH_ADAPTER=playwright`：真实抖音浏览器发布；`mock` 仅供隔离测试。`AVATAR_STUDIO_DOUYIN_BROWSER_CHANNEL` 默认 `chrome`。

未配置预算时默认不允许可计费提交。界面确认调用清单与服务端预算授权是两件事。超时后若供应商可能已受理，任务进入待对账状态，不用重复提交猜测结果。

## 代码位置

```text
production/
  start.ps1                 Windows 启动与健康检查
  使用说明.md               五步操作及问题定位
  frontend/src/
    components/             创作向导、进度、成片交付
    views/                  七入口页面
    stores/                 作品与任务状态、刷新恢复
    api/                    接口类型与 HTTP 适配
  backend/app/
    agents/                 总控、角色、DeepAgents、受控工具
    content/                口播稿与质量评估
    avatar/                 人物、变体、权属、身份准入
    tts/                    MiniMax、音轨、试听与确认
    digital_human/          M2 / OmniHuman、底片与原片审核
    video/                  ASR、时间轴、字幕、FFmpeg、技术检查
    postprocess_worker.py   代码后期执行与持久记录
    delivery.py             成片确认、版本校验、ZIP 下载
    publishing/             抖音浏览器 Adapter、任务与人工发布确认
    workflow_api.py         后期与交付聚合接口
    engine.py               调度、等待选择、取消、恢复
    jobs.py                 外部作业台账、幂等、租约与预算
    memory.py               四层记忆
    lineage.py              上下游依赖失效
    storage.py              PostgreSQL 仓储
  backend/tests/            后端回归
  frontend/tests/           前端状态回归
  skills/                   业务 Skill
  runtime/                  启动日志与进程号
```

## 开发验证

在项目根目录运行：

```powershell
.\production\backend\.venv\Scripts\python.exe -m pytest production/backend/tests -q
npm --prefix production/frontend test
npm --prefix production/frontend run typecheck
npm --prefix production/frontend run build
```

后端测试默认使用独立数据库。大规模测试可将 `AVATAR_STUDIO_TEST_ROOT` 和 pytest `--basetemp` 指向空间充足的磁盘，避免将媒体测试文件写满系统盘。Mock/契约测试与真实 Provider、云端 GPU、人工画面审核分别验收。
