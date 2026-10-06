"""SSH Channel.send 允许短写；大于单个 SSH 包的上传必须发送完整。"""
import importlib.util
from pathlib import Path


def test_tunnel_preserves_upload_when_channel_only_accepts_partial_packets(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts/m2_worker_tunnel.py"
    spec = importlib.util.spec_from_file_location("tunnel_upload_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = bytes(range(256)) * 230

    class Socket:
        def __init__(self):
            self.chunks = [payload, b""]
        def recv(self, limit):
            return self.chunks.pop(0)
        def close(self):
            pass

    class Channel:
        received = b""
        def send(self, data):
            accepted = min(len(data), 32704)
            self.received += data[:accepted]
            return accepted
        def sendall(self, data):
            while data:
                data = data[self.send(data):]
        def close(self):
            pass

    socket, channel = Socket(), Channel()
    monkeypatch.setattr(module.select, "select", lambda *args: ([socket], [], []))
    handler = module._ForwardHandler.__new__(module._ForwardHandler)
    handler.request = socket
    handler._pump(channel)
    assert channel.received == payload
