#!/usr/bin/env python3
"""M2 GPU Worker —— 跑在算力云实例上的最小 HTTP 作业服务。

这个进程**只管作业生命周期**，不做任何推理。推理通过一个外部 runner 进程完成，
契约极简：

    runner <job.json> <result.json>
      job.json    = {"job_id", "stage", "model", "args": {...}, "work_dir": "绝对路径"}
      result.json = {"state": "succeeded", "output_path": "...", "duration_ms": 1234,
                     "sha256": "...", "notes": [...]}

这么切的原因：GPU 环境深、依赖重、每次开实例都要重配；HTTP 层很薄且和这些无关。
把两者分开后，HTTP 层可以在没有 GPU 的机器上用假 runner 完整验证，
而 GPU 部分留在云机上单独调试，互不牵连。

交付边界（本机尚未在真实算力云上跑过，见 skills/docs 的实测记录）：
  - 本文件写完后只经过本机静态检查 + 假 runner 的端到端验证；
  - **未在 RTX 4080 实例上执行过真实作业**，不作任何"可用"宣称。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

WORKER_VERSION = "2"
ASSET_PROTOCOL_VERSION = WORKER_VERSION
MAX_ASSET_BYTES = 100 * 1024 * 1024
ASSET_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "mp4", "wav", "mp3")
ASSET_NAME_RE = re.compile(r"([0-9a-f]{64})\.(png|jpg|jpeg|webp|mp4|wav|mp3)")
INPUT_HASH_FIELDS = {"source_image_url": "source_hash", "motion_ref_url": "motion_hash",
                     "plate_url": "plate_hash", "audio_url": "audio_hash"}
INPUT_EXTENSIONS = {"source_image_url": {"png", "jpg", "jpeg", "webp"}, "motion_ref_url": {"mp4"},
                    "plate_url": {"mp4"}, "audio_url": {"wav", "mp3"}}

TERMINAL_STATES = ("succeeded", "failed")
# parse_m2_response 认识的状态词有限：queued/running/succeeded/failed/error。
# worker 不得自造状态词，否则后端会把确定结果判成 unknown。
KNOWN_STATES = ("queued", "running", "succeeded", "failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class JobStore:
    """作业表。内存态：进程重启即丢失，不留"看起来已完成"的假象。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        # 同参数作业复用：键是 (stage, args 的规范 JSON) 的哈希。
        # 幂等在这里由 worker 兜一层——后端已有 resubmit_guard，但那是进程内的，
        # 跨进程/重发时守不住。注意：复用只在同一进程内存活，重启后必须重跑。
        self._by_args: Dict[str, str] = {}

    def args_key(self, stage: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(f"{stage}|{canonical}".encode("utf-8")).hexdigest()[:32]

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    def find_reusable(self, stage: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = self.args_key(stage, args)
        with self._lock:
            job_id = self._by_args.get(key)
            return dict(self._jobs[job_id]) if job_id else None

    def create(self, stage: str, model: str, args: Dict[str, Any]) -> Dict[str, Any]:
        key = self.args_key(stage, args)
        with self._lock:
            existing = self._by_args.get(key)
            if existing:
                return dict(self._jobs[existing])
            job_id = uuid.uuid4().hex[:16]
            job = {
                "job_id": job_id,
                "stage": stage,
                "model": model,
                "args": dict(args),
                "state": "queued",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "error": None,
                "code": None,
                "output_path": None,
                "output_hash": None,
                "duration_ms": None,
                "notes": [],
                "attempts": 0,
            }
            self._jobs[job_id] = job
            self._by_args[key] = job_id
            return dict(job)

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.setdefault(job_id, {"job_id": job_id})
            job.update(fields)
            job["updated_at"] = _now_iso()


# 本 worker 认识的阶段。字幕链路（asr / align）与 M2 两阶段共用这一个程序，
# 阶段集合写在这里，新增阶段只需在此登记并配对应 runner。
KNOWN_STAGES = ("motion", "lipsync", "asr", "align")


def _to_argv(command: Any) -> list:
    """命令可以是字符串或列表。

    字符串用 shlex 切字符串标记法：Windows 路径普遍含空格，直接 .split()
    会把 `C:\\Program Files\\...` 拆成两半，然后报"找不到文件"。
    """
    if isinstance(command, (list, tuple)):
        return list(command)
    return shlex.split(str(command), posix=(os.sep == "/"))


class Runner:
    """把 job 交给外部 runner 脚本执行。runner 不在就判 failed，绝不伪造成功。

    M2 两个阶段用的是两套完全不同的推理栈（LivePortrait / MuseTalk），
    所以 runner 是**按 stage 分派**的：`{"motion": ..., "lipsync": ...}`。
    `"*"` 是兜底（测试与单阶段部署用）。
    """

    def __init__(self, commands: Dict[str, Any], work_root: str, timeout_sec: int) -> None:
        self.commands: Dict[str, list] = {k: _to_argv(v) for k, v in commands.items()}
        self.work_root = work_root
        self.timeout_sec = timeout_sec

    def command_for(self, stage: str) -> Optional[list]:
        return self.commands.get(stage) or self.commands.get("*")

    def run(self, job: Dict[str, Any], store: JobStore) -> None:
        job_id = job["job_id"]
        stage = str(job.get("stage") or "")
        command = self.command_for(stage)
        if not command:
            store._update(job_id, state="failed", code="NO_RUNNER_FOR_STAGE",
                          error=f"阶段 {stage!r} 没有配置 runner；"
                                "缺一半配置会让作业跑空却不报错，这里必须显式失败")
            return
        work_dir = os.path.join(self.work_root, job_id)
        os.makedirs(work_dir, exist_ok=True)
        store._update(job_id, state="running", started_at=_now_iso(),
                      attempts=int(job.get("attempts", 0)) + 1)

        job_path = os.path.join(work_dir, "job.json")
        result_path = os.path.join(work_dir, "result.json")
        # work_dir 必须进 job.json：runner 的 cwd 是这个目录，
        # 素材路径只能从这里推，不能依赖相对路径（相对路径在不同 cwd 下会跑错文件）
        staged = dict(job, work_dir=work_dir)
        staged["args"] = self._stage_asset_refs(dict(job.get("args") or {}), work_dir)
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(staged, f, ensure_ascii=False, indent=2)

        argv = command + [job_path, result_path]
        try:
            proc = subprocess.run(argv, cwd=work_dir, timeout=self.timeout_sec,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            raw = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        except subprocess.TimeoutExpired:
            store._update(job_id, state="failed", code="RUNNER_TIMEOUT",
                          error=f"runner 超时（>{self.timeout_sec}s），结果未知需对账")
            return
        except FileNotFoundError:
            store._update(job_id, state="failed", code="RUNNER_NOT_FOUND",
                          error=f"找不到 runner：{argv[0]}（未配置真实推理入口，不假装成功）")
            return

        if not os.path.isfile(result_path):
            store._update(job_id, state="failed", code="RUNNER_NO_RESULT",
                          error="runner 未产出 result.json；退出码为 0 也不等于推理成功",
            runner_output_tail=raw[-4000:])

            return

        try:
            with open(result_path, encoding="utf-8") as f:
                result = json.load(f)
        except Exception as e:  # noqa: BLE001 - result 损坏要能被看见
            store._update(job_id, state="failed", code="RUNNER_RESULT_UNREADABLE",
                          error=f"result.json 不可解析：{type(e).__name__}: {e}",
                          runner_output_tail=raw[-4000:])
            return

        state = str(result.get("state", "")).strip()
        if state not in TERMINAL_STATES:
            store._update(job_id, state="failed", code="RUNNER_STATE_INVALID",
                          error=f"runner 回报的 state={state!r} 不是终态；worker 不替它猜",
                          runner_output_tail=raw[-4000:])
            return

        if state == "failed":
            store._update(job_id, state="failed",
                          code=str(result.get("code") or "RUNNER_FAILED"),
                          error=str(result.get("error") or "runner 报失败"),
                          runner_output_tail=raw[-4000:])
            return

        output_path = str(result.get("output_path") or "")
        if not output_path or not os.path.isfile(output_path):
            store._update(job_id, state="failed", code="RUNNER_OUTPUT_MISSING",
                          error=f"succeeded 但没有输出文件：{output_path or '(未提供)'}",
                          runner_output_tail=raw[-4000:])
            return

        duration_ms = result.get("duration_ms")
        if duration_ms is not None:
            try:
                duration_ms = int(duration_ms)
            except (TypeError, ValueError):
                store._update(job_id, state="failed", code="RUNNER_DURATION_INVALID",
                              error=f"duration_ms 非整数：{duration_ms!r}")
                return

        store._update(job_id, state="succeeded", output_path=output_path,
                      output_hash="sha256:" + _sha256_file(output_path),
                      duration_ms=duration_ms,
                      notes=list(result.get("notes") or []),
                      runner_output_tail=raw[-4000:])

    def _stage_asset_refs(self, args: Dict[str, Any], work_dir: str) -> Dict[str, Any]:
        """将本 worker 自己接收的 worker-asset 引用映射到作业私有目录。"""
        out = dict(args)
        for key, value in list(out.items()):
            if not isinstance(value, str):
                continue
            if value.startswith("worker-asset://"):
                name = value[len("worker-asset://"):]
                match = ASSET_NAME_RE.fullmatch(name)
                if not match or key not in INPUT_HASH_FIELDS:
                    raise ValueError("动作资产引用格式非法")
                source = os.path.join(self.work_root, "_assets", name)
                if not os.path.isfile(source):
                    raise ValueError("动作资产不属于本 worker 或已被清理")
                target = os.path.join(work_dir, "inputs", name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(source, target)
                if _sha256_file(target) != match.group(1):
                    raise ValueError("素材内容已变化，与作业引用的 SHA256 不一致")
                out[key] = Path(target).resolve().as_uri()
            elif key in ("motion_ref_url", "source_image_url", "plate_url", "audio_url") and value.startswith("file:"):
                raise ValueError("不接受客户端直接提交的 file URI")
        return out


class Worker:
    """单 GPU 串行：一次只跑一个作业。排队是常态，不是失败。"""

    def __init__(self, runner: Runner, store: JobStore) -> None:
        self.runner = runner
        self.store = store
        self._q: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="gpu-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, job: Dict[str, Any]) -> None:
        self._q.put(job["job_id"])

    def pending(self) -> int:
        return self._q.qsize()

    def _loop(self) -> None:
        while True:
            job_id = self._q.get()
            job = self.store.get(job_id)
            if job:
                try:
                    self.runner.run(job, self.store)
                except Exception as e:  # noqa: BLE001 - 不让单个作业拖垮 worker
                    self.store._update(job_id, state="failed", code="WORKER_INTERNAL",
                                       error=f"{type(e).__name__}: {e}")
            self._q.task_done()


class Handler(BaseHTTPRequestHandler):
    server_version = f"M2GPUWorker/{WORKER_VERSION}"

    # ---- 基础设施 ----

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = getattr(self.server, "token", "")  # type: ignore[attr-defined]
        if not expected:
            # 没配 token 就是裸奔，明确拒绝而不是"先跑起来再说"。
            self._json(503, {"error": "worker 未配置 token，拒绝服务"})
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or header[7:].strip() != expected:
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- 端点 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 命名约定
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            return self._json(200, {
                "state": "ok", "version": ASSET_PROTOCOL_VERSION,
                "capabilities": {"asset_upload": "sha256-v1", "asset_extensions": list(ASSET_EXTENSIONS)},
                "runner": getattr(self.server, "runner_command", ""),  # type: ignore[attr-defined]
                "runner_stages": getattr(self.server, "runner_stages", []),  # type: ignore[attr-defined]
                "pending": self.server.worker.pending(),  # type: ignore[attr-defined]
                "gpu_probed": getattr(self.server, "gpu_note", "未在本机核验"),  # type: ignore[attr-defined]
            })

        if path.startswith("/v1/jobs/"):
            if not self._authorized():
                return
            job_id = path[len("/v1/jobs/"):].strip("/")
            job = self.server.store.get(job_id)  # type: ignore[attr-defined]
            if not job:
                return self._json(404, {"error": "job not found", "job_id": job_id})
            return self._json(200, self._public(job))

        if path.startswith("/v1/outputs/"):
            if not self._authorized():
                return
            job_id = path[len("/v1/outputs/"):].strip("/").split(".")[0]
            job = self.server.store.get(job_id)  # type: ignore[attr-defined]
            if not job or job.get("state") != "succeeded" or not job.get("output_path"):
                return self._json(404, {"error": "输出不存在或作业未完成"})
            p = job["output_path"]
            size = os.path.getsize(p)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return

        self._json(404, {"error": "not found", "path": path})

    def do_PUT(self) -> None:  # noqa: N802
        """接收本地用户动作；只按声明 SHA-256 命名，避免路径注入和错配。"""
        path = urlparse(self.path).path
        match = ASSET_NAME_RE.fullmatch(path.removeprefix("/v1/assets/")) if path.startswith("/v1/assets/") else None
        if not match:
            return self._json(404, {"error": "asset endpoint not found"})
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length >= MAX_ASSET_BYTES:
            return self._json(413, {"code": "ASSET_TOO_LARGE", "error": "资产为空或超过 100MiB"})
        root = os.path.join(self.server.worker.runner.work_root, "_assets")  # type: ignore[attr-defined]
        os.makedirs(root, exist_ok=True)
        expected = match.group(1).lower()
        extension = match.group(2)
        target = os.path.join(root, expected + "." + extension)
        tmp = target + ".uploading-" + uuid.uuid4().hex
        try:
            digest_ctx = hashlib.sha256()
            remaining = length
            with open(tmp, "wb") as out:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    digest_ctx.update(chunk)
                    remaining -= len(chunk)
            digest = digest_ctx.hexdigest()
            if remaining or digest != expected:
                return self._json(422, {"code": "ASSET_HASH_MISMATCH", "error": "上传内容哈希与路径不一致"})
            os.replace(tmp, target)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        return self._json(201, {"asset_uri": f"worker-asset://{expected}.{extension}",
                                "file_hash": "sha256:" + expected, "bytes": length})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/v1/jobs":
            return self._json(404, {"error": "not found", "path": path})
        if not self._authorized():
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4 * 1024 * 1024:
            return self._json(413, {"error": "body 缺失或过大"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"JSON 不可解析：{type(e).__name__}: {e}"})

        stage = str(payload.get("stage") or "").strip()
        # 只挡**完全未知**的阶段。已知阶段即便本实例没配 runner 也要放行进队列：
        # 那时 Runner 会把它记成 NO_RUNNER_FOR_STAGE 显式失败——
        # 「没配 runner」必须是看得见的失败，不能变成 422 被上层当成"请求不合法"
        # 从而表现成 unknown，那是把配置缺口伪装成输入错误。
        if stage not in KNOWN_STAGES:
            return self._json(422, {"error": f"stage 必须是 {KNOWN_STAGES} 之一：{stage!r}"})
        model = str(payload.get("model") or "").strip()
        args = payload.get("args")
        if not isinstance(args, dict):
            return self._json(422, {"error": "args 必须是对象"})
        # 客户端只能引用本 worker 通过 /v1/assets 接收的确切哈希；
        # 直接 file URI 会造成远端任意路径读取，必须在入队前拒绝。
        for key in INPUT_HASH_FIELDS:
            value = args.get(key)
            if not isinstance(value, str):
                continue
            if value.startswith("file:"):
                return self._json(422, {"code": "UNSAFE_INPUT_URI", "error": f"{key} 不接受 file URI"})
            if value.startswith("worker-asset://"):
                name = value[len("worker-asset://"):]
                match = ASSET_NAME_RE.fullmatch(name)
                if not match or match.group(2) not in INPUT_EXTENSIONS[key]:
                    return self._json(422, {"code": "ASSET_REFERENCE_INVALID", "error": "资产引用格式非法"})
                asset = os.path.join(self.server.worker.runner.work_root, "_assets", name.lower())  # type: ignore[attr-defined]
                if not os.path.isfile(asset):
                    return self._json(422, {"code": "ASSET_NOT_FOUND", "error": "资产不属于本 worker 或已被清理"})
                declared_hash = str(args.get(INPUT_HASH_FIELDS[key]) or "").removeprefix("sha256:").lower()
                if declared_hash != match.group(1):
                    return self._json(422, {"code": "ASSET_HASH_MISMATCH", "error": "资产引用与声明哈希不一致"})
            elif not value.startswith(("http://", "https://")):
                return self._json(422, {"code": "UNSUPPORTED_INPUT_URI", "error": f"{key} 仅接受 http(s) 或 worker-asset 引用"})

        reuse = self.server.store.find_reusable(stage, args)  # type: ignore[attr-defined]
        if reuse:
            return self._json(200, dict(self._public(reuse), reused=True))
        job = self.server.store.create(stage, model, args)  # type: ignore[attr-defined]
        self.server.worker.submit(job)  # type: ignore[attr-defined]
        self._json(202, dict(self._public(job), reused=False))

    # ---- 视图 ----

    def _public(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """出参严格对齐后端 parse_m2_response：state/output_url/output_hash/duration_ms。"""
        base = getattr(self.server, "public_base", "")  # type: ignore[attr-defined]
        out: Dict[str, Any] = {
            "job_id": job["job_id"],
            "state": job.get("state"),
            "stage": job.get("stage"),
            "updated_at": job.get("updated_at"),
            "error": job.get("error"),
            "code": job.get("code"),
            "duration_ms": job.get("duration_ms"),
            "output_hash": job.get("output_hash"),
            "attempts": job.get("attempts"),
            "notes": job.get("notes") or [],
        }
        if job.get("state") == "succeeded" and job.get("output_path"):
            out["output_url"] = f"{base}/v1/outputs/{job['job_id']}.mp4"
        # 失败必须带上 runner 的输出尾巴。只给一句"未产出 result.json"会让真正的原因
        # （脚本路径写错、权重缺失、CUDA OOM…）永远埋在远端，本机无从判断。
        if job.get("state") == "failed":
            out["runner_output_tail"] = (job.get("runner_output_tail") or "")[-2000:]
        return out


def check_commands_resolvable(commands: Dict[str, Any]) -> list:
    """启动前校验 runner 是否指向真实存在的文件。

    子进程的 cwd 是每个作业自己的目录，**相对路径在这里必然解析失败**——
    而失败形态是 python 说一句 "can't open file ..."，作业被记成
    "未产出 result.json"，真实原因（路径写错）完全看不见。
    所以启动就把这个坑挡掉。
    """
    problems = []
    for stage, command in commands.items():
        argv = _to_argv(command)
        if not argv:
            problems.append(f"{stage}: runner 命令为空")
            continue
        if not os.path.isfile(argv[0]) and not shutil.which(argv[0]):
            problems.append(f"{stage}: 找不到可执行程序 {argv[0]!r}")
            continue
        # 形如 [python, script.py]：第二项才是脚本，且必须是**绝对路径**
        scriptish = next((a for a in argv[1:] if a.endswith(".py")), None)
        if scriptish and not os.path.isabs(scriptish):
            problems.append(f"{stage}: runner 脚本用了相对路径 {scriptish!r}；"
                            "子进程 cwd 是作业目录，必须用绝对路径")
        elif scriptish and not os.path.isfile(scriptish):
            problems.append(f"{stage}: runner 脚本不存在 {scriptish!r}")
    return problems


def build_server(*, host: str, port: int, token: str, runner_commands: Dict[str, Any],
                 work_root: str, public_base: str, timeout_sec: int) -> ThreadingHTTPServer:
    os.makedirs(work_root, exist_ok=True)
    store = JobStore()
    runner = Runner(commands=runner_commands, work_root=work_root, timeout_sec=timeout_sec)
    worker = Worker(runner, store)
    worker.start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.store = store  # type: ignore[attr-defined]
    httpd.worker = worker  # type: ignore[attr-defined]
    httpd.token = token  # type: ignore[attr-defined]
    httpd.public_base = public_base.rstrip("/")  # type: ignore[attr-defined]
    httpd.runner_command = json.dumps({k: v for k, v in runner_commands.items()},  # type: ignore[attr-defined]
                                      ensure_ascii=False)
    httpd.runner_stages = sorted(runner_commands)  # type: ignore[attr-defined]
    return httpd


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="M2 GPU worker (HTTP)")
    ap.add_argument("--host", default=os.environ.get("M2_WORKER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("M2_WORKER_PORT", "8080")))
    ap.add_argument("--token", default=os.environ.get("M2_WORKER_TOKEN", ""))
    ap.add_argument("--runner", default=os.environ.get("M2_WORKER_RUNNER", ""),
                    help="兜底 runner（单阶段部署，或未按 stage 拆分时用）")
    ap.add_argument("--runner-motion", default=os.environ.get("M2_WORKER_RUNNER_MOTION", ""),
                    help="motion 阶段 runner（LivePortrait 动态底片）")
    ap.add_argument("--runner-lipsync", default=os.environ.get("M2_WORKER_RUNNER_LIPSYNC", ""),
                    help="lipsync 阶段 runner（MuseTalk 对口型）")
    ap.add_argument("--runner-asr", default=os.environ.get("M2_WORKER_RUNNER_ASR", ""),
                    help="asr 阶段 runner（Qwen3-ASR 识别口播内容）")
    ap.add_argument("--runner-align", default=os.environ.get("M2_WORKER_RUNNER_ALIGN", ""),
                    help="align 阶段 runner（Qwen3-ForcedAligner 生成时间轴）")
    ap.add_argument("--work-root", default=os.environ.get("M2_WORKER_WORK_ROOT",
                                                          tempfile.gettempdir()))
    ap.add_argument("--public-base", default=os.environ.get("M2_WORKER_PUBLIC_BASE", ""))
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("M2_WORKER_TIMEOUT_SEC", "3600")))
    ns = ap.parse_args(argv)

    if not ns.token:
        print("缺少 token：设置 M2_WORKER_TOKEN，并与后端 AVATAR_STUDIO_M2_WORKER_TOKEN 一致",
              file=sys.stderr)
        return 2
    commands: Dict[str, Any] = {}
    if ns.runner:
        commands["*"] = ns.runner
    if ns.runner_motion:
        commands["motion"] = ns.runner_motion
    if ns.runner_lipsync:
        commands["lipsync"] = ns.runner_lipsync
    if ns.runner_asr:
        commands["asr"] = ns.runner_asr
    if ns.runner_align:
        commands["align"] = ns.runner_align
    if not commands:
        print("缺少 runner：设置 --runner（或 --runner-motion / --runner-lipsync）指向真实推理入口；"
              "不提供就没有任何作业能被真正执行", file=sys.stderr)
        return 2
    problems = check_commands_resolvable(commands)
    if problems:
        for p in problems:
            print(f"[runner 校验失败] {p}", file=sys.stderr)
        return 2

    public_base = ns.public_base or f"http://{ns.host}:{ns.port}"
    httpd = build_server(host=ns.host, port=ns.port, token=ns.token,
                         runner_commands=commands, work_root=ns.work_root,
                         public_base=public_base, timeout_sec=ns.timeout)
    print(json.dumps({"listening": f"{ns.host}:{ns.port}", "runners": sorted(commands),
                      "public_base": public_base, "work_root": ns.work_root},
                     ensure_ascii=False), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
