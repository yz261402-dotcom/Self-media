"""M2 源图解析：已审变体优先；未选择变体时使用本人物已定版原图。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import storage as st
from ..avatar import identity_gate, licensing, registry
from ..avatar.registry import AssetRejected
from .errors import TalkingHeadBlocked, TalkingHeadRejected


def resolve_plate_source(work: Dict[str, Any], *, variant_id: Optional[str] = None) -> Dict[str, Any]:
    identity_id = work.get("identity_id")
    if not identity_id:
        raise TalkingHeadRejected("IDENTITY_REQUIRED", "未选定人物，不得生成底片")
    ident = registry.get_identity(identity_id)
    if not ident:
        raise TalkingHeadRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    if ident.get("approval_state") != "approved" or not ident.get("frozen"):
        raise TalkingHeadBlocked("IDENTITY_NOT_APPROVED", "人物未定版或未冻结，不得生成底片")
    if not ident.get("identity_digest"):
        raise TalkingHeadRejected("IDENTITY_BASE_MISSING", "缺少身份基准摘要，无法核对源图")

    selected = variant_id if variant_id is not None else work.get("variant_id")
    if selected:
        variant = st.get("variants", selected)
        if not variant:
            raise TalkingHeadRejected("VARIANT_NOT_FOUND", f"变体不存在：{selected}")
        if variant.get("identity_id") != identity_id:
            raise TalkingHeadRejected("VARIANT_IDENTITY_MISMATCH", "变体不属于本作品选定人物，属换人负例")
        if variant.get("approval_state") != "approved" or not variant.get("review_id"):
            raise TalkingHeadBlocked("VARIANT_NOT_APPROVED", "变体未通过身份审核，不得用于生成底片")
        asset_id = variant.get("asset_id")
        if not asset_id:
            raise TalkingHeadRejected("VARIANT_ASSET_MISSING", "变体未绑定具体资产")
        kind = "variant"
        style = variant.get("declared_style", "")
        label = variant.get("preview_label") or "已审人物变体"
    else:
        ready, reasons = registry.production_ready(identity_id)
        if not ready:
            raise TalkingHeadBlocked("IDENTITY_NOT_PRODUCTION_READY", "；".join(reasons))
        asset_id = ident.get("hero_asset_id")
        kind = "hero"
        # 本项目原图由写实人物流程创建并经用户审核定版；尊重已登记的显式画风声明。
        style = (ident.get("keywords") or {}).get("style") or "realistic"
        label = f"{ident.get('name') or '当前人物'} · 已定版原图"
    asset = registry.get_asset(asset_id) or {}
    try:
        identity_gate.admit_for_production(asset_id, identity_id, expected_kind=kind,
                                           declared_style=asset.get("declared_style") or style)
        registry._require_hash(asset)
        licensing.require(asset.get("license_id"), subject=label)
    except AssetRejected as error:
        raise TalkingHeadRejected(error.code, error.message, error.detail) from error
    except licensing.LicenseBlocked as error:
        raise TalkingHeadBlocked(error.code, error.message, error.detail) from error
    return {"kind": kind, "asset_id": asset_id, "asset_hash": asset["file_hash"],
            "identity_id": identity_id, "variant_id": selected or None,
            "storage_uri": asset["storage_uri"], "label": label}
