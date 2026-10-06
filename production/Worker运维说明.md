# 云端 GPU Worker 恢复

本机 SSH 隧道与云端 Worker 是两个进程。隧道已经连接但云端 8080 拒绝连接时，需要恢复 Worker；重复启动隧道不能解决。

## 首次安装

在本机 PowerShell 执行：

```powershell
.\production\scripts\worker-service.ps1 -Action Install
```

只在本机窗口输入一次算力云 SSH 密码。程序复用已固定的服务器身份和现有三套 Python 环境，预检必要脚本后，在远端 `/root/autodl-tmp/avatar-m2worker/service/` 安装守护程序和启动入口。不会重新安装模型或 CUDA，也不更改旧作业目录。

已有 Worker Token 从本机后端配置通过加密 SSH 的标准输入传输，保存在云端 `service/env.json`，权限为 0600；Token 不进入命令行和本机安装日志。SSH 密码不保存。

另生成一把专用于 Worker 控制的密钥，服务器只接受 `ensure` 和 `status`，禁止普通 shell、PTY 和端口转发；它与已经安装的隧道密钥分开。私钥存放在用户 `.ssh/avatar_studio/` 受限目录。

## 平时使用

```powershell
.\production\start.ps1
```

首次安装成功后，项目启动顺序为：确保云端 Worker 存在 → 检查隧道及四阶段健康 → 启动本机前后端。云端实例必须已开机且 SSH 地址未改变。实例重启后，下次启动项目会重新拉起 Worker；这不等于算力云在无人操作时会自动开机，也不依赖容器一定有 systemd。

```powershell
# 只检查云端 Worker
.\production\scripts\worker-service.ps1 -Action Status

# 需要时启动守护和 Worker
.\production\scripts\worker-service.ps1 -Action Start

# 检查从本机经隧道可达
.\production\scripts\tunnel.ps1 -Action Status
```

已有健康 Worker 会直接复用。守护进程是单例，Worker 退出后按间隔尝试恢复。已有进程仍活着但健康失败、端口被占用、或者有本项目的推理残留时，会等待并报告阻断原因，不自动结束进程。

远端受限日志：`service/guard.log`、`service/worker.log`；运行状态：`service/state.json`。首次安装非敏感回执：本机 `production/runtime/worker-service/install-result.json`。

## 验证范围

`ready` 要求健康接口返回协议版本至少 2，并提供 `motion/lipsync/asr/align`。恢复服务后，再运行以下无作业鉴权检查：

```powershell
.\production\backend\.venv\Scripts\python.exe .\production\scripts\manage_worker.py verify-auth
```

它查询一个随机且不存在的作业 ID。匿名请求应返回 401，有效 Token 应返回 404，确认客户端和服务端令牌匹配，不会创建作业或调用模型。

这些检查证明服务与鉴权就绪，不证明模型已经完成一次字幕推理。Worker 作业索引仍在内存中，异常重启可能丢失旧作业状态；守护程序不会重放、重提旧作业或清理结果。`pending=0` 仅表示排队数量，不表示没有正在执行的任务。

服务代码和 Token 已存在且不同于本次安装内容时，安装器会拒绝覆盖，需要先安排维护。控制密钥没有停机/杀进程权限，避免日常启动误中断正在运行的任务。

## 本机配置验收（2026-09-29）

已完成本实例安装：四阶段健康、guard_running=true、免密 ensure/status、非法控制命令拒绝、M2 和 ASR 的无作业鉴权检查均通过。`start.ps1 -NoBrowser` 实际运行通过并复用了已有前后端。现场结果保存在 `production/runtime/worker-service/verification.json`。本次未提交字幕/视频生成任务，未通过强杀运行中的 Worker 演练恢复。
