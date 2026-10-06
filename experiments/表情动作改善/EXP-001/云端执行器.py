"""本轮 SSH 操作桥；密码仅由隐藏终端输入，不写文件或日志。"""
import getpass
import json
from pathlib import Path
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
import paramiko

client = paramiko.SSHClient()
client.load_system_host_keys()
known = Path.home() / ".ssh" / "known_hosts"
if known.is_file():
    client.load_host_keys(str(known))
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    secret = getpass.getpass("SSH password (not stored): ")
    client.connect("connect.westc.seetacloud.com", port=35884, username="root",
                   password=secret, timeout=20, auth_timeout=20, banner_timeout=20,
                   look_for_keys=False, allow_agent=False)
    client.get_transport().set_keepalive(20)
    print(json.dumps({"connected": True, "host_key_fingerprint": client.get_transport().get_remote_server_key().get_fingerprint().hex()}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            action = request["action"]
            if action == "close":
                break
            # 只在新请求发送之前重连，不自动重放已发送或结果不明的命令。
            if client.get_transport() is None or not client.get_transport().is_active():
                client.connect("connect.westc.seetacloud.com", port=35884, username="root",
                               password=secret, timeout=20, auth_timeout=20, banner_timeout=20,
                               look_for_keys=False, allow_agent=False)
                client.get_transport().set_keepalive(20)
            if action == "exec":
                _, stdout, stderr = client.exec_command(request["command"], timeout=request.get("timeout", 45))
                result = {"stdout": stdout.read().decode("utf-8", "replace"),
                          "stderr": stderr.read().decode("utf-8", "replace"),
                          "exit_code": stdout.channel.recv_exit_status()}
            elif action in ("put", "get"):
                with client.open_sftp() as sftp:
                    if action == "put":
                        sftp.put(request["local"], request["remote"])
                    else:
                        destination = Path(request["local"])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(request["remote"], str(destination))
                result = {"transferred": action, "local": request["local"], "remote": request["remote"]}
            else:
                raise ValueError("Unknown action")
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({"error_type": type(error).__name__, "error": str(error)}, ensure_ascii=False), flush=True)
finally:
    secret = None
    client.close()
