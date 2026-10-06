#!/usr/bin/env python3
"""临时运维脚本：在算力云上启动 M2 worker（四阶段）。

令牌取自后端 `.env` 的 `AVATAR_STUDIO_M2_WORKER_TOKEN`（经 SSH 环境变量下发，
不写远端文件、不回显）。密码取自 `M2_SSH_PASSWORD`。
一次性运维用，不属于产品代码。
"""
from __future__ import annotations

import os
import re
import shlex
import sys
import time

import paramiko

HOST = os.environ.get("M2_SSH_HOST", "connect.weste.seetacloud.com")
PORT = int(os.environ.get("M2_SSH_PORT", "21170"))
USER = os.environ.get("M2_SSH_USER", "root")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "backend", ".env")
REMOTE_ROOT = "/root/autodl-tmp/avatar-m2worker"


def read_token() -> str:
    """从后端 .env 取 worker 令牌（与后端保持一致）。"""
    with open(os.path.abspath(ENV_PATH), encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("AVATAR_STUDIO_M2_WORKER_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def run(client, cmd: str, timeout: float = 90.0) -> tuple:
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), o, e


def main() -> int:
    pw = os.environ.get("M2_SSH_PASSWORD", "").strip()
    token = read_token()
    if not pw:
        print("缺少 M2_SSH_PASSWORD", file=sys.stderr)
        return 2
    if not token:
        print("未能从 backend/.env 读到 AVATAR_STUDIO_M2_WORKER_TOKEN", file=sys.stderr)
        return 2
    print(f"令牌已读取（长度 {len(token)}，不回显）")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 算力云网关偶发 `Exceeded MaxStartups` / banner 读失败（未认证连接数限制），
    # 属于瞬时状态，重试几次即可，不代表凭据或实例有问题。
    last = None
    for attempt in range(6):
        try:
            c.connect(HOST, port=PORT, username=USER, password=pw,
                      timeout=25, auth_timeout=25, banner_timeout=25,
                      look_for_keys=False, allow_agent=False)
            last = None
            break
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"连接第 {attempt + 1} 次失败：{type(e).__name__}: {e}")
            time.sleep(8)
    if last is not None:
        print("SSH 连接重试后仍失败", file=sys.stderr)
        return 2
    c.get_transport().set_keepalive(20)
    print(f"已连接 {USER}@{HOST}:{PORT}")

    # 先确认没有残留 worker 争用 GPU（pkill -f 会匹配到自己，用字符类规避）
    rc, o, _ = run(c, "ps -eo pid,cmd | grep 'worker_serve[r].py' || true")
    if o.strip():
        print("检测到既有 worker，先停掉：")
        print(o.strip())
        run(c, "pkill -f 'worker_serve[r].py' || true")
        time.sleep(3)

    # setsid + nohup：脱离本次 SSH 会话，SSH 断开后继续跑
    start = (
        f"cd {shlex.quote(REMOTE_ROOT)} && "
        f"M2_WORKER_TOKEN={shlex.quote(token)} "
        f"setsid nohup bash start-all-stages.sh "
        f"> {REMOTE_ROOT}/worker-all-stages.log 2>&1 < /dev/null & "
        f"echo started"
    )
    rc, o, e = run(c, start)
    print(f"启动命令 rc={rc} out={o.strip()!r}")
    if e.strip():
        print("stderr:", e.strip()[:300])

    # 等 healthz 就绪
    ok = False
    for i in range(12):
        time.sleep(4)
        rc, o, _ = run(c, "curl -s --max-time 6 http://127.0.0.1:8080/v1/healthz || echo NO", timeout=30)
        if o.strip() and o.strip() != "NO":
            print(f"\nhealthz 就绪（第 {i + 1} 次尝试）：")
            print(o.strip()[:900])
            ok = True
            break
    if not ok:
        print("\nhealthz 仍未响应，最后 40 行日志：")
        _rc, log, _ = run(c, f"tail -40 {REMOTE_ROOT}/worker-all-stages.log", timeout=30)
        print(log.strip()[:2000])
    c.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
