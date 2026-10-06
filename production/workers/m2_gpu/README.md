# M2 远端 GPU Worker

## 当前完整启动入口（2026-09-26）

日常自动检查/恢复使用[Worker 运维说明](../../Worker运维说明.md)中的一次性安装入口。它沿用下方四阶段启动脚本，添加单实例守护与仅允许 ensure/status 的控制密钥；不更改模型或旧作业。下面保留手动部署方式。

算力云已部署的 `motion / lipsync / asr / align` 四阶段统一使用 [启动完整服务.sh](启动完整服务.sh)。
将此文件部署为 `/root/autodl-tmp/avatar-m2worker/start-all-stages.sh`，在既有
`M2_WORKER_TOKEN` 环境变量已注入的终端中执行 `bash start-all-stages.sh`。
启动前确认旧 Worker 与推理均已退出；不要同时起两个进程争用 GPU。
该脚本复用三个独立 Python 环境，没有合并 LivePortrait、MuseTalk 与 Qwen 的依赖。

后端连接配置（只列非敏感项）：

```ini
AVATAR_STUDIO_M2_WORKER_URL=http://127.0.0.1:18080
AVATAR_STUDIO_ASR_PROVIDER=qwen3_cloud
AVATAR_STUDIO_ASR_WORKER_URL=http://127.0.0.1:18080
```

两个地址复用同一条到云端 `127.0.0.1:8080` 的 SSH 隧道，四阶段使用同一个串行队列。
ASR 未单独配置令牌时沿用 M2 令牌。修改后端 `.env` 后需重启后端。
`GET /v1/healthz` 的 `runner_stages` 必须包含 `align, asr, lipsync, motion`；
只看到 `lipsync, motion` 代表字幕入口尚未启用。健康检查不代替真实字幕推理验收。

Qwen 权重与环境位于 `/root/autodl-tmp/avatar-subtitles/ASR-001`，
识别使用 Qwen3-ASR-1.7B，对齐使用 Qwen3-ForcedAligner-0.6B。
推理入口使用该目录内的 `qwen_inference.py` 与 `qwen_audio_chunks.py`，源文件在本 Worker 目录。
超过 180 秒的输入按约 120 秒低能量边界识别；align runner 使用同音频、同正文的
ASR job 结果匹配冻结稿，逐段对齐并恢复采样点偏移，不再拒绝整条长音频。
短音频沿用单次路径。长字幕实测证据见 `experiments/MVP重构/20260926-自由视频时长/`。
当前字幕适配器仍通过 TOS 传递音频，后端需加载已有 TOS 凭据。
Worker 作业索引仍为内存状态：维护前必须确认无在途任务，保留终态回执与媒体原件。
本次恢复记录位于 `experiments/MVP重构/20260926-云端字幕恢复/`。

把 LivePortrait / MuseTalk 两阶段推理包装成一个 **HTTP 作业服务**，让后端不用再
直接操作远端 shell（不再用 `paramiko` 逐条 `exec_command` 拼命令，改用统一 HTTP 契约）。

## 它解决什么

后端 `build_liveportrait_request` / `build_musetalk_request` 产出的请求字典只有
`{stage, model, args}`，**没有 url**；而 `urllib_transport` 要读 `req["url"]`。中间这一层
（`app/digital_human/worker_client.py`）负责翻译 + 提交 + 轮询到终态。

补上前，界面发起点提交走 M2 必然是 `M2_TRANSPORT_UNAVAILABLE`（409）——
这是接线缺失，不是"模型没部署好"。

## 目录说明

| 文件 | 跑在哪 | 作用 |
|---|---|---|
| `worker_server.py` | 算力云实例 | 受理作业、单 GPU 串行执行、失败了如实报失败（不伪造结果） |
| `runner_liveportrait.py` | 算力云实例 | 阶段一：人物图 + 动作参考 → 动态底片 |
| `runner_musetalk.py` | 算力云实例 | 阶段二：底片 + 最终音频 → 口型原片 |
| `runner_common.py` | 算力云实例 | 下载素材、**校验哈希**、量时长、写结果 |

runner 契约极简，所以 HTTP 层能在本机用假 runner 完整验证（已有 13 项测试）：

```
runner <job.json> <result.json>
  job.json    {"job_id","stage","model","args","work_dir"}
  result.json {"state":"succeeded","output_path","duration_ms","sha256","notes"}
```

## 三步接通

### 1. 在算力云实例上起 worker

> **runner 路径必须写绝对路径。** 子进程的工作目录是每个作业自己的目录，
> 相对路径必然解析失败，而失败形态只是 `can't open file`，作业被记成
> "未产出 result.json"，真实原因完全看不见。worker 启动时会校验并直接拒绝启动。

```bash
export M2_WORKER_TOKEN=<自己生成一串随机串>
export M2_LP_ROOT=/root/autodl-tmp/avatar-motion/EXP-001
export M2_LP_PYTHON=$M2_LP_ROOT/env-liveportrait/bin/python
export M2_MT_ROOT=/root/autodl-tmp/avatar-musetalk/MT-001
export M2_MT_PYTHON=$M2_MT_ROOT/env/bin/python

# 两阶段是两套推理栈，各自一个 runner；worker 按 stage 分派，单进程串行跑同一张 GPU。
setsid nohup $M2_MT_PYTHON worker_server.py \
  --host 127.0.0.1 --port 8080 --token "$M2_WORKER_TOKEN" \
  --runner-motion  "$M2_LP_PYTHON /root/autodl-tmp/avatar-m2worker/runner_liveportrait.py" \
  --runner-lipsync "$M2_MT_PYTHON /root/autodl-tmp/avatar-m2worker/runner_musetalk.py" \
  --work-root /root/autodl-tmp/avatar-m2worker/jobs --timeout 2400 \
  > worker.log 2>&1 < /dev/null &
```

实例上有 venv、没有系统 python（`share`/`miniconda` 视实例镜像而定），
worker 本身只用标准库，用哪个推理环境的 python 起都行。

**重启不要用 `pkill -f worker_server.py`**：`-f` 匹配整条命令行，
会把执行这条命令的 shell 自己也匹配上，表现为"命令没有输出、进程也没起来"。
要么 `pkill -f 'worker_serve[r].py'`，要么按 PID 杀。

自检（本机执行）：

```bash
curl -H "Authorization: Bearer $M2_WORKER_TOKEN" http://127.0.0.1:8080/v1/healthz
```

### 2. 把端口转发到本机

长期使用请按[隧道使用说明](../../隧道使用说明.md)安装 Windows OpenSSH 专用密钥与登录任务。项目启动会自动确保连接并检查四阶段健康。下面的 Paramiko 命令保留为历史手动用法，不提供断线自动恢复；不要与托管隧道同时占用 18080。

```bash
set M2_SSH_PASSWORD=<实例 SSH 密码>
python production/scripts/m2_worker_tunnel.py --remote-port 8080 --local-port 18080
```

启动时会**先穿透检查一次**：经 SSH 通道向容器内 `127.0.0.1:8080` 发一次
`GET /v1/healthz`，拿到 HTTP 响应才算通过——这一件事同时证明"允许转发"和
"worker 真的在听"。

> 早先的实现是连一个没人听的高端口、靠异常文本里的关键字判断。
> 那不可靠：paramiko 的异常串只有 `ChannelException(2, 'Connect failed')`，
> 既看不出"服务端禁止"还是"目标端口没服务"，实测把可用环境误判成不可用。

隧道要常驻；用 shell 的 `&` 起的进程会随该次命令结束被回收，
请以常驻后台任务的方式运行。

### 3. 后端指向这个本机端口

```ini
AVATAR_STUDIO_M2_WORKER_URL=http://127.0.0.1:18080
AVATAR_STUDIO_M2_WORKER_TOKEN=<与远端 M2_WORKER_TOKEN 一致>
```

留空则保持 `M2_TRANSPORT_UNAVAILABLE`（409 / retryable）——**这是如实表示"还没接"**。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/healthz` | 存活、排队长度与资产协议版本，**不需要 token**；动作上传要求 `version=2` |
| PUT | `/v1/assets/<sha256>.mp4` | 鉴权接收动作参考，流式校验 SHA-256 后保存；上限 100MiB |
| POST | `/v1/jobs` | `{"stage":"motion"\|"lipsync","model","args"}` → 202 `{job_id,state}` |
| GET | `/v1/jobs/{id}` | `{state,output_url,output_hash,duration_ms,error,code}` |
| GET | `/v1/outputs/{id}.mp4` | 成品字节（成功后才有） |

`state` 只用 `queued / running / succeeded / failed` 四个词，与
`providers.parse_m2_response` 严格对齐。自造状态词会被后端判成 `unknown`。

### 用户自定义动作参考

后端只在用户点击生成底片时，将 `assets/动作参考/用户上传/<id>/动作参考.mp4`
按登记 SHA-256 上传到 v2 Worker。上传成功回执为
`worker-asset://<sha256>.mp4`，提交作业时 Worker 将它复制到该作业的私有目录再
交给 runner。客户端直接提交 Windows 路径、`file:`、未经本次上传回执产生的
`worker-asset:` 或其他未知 scheme 都会在入队前拒绝。Worker v1 不支持该能力时，
后端返回 `ACTION_UPGRADE_REQUIRED`，不会把本地路径当 URL 发送，也不会回退到公共对象存储。

## 已验证 / 未验证

**已验证（2026-09-24，算力云 RTX 4080 实例真实执行）**：
- 13 项端到端测试跑真实 worker 服务 + 假 runner（请求翻译、提交 + 轮询收敛、
  出品下载带 token、401 不判成功、runner 崩溃/报失败不判成功、超时回 running、
  同参数幂等复用）+ 2 项启动校验（runner 相对路径/缺失脚本在启动前拒绝）。
- **AutoDL SSH TCP 转发可用**，穿透 healthz 实测 200。
- **motion 阶段真实出片**：host_b.png + ali_motion_loop25.mp4 → 23.200s 底片，
  提交到终态 126.7s（job `bd9bf651889c4e29`，sha256 `b55f723d…`）。
- **lipsync 阶段真实出片**：上一步底片（经 TOS 公开 URL 回灌，哈希核验一致）+
  真实 TTS 音频 → 720×1280/25fps/579 帧 + AAC 32kHz 单声道，23.187s，
  用时 217.9s（job `2ab8b5433b5c4c45`，sha256 `648f9712…`）。
- **音轨核对实测**：成片音轨与源音频最佳偏移 0 样本（0.0ms），相关系数 0.9996；
  尾部多 425 样本（13.3ms）为 MuseTalk 补帧，远小于 50ms 门禁。
- 抽帧目视两帧：人物一致、口型随时间变化。**这只是抽查，不是身份/口型的正式验收**
  ——看检结论依项目约定由人承担。

**仍未验证**：
- 正式的身份一致性（硬保护区逐像素比对是 compose 链路的能力，M2 生成链路没有）；
  口型质量的正式验收；
- worker 进程崩溃/实例重启后的失败恢复（作业表在内存，重启即丢，这是刻意设计）；
- 多作业排队下的行为（本日只跑了串行两单）。

实测记录与产物哈希：`experiments/M2-Worker链路/MW-001/`。
