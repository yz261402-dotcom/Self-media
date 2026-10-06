import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "setup_tunnel.py"


def load_module():
    assert SCRIPT.exists(), "长期隧道安装程序尚未实现"
    spec = importlib.util.spec_from_file_location("setup_tunnel_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unknown_host_is_refused_without_trusting_network(tmp_path):
    module = load_module()
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
    with pytest.raises(ValueError, match="known_hosts"):
        module.trusted_host_lines("ssh-keygen", "example.com", 2222, tmp_path / "known_hosts", run=run)


def test_only_previously_trusted_host_records_are_pinned(tmp_path):
    module = load_module()
    record = "[example.com]:2222 ssh-ed25519 AAAATEST"
    def run(args, **kwargs):
        assert args[1:3] == ["-F", "[example.com]:2222"]
        return subprocess.CompletedProcess(args, 0, stdout="# Host found\n" + record + "\n", stderr="")
    assert module.trusted_host_lines("ssh-keygen", "example.com", 2222, tmp_path / "known_hosts", run=run) == record + "\n"


@pytest.mark.parametrize("host,user,port", [("-oProxyCommand=bad", "root", 22), ("x;bad", "root", 22), ("example.com", "-root", 22), ("example.com", "root", 0), ("example.com", "root", 65536)])
def test_rejects_unsafe_connection_settings(host, user, port):
    module = load_module()
    with pytest.raises(ValueError):
        module.validate_target(host, user, port)


def test_bootstrap_disables_shell_and_does_not_overwrite_authorized_keys():
    module = load_module()
    script = module.install_command("ssh-ed25519 AAAATEST avatar-studio-tunnel", 8080)
    assert 'command="/bin/false"' in script
    assert 'restrict,port-forwarding,permitopen="127.0.0.1:8080"' in script
    assert 'grep -Fqx' in script
    assert '>> "$auth_file"' in script
    assert 'chmod 600' in script
    assert "pkill" not in script


def test_public_key_rejects_shell_injection():
    module = load_module()
    with pytest.raises(ValueError):
        module.install_command('ssh-ed25519 AAAA\n$(bad)', 8080)


def test_bootstrap_uses_strict_host_checking_and_no_saved_password():
    module = load_module()
    cfg = dict(ssh_executable="ssh", ssh_host="example.com", ssh_user="root", ssh_port=22, known_hosts_file="C:/known hosts")
    args = module.bootstrap_args(cfg)
    assert "StrictHostKeyChecking=yes" in args
    assert "PreferredAuthentications=password,keyboard-interactive" in args
    assert 'UserKnownHostsFile="C:/known hosts"' in args
    assert "BatchMode=yes" not in args
    assert "-F" in args
    assert "password" not in json.dumps(cfg)


def test_failed_install_keeps_diagnostic_receipt_without_success_marker(tmp_path, monkeypatch):
    module = load_module()
    config_path = tmp_path / "config.json"
    key_path = tmp_path / "id_ed25519"
    key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST avatar-studio-tunnel")
    cfg = dict(ssh_executable="ssh", ssh_host="example.com", ssh_user="root", ssh_port=22,
               known_hosts_file=str(tmp_path / "known_hosts"), identity_file=str(key_path), remote_port=8080)
    monkeypatch.setattr(module, "CONFIG", config_path)
    monkeypatch.setattr(module, "prepare", lambda *args: cfg)
    def run(args, **kwargs):
        assert "-E" in args
        assert "LogLevel=VERBOSE" in args
        return subprocess.CompletedProcess(args, 255)
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main(["--install-key"]) == 255
    receipt = json.loads((tmp_path / "key-install-result.json").read_text(encoding="utf-8"))
    assert receipt["exit_code"] == 255
    assert receipt["installed"] is False
    assert not (tmp_path / "key-installed.json").exists()
