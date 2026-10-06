"""Wan 整轨媒体验证与既有 TOS 桥接；不切分、不变速、不截取音频。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from decimal import Decimal, ROUND_CEILING
from typing import Any, Dict

from . import omni_media, providers
from .errors import TalkingHeadBlocked, TalkingHeadRejected
from .routes import assert_wan_audio_duration_allowed, wan_generation_seconds

MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def validate_image(path) -> Dict[str, Any]:
    """上传临时文件也按真实内容验证，不依赖扩展名。"""
    from PIL import Image
    try:
        path = Path(path)
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError("image exceeds size limit")
        with Image.open(path) as picture:
            if picture.format not in ("PNG", "JPEG", "WEBP") or getattr(picture, "is_animated", False):
                raise ValueError("unsupported image")
            width, height = picture.size
            if min(width, height) < 240 or max(width, height) > 8000 or max(width, height) / min(width, height) > 8:
                raise ValueError("unsupported dimensions")
            if "A" in picture.getbands() or "transparency" in picture.info:
                raise ValueError("transparent image")
            result = {"width": width, "height": height, "format": picture.format}
            picture.verify()
        with Image.open(path) as picture:
            picture.load()
        return result
    except Exception as error:
        raise TalkingHeadRejected("WAN_IMAGE_INVALID", "参考图需为不透明静态 PNG/JPEG/WEBP，不超过 20 MB、边长 240–8000 像素且宽高比不超过 8。") from error


def prepare_native_media(source: Dict[str, Any]) -> Dict[str, Any]:
    from .wan_inputs import local_image_path
    try:
        path = (local_image_path(source) if source.get("kind") == "wan_image" else
                omni_media._local_media_path("source_image_url", source.get("storage_uri"), source.get("asset_hash")))
        info = validate_image(path)
    except TalkingHeadBlocked as error:
        raise TalkingHeadBlocked(error.code.replace("OMNIHUMAN", "WAN"),
                                 "参考图缺失、哈希变化或未登记，请重新检查本地素材。", error.detail) from error
    ak, sk = os.environ.get(providers.SECRET_ENV_AK, ""), os.environ.get(providers.SECRET_ENV_SK, "")
    if not ak or not sk:
        raise TalkingHeadBlocked("WAN_MEDIA_STORAGE_NOT_CONFIGURED", "缺少现有 TOS 存储凭据，尚未发布参考图或提交生成。")
    extension, mime = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                       "WEBP": ("webp", "image/webp")}[info["format"]]
    digest = source["asset_hash"]
    key = f"wan/image/{digest[7:]}.{extension}"
    try:
        publisher = omni_media._publisher(ak, sk)
        if not publisher.exists(key):
            publisher.ensure_bucket()
            publisher.publish_file(str(path), key=key, content_type=mime)
        url = publisher.url_for(key)
    except Exception as error:
        raise TalkingHeadBlocked("WAN_MEDIA_UPLOAD_FAILED", "参考图发布失败，请检查 TOS 配置及网络；尚未提交生成。") from error
    try:
        publisher.verify_public_file(url, digest)
    except Exception as error:
        raise TalkingHeadBlocked("WAN_MEDIA_NOT_READABLE", "参考图外部读取或哈希校验未通过，尚未提交生成。") from error
    return {"image_url": url}


def prepare_media(source: Dict[str, Any], audio: Dict[str, Any]) -> Dict[str, Any]:
    """只允许核对过真实本地字节的输入；公开 URL 由已配置的 TOS 提供。"""
    from ..video import ffmpeg
    from PIL import Image

    try:
        audio_path = omni_media._local_media_path("audio_url", audio.get("storage_uri"), audio.get("file_hash"))
        image_path = omni_media._local_media_path("source_image_url", source.get("storage_uri"), source.get("asset_hash"))
    except TalkingHeadBlocked as error:
        raise TalkingHeadBlocked(error.code.replace("OMNIHUMAN", "WAN"),
                                 "Wan 人物图或配音文件缺失、哈希变化或未登记；请重新检查本地素材。",
                                 error.detail) from error
    if audio_path.suffix.lower() not in (".wav", ".mp3"):
        raise TalkingHeadRejected("WAN_AUDIO_FORMAT_INVALID", "Wan 原音频必须为 WAV 或 MP3；首版不自动转换或截取。")
    if not 0 < audio_path.stat().st_size <= MAX_AUDIO_BYTES:
        raise TalkingHeadRejected("WAN_AUDIO_SIZE_INVALID", "Wan 原音频大小必须大于 0 且不超过 15 MB。")
    try:
        cp = ffmpeg._run([ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file",
                          "-print_format", "json", "-show_format", "-show_streams", str(audio_path)], timeout=30)
        if cp.returncode:
            raise ValueError("probe failed")
        probed = json.loads(cp.stdout)
        streams = probed.get("streams") or []
        format_info = probed.get("format") or {}
        duration = Decimal(str(format_info.get("duration") or "0")) * 1000
        if not duration.is_finite() or duration <= 0:
            raise ValueError("invalid duration")
        actual_ms = int(duration.to_integral_value(rounding=ROUND_CEILING))
        if (format_info.get("format_name") not in ("wav", "mp3") or
                not any(s.get("codec_type") == "audio" for s in streams) or
                any(s.get("codec_type") == "video" for s in streams)):
            raise ValueError("not a standalone audio file")
        if format_info["format_name"] == "mp3":
            # MP3 container duration can include encoder delay/padding. Decode only
            # for measurement; publish exactly the original, unchanged MP3 bytes.
            pcm = ffmpeg.run_ffmpeg(["-v", "error", "-protocol_whitelist", "file", "-i", str(audio_path),
                                     "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                                     "-f", "s16le", "pipe:1"], timeout=30)
            if pcm.returncode or pcm.stderr.strip() or not pcm.stdout or len(pcm.stdout) % 2:
                raise ValueError("complete MP3 decode failed")
            actual_ms = (len(pcm.stdout) * 1000 + 31999) // 32000
    except Exception as error:
        raise TalkingHeadBlocked("WAN_AUDIO_UNVERIFIABLE", "无法核验 Wan 原音频真实格式和时长；尚未上传或提交。") from error
    assert_wan_audio_duration_allowed(actual_ms)
    declared_ms = int(audio.get("duration_ms") or 0)
    tolerance = 100 if format_info["format_name"] == "mp3" else 1
    if (abs(actual_ms - declared_ms) > tolerance or
            wan_generation_seconds(actual_ms) != wan_generation_seconds(declared_ms)):
        raise TalkingHeadRejected("WAN_AUDIO_DURATION_MISMATCH",
                                  "原音频实测时长与已确认资产记录不一致，请重新确认音频。")
    try:
        ffmpeg.full_decode_check(str(audio_path))
    except Exception as error:
        raise TalkingHeadBlocked("WAN_AUDIO_UNVERIFIABLE", "完整音频解码校验失败，不能把损坏音频交给 Wan。") from error
    try:
        if not 0 < image_path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError("image exceeds size limit")
        with Image.open(image_path) as picture:
            if picture.format not in ("PNG", "JPEG", "WEBP") or getattr(picture, "is_animated", False):
                raise ValueError("unsupported image")
            width, height = picture.size
            if min(width, height) < 240 or max(width, height) > 8000 or max(width, height) / min(width, height) > 8:
                raise ValueError("unsupported image dimensions")
            if "A" in picture.getbands() or "transparency" in picture.info:
                raise ValueError("transparent image not supported")
            picture.verify()
    except Exception as error:
        raise TalkingHeadRejected("WAN_IMAGE_INVALID", "Wan 人物图需为不透明静态 PNG/JPEG/WEBP，大小不超过 20 MB、边长 240–8000 像素且宽高比不超过 8。") from error
    ak = os.environ.get(providers.SECRET_ENV_AK, "")
    sk = os.environ.get(providers.SECRET_ENV_SK, "")
    if not ak or not sk:
        raise TalkingHeadBlocked("WAN_MEDIA_STORAGE_NOT_CONFIGURED",
                                 "Wan 复用现有 TOS 素材发布设施；缺少存储凭据，尚未上传或提交。")
    try:
        return dict(omni_media.prepare_media(source, audio, ak=ak, sk=sk), actual_audio_duration_ms=actual_ms)
    except TalkingHeadBlocked as error:
        raise TalkingHeadBlocked(error.code.replace("OMNIHUMAN", "WAN"),
                                 "Wan 素材发布或匿名读取校验失败；请检查现有 TOS 配置与网络，尚未提交生成。",
                                 error.detail) from error
