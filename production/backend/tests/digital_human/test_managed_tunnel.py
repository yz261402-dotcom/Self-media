"""Managed tunnel safety and lifecycle tests; no remote/GPU requests."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "managed_tunnel.py"


def test_supervisor_entrypoint_exists():
    assert SCRIPT.is_file(), "managed OpenSSH supervisor is not implemented"


@pytest.fixture(scope="module")
def tunnel():
    assert SCRIPT.is_file(), "managed OpenSSH supervisor is not implemented"
    spec = importlib.util.spec_from_file_location("managed_tunnel_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def settings(tmp_path):
    root = Path(tmp_path)
    key = root / "id_tunnel"
    known = root / "known_hosts"
    key.write_text("test key", encoding="utf-8")
    known.write_text("test host key", encoding="utf-8")
    return {
        "ssh_executable": sys.executable,
        "ssh_host": "connect.weste.seetacloud.com",
        "ssh_port": 21170,
        "ssh_user": "root",
        "identity_file": str(key),
        "known_hosts_file": str(known),
        "local_port": 18080,
        "remote_port": 8080,
        "runtime_dir": str(root / "runtime"),
        "required_stages": ["align", "asr", "lipsync", "motion"],
    }


def test_ssh_uses_explicit_identity_strict_hosts_and_only_loopback(tunnel, settings):
    command = tunnel.build_ssh_command(tunnel.Config.from_dict(settings))
    assert command[0] == sys.executable
    assert command[command.index("-F") + 1] == "none"
    assert command[command.index("-L") + 1] == "127.0.0.1:18080:127.0.0.1:8080"
    assert command[command.index("-i") + 1] == settings["identity_file"]
    for flag in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                 "GlobalKnownHostsFile=none", "ExitOnForwardFailure=yes", "ForwardAgent=no",
                 "ServerAliveInterval=20", "ServerAliveCountMax=3", "ConnectTimeout=10",
                 "ConnectionAttempts=1"):
        assert flag in command
    assert command[-1] == "root@connect.weste.seetacloud.com"
    assert "-N" in command and "-T" in command


@pytest.mark.parametrize("field,value", [
    ("ssh_host", "-oProxyCommand=calc"), ("ssh_user", "root -oProxyCommand=calc"),
    ("ssh_host", "host\nHost *"), ("ssh_port", True), ("local_port", 0),
    ("remote_port", 65536), ("ssh_port", "22"), ("required_stages", "asr"),
    ("required_stages", ["asr"]), ("local_bind", "0.0.0.0"),
    ("identity_file", "relative-key"), ("known_hosts_file", "none"),
])
def test_config_rejects_option_injection_and_weakened_settings(tunnel, settings, field, value):
    settings[field] = value
    with pytest.raises(ValueError):
        tunnel.Config.from_dict(settings)


def test_singleton_lock_cannot_be_taken_until_previous_owner_releases(tunnel, tmp_path):
    path = Path(tmp_path) / "lock"
    first = tunnel.SingleInstanceLock(path)
    second = tunnel.SingleInstanceLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


@pytest.fixture()
def health_server():
    state = {"payload": {"state": "ok", "version": 2,
                         "runner_stages": ["align", "asr", "lipsync", "motion"]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/v1/healthz"
            body = state.get("body", json.dumps(state["payload"]).encode())
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_accepts_the_workers_string_protocol_version(tunnel, health_server):
    state, port = health_server
    state["payload"]["version"] = "2"
    assert tunnel.probe_worker(port, tunnel.STAGES)[0] is True


def test_health_probe_bypasses_proxies_and_verifies_protocol(tunnel, health_server, monkeypatch):
    state, port = health_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert tunnel.probe_worker(port, ["align", "asr", "lipsync", "motion"])[0]
    state["payload"]["version"] = 1
    assert not tunnel.probe_worker(port, ["asr"])[0]
    state["payload"]["version"] = 2
    state["payload"]["runner_stages"] = ["motion"]
    assert not tunnel.probe_worker(port, ["asr"])[0]
    state["body"] = b"<html>proxy error</html>"
    assert not tunnel.probe_worker(port, ["asr"])[0]
    state["body"] = b"x" * 70000
    assert not tunnel.probe_worker(port, ["asr"])[0]


class Child:
    pid = os.getpid()

    def __init__(self):
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None):
        return self.exit_code


def test_existing_port_is_not_adopted_or_killed(tunnel, settings):
    spawned = []
    supervisor = tunnel.Supervisor(tunnel.Config.from_dict(settings),
                                  spawn=lambda: spawned.append(Child()),
                                  port_busy=lambda: True, probe=lambda: (True, "healthy"))
    assert supervisor.step()["state"] == "port_in_use"
    assert spawned == []
    supervisor.close()


def test_worker_failure_preserves_existing_ssh_child(tunnel, settings):
    child = Child()
    health = [False]
    calls = []

    def spawn():
        calls.append(1)
        return child

    supervisor = tunnel.Supervisor(tunnel.Config.from_dict(settings), spawn=spawn,
                                  port_busy=lambda: False,
                                  probe=lambda: (health[0], "worker unavailable"))
    assert supervisor.step()["state"] == "connecting"
    assert supervisor.step()["state"] == "worker_unavailable"
    assert not child.terminated and len(calls) == 1
    health[0] = True
    assert supervisor.step()["state"] == "ready"
    supervisor.close()
    assert child.terminated


def test_exponential_backoff_resets_only_after_stable_ssh(tunnel):
    backoff = tunnel.Backoff(jitter=lambda: 0.0)
    assert [backoff.after_exit(1) for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff.after_exit(60) == 1
    assert backoff.after_exit(1) == 2


def test_ssh_exit_waits_before_spawn_and_recovers(tunnel, settings):
    children = [Child(), Child()]
    now = [0.0]
    calls = []

    def spawn():
        child = children[len(calls)]
        calls.append(child)
        return child

    supervisor = tunnel.Supervisor(tunnel.Config.from_dict(settings), spawn=spawn,
                                  port_busy=lambda: False, probe=lambda: (True, "ready"),
                                  monotonic=lambda: now[0], backoff=tunnel.Backoff(jitter=lambda: 0))
    supervisor.step()
    children[0].exit_code = 255
    assert supervisor.step()["state"] == "reconnecting"
    now[0] = 0.5
    supervisor.step()
    assert len(calls) == 1
    now[0] = 1.1
    supervisor.step()
    assert len(calls) == 2
    assert supervisor.step()["state"] == "ready"
    supervisor.close()


def test_status_never_reports_stale_or_dead_process_as_ready(tunnel, settings):
    config = tunnel.Config.from_dict(settings)
    supervisor = tunnel.Supervisor(config, spawn=Child, port_busy=lambda: False,
                                  probe=lambda: (True, "ready"))
    supervisor.step()
    supervisor.step()
    assert tunnel.read_status(config)["state"] == "ready"
    status_file = Path(config.runtime_dir) / "status.json"
    status = json.loads(status_file.read_text(encoding="utf-8"))
    status["updated_at"] = "2000-01-01T00:00:00+00:00"
    status_file.write_text(json.dumps(status), encoding="utf-8")
    assert tunnel.read_status(config)["state"] == "stopped"
    status["supervisor_pid"] = 2147483647
    status_file.write_text(json.dumps(status), encoding="utf-8")
    assert tunnel.read_status(config)["state"] == "stopped"
    supervisor.close()


def test_stop_request_is_acknowledged_and_closes_owned_child(tunnel, settings):
    config = tunnel.Config.from_dict(settings)
    child = Child()
    supervisor = tunnel.Supervisor(config, spawn=lambda: child, port_busy=lambda: False,
                                  probe=lambda: (True, "ready"))
    supervisor.step()
    assert tunnel.request_stop(config)["state"] == "stopping"
    assert supervisor.step()["state"] == "stopped"
    assert child.terminated


def test_check_uses_fresh_probe_not_cached_ready(tunnel, settings, monkeypatch):
    config = tunnel.Config.from_dict(settings)
    supervisor = tunnel.Supervisor(config, spawn=Child, port_busy=lambda: False,
                                  probe=lambda: (True, "ready"))
    supervisor.step()
    supervisor.step()
    monkeypatch.setattr(tunnel, "probe_worker", lambda *_: (False, "refused"))
    assert tunnel.check_status(config)["state"] == "worker_unavailable"
    supervisor.close()


def test_slow_health_response_cannot_extend_total_deadline(tunnel):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    finished = threading.Event()

    def slow_response():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 100\r\n\r\n")
                while not finished.wait(0.04):
                    connection.sendall(b" ")
        except OSError:
            pass

    thread = threading.Thread(target=slow_response, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        assert not tunnel.probe_worker(port, ["asr"], timeout=0.2)[0]
        assert time.monotonic() - started < 1.0
    finally:
        finished.set()
        listener.close()
        thread.join(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime contract")
def test_windows_job_close_terminates_its_child(tunnel):
    job = tunnel.WindowsChildJob()
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        job.attach(process)
        assert process.poll() is None
        job.close()
        assert process.wait(timeout=5) is not None
    finally:
        job.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_cli_uses_structured_output_and_stopped_status_exit_code(tunnel, settings, tmp_path):
    config_file = Path(tmp_path) / "config.json"
    config_file.write_text(json.dumps(settings), encoding="utf-8-sig")
    status = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file), "--status"],
                            capture_output=True, text=True, timeout=10)
    assert status.returncode == 2
    assert json.loads(status.stdout)["state"] == "stopped"
    assert status.stderr == ""
    stop = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file), "--stop"],
                          capture_output=True, text=True, timeout=10)
    assert stop.returncode == 0
    assert json.loads(stop.stdout)["state"] == "stopping"


def test_installed_openssh_accepts_pinned_known_hosts_path_with_spaces(tunnel, settings, tmp_path):
    ssh = shutil.which("ssh")
    if not ssh:
        pytest.skip("OpenSSH is not installed")
    known = Path(tmp_path) / "known hosts file"
    known.write_text("", encoding="utf-8")
    settings["ssh_executable"] = ssh
    settings["known_hosts_file"] = str(known)
    command = tunnel.build_ssh_command(tunnel.Config.from_dict(settings))
    command.insert(1, "-G")  # Print config only; never connect to the remote host.
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert "stricthostkeychecking true" in result.stdout
    assert f"userknownhostsfile {known.as_posix()}" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows process lifetime contract")
def test_windows_forced_supervisor_exit_does_not_orphan_ssh(tunnel):
    code = (
        "import importlib.util,sys,subprocess,time; "
        "s=importlib.util.spec_from_file_location('managed',sys.argv[1]); "
        "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
        "j=m.WindowsChildJob(); "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "creationflags=subprocess.CREATE_NO_WINDOW); "
        "j.attach(p); print(p.pid,flush=True); time.sleep(30)"
    )
    manager = subprocess.Popen([sys.executable, "-c", code, str(SCRIPT)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    child_pid = None
    try:
        child_pid = int(manager.stdout.readline().strip())
        assert tunnel.process_alive(child_pid)
        manager.terminate()
        manager.wait(timeout=5)
        deadline = time.monotonic() + 5
        while tunnel.process_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not tunnel.process_alive(child_pid)
    finally:
        if manager.poll() is None:
            manager.terminate()
            manager.wait(timeout=5)
        manager.stdout.close()
        manager.stderr.close()
        if child_pid and tunnel.process_alive(child_pid):
            os.kill(child_pid, signal.SIGTERM)
