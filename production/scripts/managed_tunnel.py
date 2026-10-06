#!/usr/bin/env python3
"""Supervise a pinned-key, loopback-only OpenSSH tunnel without submitting jobs.

The configuration contains paths and public connection settings only. Secrets,
interactive prompts, remote commands and worker restarts are deliberately absent.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time


STAGES = frozenset({"align", "asr", "lipsync", "motion"})
STALE_SECONDS = 20


@dataclass(frozen=True)
class Config:
    ssh_executable: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    identity_file: str
    known_hosts_file: str
    local_port: int
    remote_port: int
    runtime_dir: str
    required_stages: list[str]

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("configuration must contain exactly the documented fields")
        for field in ("ssh_port", "local_port", "remote_port"):
            value = data[field]
            if type(value) is not int or not 1 <= value <= 65535:
                raise ValueError(f"{field} must be an integer between 1 and 65535")
        for field in ("ssh_executable", "identity_file", "known_hosts_file", "runtime_dir"):
            value = data[field]
            if (not isinstance(value, str) or not Path(value).is_absolute()
                    or any(ord(c) < 32 for c in value) or '"' in value or "%" in value):
                raise ValueError(f"{field} must be an absolute, literal path")
            if field != "runtime_dir" and not Path(value).is_file():
                raise ValueError(f"{field} does not point to an existing file")
        if not isinstance(data["ssh_host"], str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", data["ssh_host"]):
            raise ValueError("ssh_host must be a literal hostname or IPv4 address")
        if not isinstance(data["ssh_user"], str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_-]{0,63}", data["ssh_user"]):
            raise ValueError("ssh_user is invalid")
        stages = data["required_stages"]
        if (not isinstance(stages, list) or any(type(s) is not str for s in stages)
                or len(stages) != 4 or set(stages) != STAGES):
            raise ValueError("required_stages must contain align, asr, lipsync and motion")
        return cls(**data)

    @classmethod
    def load(cls, filename):
        if not Path(filename).is_absolute():
            raise ValueError("--config must be an absolute path")
        with open(filename, encoding="utf-8-sig") as handle:
            return cls.from_dict(json.load(handle))


def build_ssh_command(config):
    # Quote this value for OpenSSH's own config lexer, in addition to using an
    # argv array for Windows. Forward slashes avoid config-file backslash escapes.
    known = Path(config.known_hosts_file).as_posix()
    options = ["BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
               f'UserKnownHostsFile="{known}"', "GlobalKnownHostsFile=none",
               "ExitOnForwardFailure=yes", "ServerAliveInterval=20",
               "ServerAliveCountMax=3", "ConnectTimeout=10", "ConnectionAttempts=1",
               "ForwardAgent=no"]
    command = [config.ssh_executable, "-F", "none", "-N", "-T", "-p", str(config.ssh_port),
               "-i", config.identity_file, "-L",
               f"127.0.0.1:{config.local_port}:127.0.0.1:{config.remote_port}"]
    for option in options:
        command.extend(["-o", option])
    return command + [f"{config.ssh_user}@{config.ssh_host}"]


class SingleInstanceLock:
    """An OS-owned byte lock, automatically released on process death."""

    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def port_is_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def probe_worker(port, required_stages, timeout=3.0):
    """A proxy-free, size-bounded HTTP GET with a total wall-clock deadline."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(b"GET /v1/healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            received = bytearray()
            body_offset = None
            length = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False, "worker health check timed out"
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
                if body_offset is None:
                    separator = received.find(b"\r\n\r\n")
                    if separator < 0:
                        if len(received) > 16384:
                            return False, "worker health headers exceed limit"
                        continue
                    if separator > 16384:
                        return False, "worker health headers exceed limit"
                    header = received[:separator].decode("iso-8859-1").split("\r\n")
                    if len(header[0].split()) < 2 or header[0].split()[1] != "200":
                        return False, "worker health HTTP status is not 200"
                    fields = dict(line.lower().split(":", 1) for line in header[1:] if ":" in line)
                    if "transfer-encoding" in fields:
                        return False, "worker health uses unsupported transfer encoding"
                    if "content-length" in fields:
                        length = int(fields["content-length"].strip())
                        if not 0 <= length <= 65536:
                            return False, "worker health body exceeds limit"
                    body_offset = separator + 4
                body_length = len(received) - body_offset
                if body_length > 65536:
                    return False, "worker health body exceeds limit"
                if length is not None and body_length >= length:
                    break
            if body_offset is None:
                return False, "worker health response is incomplete"
            body = received[body_offset:]
            if length is not None and len(body) != length:
                return False, "worker health body is incomplete"
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("state") != "ok":
                return False, "worker is not healthy"
            version = payload.get("version")
            # worker_server.WORKER_VERSION is a string; accept its canonical
            # decimal representation while still rejecting floats and booleans.
            if isinstance(version, str) and re.fullmatch(r"[0-9]{1,6}", version):
                version = int(version)
            if type(version) is not int or version < 2:
                return False, "worker protocol version must be >= 2"
            stages = payload.get("runner_stages")
            if (not isinstance(stages, list) or any(type(s) is not str for s in stages)
                    or not set(required_stages).issubset(stages)):
                return False, "worker does not provide all required stages"
            return True, "SSH tunnel and four-stage worker are healthy"
    except (OSError, ValueError, TypeError):
        return False, "worker health request failed; SSH process is checked separately"


class Backoff:
    def __init__(self, jitter=random.random):
        self.failures = 0
        self.jitter = jitter

    def after_exit(self, lifetime):
        if lifetime >= 60:
            self.failures = 0
        base = min(60, 2 ** min(self.failures, 6))
        self.failures += 1
        return min(60.0, base * (1 + 0.2 * self.jitter()))


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def atomic_status(config, status):
    directory = Path(config.runtime_dir)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".status.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, directory / "status.json")


def read_status(config):
    try:
        status = json.loads((Path(config.runtime_dir) / "status.json").read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])).total_seconds()
        active = status.get("state") not in {"stopped", "configuration_error"}
        if active and (not 0 <= age <= STALE_SECONDS or not process_alive(status.get("supervisor_pid"))):
            return {**status, "state": "stopped", "detail": "supervisor heartbeat is stale or process exited"}
        if status.get("state") == "ready" and not process_alive(status.get("ssh_pid")):
            return {**status, "state": "reconnecting", "detail": "SSH process exited"}
        return status
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"state": "stopped", "detail": "no valid supervisor status"}


def check_status(config):
    status = read_status(config)
    if status["state"] in {"stopped", "configuration_error", "port_in_use"}:
        return status
    if not process_alive(status.get("ssh_pid")):
        return {**status, "state": "reconnecting", "detail": "SSH process is not running"}
    healthy, detail = probe_worker(config.local_port, config.required_stages)
    return {**status, "state": "ready" if healthy else "worker_unavailable", "detail": detail,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def request_stop(config):
    directory = Path(config.runtime_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop.request").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return {"state": "stopping", "detail": "stop requested; the supervisor will close its own SSH child"}


class WindowsChildJob:
    """Closing the supervisor's job handle also closes its own SSH children."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, process._handle):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def make_logger(config):
    logger = logging.getLogger(f"managed_tunnel.{os.getpid()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        Path(config.runtime_dir).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(Path(config.runtime_dir) / "tunnel.log", maxBytes=512 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def log_ssh_output(stream, logger):
    try:
        while True:
            line = stream.readline(2048)
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).strip()
            if text:
                logger.info("ssh: %s", text)
    except (OSError, ValueError):
        pass
    finally:
        stream.close()


class Supervisor:
    def __init__(self, config, *, spawn=None, port_busy=None, probe=None,
                 monotonic=time.monotonic, backoff=None):
        self.config = config
        self.logger = make_logger(config)
        self.child = None
        self.job = None
        self.spawn = spawn or self._spawn
        self.port_busy = port_busy or (lambda: port_is_busy(config.local_port))
        self.probe = probe or (lambda: probe_worker(config.local_port, config.required_stages))
        self.monotonic = monotonic
        self.backoff = backoff or Backoff()
        self.spawned_at = 0.0
        self.retry_at = 0.0
        self.last_state = None
        self.closed = False

    def _spawn(self):
        if self.job is None:
            self.job = WindowsChildJob()
        process = subprocess.Popen(build_ssh_command(self.config), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            self.job.attach(process)
        except OSError:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            raise
        threading.Thread(target=log_ssh_output, args=(process.stdout, self.logger), daemon=True).start()
        return process

    def _status(self, state, detail):
        status = {"state": state, "detail": detail,
                  "updated_at": datetime.now(timezone.utc).isoformat(),
                  "supervisor_pid": os.getpid(), "ssh_pid": self.child.pid if self.child else None}
        atomic_status(self.config, status)
        if state != self.last_state:
            self.logger.info("%s: %s", state, detail)
            self.last_state = state
        return status

    def step(self):
        if self.closed or (Path(self.config.runtime_dir) / "stop.request").exists():
            return self.close()
        now = self.monotonic()
        if self.child is not None:
            code = self.child.poll()
            if code is None:
                healthy, detail = self.probe()
                return self._status("ready" if healthy else "worker_unavailable", detail)
            delay = self.backoff.after_exit(now - self.spawned_at)
            self.logger.warning("SSH child exited (%s); retry in %.1f seconds", code, delay)
            self.child = None
            self.retry_at = now + delay
        if now < self.retry_at:
            return self._status("reconnecting", "waiting before reconnecting SSH")
        if self.port_busy():
            return self._status("port_in_use", "local port is already occupied; no existing process was modified")
        try:
            self.child = self.spawn()
            self.spawned_at = now
            return self._status("connecting", "OpenSSH started; waiting for health check")
        except OSError:
            self.retry_at = now + self.backoff.after_exit(0)
            return self._status("reconnecting", "could not start a managed SSH child; retry scheduled")

    def close(self):
        if not self.closed:
            self.closed = True
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
            self.child = None
            if self.job:
                self.job.close()
        return self._status("stopped", "supervisor stopped and its SSH child was closed")


def run(config):
    lock = SingleInstanceLock(Path(config.runtime_dir) / "lock")
    if not lock.acquire():
        print(json.dumps({"state": "already_running", "detail": "another supervisor owns the runtime lock"}))
        return 0
    supervisor = None
    try:
        # Only the lock holder can remove an old stop request.
        (Path(config.runtime_dir) / "stop.request").unlink(missing_ok=True)
        supervisor = Supervisor(config)
        stopping = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        while not stopping.is_set():
            if supervisor.step()["state"] == "stopped":
                break
            stopping.wait(1)
        return 0
    finally:
        try:
            if supervisor is not None:
                supervisor.close()
        finally:
            lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
    except (OSError, ValueError, TypeError) as error:
        # Validation messages name fields only; no secrets or config contents.
        detail = str(error) if isinstance(error, ValueError) else "configuration could not be loaded"
        print(json.dumps({"state": "configuration_error", "detail": detail}))
        return 2
    if args.status or args.check or args.stop:
        status = request_stop(config) if args.stop else check_status(config) if args.check else read_status(config)
        print(json.dumps(status, ensure_ascii=False))
        return 0 if args.stop or status["state"] == "ready" else 2
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
