import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "workers/m2_gpu/worker_service.py"
TOKEN = "private-worker-token-for-tests"


def load_module():
    assert SCRIPT.exists(), "Worker service recovery has not been implemented"
    spec = importlib.util.spec_from_file_location("worker_service_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_module()


@pytest.fixture
def service(module, tmp_path, monkeypatch):
    root = tmp_path / "worker"
    managed = root / "service"
    managed.mkdir(parents=True)
    (managed / "env.json").write_text(json.dumps({"M2_WORKER_TOKEN": TOKEN}), encoding="utf-8")
    (managed / "env.json").chmod(0o600)
    (managed / "start-all-stages.sh").write_text("exit 0\n")
    monkeypatch.setattr(module, "probe_health", lambda: None)
    monkeypatch.setattr(module, "port_in_use", lambda: False)
    monkeypatch.setattr(module, "scan_project_processes", lambda root: [])
    monkeypatch.setattr(module, "guard_is_running", lambda root: False)
    return module.Service(root)


def ready():
    return {"state": "ok", "version": "2", "runner_stages": ["asr", "align", "motion", "lipsync"]}


def test_health_requires_all_stages_and_protocol_v2(module):
    assert module.health_ready(ready())
    for health in (None, {}, {**ready(), "version": "1"}, {**ready(), "version": True},
                   {**ready(), "runner_stages": ["asr"]}, {**ready(), "state": "error"}):
        assert not module.health_ready(health)


def test_health_disables_proxy_and_bounds_reads_and_timeout(module, monkeypatch):
    seen = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count):
            seen["read_limit"] = count
            return json.dumps(ready()).encode()
    class Opener:
        def open(self, url, timeout):
            seen.update(url=url, timeout=timeout)
            return Response()
    def opener(*handlers):
        seen["proxies"] = handlers[0].proxies
        return Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", opener)
    assert module.health_ready(module.probe_health())
    assert seen == {"proxies": {}, "url": "http://127.0.0.1:8080/v1/healthz", "timeout": 3,
                    "read_limit": 65537}


def test_existing_healthy_worker_is_reused_without_spawning(service, module, monkeypatch):
    monkeypatch.setattr(module, "probe_health", ready)
    monkeypatch.setattr(service, "spawn_worker", lambda env: pytest.fail("must reuse existing worker"))
    assert service.guard_step(0)["state"] == "ready"


@pytest.mark.parametrize("kind", ["listener", "runner", "old_worker"])
def test_uncertain_existing_activity_blocks_new_worker(service, module, monkeypatch, kind):
    if kind == "listener":
        monkeypatch.setattr(module, "port_in_use", lambda: True)
    else:
        monkeypatch.setattr(module, "scan_project_processes", lambda root: [{"pid": 42, "kind": kind}])
    monkeypatch.setattr(service, "spawn_worker", lambda env: pytest.fail("must not duplicate existing activity"))
    assert service.guard_step(0)["state"] == "blocked"


def test_health_pending_zero_does_not_authorize_restart(service, module, monkeypatch):
    monkeypatch.setattr(module, "probe_health", lambda: {**ready(), "runner_stages": ["asr"], "pending": 0})
    monkeypatch.setattr(module, "port_in_use", lambda: True)
    monkeypatch.setattr(service, "spawn_worker", lambda env: pytest.fail("pending is not safe-to-restart evidence"))
    assert service.guard_step(0)["state"] == "blocked"


def test_new_worker_gets_token_only_in_environment(service, module, monkeypatch):
    calls = []
    pipe = io.BytesIO()
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(pid=321 + len(calls), stdin=pipe, poll=lambda: None)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    assert service.guard_step(0)["state"] == "starting"
    assert len(calls) == 2
    logger, worker = calls
    assert logger[0][-1] == "_log"
    assert "M2_WORKER_TOKEN" not in logger[1]["env"]
    assert worker[0] == ["/bin/bash", str(service.managed / "start-all-stages.sh")]
    assert worker[1]["env"]["M2_WORKER_TOKEN"] == TOKEN
    assert worker[1]["stdin"] == subprocess.DEVNULL
    assert worker[1]["start_new_session"] and logger[1]["start_new_session"]
    assert TOKEN not in str([command for command, _ in calls])
    assert pipe.closed


def test_unhealthy_live_child_is_never_killed_or_replaced(service, monkeypatch):
    service.child = SimpleNamespace(pid=100, poll=lambda: None)
    service.started_at = 0
    monkeypatch.setattr(service, "spawn_worker", lambda env: pytest.fail("must leave active process alone"))
    assert service.guard_step(120)["state"] == "worker_unavailable"
    assert service.child.pid == 100


def test_exit_backoff_and_orphan_runner_defer_restart(service, module, monkeypatch):
    starts = []
    child = SimpleNamespace(pid=100, poll=lambda: None)
    monkeypatch.setattr(service, "spawn_worker", lambda env: starts.append(True) or child)
    assert service.guard_step(0)["state"] == "starting"
    service.child = SimpleNamespace(pid=100, poll=lambda: 1)
    assert service.guard_step(1)["state"] == "worker_unavailable"
    service.guard_step(2)
    assert len(starts) == 1
    monkeypatch.setattr(module, "scan_project_processes", lambda root: [{"pid": 101, "kind": "runner"}])
    assert service.guard_step(5)["state"] == "blocked"
    assert len(starts) == 1
    monkeypatch.setattr(module, "scan_project_processes", lambda root: [])
    assert service.guard_step(6)["state"] == "starting"
    assert len(starts) == 2


def test_configuration_error_does_not_expose_secret(service):
    (service.managed / "env.json").write_text('{"M2_WORKER_TOKEN": "' + TOKEN)
    result = service.guard_step(0)
    assert result["state"] == "configuration_error"
    assert TOKEN not in json.dumps(result)


def test_environment_file_must_only_contain_worker_token(service, module):
    (service.managed / "env.json").write_text(json.dumps({"M2_WORKER_TOKEN": TOKEN, "BASH_ENV": "evil"}))
    with pytest.raises(module.ConfigurationError):
        module.load_worker_environment(service.managed / "env.json")


class FakeLock:
    def __init__(self, path, acquired=True): self.acquired = acquired
    def __enter__(self): return self
    def __exit__(self, *args): pass


def test_ensure_reuses_guard_and_healthy_worker(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", FakeLock)
    monkeypatch.setattr(module, "guard_is_running", lambda root: True)
    monkeypatch.setattr(module, "probe_health", ready)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("guard exists"))
    assert service.ensure()["state"] == "ready"


def test_ensure_launches_guard_detached_without_token(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", FakeLock)
    monkeypatch.setattr(module, "probe_health", ready)
    monkeypatch.setenv("M2_WORKER_TOKEN", TOKEN)
    checks = iter((False, True))
    monkeypatch.setattr(module, "guard_is_running", lambda root: next(checks))
    calls = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert service.ensure()["state"] == "ready"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0][-1] == "guard"
    assert kwargs["start_new_session"] and kwargs["close_fds"]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "M2_WORKER_TOKEN" not in kwargs["env"]
    assert TOKEN not in str(args)


def test_ensure_does_not_claim_guard_installed_when_detached_process_exited(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", FakeLock)
    monkeypatch.setattr(module, "probe_health", ready)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: None)
    result = service.ensure(wait_seconds=0)
    assert result["state"] == "configuration_error"
    assert result["worker_ready"] is True


def test_concurrent_ensure_does_not_launch_second_guard(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", lambda path: FakeLock(path, acquired=False))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("concurrent launch"))
    assert service.ensure()["state"] == "starting"


def test_ensure_waits_for_starting_process_instead_of_reporting_conflict(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", FakeLock)
    monkeypatch.setattr(module, "guard_is_running", lambda root: True)
    snapshots = iter([
        {"state": "blocked", "detail": "project process appeared", "guard_running": True},
        {"state": "ready", "detail": "ready", "guard_running": True},
    ])
    monkeypatch.setattr(service, "status", lambda: next(snapshots))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert service.ensure()["state"] == "ready"


def test_duplicate_guard_cannot_enter_loop(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", lambda path: FakeLock(path, acquired=False))
    monkeypatch.setattr(service, "guard_step", lambda now: pytest.fail("duplicate guard loop"))
    service.guard()


def test_disk_full_status_write_does_not_end_guard(service, module, monkeypatch):
    monkeypatch.setattr(module, "FileLock", FakeLock)
    class StopLoop(Exception): pass
    def publish(value): raise OSError("disk full")
    def sleep(seconds): raise StopLoop()
    monkeypatch.setattr(service, "publish", publish)
    monkeypatch.setattr(service, "guard_step", lambda now: {"state": "ready", "detail": "ready"})
    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        service.guard()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and flock are tested on the Linux Worker")
def test_insecure_permissions_rejected_and_lock_is_exclusive(service, module):
    path = service.managed / "env.json"
    path.chmod(0o644)
    with pytest.raises(module.ConfigurationError):
        module.load_worker_environment(path)
    path.chmod(0o600)
    with module.FileLock(service.managed / "test.lock") as first:
        assert first.acquired
        with module.FileLock(service.managed / "test.lock") as second:
            assert not second.acquired


def test_scan_only_project_executables_and_never_returns_commandline(module, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    root = Path("/root/autodl-tmp/avatar-m2worker")
    commands = {
        11: ["python", str(root / "worker_server.py"), "--token", TOKEN],
        12: ["python", str(root / "runner_qwen_asr.py"), "job.json"],
        13: ["python", "/different/worker_server.py"],
        14: ["bash", "-c", "cat " + str(root / "worker_server.py")],
        15: ["python", str(root / "service/worker_service.py"), "guard"],
    }
    for pid, command in commands.items():
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(item.encode() for item in command))
    found = module.scan_project_processes(root, proc_root=proc)
    assert sorted(item["pid"] for item in found) == [11, 12]
    assert TOKEN not in json.dumps(found)


def test_orphan_inference_children_are_detected_by_project_path_or_cwd(module, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(module, "INFERENCE_ROOTS", (root,))
    commands = {
        11: ["python", str(root / "qwen_inference.py")],
        12: ["python", "-m", "scripts.inference_mt001"],
        13: ["python", "inference.py"],
        14: ["python", "/different/qwen_inference.py"],
    }
    for pid, command in commands.items():
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(item.encode() for item in command))
        if pid in (12, 13):
            # /proc/PID/cwd is a symlink on Linux; use a directory plus a resolver
            # boundary on Windows, where ordinary users cannot create symlinks.
            (folder / "cwd").mkdir()
    original = module.process_cwd
    monkeypatch.setattr(module, "process_cwd", lambda folder: root if (folder / "cwd").exists() else original(folder))
    assert sorted(item["pid"] for item in module.scan_project_processes(root, proc)) == [11, 12, 13]


def test_relative_worker_and_runner_under_project_are_conflicts(module, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    root = tmp_path / "worker"
    commands = {11: ["python", "worker_server.py"], 12: ["python", "../runner_qwen_asr.py"],
                13: ["python", "worker_server.py"]}
    for pid, command in commands.items():
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(item.encode() for item in command))
    monkeypatch.setattr(module, "process_cwd", lambda folder: root / "jobs" if folder.name != "13" else tmp_path / "other")
    assert sorted(item["pid"] for item in module.scan_project_processes(root, proc)) == [11, 12]


def test_cuda_library_search_path_is_preserved(module, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/cuda/lib64")
    assert module.clean_environment()["LD_LIBRARY_PATH"] == "/usr/local/cuda/lib64"


@pytest.mark.parametrize("original", ["", "ensure; id", " ensure", "ensure ", "guard", "_log", "status\n", "bash", "status --root /tmp"])
def test_forced_key_rejects_every_non_allowlisted_command(module, monkeypatch, capsys, original):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", original)
    assert module.main(["forced"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "blocked"


@pytest.mark.parametrize("original", ["ensure", "status"])
def test_forced_key_dispatches_only_allowed_commands(module, monkeypatch, capsys, original):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", original)
    monkeypatch.setattr(module.Service, original, lambda self: {"state": "ready", "detail": "test"})
    assert module.main(["forced"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "ready"


def test_status_removes_untrusted_health_fields(service, module, monkeypatch):
    monkeypatch.setattr(module, "probe_health", lambda: {**ready(), "runner": TOKEN, "gpu_probed": TOKEN})
    result = service.status()
    assert result["state"] == "ready"
    assert TOKEN not in json.dumps(result)


def test_log_capture_redacts_token_split_across_read_chunks(module, tmp_path):
    target = tmp_path / "worker.log"
    chunks = [b"start " + TOKEN[:12].encode(), TOKEN[12:].encode() + b" end\n", b""]
    stream = SimpleNamespace(read=lambda size: chunks.pop(0))
    module.capture_log(stream, target, TOKEN, max_bytes=512, backups=3)
    text = target.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "[REDACTED]" in text


def test_log_capture_bounds_unterminated_lines_and_rotates(module, tmp_path):
    target = tmp_path / "worker.log"
    stream = io.BytesIO(("x" * 500 + "\n") .encode() * 200)
    module.capture_log(stream, target, TOKEN, max_bytes=1024, backups=3)
    files = list(tmp_path.iterdir())
    assert len(files) <= 4
    assert all(path.stat().st_size <= 1200 for path in files)


def test_log_capture_redacts_tokens_on_long_unterminated_line(module, tmp_path):
    target = tmp_path / "worker.log"
    content = ("x" * 251 + TOKEN + "y" * 251 + TOKEN) * 20
    module.capture_log(io.BytesIO(content.encode()), target, TOKEN, max_bytes=1024, backups=100)
    output = "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert TOKEN not in output
    assert output.count("[REDACTED]") == 40
