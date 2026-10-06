#!/usr/bin/env python3
"""两个 GPU runner 共用的小工具：取输入、校哈希、量时长、写结果。

放在 runner 侧而不是 worker 侧的理由：
- 下载与哈希校验只在**作业进程**里发生，worker 主线程不被 IO 卡住；
- 哈希校验是与推理同层的验收，不该被 HTTP 层捎带 —— 对不上必须让作业失败，
  否则就是拿另一个文件跑完了还登记成功。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional


def read_job(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_result(path: str, result: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def fail_result(reason: Dict[str, Any]) -> Dict[str, Any]:
    """失败结果。runner 一律用这个出去，不靠抛异常传递语义。"""
    return {"state": "failed", "code": reason.get("code", "RUNNER_FAILED"),
            "error": reason.get("error", "runner 失败"), "notes": reason.get("notes", [])}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: str) -> None:
    """直接从对象存储取素材。不重试：失败要被看见，而不是被重试掩盖成偶发。"""
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def fetch_and_verify(url: str, dest: str, declared: str, label: str) -> None:
    """下载并核对哈希。declared 形如 `sha256:abc...`。

    哈希是**版本身份**：底片/音频对不上就说明拿到的不是登记过的那一份，
    继续跑下去产出的东西无法追溯到任何被审核过的输入。
    """
    download(url, dest)
    if not declared:
        raise ValueError(f"{label} 未提供声明哈希，无法核对输入版本")
    expected = declared.split(":", 1)[1] if ":" in declared else declared
    actual = sha256_file(dest)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"{label} 哈希不符：声明 {expected[:16]}… 实际 {actual[:16]}…；"
            "拒绝用来源不明的输入推理")


def probe_duration_ms(path: str) -> Optional[int]:
    """用 ffprobe 量真实时长。没有 ffprobe 或结果异常时返回 None。

    返回 None 不构成失败：时长是元信息，后端 register_video 会自己再验一次；
    但也不能拿 argument 里的 seconds 冒充实测值。
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, check=True, timeout=60).stdout
        return int(round(float(out.decode().strip()) * 1000))
    except Exception:  # noqa: BLE001 - 量不到就如实说量不到
        return None


def find_file(root: str, name: str) -> Optional[str]:
    for dirpath, _dirnames, filenames in os.walk(root):
        if name in filenames:
            return os.path.join(dirpath, name)
    return None


def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"缺少环境变量 {name}：runner 不猜路径，猜错会跑错环境")
    return v


def run_capture(argv: list, *, cwd: str, timeout: int,
                env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    if env is None:
        env = dict(os.environ)
    # 子进程一律继承一份显式 env：依赖"启动 worker 的那个 shell 设过什么"
    # 会让同一份 runner 在不同启动方式下跑出不同结果，事后无法复现。
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.run(argv, cwd=cwd, timeout=timeout, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def child_env(defaults: Dict[str, str]) -> Dict[str, str]:
    """在父进程环境上叠加默认值（父进程已设的不覆盖）。

    缓存/离线相关的变量必须显式给：推理进程的默认行为是联网去找权重，
    而 MT-001 的权重是**已下到本地**的，联网失败会卡死而不是报错。
    """
    env = dict(os.environ)
    for k, v in defaults.items():
        env.setdefault(k, v)
    return env


def main_path() -> None:
    sys.stderr.write("此文件是工具库，不被直接调用\n")
