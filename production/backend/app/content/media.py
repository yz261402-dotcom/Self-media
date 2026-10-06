"""参考视频媒体资产：受控落盘、探测与完整解码校验、内容寻址去重、按作品隔离。

只接受服务端自己保存的文件：`storage_uri` 是相对工作区的固定形状，读取时必须解析回受控目录内，
任何调用方都不能传入任意绝对路径。外部进程一律 `shell=False` 且带超时。
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Optional

from .. import storage as st
from ..avatar import person_image
from ..contracts import now_iso
from ..video import ffmpeg
from ..video.errors import VideoBlocked
from . import contracts as cc
from . import costs, repository as repo
from .errors import ContentRejected

ALLOWED = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/webm", "application/octet-stream", ""}
PROBE_TIMEOUT = 30
DECODE_TIMEOUT = 900
CHUNK = 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
TABLE = "content_media_assets"


def media_root() -> Path:
    return Path(person_image.workspace_root()).resolve() / "content-media"


def resolve_path(asset: cc.MediaAsset) -> Path:
    """storage_uri → 受控目录内的真实文件；越界或缺失都拒绝。"""
    root = media_root()
    target = (Path(person_image.workspace_root()).resolve() / asset.storage_uri).resolve()
    if not target.is_relative_to(root):
        raise ContentRejected("媒体存储位置不在受控目录内", "MEDIA_PATH_INVALID")
    if not target.is_file():
        raise ContentRejected("媒体文件已丢失，请重新上传", "MEDIA_FILE_MISSING")
    return target


def probe_video(path: str) -> Dict[str, object]:
    """ffprobe 读取视频流的声明时长与画面尺寸；不代表媒体内容已经完整解码。"""
    try:
        cp = subprocess.run([ffmpeg.ffprobe_bin(), "-v", "error", "-protocol_whitelist", "file",
                             "-print_format", "json", "-show_format", "-show_streams", str(path)],
                            capture_output=True, shell=False, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ContentRejected("视频探测超时，文件可能已损坏", "MEDIA_INVALID") from None
    except Exception as exc:  # noqa: BLE001  ffmpeg/ffprobe 缺失或无法启动
        raise ContentRejected("无法运行视频探测工具（ffprobe）：" + str(getattr(exc, "message", type(exc).__name__)),
                              "MEDIA_TOOL_UNAVAILABLE") from None
    if cp.returncode != 0:
        raise ContentRejected("视频无法读取，请上传可正常播放的 MP4/MOV/WebM 文件", "MEDIA_INVALID")
    try:
        info = json.loads(cp.stdout.decode("utf-8"))
        video = next(s for s in info.get("streams") or [] if s.get("codec_type") == "video")
        # 容器时长可能包含更长的音轨尾部，视觉采样必须先以视频流为准。
        # WebM/Matroska 通常把各轨时长放在 tags.DURATION，而非 stream.duration。
        duration_tag = str((video.get("tags") or {}).get("DURATION") or "").strip()
        tag_match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", duration_tag, re.ASCII)
        tag_seconds = (int(tag_match[1]) * 3600 + int(tag_match[2]) * 60 + float(tag_match[3])) if tag_match else 0
        duration = float(video.get("duration") or tag_seconds or (info.get("format") or {}).get("duration") or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (StopIteration, ValueError, TypeError, KeyError):
        raise ContentRejected("文件没有可用的视频轨或元数据无效", "MEDIA_INVALID") from None
    if duration <= 0 or width <= 0 or height <= 0 or duration != duration:
        raise ContentRejected("无法读取视频的时长或画面尺寸", "MEDIA_INVALID")
    return {"duration_ms": max(1, int(round(duration * 1000))), "width": width, "height": height}


def validate_video_complete(path: str) -> None:
    """入库前完整解码视频和可选音轨一次；前置 moov 可读也不能放行截断文件。"""
    try:
        cp = ffmpeg.run_ffmpeg(["-v", "error", "-xerror", "-err_detect", "explode",
                               "-protocol_whitelist", "file", "-i", str(path),
                               "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
                              timeout=DECODE_TIMEOUT)
    except VideoBlocked as exc:
        if exc.code == "FFMPEG_TIMEOUT":
            raise ContentRejected("视频完整性检查超时，请缩短视频后重新上传", "MEDIA_PROCESS_TIMEOUT") from None
        raise ContentRejected("无法运行视频完整性检查工具（ffmpeg）", "MEDIA_TOOL_UNAVAILABLE") from None
    if cp.returncode != 0 or cp.stderr.strip():
        raise ContentRejected("视频内容不完整或已损坏，请重新获取或上传可完整播放的原视频", "MEDIA_INCOMPLETE")


def _suffix(filename: str) -> str:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = Path(name).suffix.lower()
    if not name or len(name) > 255 or any(ord(c) < 32 for c in name) or suffix not in ALLOWED:
        raise ContentRejected("只支持 MP4、MOV、WebM 视频文件", "MEDIA_TYPE_UNSUPPORTED")
    return suffix


def save_uploaded_video(work_id: str, reference_id: str, data: BinaryIO, filename: str,
                        content_type: str) -> cc.MediaAsset:
    suffix = _suffix(filename)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in CONTENT_TYPES:
        raise ContentRejected("文件类型不是视频", "MEDIA_TYPE_UNSUPPORTED")
    return _ingest(work_id, reference_id, data, suffix, "user_upload", "")


def _ingest(work_id: str, reference_id: str, data: BinaryIO, suffix: str, source_kind: str,
             source_url: str, expected_bytes: Optional[int] = None) -> cc.MediaAsset:
    ref = repo.get_reference(reference_id or "")
    if ref is None or ref["work_id"] != work_id:
        raise ContentRejected("参考不存在或不属于该作品", "REFERENCE_NOT_FOUND")
    limits = costs.visual_limits()
    root = media_root()
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < max(MIN_FREE_BYTES, limits.max_bytes):
        raise ContentRejected("磁盘剩余空间不足，暂不能保存参考视频", "MEDIA_DISK_LOW")
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix="upload-", suffix=".part")
    tmp = Path(tmp_name)
    total, sha = 0, hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = data.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limits.max_bytes:
                    raise ContentRejected(f"视频不能超过 {limits.max_bytes // (1024 * 1024)} MiB", "MEDIA_TOO_LARGE")
                sha.update(chunk)
                out.write(chunk)
        if total == 0:
            raise ContentRejected("不能上传空文件", "MEDIA_INVALID")
        if expected_bytes is not None and total != expected_bytes:
            raise ContentRejected(f"视频下载不完整：应收到 {expected_bytes} 字节，实际收到 {total} 字节",
                                  "MEDIA_DOWNLOAD_INCOMPLETE")
        meta = probe_video(str(tmp))
        if int(meta["duration_ms"]) > limits.max_duration_ms:
            raise ContentRejected(f"视频不能超过 {limits.max_duration_ms // 60000} 分钟", "MEDIA_TOO_LONG")
        validate_video_complete(str(tmp))
        digest = "sha256:" + sha.hexdigest()
        for row in st.all_rows(TABLE, "work_id=?", [work_id]):
            if row["sha256"] == digest:
                reference_ids = list(row.get("reference_snapshot_ids") or [])
                if reference_id not in reference_ids:
                    reference_ids.append(reference_id)
                bound_at = dict(row.get("reference_bound_at") or {})
                bound_at[reference_id] = now_iso()
                row = dict(row, reference_snapshot_ids=reference_ids, reference_bound_at=bound_at)
                st.put(TABLE, row["id"], row, {"work_id": work_id})
                return cc.MediaAsset(**row)  # 同作品同内容复用，不重复落盘
        target_dir = root / work_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (sha.hexdigest() + suffix)
        os.replace(tmp, target)
        created_at = now_iso()
        asset = cc.MediaAsset(
            id=st.new_id("media"), work_id=work_id, kind="reference_video",
            storage_uri=str(target.relative_to(Path(person_image.workspace_root()).resolve())).replace("\\", "/"),
            sha256=digest, mime_type=ALLOWED[suffix], byte_size=total, duration_ms=int(meta["duration_ms"]),
            width=int(meta["width"]), height=int(meta["height"]), source_kind=source_kind,
            source_url=source_url, created_at=created_at, reference_snapshot_ids=[reference_id],
            reference_bound_at={reference_id: created_at})
        st.put(TABLE, asset.id, asset.model_dump(), {"work_id": work_id})
        return asset
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- 直链下载（供应商给出的临时地址）

DOWNLOAD_TIMEOUT = 60
MAX_REDIRECTS = 3
_SUFFIX_BY_TYPE = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
                   "application/octet-stream": ".mp4", "": ".mp4"}


def check_public_url(url: str) -> str:
    """只放行 http(s)、无内嵌账号、且解析结果全是公网地址的直链；内网/回环/链路本地一律拒绝（防 SSRF）。

    不限制端口：抖音 CDN 会 302 到公网域名的非标准端口（实测 8889）；防内网访问靠的是公网地址判断，不是端口。
    """
    parts = urllib.parse.urlsplit(url or "")
    host = parts.hostname or ""
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = 0
    if parts.scheme not in ("http", "https") or not host or not 0 < port < 65536 or parts.username or parts.password:
        raise ContentRejected("视频直链地址不合法", "MEDIA_URL_REJECTED")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    except (OSError, ValueError):
        raise ContentRejected("视频直链域名无法解析", "MEDIA_DOWNLOAD_FAILED") from None
    if not addrs or any(not a.is_global for a in addrs):
        raise ContentRejected("视频直链指向非公网地址，已拒绝", "MEDIA_URL_REJECTED")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401  重定向由 _public_open 逐跳校验
        return None


def _public_open(url: str, timeout: int = DOWNLOAD_TIMEOUT):
    """打开直链；每一跳（含重定向）都重新校验公网地址，最多 3 跳。"""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "video/*,*/*"})
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urllib.parse.urljoin(url, exc.headers["Location"])
                continue
            raise ContentRejected(f"视频下载被拒绝（HTTP {exc.code}），直链可能已过期", "MEDIA_DOWNLOAD_FAILED") from None
    raise ContentRejected("视频直链重定向过多", "MEDIA_DOWNLOAD_FAILED")


def download_video(work_id: str, reference_id: str, direct_url: str, source_page_url: str,
                   opener: Optional[Callable[[str], object]] = None) -> cc.MediaAsset:
    """把供应商给出的临时直链流式下载到受控目录，复用上传同一套大小/时长/类型校验。

    只把作品页链接记为来源，不保存带签名的直链。
    """
    try:
        resp = (opener or _public_open)(direct_url)
    except ContentRejected:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ContentRejected("视频下载失败：" + type(exc).__name__, "MEDIA_DOWNLOAD_FAILED") from None
    try:
        ctype = str(resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in _SUFFIX_BY_TYPE:
            raise ContentRejected("下载到的内容不是视频", "MEDIA_TYPE_UNSUPPORTED")
        expected_bytes = _download_expected_bytes(resp)
        if expected_bytes is not None and expected_bytes > costs.visual_limits().max_bytes:
            raise ContentRejected("视频大小超过允许的下载上限", "MEDIA_TOO_LARGE")
        return _ingest(work_id, reference_id, resp, _SUFFIX_BY_TYPE[ctype], "provider_url", source_page_url,
                       expected_bytes=expected_bytes)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ContentRejected("视频下载中断：" + type(exc).__name__, "MEDIA_DOWNLOAD_FAILED") from None
    finally:
        resp.close()


def _download_expected_bytes(resp: object) -> Optional[int]:
    """只接受完整 HTTP 实体；无长度头时仍须通过本地完整解码检查。"""
    length = str(resp.headers.get("Content-Length") or "").strip()
    if length and (not length.isascii() or not length.isdigit()):
        raise ContentRejected("视频下载响应的长度信息无效", "MEDIA_DOWNLOAD_INCOMPLETE")
    expected = int(length) if length else None
    range_header = str(resp.headers.get("Content-Range") or "").strip()
    if range_header:
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", range_header, re.I | re.ASCII)
        if not match:
            raise ContentRejected("视频下载响应的分段信息无效", "MEDIA_DOWNLOAD_INCOMPLETE")
        start, end, total = (int(value) for value in match.groups())
        if start != 0 or total <= 0 or end != total - 1 or (expected is not None and expected != total):
            raise ContentRejected("视频直链只返回了部分文件，请重新获取完整视频", "MEDIA_DOWNLOAD_INCOMPLETE")
        expected = total
    elif getattr(resp, "status", None) == 206:
        raise ContentRejected("视频直链返回了缺少长度信息的分段文件", "MEDIA_DOWNLOAD_INCOMPLETE")
    return expected


def get_media_asset(asset_id: str) -> Optional[cc.MediaAsset]:
    row = st.get(TABLE, asset_id or "")
    return cc.MediaAsset(**row) if row else None


def asset_for_work(work_id: str, asset_id: str) -> cc.MediaAsset:
    asset = get_media_asset(asset_id)
    if asset is None or asset.work_id != work_id:
        raise ContentRejected("媒体资产不存在或不属于该作品", "MEDIA_NOT_FOUND")
    return asset


def reference_media_asset_id(work_id: str, reference_id: str) -> str:
    """只读恢复当前参考及其同作品祖先的视频关联；不采用无关参考的媒体。"""
    assets = sorted(st.all_rows(TABLE, "work_id=?", [work_id]), key=lambda r: r.get("created_at", ""))
    by_id = {row["id"]: row for row in assets}
    fetches = sorted(st.all_rows("content_jobs", "work_id=?", [work_id]),
                     key=lambda r: r.get("created_at", ""))
    seen = set()
    current_id = reference_id or ""
    while current_id and current_id not in seen:
        seen.add(current_id)
        ref = repo.get_reference(current_id)
        if ref is None or ref["work_id"] != work_id:
            return ""
        linked = [row for row in assets if current_id in (row.get("reference_snapshot_ids") or [])]
        if linked:
            return max(linked, key=lambda row: (row.get("reference_bound_at") or {}).get(current_id)
                       or row.get("created_at", ""))["id"]
        explicit = ref.get("media_asset_id") or ""
        if explicit in by_id:
            return explicit
        for job in reversed(fetches):
            result = job.get("result_refs") or {}
            if (job["kind"] == "media_fetch" and job["state"] == "succeeded"
                    and result.get("reference_id") == current_id and result.get("media_asset_id") in by_id):
                return result["media_asset_id"]
        current_id = ref.get("parent_id") or ""
    return ""


def public_view(asset: cc.MediaAsset) -> Dict[str, object]:
    """对外只给必要字段，不暴露服务器存储位置。"""
    return {"id": asset.id, "duration_ms": asset.duration_ms, "width": asset.width, "height": asset.height,
            "mime_type": asset.mime_type, "sha256": asset.sha256, "byte_size": asset.byte_size,
            "source_kind": asset.source_kind}
