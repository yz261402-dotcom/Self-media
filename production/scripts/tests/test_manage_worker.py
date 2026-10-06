import importlib.util
import json
from pathlib import Path
import pytest


SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    path = SCRIPTS / (name + ".py")
    assert path.exists(), "Worker management has not been implemented"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_token_loader_matches_backend_and_honors_environment(tmp_path):
    module = load("manage_worker")
    env_file = tmp_path / ".env"
    env_file.write_text('AVATAR_STUDIO_M2_WORKER_TOKEN="from-file"\nAVATAR_STUDIO_ASR_WORKER_TOKEN=from-asr\n')
    assert module.worker_tokens(env_file, {}) == {"m2": "from-file", "asr": "from-asr"}
    assert module.worker_tokens(env_file, {"AVATAR_STUDIO_M2_WORKER_TOKEN": "from-env"})["m2"] == "from-env"


def test_control_command_is_noninteractive_and_cannot_run_shell():
    module = load("manage_worker")
    cfg = dict(ssh_executable="ssh", ssh_host="example.com", ssh_port=22, ssh_user="root",
               identity_file="C:/keys/worker", known_hosts_file="C:/known hosts")
    args = module.control_args(cfg, "ensure")
    assert "BatchMode=yes" in args
    assert "IdentitiesOnly=yes" in args
    assert "ClearAllForwardings=yes" in args
    assert "StrictHostKeyChecking=yes" in args
    assert args[-1] == "ensure"
    with pytest.raises(ValueError):
        module.control_args(cfg, "ensure; rm -rf /")


def test_install_uses_stdin_for_token_not_command_line():
    module = load("manage_worker")
    bundle = {"worker_token": "test-private-token", "public_key": "ssh-ed25519 AAAATEST avatar-studio-worker-control", "service_py": "# test", "launcher_sh": "#!/bin/bash\n"}
    cfg = dict(ssh_executable="ssh", ssh_host="example.com", ssh_port=22, ssh_user="root", known_hosts_file="C:/known hosts")
    args, payload = module.install_request(cfg, bundle, "print('installer')")
    assert "test-private-token" not in json.dumps(args)
    assert json.loads(payload)["worker_token"] == "test-private-token"
    assert "StrictHostKeyChecking=yes" in args


def test_installer_refuses_incomplete_bundle_without_echoing_secrets():
    module = load("worker_service_install")
    with pytest.raises(ValueError) as error:
        module.validate_bundle({"worker_token": "must-not-appear"})
    assert "must-not-appear" not in str(error.value)


def test_missing_dependency_prevents_all_remote_changes(tmp_path):
    module = load("worker_service_install")
    bundle = {"worker_token": "test-private-token", "public_key": "ssh-ed25519 AAAATEST avatar-studio-worker-control", "service_py": "# test", "launcher_sh": "#!/bin/bash\n"}
    root, home = tmp_path / "worker", tmp_path / "user"
    with pytest.raises(ValueError, match="dependency"):
        module.install(bundle, root=root, user_home=home, required_files=[tmp_path / "absent"])
    assert not root.exists()
    assert not home.exists()


def test_private_file_writer_replaces_content_without_partial_json(tmp_path):
    module = load("worker_service_install")
    path = tmp_path / "env.json"
    module.write_private(path, '{"M2_WORKER_TOKEN":"first"}', 0o600)
    module.write_private(path, '{"M2_WORKER_TOKEN":"second"}', 0o600)
    assert json.loads(path.read_text())["M2_WORKER_TOKEN"] == "second"
    assert list(tmp_path.iterdir()) == [path]


def test_install_cannot_claim_ready_after_unsuccessful_ssh_exit():
    module = load("manage_worker")
    result = module.install_response(255, b'{"state":"ready","installed":true}', "token")
    assert result["state"] != "ready"
    assert result["installed"] is False


def test_install_diagnostics_redact_tokens_before_json_escaping():
    module = load("manage_worker")
    secret = 'tok"en\\private'
    body = json.dumps({"state": "blocked", "installed": True, "detail": secret, "nested": [secret]}).encode()
    result = module.install_response(0, body, secret)
    assert result["detail"] == "[redacted]"
    assert result["nested"] == ["[redacted]"]


def test_auth_probe_refuses_to_read_or_send_token_without_managed_tunnel(monkeypatch):
    module = load("manage_worker")
    def unavailable():
        raise ValueError("No trusted tunnel")
    monkeypatch.setattr(module, "verified_local_port", unavailable)
    monkeypatch.setattr(module, "worker_tokens", lambda *args: pytest.fail("read token before checking tunnel"))
    with pytest.raises(ValueError, match="trusted tunnel"):
        module.verify_auth()
