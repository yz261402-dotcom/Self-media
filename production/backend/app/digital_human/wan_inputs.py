"""Wan 独立参考图或已审核人物图；作品绑定与本地原始字节共同校验。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict

from . import worker_client
from .errors import TalkingHeadBlocked, TalkingHeadRejected


def local_image_path(source: Dict[str, Any]) -> Path:
    """解析独立上传图，禁止 URL、越界路径与未匹配的原始文件。"""
    uri = source.get("storage_uri")
    digest = source.get("asset_hash")
    if not isinstance(uri, str) or not uri or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(digest or "")):
        raise TalkingHeadRejected("WAN_IMAGE_INPUT_INVALID", "参考图缺少本地文件记录或 SHA-256 哈希。")
    root = worker_client._workspace_root().resolve()
    raw = Path(uri.replace("\\", "/"))
    if not raw.is_absolute() and (":" in uri or ".." in raw.parts):
        raise TalkingHeadBlocked("WAN_MEDIA_PATH_INVALID", "参考图路径不合法，请重新上传。")
    path = (raw if raw.is_absolute() else root / raw).resolve()
    allowed = (root / "assets", root / "production/backend/runtime/wan_inputs")
    if not any(path.is_relative_to(base) for base in allowed) or not path.is_file():
        raise TalkingHeadBlocked("WAN_MEDIA_PATH_INVALID", "参考图不在允许的素材目录中，或本地文件已缺失。")
    try:
        with path.open("rb") as stream:
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise TalkingHeadBlocked("WAN_MEDIA_PATH_INVALID", "参考图暂时无法读取，请重新检查本地文件。") from error
    if actual != digest:
        raise TalkingHeadBlocked("WAN_MEDIA_HASH_MISMATCH", "参考图文件已变化，请重新上传并确认费用。")
    return path


def resolve_source(work: Dict[str, Any]) -> Dict[str, Any]:
    source = work.get("wan_image")
    if source is not None:
        if not isinstance(source, dict) or not source.get("asset_id"):
            raise TalkingHeadRejected("WAN_IMAGE_INPUT_INVALID", "参考图记录不完整，请重新上传。")
        if not work.get("id") or source.get("work_id") != work["id"]:
            raise TalkingHeadRejected("WAN_IMAGE_WORK_MISMATCH", "参考图不属于当前作品，请重新上传。")
        from .wan_media import validate_image
        validate_image(local_image_path(source))
        return dict(source, kind="wan_image", identity_id=None, variant_id=None,
                    label=source.get("label") or "已上传参考图")
    from .plate_source import resolve_plate_source
    return resolve_plate_source(work)
