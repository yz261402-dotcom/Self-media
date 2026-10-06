#!/usr/bin/env python3
"""临时运维脚本：经 SSH 检查/启动算力云上的 M2 worker。

密码从环境变量 `M2_SSH_PASSWORD` 读取，不写文件、不回显。
一次性运维用，不属于产品代码。
"""
from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ.get("M2_SSH_HOST", "connect.weste.seetacloud.com")
PORT = int(os.environ.get("M2_SSH_PORT", "21170"))
USER = os.environ.get("M2_SSH_USER", "root")


def run(client, cmd: str, timeout: float = 60.0) -> tuple:
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return rc, o, e


def main() -> int:
    pw = os.environ.get("M2_SSH_PASSWORD", "").strip()
    if not pw:
        print("缺少 M2_SSH_PASSWORD", file=sys.stderr)
        return 2
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 算力云网关偶发 `Exceeded MaxStartups` / banner 读失败（未认证连接数限制），
    # 是瞬时状态，重试几次即可。
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
            print(f"连接第 {attempt + 1} 次失败：{type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(8)
    if last is not None:
        print(f"SSH 连接失败：{type(last).__name__}: {last}", file=sys.stderr)
        return 2
    c.get_transport().set_keepalive(20)
    print(f"已连接 {USER}@{HOST}:{PORT}")

    checks = [
        ("worker 进程", "ps -eo pid,etime,cmd | grep 'worker_serve[r].py' || echo '(无)'"),
        ("GPU", "nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo '(nvidia-smi 不可用)'"),
        ("容器内 healthz", "curl -s --max-time 8 http://127.0.0.1:8080/v1/healthz || echo '(8080 无响应)'"),
        ("worker 目录", "ls -1 /root/autodl-tmp/avatar-m2worker/ 2>/dev/null | head -20 || echo '(目录不存在)'"),
    ]
    for label, cmd in checks:
        rc, o, e = run(c, cmd)
        print(f"\n--- {label} (rc={rc}) ---")
        print(o.strip() or "(空)")
        if e.strip():
            print("stderr:", e.strip()[:400])
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
