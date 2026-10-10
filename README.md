# Self-media 自媒体视频生成

单用户视频制作工作台，支持固定人物口播和短视频分镜两条路线：创作剧本、模型检查、人工审核、生成图片与视频、拼接、字幕和交付包，再由用户确认抖音投稿。

**本 README 面向从 GitHub 下载 ZIP 的接收者。** 不需要原作者的电脑目录、数据库、模型密钥或 SSH 登录资料。先按下方步骤启动本机工作台，再根据制作路线接入自己的服务。

## 交付内容与部署范围

- 仓库提供前后端源码、锁定依赖、数据库 Compose 配置、空密钥配置模板、业务 Skill、Worker 代码和示例素材。
- ZIP 不含 `.venv`、`node_modules`、真实 `.env`、本机 PostgreSQL 数据、历史项目状态、浏览器登录资料、云端模型权重和已安装的 GPU 环境。这些需要接收者自行准备。
- 首次启动会创建新的业务表并登记基础人物／音色条目；**它不会恢复原作者的项目、人物审核和可生产状态**。在人物页上传或重新登记素材，完成参考政策、权属与人工审核后再用于真实制作。
- 当前提供完整启动脚本的是 **Windows 10/11 单机工作台**。Linux GPU 是可选节点。多机 Linux 生产方案是后续部署目标，需要另做认证、共享媒体、调度恢复和远程发布适配。
- 当前后端没有完整用户认证。默认只监听本机地址，适合单用户使用；对外服务需要先增加访问控制和 HTTPS。

## 一、首次部署：Windows 本机工作台

以下命令均在**解压后的项目根目录**执行，也就是能看到 `README.md`、`production`、`assets` 的目录。建议使用较短目录，例如 `D:\Self-media`，避免 Windows 依赖构建碰到路径长度限制。保留完整目录结构，不要只复制 `production`。

### 1. 安装基础软件

- Windows 10/11 64 位。
- **Python 3.13 64 位**：后端锁定环境以此为基准；不要使用 Python 3.10/3.11 安装这份锁文件。Windows Python Launcher 的 `py -3.13` 应可用。
- **Node.js 24 LTS**，同时安装 npm。
- **Docker Desktop**：启用 WSL 2／Linux containers 并启动引擎，用于运行 PostgreSQL 17。已有 PostgreSQL 的用户可按后面的替代步骤配置。
- 真实视频处理另需 **FFmpeg + ffprobe + libass 字幕滤镜**；仅浏览工作台时可稍后安装。真实抖音发布另需 Google Chrome。

推荐本机 8 核 CPU、16–32 GB 内存，系统和媒体磁盘预留至少 20 GB；制作越多，媒体空间需求越大。这是使用建议，不是并发产能承诺。

检查版本和 Docker 引擎：

```powershell
py -3.13 --version
node --version
npm.cmd --version
docker version
docker compose version
```

没有安装 Git 也可以使用 GitHub 的 **Code → Download ZIP**，解压后部署；私有仓库须给接收者仓库访问权限，或直接发送下载好的 ZIP。

### 2. 安装项目依赖

```powershell
py -3.13 -m venv production/backend/.venv
.\production\backend\.venv\Scripts\python.exe -m pip install --upgrade pip
.\production\backend\.venv\Scripts\python.exe -m pip install -r production/backend/requirements.lock.txt
npm.cmd --prefix production/frontend ci
```

无需激活虚拟环境，后续直接使用上述 Python 路径。`requirements.lock.txt` 与 `package-lock.json` 是复现当前版本的入口；首次安装需要能访问 Python 包索引和 npm registry。

若 `py` 不存在，但已经安装正确版本的 Python，可先确认 `python --version` 为 3.13，再用 `python -m venv production/backend/.venv` 替代第一行。

### 3. 创建配置文件

下面命令**仅首次执行**，不覆盖已有配置：

```powershell
if (-not (Test-Path -LiteralPath 'production/backend/.env')) {
    Copy-Item -LiteralPath 'production/backend/.env.example' -Destination 'production/backend/.env'
}
notepad production/backend/.env
```

先修改数据库密码，其他模型密钥保持空。可以用 `[guid]::NewGuid().ToString('N')` 生成随机密码，在 `.env` 内同步替换两个位置：

```dotenv
POSTGRES_USER=avatar
POSTGRES_DB=avatar_studio
POSTGRES_PASSWORD=换成自己的随机密码
AVATAR_STUDIO_DATABASE_URL=postgresql://avatar:换成自己的随机密码@127.0.0.1:15432/avatar_studio
```

密码使用字母／数字可避免连接串转义；含 `@`、`:`、`/` 等字符时需要做 URL 编码。`.env` 是 UTF-8 文本，填写 `KEY=value`，不要把注释放在值后。

首次保持 `AVATAR_STUDIO_MODE=mock`、授权开关为 `0`、供应商密钥为空、`AVATAR_STUDIO_SECRETS_FILE` 为空。**Mock 标签和零媒体预算不是所有 API 的统一禁费开关**；演示部署应保持密钥为空，也不要在系统环境变量中注入真实凭据。

后端读取 `production/backend/.env`，已设置的系统／终端环境变量优先。不要使用原作者的配置或密码，不需要设置指向原作者目录的 `AVATAR_STUDIO_WORKSPACE`。

### 4. 启动 PostgreSQL

本项目包含 [production/compose.yaml](production/compose.yaml)，只运行数据库；前后端仍在 Windows 宿主机启动：

```powershell
docker compose --env-file production/backend/.env -f production/compose.yaml up -d --wait
docker compose --env-file production/backend/.env -f production/compose.yaml ps
```

应看到 `postgres` 为 `healthy`，容器名 `avatar-studio-pg`。宿主机端口为 **127.0.0.1:15432**，容器内部为 `5432`。数据保存在 Docker named volume，普通停机不会删除。

如果已有同名容器，先确认它是不是本项目的旧数据库。不要覆盖或删除不认识的容器／卷。已有 `avatar-studio-pg` 可用 `docker start avatar-studio-pg` 启动，并把 `.env` 连接串改成它已有的真实账号、密码和数据库。

数据库卷首次初始化后，修改 `.env` 中 `POSTGRES_PASSWORD` **不会自动修改数据库内密码**；已有库需通过 PostgreSQL 修改密码并同步连接串。

不使用 Docker 时：安装 PostgreSQL 17，创建 `avatar_studio` 数据库和具有建表权限的账号，把连接串改成实际地址、端口、账号和密码。应用启动时创建表，不会替你创建数据库；`mock` 模式也需要数据库。

### 5. 启动工作台

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\production\start.ps1 -SkipGpuTunnel
```

脚本启动 FastAPI、Vue 开发服务、本机分镜拼接 Worker 和字幕 Worker，然后打开浏览器：

- 工作台：[http://127.0.0.1:5173](http://127.0.0.1:5173)
- 后端健康：[http://127.0.0.1:8000/api/v1/health](http://127.0.0.1:8000/api/v1/health)
- 接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

`-SkipGpuTunnel` 跳过 SSH 隧道和远端 Worker 恢复。字幕 Worker 若有历史排队任务，仍按其既有配置执行；全新空库、空密钥部署不含这类历史任务。

检查首次启动：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health | Select-Object ok, mode
Invoke-RestMethod http://127.0.0.1:5173/api/v1/health | Select-Object ok, mode
```

两项应返回 `ok=True`、`mode=mock`。第二项验证前端代理也能访问后端。页面应能展示工作台、创建作品和保存基本设置；空密钥时 AI 生成功能会提示未配置。**这一步只证明软件与数据库启动成功，不证明真实视频链路完成。**

服务在后台运行，关闭网页不会停止服务。再次执行启动脚本会复用健康服务；修改后端代码或 `.env` 后，需要停止并重新启动后端及本机 Worker。

端口冲突时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\production\start.ps1 -BackendPort 8001 -FrontendPort 5174 -SkipGpuTunnel
```

数据库 `15432` 若被占用，修改 Compose 的宿主端口和 `.env` 连接串，两者必须一致。不同前后端端口不会自动隔离数据库。

## 二、接入真实功能

先完成上述启动，再按需要编辑 `.env`。详细键名和用途见 [.env.example](production/backend/.env.example)，模板所有 Key／Token 都为空。

### 运行模式与费用

新数据库接入真实功能时，使用显式授权的 **integration** 模式：

```dotenv
AVATAR_STUDIO_MODE=integration
AVATAR_STUDIO_ALLOW_INTEGRATION=1
AVATAR_STUDIO_ALLOW_PRODUCTION=0
```

Integration 可以调用真实服务并收费，不是免费模拟模式。按实际计划填写媒体预算 `AVATAR_STUDIO_BUDGET_PER_TASK_FEN`、`AVATAR_STUDIO_BUDGET_DAILY_FEN`、`AVATAR_STUDIO_BUDGET_MAX_SUBMISSIONS`，人民币分为单位，例如 `100` 表示 ¥1。**内容创作当前不设金额／次数配额**，这组预算不能限制所有写稿、参考导入调用；同时在供应商后台设置额度和限额，保留页面费用确认。

`production` 除配置与授权外，还要求数据库中的供应商成功实测证据。当前新版正常调用没有全部接入这张成功证据表，新空库不能保证直接进入 production；初次真实部署以 integration 运行，生产准入闭环仍需补齐。不要复制他人的证据或手写“成功”记录绕过检查。供应商“检查”按钮读取已有证据，不会自动发付费试请求。

### 按路线配置

- **AI 策划、写稿和审核**：填写自己的 `AVATAR_STUDIO_OPENAI_BASE_URL`、`AVATAR_STUDIO_OPENAI_API_KEY`。默认写稿／重写 `gpt-6.1-sol`，策划／审核 `gpt-6-astra`；当前使用 Chat Completions 的 `/v1/chat/completions`，发送 `developer` 消息、`response_format=json_object`、`max_completion_tokens` 和 `reasoning_effort`（默认 `medium`）；使用者的渠道必须支持这些参数及精确模型 ID。文本和图片 Key 分开。美元费用预留还需 `AVATAR_STUDIO_FX_USD_CNY` 与 `AVATAR_STUDIO_FX_DATE`，填写采用的汇率与日期；默认参考价格不等于渠道真实账单。
- **人物／分镜图片**：填写 `AVATAR_STUDIO_GPTIMAGE_BASE_URL` 和 `AVATAR_STUDIO_GPTIMAGE_API_KEY`；默认 `gpt-image-2.5-sunburst`。先确认自己的渠道支持，生成结果仍须人工审核。
- **Wan 分镜视频**：填写 `AVATAR_STUDIO_QWEN_API_KEY`，以及 `AVATAR_STUDIO_WAN_WORKSPACE_ID` 或 `AVATAR_STUDIO_WAN_BASE_URL`。当前适配器使用 `https://<自己的工作空间ID>.cn-beijing.maas.aliyuncs.com/api/v1`，普通文本兼容接口地址不能代替它；还需要素材可达和页面费用授权。
- **参考检索、转写与画面分析**：红狐用 `AVATAR_STUDIO_REDFOX_API_KEY`；可选 Qwen3-VL 画面分析用百炼 Key。可不用这两项，改用自己准备的资料、直接主题生成或人工输入。
- **口播 TTS**：填写 `AVATAR_STUDIO_MINIMAX_API_KEY` 和实际语音价格配置，当前适配器使用官方 `speech-2.8-hd`。音色需在本人账号下可用并完成界面确认；Wan 分镜原声路线不要求额外 TTS。
- **M2 数字人口播**：准备真正运行 LivePortrait／MuseTalk 的 Linux GPU Worker，填写 `AVATAR_STUDIO_M2_WORKER_URL` 和 `AVATAR_STUDIO_M2_WORKER_TOKEN`。
- **字幕识别／对齐**：准备支持 `asr`／`align` 的 Worker，设置 `AVATAR_STUDIO_ASR_PROVIDER=qwen3_cloud`、`AVATAR_STUDIO_ASR_WORKER_URL`、匹配的令牌。部分现有字幕传输路径还需要火山 TOS。字幕只能使用成功识别／对齐的时间轴，GPU 未就绪时不能仅靠本机 FFmpeg 生成时间轴。
- **OmniHuman／TOS 素材传输**：配置自己账号的火山 AK／SK、已开通地域和自己的 TOS 桶。当前上传实现给供应商输入对象设置 public-read，需理解这些对象可被公开下载；私有桶签名 URL 全链路支持仍需改造。

没有 GPU 也可以启动工作台、创作和人工审核；有相应模型 API 时还可以生成图片、TTS 或 Wan 视频。完整字幕、M2 口播需要另行配置远端推理能力。DeepAgents 异常时可用独立内容流程；模型不可用时可导入／手写分镜，系统如实记录未进行模型审核。

结果未知的付费请求先核对原任务，不自动重复提交。HTTP 403、模型不存在、余额不足应处理对应账号或配置；增加超时不能解决权限问题。

## 三、视频后期与可选节点

### FFmpeg

安装包含 `ffmpeg.exe` 与 `ffprobe.exe` 的完整 Windows 构建，支持 H.264、AAC 和 libass。两者放在同一 `bin` 目录，加入 PATH，或在 `.env` 设置：

```dotenv
AVATAR_STUDIO_FFMPEG_BIN=C:/ffmpeg/bin
```

检查真实安装路径：

```powershell
& 'C:/ffmpeg/bin/ffmpeg.exe' -version
& 'C:/ffmpeg/bin/ffprobe.exe' -version
& 'C:/ffmpeg/bin/ffmpeg.exe' -filters | Select-String 'ass|subtitles'
```

字幕默认字体为 Microsoft YaHei。Windows 需有对应字体；Linux 迁移需要安装适用的中文字体并调整样式，避免字幕方框。当前拼接／烧录使用 CPU 的 `libx264` 编码，不要求本机 NVIDIA GPU。

### Linux GPU Worker

仓库 [production/workers/m2_gpu](production/workers/m2_gpu) 提供 HTTP Worker、runner 和字幕推理脚本，**不附带模型权重、CUDA 或三套 Python 推理环境**。现有 [启动完整服务.sh](production/workers/m2_gpu/启动完整服务.sh) 和恢复服务面向已经安装模型的实例，不是全新 GPU 的一键安装器。

新实例需分别部署 LivePortrait、MuseTalk、Qwen3-ASR／ForcedAligner，按实际路径调整 runner 配置，并核对模型许可和显存。四阶段共用单 GPU 串行队列；成功部署后，健康接口需含 `motion`、`lipsync`、`asr`、`align`。服务健康不等于推理已验收。

连接自己的实例时，使用控制台给出的 SSH 主机和端口。可以先在独立 PowerShell 建立临时隧道：

```powershell
# 替换 ssh-host 和 12345；不要照抄历史文档里的原作者实例地址。
ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 -p 12345 root@ssh-host
```

先通过可信渠道核对服务器主机指纹；终端提示时本机输入自己的密码，保持窗口运行。`.env` 两项 Worker URL 可设为 `http://127.0.0.1:18080`，令牌与远端一致。

长期托管隧道参阅 [隧道使用说明](production/隧道使用说明.md) 和 [Worker 运维说明](production/Worker运维说明.md)。首次准备程序必须显式传自己的地址参数；不能复用原作者私钥、旧实例和登录任务配置。异常重启后的 GPU 作业索引恢复仍有限制，先核对历史任务再决定重新生成。

### 抖音浏览器发布

安装 Google Chrome 和可选依赖：

```powershell
.\production\backend\.venv\Scripts\python.exe -m pip install -r production/backend/requirements-publish.txt
```

将 `.env` 的 `AVATAR_STUDIO_PUBLISH_ADAPTER` 改为 `playwright`，重启后端。发布页打开专用 Chrome，由本人扫码／完成验证，使用当前用户的交互桌面会话。默认 Chrome channel 不要求另下载 Playwright Chromium。

登录状态由本机新建，不能从原作者复制。准备上传、最终确认提交和平台审核通过是不同状态；最终发布需要用户核对并点击确认。当前只支持单账号、单条、即时投稿，真实发布全链路仍需要在接收者账号下验收。

## 四、停止、更新与备份

关闭网页不会停止后台进程。`production/runtime` 保存启动日志和 PID 文件；Windows venv Python 可能有 launcher 与子进程，释放后端端口时应检查实际监听进程。

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(8000,5173) }
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn|vite|app\.short_video\.(worker|subtitle_jobs)' } | Select-Object ProcessId,ParentProcessId,CommandLine
```

核对属于本项目后，用 `Stop-Process -Id <已核对的进程号>` 结束前后端和本机 Worker。PID 文件可能过期，不要盲目照抄旧号，也不要结束别的项目或整个 Docker Desktop。

数据库停机与再次启动：

```powershell
docker compose --env-file production/backend/.env -f production/compose.yaml stop
docker compose --env-file production/backend/.env -f production/compose.yaml start
```

没有进行中的制作任务时再安排停机。Compose `down` 保留 named volume，但 **`down -v` 会删除该部署的数据卷**，不要用于普通停止或升级。

更新源码前备份 `.env`、数据库、`production/backend/runtime`、`production/runtime`、`assets`、`content-media`。这些文件对应同一套业务状态，不应只复制视频而丢失数据库。将新版 ZIP 解压到新目录，再迁移自己的配置与数据；不要用别人数据库的审核／账本冒充自己的记录。

数据库可在 Docker Desktop 正常运行时导出，不使用 PowerShell 重定向传递二进制 dump：

```powershell
New-Item -ItemType Directory -Force backup | Out-Null
docker compose --env-file production/backend/.env -f production/compose.yaml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/avatar-studio.dump'
docker cp avatar-studio-pg:/tmp/avatar-studio.dump backup/avatar-studio.dump
```

备份自己的登录资料和密钥时单独保管，不放进分享 ZIP。GitHub ZIP 是源码交付，不是本人业务数据的备份。恢复数据库使用 `pg_restore`，应先在独立数据库和新目录验证，避免覆盖正在使用的库。

## 五、常见部署问题

- **找不到 Python／Node／docker**：确认安装版本和 PATH，重开 PowerShell；Docker Desktop 必须处于 Linux engine 运行状态。
- **pip 构建报 WinError 17、路径过长或 wheel 文件找不到**：先使用较短的项目目录，再在当前终端执行下面命令。只调整本次终端的临时目录，不修改系统设置。
- **数据库连接拒绝**：检查容器是否 healthy、宿主端口是否为 `15432`，以及 `.env` 用户／密码／数据库是否与首次创建一致。只开启 Docker Desktop 不等于已启动数据库容器。
- **缺少 .venv／node_modules**：执行首次依赖安装步骤，启动脚本不会自动安装。
- **后端返回 mock**：模板默认如此；integration 需显式授权。production 还有成功实测证据准入，新空库暂不能仅靠填 Key 开启。
- **模型按钮提示未配置／汇率缺失**：配置对应能力和汇率；不要把模型访问网页地址或 Markdown 链接填进 API URL。
- **人物 B 有条目却不能制作**：新库只登记基础条目，没有继承原作者的资产准入；用人物页补齐素材、参考政策和审核，或新建自己的角色。
- **没有字幕／字幕报错**：检查 FFmpeg libass、中文字体、远端 ASR 和 align；本机启动成功不代表 GPU 已部署。
- **改配置后仍用旧值**：重启后端及本机 Worker，检查是否有覆盖 `.env` 的系统环境变量。启动脚本会复用旧进程。
- **更新后找不到原素材**：确认保留完整目录与业务库，历史绝对路径记录可能需要重新登记。没有提供任意历史数据库跨机器自动迁移工具。

日志位置：`production/runtime/backend-<端口>.err.log`、`frontend-<端口>.err.log`、`storyboard-worker.err.log`、`subtitle-worker.err.log`。排查时只分享脱敏日志，不分享 `.env` 或浏览器 profile。

```powershell
$installTemp = Join-Path $env:LOCALAPPDATA 'sm-pip-temp'
New-Item -ItemType Directory -Force $installTemp | Out-Null
$env:TEMP = $installTemp
$env:TMP = $env:TEMP
.\production\backend\.venv\Scripts\python.exe -m pip install --no-cache-dir -r production/backend/requirements.lock.txt
```

## 六、功能与技术栈

支持人物／变体管理、内容创作、TTS、视频合成、交付和抖音投稿。指标监控、领域模型选择、多账号与定时发布尚未实现。

- 前端：Vue 3、TypeScript、Pinia、Vite。
- 后端：FastAPI、Pydantic、PostgreSQL、psycopg 3。
- 编排：DeepAgents 和受控业务工具，独立内容任务提供框架之外的入口。
- 模型：GPT 文本、GPT Image 2.5、MiniMax、Wan、OmniHuman、Qwen3-VL／ASR／ForcedAligner。
- 数字人：LivePortrait + MuseTalk；后期：FFmpeg、ffprobe、libass。
- 发布：Playwright 操作抖音创作者中心，不是官方发布 OpenAPI。

分镜人工评分支持五项或八项 1–5 分，全部启用项平均分**严格大于 4**才通过；手工修改确认采用记录独立保留。真实成片应检查身份、服装、音画、字幕和技术规格，Mock 结果不能当成真实交付。

## 七、文档与代码入口

- [production/README.md](production/README.md)：业务链路、能力边界与代码位置。
- [使用说明](production/使用说明.md)：页面操作、人工审核、采用与恢复。
- [最新部署方案](specs/汇报/自媒体视频生成-部署方案.md)／[HTML 版](specs/汇报/自媒体视频生成-部署方案.html)：当前单机与未来多节点规划、示例预算。
- [业务 Skill 改造记录](specs/业务Skill渐进加载改造-20261008.md)：按阶段加载规则；入口为 [策划](production/skills/topic-planning/SKILL.md)、[写作](production/skills/content-writing/SKILL.md)、[审核](production/skills/content-review/SKILL.md)。
- `production/backend/app/content`：创作、独立审核、人工评分和手工导入；`short_video`：镜头图片、视频、拼接与字幕。
- `production/backend/app/agents`：编排与受控工具；`jobs.py`：外部作业、幂等与预算；`storage.py`：PostgreSQL 仓储。
- `production/frontend/src`：页面、组件、状态和 HTTP API。
- `assets`：素材，与 `production` 同级；`content-media`：作品输入素材。

## 八、开发验证

数据库运行后，在项目根目录执行；测试期不会读取后端 `.env`，先提供与自己数据库一致的连接串：

```powershell
$env:AVATAR_STUDIO_DATABASE_URL = 'postgresql://avatar:自己的密码@127.0.0.1:15432/avatar_studio'
.\production\backend\.venv\Scripts\python.exe -m pytest production/backend/tests -q --basetemp production/runtime/pytest-deploy-check
npm.cmd --prefix production/frontend test
npm.cmd --prefix production/frontend run typecheck
npm.cmd --prefix production/frontend run build
```

后端测试使用独立 PostgreSQL schema；测试账号需要 schema 创建权限。媒体测试会占较多磁盘，可把临时目录指向空间充足的磁盘。测试／健康检查与收费模型、GPU 推理、成片观看和真实发布分别验收。

`npm run build` 只生成 `dist`，生产静态服务器仍需配置 `/api`、`/media` 反向代理；Vite 开发服务和单独的 `vite preview` 不等于完整生产部署。按 [最新部署方案](specs/汇报/自媒体视频生成-部署方案.md) 完成认证、HTTPS、备份和端到端验收后再对外使用。

## 九、分享源码

发送 GitHub 下载的完整源码 ZIP，并告诉接收者从本 README 的“一、首次部署”开始。完成修改后须提交并推送到 GitHub，下载的 ZIP 才会包含新版文档与 Compose 文件。

不要直接压缩已经运行过的工作目录：Git 忽略规则**不会**让资源管理器 ZIP 自动排除 `.env`、数据库、SSH 配置、浏览器 profile、依赖和私有素材。如果还需交付自己的作品数据，应独立导出、核对媒体使用范围，并与源码包分开传递。

### 本次部署验证范围

2026-10-10 在本机 Windows 的隔离短目录中，从受版本控制的源码 ZIP 新建虚拟环境、安装 Python 3.13 锁定依赖和 npm 锁定依赖，`pip check`、前端类型检查／构建、PostgreSQL Compose healthy、启动脚本及前端代理检查通过。浏览器创建作品后，重启后端仍能读取同一记录；数据库 `pg_dump`／`docker cp` 备份命令执行通过。

安装中遇到 Windows 长路径／临时目录问题，采用短项目路径、短 TEMP 和 `--no-cache-dir` 后成功。此验收使用全新配置与数据库，不代表在另一台实体电脑或 Linux 上已验收；没有连接算力云、调用付费模型或发布作品。
