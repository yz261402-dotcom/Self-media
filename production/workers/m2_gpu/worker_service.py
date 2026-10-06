#!/usr/bin/env python3
"""Conservative, per-project Linux Worker recovery; never kills or resubmits jobs.

Deploy this file, env.json (0600), and start-all-stages.sh into ROOT/service/.
The SSH control key must use a forced command ending in ``worker_service.py forced``
with ``restrict``. Only the exact remote commands ``ensure`` and ``status`` work.
"""
from __future__ import annotations

import codecs
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import urllib.request


DEFAULT_ROOT = Path("/root/autodl-tmp/avatar-m2worker")
INFERENCE_ROOTS = (
    Path("/root/autodl-tmp/avatar-motion/EXP-001"),
    Path("/root/autodl-tmp/avatar-musetalk/MT-001"),
    Path("/root/autodl-tmp/avatar-subtitles/ASR-001"),
)
STAGES = frozenset(("motion", "lipsync", "asr", "align"))
HEALTH_URL = "http://127.0.0.1:8080/v1/healthz"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 3


class ConfigurationError(Exception):
    """Only constant, non-secret messages may be used with this exception."""


def load_worker_environment(path):
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ConfigurationError("env.json must be a regular file")
        if os.name == "posix" and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
            raise ConfigurationError("env.json requires owner-only permissions 0600")
        if info.st_size > 16384:
            raise ConfigurationError("env.json exceeds the supported size")
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError("env.json is missing or invalid") from None
    if not isinstance(values, dict) or set(values) != {"M2_WORKER_TOKEN"}:
        raise ConfigurationError("env.json must contain only M2_WORKER_TOKEN")
    token = values["M2_WORKER_TOKEN"]
    if not isinstance(token, str) or not token.strip() or len(token) > 8192 or any(c in token for c in "\0\r\n"):
        raise ConfigurationError("M2_WORKER_TOKEN is missing or invalid")
    return values


def health_ready(value):
    if not isinstance(value, dict) or value.get("state") != "ok":
        return False
    try:
        version = value.get("version")
        if isinstance(version, bool) or int(version) < 2:
            return False
        stages = value.get("runner_stages")
        return isinstance(stages, list) and STAGES.issubset(set(stages))
    except (TypeError, ValueError):
        return False


def probe_health():
    """Never inherit HTTP_PROXY for the loopback endpoint or expose response text."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH_URL, timeout=3) as response:
            body = response.read(65537)
        if len(body) > 65536:
            return None
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def port_in_use():
    # Binding catches a listener on any local address, including wildcard bind;
    # a successful connect alone would miss startup and listen backlog races.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 8080))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def process_cwd(folder):
    try:
        return (folder / "cwd").resolve(strict=True)
    except OSError:
        return None


def under(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def scan_project_processes(root, proc_root=Path("/proc")):
    """Return only PIDs/kinds, including inference grandchildren after runner exit.

    Match executable arguments exactly, not shell strings containing a path.
    Relative inference entrypoints require a cwd inside a known project root.
    Unknown process state fails closed, because another GPU job may be running.
    """
    root, proc_root = Path(root), Path(proc_root)
    entries = {str(root / "worker_server.py"): "worker"}
    entries.update({str(root / name): "runner" for name in (
        "runner_liveportrait.py", "runner_musetalk.py", "runner_qwen_asr.py", "runner_qwen_align.py")})
    entries[str(root / "service/start-all-stages.sh")] = "launcher"
    entries[str(root / "start-all-stages.sh")] = "launcher"
    relative_entries = {Path(path).name: kind for path, kind in entries.items()}
    found = []
    try:
        folders = list(proc_root.iterdir())
    except OSError:
        return [{"pid": None, "kind": "process_scan_unavailable"}]
    for folder in folders:
        if not folder.name.isdigit() or int(folder.name) == os.getpid():
            continue
        try:
            raw = (folder / "cmdline").read_bytes()
        except FileNotFoundError:
            continue  # A process that ended while scanning is not a conflict.
        except OSError:
            found.append({"pid": int(folder.name), "kind": "process_scan_unavailable"})
            continue
        args = [piece.decode("utf-8", "replace") for piece in raw.split(b"\0") if piece]
        kind = next((entries[arg] for arg in args if arg in entries), None)
        if kind is None:
            relative = next((relative_entries[Path(arg).name] for arg in args
                             if not Path(arg).is_absolute() and Path(arg).name in relative_entries), None)
            if relative:
                cwd = process_cwd(folder)
                if cwd is not None and under(cwd, root):
                    kind = relative
        if kind is None:
            for arg in args:
                if Path(arg).name not in ("inference.py", "qwen_inference.py") and arg != "scripts.inference_mt001":
                    continue
                if Path(arg).is_absolute():
                    ours = any(under(Path(arg), item) for item in INFERENCE_ROOTS)
                else:
                    cwd = process_cwd(folder)
                    ours = (cwd is not None and any(under(cwd, item) for item in (*INFERENCE_ROOTS, root))) or any(
                        Path(part).is_absolute() and any(under(Path(part), item) for item in INFERENCE_ROOTS)
                        for part in args)
                if ours:
                    kind = "inference"
                    break
        if kind:
            found.append({"pid": int(folder.name), "kind": kind})
    return sorted(found, key=lambda value: value["pid"] or 0)


class FileLock:
    """flock is held by one descriptor; never delete the lock inode."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None
        self.acquired = False

    def __enter__(self):
        import fcntl
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(self.path), flags, 0o600)
        self.file = os.fdopen(descriptor, "a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except BlockingIOError:
            pass
        return self

    def __exit__(self, *args):
        if self.file is not None:
            self.file.close()


def guard_is_running(root):
    with FileLock(Path(root) / "service/guard.lock") as lock:
        return not lock.acquired


def clean_environment():
    # Forced commands must not inherit shell startup hooks or Python injection.
    values = dict(os.environ)
    for key in tuple(values):
        if key == "M2_WORKER_TOKEN" or key in ("BASH_ENV", "ENV", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME") or key.startswith("SSH_"):
            values.pop(key, None)
    return values


def rotating_logger(path, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
    logger = logging.Logger("worker-service:" + str(path))
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def close_logger(logger):
    for handler in logger.handlers:
        handler.close()


def capture_log(stream, path, token, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
    """Drain independently of guard lifetime, with bounded lines and redaction.

    A partial token is retained across reads, including very long unbroken lines.
    The worker receives a pipe owned by this detached process rather than guard.
    """
    logger = rotating_logger(path, max_bytes, backups)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    # Ensure each emitted record can fit in one rotation, even for no-newline output.
    chunk_limit = max(64, min(8192, max_bytes // 4))
    read = getattr(stream, "read1", stream.read)
    try:
        while True:
            data = read(4096)
            pending += decoder.decode(data, final=not data)
            while "\n" in pending or len(pending) > chunk_limit + len(token) or (not data and pending):
                newline = pending.find("\n")
                cut = min(newline + 1 if newline >= 0 else len(pending), chunk_limit)
                # Move a cut before any complete secret crossing that boundary.
                start = pending.find(token, max(0, cut - len(token) + 1))
                if 0 <= start < cut < start + len(token):
                    cut = start if start else start + len(token)
                piece, pending = pending[:cut], pending[cut:]
                logger.info(piece.replace(token, "[REDACTED]").rstrip("\r\n"))
            if not data:
                break
    finally:
        close_logger(logger)


def result(state, detail, **values):
    return {"state": state, "detail": detail, **values}


class Service:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)
        self.managed = self.root / "service"
        self.child = None
        self.started_at = None
        self.next_start = 0.0
        self.backoff = 2.0

    def observe(self):
        health = probe_health()
        if health_ready(health):
            return result("ready", "Existing four-stage Worker is healthy; no jobs were submitted",
                          runner_stages=sorted(STAGES), version=int(health["version"]))
        processes = scan_project_processes(self.root)
        if processes or port_in_use():
            return result("blocked", "Existing listener or project process requires waiting; nothing was stopped",
                          processes=processes)
        return result("worker_unavailable", "No Worker listener or project inference process is active")

    def status(self):
        value = self.observe()
        value["guard_running"] = guard_is_running(self.root)
        return value

    def validate(self):
        values = load_worker_environment(self.managed / "env.json")
        if not (self.managed / "start-all-stages.sh").is_file():
            raise ConfigurationError("service/start-all-stages.sh is missing")
        return values

    def ensure(self, wait_seconds=12):
        try:
            self.validate()
        except ConfigurationError as exc:
            return result("configuration_error", str(exc))
        with FileLock(self.managed / "launch.lock") as launch:
            if not launch.acquired:
                return result("starting", "Another ensure request is checking the guard")
            if not guard_is_running(self.root):
                subprocess.Popen([sys.executable, str(self.managed / "worker_service.py"), "guard"],
                                 cwd=str(self.root), env=clean_environment(), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
            deadline = time.monotonic() + max(0, wait_seconds)
            while True:
                value = self.status()
                if value["state"] == "ready" and value["guard_running"]:
                    return value
                if time.monotonic() >= deadline:
                    if value["state"] == "ready":
                        return result("configuration_error", "Worker is healthy but the service guard did not start",
                                      worker_ready=True, guard_running=False)
                    if value["state"] == "blocked":
                        return value
                    return result("starting" if value["guard_running"] else "worker_unavailable",
                                  "Guard is checking Worker startup; query status for the latest result",
                                  guard_running=value["guard_running"])
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def spawn_worker(self, worker_env):
        base_env = clean_environment()
        logger = subprocess.Popen([sys.executable, str(self.managed / "worker_service.py"), "_log"],
                                  cwd=str(self.root), env=base_env, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True, close_fds=True)
        try:
            return subprocess.Popen(["/bin/bash", str(self.managed / "start-all-stages.sh")],
                                    cwd=str(self.root), env={**base_env, **worker_env},
                                    stdin=subprocess.DEVNULL, stdout=logger.stdin, stderr=subprocess.STDOUT,
                                    start_new_session=True, close_fds=True)
        finally:
            logger.stdin.close()

    def guard_step(self, now):
        if self.child is not None:
            if self.child.poll() is None:
                value = self.observe()
                if value["state"] == "ready":
                    if self.started_at is not None and now - self.started_at >= 60:
                        self.backoff = 2.0
                    return value
                age = now - self.started_at if self.started_at is not None else 60
                return result("starting" if age < 60 else "worker_unavailable",
                              "Managed Worker is still alive; it will not be killed or replaced",
                              worker_pid=self.child.pid)
            self.child = None
            self.next_start = now + self.backoff
            self.backoff = min(60.0, self.backoff * 2)
        observed = self.observe()
        if observed["state"] in ("ready", "blocked"):
            return observed
        if now < self.next_start:
            return result("worker_unavailable", "Worker exited; waiting before a safe restart",
                          retry_after_seconds=round(self.next_start - now, 1))
        try:
            values = self.validate()
            # Probe again immediately before starting: processes may have appeared
            # during validation. flock serializes all managed launch attempts.
            observed = self.observe()
            if observed["state"] in ("ready", "blocked"):
                return observed
            self.child = self.spawn_worker(values)
            self.started_at = now
            return result("starting", "Started the Worker service; no jobs were submitted", worker_pid=self.child.pid)
        except ConfigurationError as exc:
            return result("configuration_error", str(exc))
        except OSError:
            self.next_start = now + self.backoff
            self.backoff = min(60.0, self.backoff * 2)
            return result("worker_unavailable", "Worker process could not be started; see restricted service logs")

    def publish(self, value):
        # State contains only values built by this module, never raw health/proc text.
        path = self.managed / "state.json"
        temp = self.managed / ("state." + str(os.getpid()) + ".tmp")
        temp.write_text(json.dumps({**value, "guard_pid": os.getpid(), "updated_at": time.time()}), encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)

    def guard(self):
        with FileLock(self.managed / "guard.lock") as lock:
            if not lock.acquired:
                return
            logger = rotating_logger(self.managed / "guard.log")
            previous = None
            try:
                while True:
                    value = self.guard_step(time.monotonic())
                    try:
                        self.publish(value)
                    except OSError:
                        logger.warning("Unable to update status file; guard remains active")
                    fingerprint = (value["state"], value["detail"])
                    if fingerprint != previous:
                        logger.info(json.dumps(value, ensure_ascii=True))
                        previous = fingerprint
                    time.sleep(2 if value["state"] == "starting" else 5)
            finally:
                # Independent sessions deliberately outlive guard. No kill/wait.
                close_logger(logger)


def main(argv=None):
    os.umask(0o077)
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["forced"]:
        command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        if command not in ("ensure", "status"):
            print(json.dumps(result("blocked", "Only exact commands ensure and status are permitted")))
            return 2
        args = [command]
    if len(args) != 1 or args[0] not in ("ensure", "status", "guard", "_log"):
        print(json.dumps(result("blocked", "Supported commands: ensure, status, guard")))
        return 2
    service = Service()
    try:
        if args[0] == "guard":
            service.guard()
            return 0
        if args[0] == "_log":
            token = load_worker_environment(service.managed / "env.json")["M2_WORKER_TOKEN"]
            capture_log(sys.stdin.buffer, service.managed / "worker.log", token)
            return 0
        value = service.ensure() if args[0] == "ensure" else service.status()
    except ConfigurationError as exc:
        value = result("configuration_error", str(exc))
    except (OSError, ImportError):
        value = result("configuration_error", "Service requires Linux and readable, owner-only installed files")
    print(json.dumps(value, ensure_ascii=True))
    return 0 if value["state"] in ("ready", "starting") else 1


if __name__ == "__main__":
    raise SystemExit(main())
