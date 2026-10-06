"""分段音频必须真的是"每段一段音频"，而不是把整轨重复发给每一段。

为什么单独立一组用例
--------------------
OmniHuman 的 `audio_url` 是**整段音频**，官方契约写明时长必须小于 60 秒；
超长时"提交任务正常，**查询任务**会报 `code 50215 Input invalid for this service`"。
只按分段算钱、却把整轨发出去，症状是"提交成功、查询永远失败"——
看起来像云端故障，实际是我们发错了音频（2026-09-28 真实踩到）。

这里用**真实 ffmpeg** 跑切片，并断言切片与源音轨的对应窗口**逐字节一致**：
只要有人为了"让时长塞进上限"去变速或重采样，字节比对立刻失败。
"""
from __future__ import annotations

import hashlib
import struct
import wave
from pathlib import Path

import pytest

from app.digital_human import omni_media

SR = 32000


def _ffmpeg_available() -> bool:
    try:
        from app.video import ffmpeg as vff

        vff.ffmpeg_bin()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_available(), reason="本机没有 ffmpeg，切片无法验证")


def _write_wav(path: Path, seconds: float) -> bytes:
    """写一条确定性 PCM 音轨（锯齿样值），便于逐字节比对。"""
    frames = int(SR * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes(b"".join(
            struct.pack("<h", (i * 37) % 30011 - 15000) for i in range(frames)))
    return path.read_bytes()


def _pcm(path: Path):
    with wave.open(str(path), "rb") as handle:
        return handle.getframerate(), handle.getnchannels(), handle.readframes(handle.getnframes())


class _Publisher:
    """只记录对象的替身：保留"内容寻址 + 匿名读回校验"这两条关键语义。"""

    def __init__(self):
        self.objects = {}
        self.uploads = []

    def exists(self, key):
        return key in self.objects

    def ensure_bucket(self):
        pass

    def publish_file(self, path, key=None, content_type=None):
        self.objects[key] = Path(path).read_bytes()
        self.uploads.append((key, content_type))
        return self.url_for(key)

    def url_for(self, key):
        return "https://bucket.example.com/" + key

    def verify_public_file(self, url, expected_hash):
        data = self.objects[url.split(".com/", 1)[1]]
        if "sha256:" + hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("远端字节与本地不一致")


@pytest.fixture()
def track(tmp_path):
    """6 秒真实音轨（PCM s16le / 32kHz / 单声道），与项目里最终配音同格式。"""
    path = Path(tmp_path) / "final.wav"
    _write_wav(path, 6.0)
    return path


def test_slice_is_sample_exact_and_never_resamples(track):
    """切片 = 源音轨对应窗口的原样字节：不加速、不重采样、不截字。"""
    _, _, source = _pcm(track)
    for start_ms, end_ms in ((0, 1500), (1000, 2000), (2500, 4000), (4200, 6000)):
        dest = track.parent / f"cut_{start_ms}.wav"
        omni_media._slice_audio(track, start_ms, end_ms, dest)
        rate, channels, data = _pcm(dest)
        assert rate == SR and channels == 1, "采样率/声道数必须原样保留"
        assert len(data) // 2 == (end_ms - start_ms) * (SR // 1000), "时长必须精确到采样"
        assert data == source[start_ms * 2 * (SR // 1000):end_ms * 2 * (SR // 1000)], \
            "切片必须与源窗口逐字节一致：任何变速/重采样都会破坏它"


def test_each_segment_is_published_as_its_own_clip(track, monkeypatch):
    """每段发布一份自己的音频；对象按内容寻址，因此重跑天然复用。"""
    publisher = _Publisher()
    monkeypatch.setattr(omni_media, "_publisher", lambda ak, sk: publisher)

    segments = [
        {"segment_id": "oh_seg_1", "index": 0, "start_ms": 0, "end_ms": 1500,
         "duration_ms": 1500, "global_offset_ms": 0},
        {"segment_id": "oh_seg_2", "index": 1, "start_ms": 1500, "end_ms": 3000,
         "duration_ms": 1500, "global_offset_ms": 1500},
    ]
    audio = {"duration_ms": 6000}
    urls = omni_media.prepare_segment_audio(
        audio, segments, audio_url="https://cdn.example.com/full.wav",
        audio_path=track, ak="ak", sk="sk")

    assert set(urls) == {"oh_seg_1", "oh_seg_2"}
    assert urls["oh_seg_1"] != urls["oh_seg_2"], "两段必须是两份不同的音频"
    assert "https://cdn.example.com/full.wav" not in urls.values(), \
        "整轨音频超过 60 秒上限，绝不能作为分段音频发出"
    assert len(publisher.uploads) == 2
    for key, content_type in publisher.uploads:
        assert key.startswith("omnihuman/audio_seg/") and key.endswith(".wav")
        assert content_type == "audio/wav"

    again = omni_media.prepare_segment_audio(
        audio, segments, audio_url="https://cdn.example.com/full.wav",
        audio_path=track, ak="ak", sk="sk")
    assert again == urls
    assert len(publisher.uploads) == 2, "内容相同就不该重复上传"


def test_single_call_keeps_the_full_track_without_reencoding(track, monkeypatch):
    """整轨就是这一段（未超限的单次调用）：直接用整轨，不做无谓重编码。"""
    publisher = _Publisher()
    monkeypatch.setattr(omni_media, "_publisher", lambda ak, sk: publisher)

    segments = [{"segment_id": "oh_seg_1", "index": 0, "start_ms": 0, "end_ms": 6000,
                 "duration_ms": 6000, "global_offset_ms": 0, "single_call": True}]
    urls = omni_media.prepare_segment_audio(
        {"duration_ms": 6000}, segments, audio_url="https://cdn.example.com/full.wav",
        audio_path=track, ak="ak", sk="sk")
    assert urls == {"oh_seg_1": "https://cdn.example.com/full.wav"}
    assert publisher.uploads == [], "整轨即该段时不应再切一份"


def test_remote_only_audio_is_blocked_instead_of_falling_back_to_full_track(monkeypatch):
    """只有公网地址、没有本地文件时宁可阻断：整轨兜底会把参数错误伪装成云端故障。"""
    monkeypatch.setattr(omni_media, "_publisher", lambda ak, sk: _Publisher())
    segments = [{"segment_id": "oh_seg_2", "index": 1, "start_ms": 1500, "end_ms": 3000,
                 "duration_ms": 1500, "global_offset_ms": 1500}]
    with pytest.raises(Exception) as error:
        omni_media.prepare_segment_audio(
            {"duration_ms": 6000}, segments, audio_url="https://cdn.example.com/full.wav",
            audio_path=None, ak="ak", sk="sk")
    assert getattr(error.value, "code", "") == "OMNIHUMAN_SEGMENT_AUDIO_UNSLICEABLE"


def test_missing_segment_id_is_rejected(monkeypatch):
    monkeypatch.setattr(omni_media, "_publisher", lambda ak, sk: _Publisher())
    with pytest.raises(Exception) as error:
        omni_media.prepare_segment_audio(
            {"duration_ms": 6000}, [{"start_ms": 0, "end_ms": 1000}],
            audio_url="https://cdn.example.com/full.wav", audio_path=None, ak="ak", sk="sk")
    assert getattr(error.value, "code", "") == "OMNIHUMAN_SEGMENT_ID_MISSING"
