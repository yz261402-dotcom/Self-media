"""动作/表情意图映射（T03）。

M2 将 motion 意图映射到已获准的动作参考与受支持参数；
OmniHuman 1.5 的自由描述留到 T05 与最终 TTS 一起提交，此处不做承诺；
EXP-001 的短循环参考不直接转为长期母版。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import MotionReference
from . import licensing
from .registry import AssetRejected

# 受支持动作键与参数取值区间。未登记的动作键一律视为不支持。
MOTION_CATALOG: Dict[str, Dict[str, Any]] = {
    "nod": {"label": "轻微点头", "params": {"amplitude": [0.1, 0.6], "speed": [0.5, 1.5]}},
    "head_turn_small": {"label": "小幅转头", "params": {"amplitude": [0.1, 0.5], "speed": [0.5, 1.5]}},
    "gesture_open": {"label": "开放手势", "params": {"amplitude": [0.2, 0.8], "speed": [0.5, 1.5]}},
    "smile": {"label": "微笑", "params": {"intensity": [0.1, 0.7]}},
    "serious": {"label": "严肃陈述", "params": {"intensity": [0.1, 0.6]}},
}

# 长期母版的最短循环时长：短循环只作实验参考
MIN_MASTER_LOOP_SECONDS = 3.0


def register_motion_reference(identity_id: str, motion_key: str, asset_id: str,
                              label: str = "", description: str = "",
                              supported_params: Optional[Dict[str, Any]] = None,
                              loop_seconds: Optional[float] = None,
                              license_id: Optional[str] = None, note: str = "") -> Dict[str, Any]:
    if motion_key not in MOTION_CATALOG:
        raise AssetRejected("MOTION_UNSUPPORTED", f"动作键未受支持：{motion_key}",
                            {"supported": sorted(MOTION_CATALOG)})
    asset = st.get("avatar_assets", asset_id) if asset_id else None
    if not asset:
        raise AssetRejected("ASSET_NOT_FOUND", f"动作参考资产不存在：{asset_id}")
    if asset.get("kind") != "motion":
        raise AssetRejected("ASSET_KIND_MISMATCH",
                            f"动作参考资产种类必须为 motion，实际 {asset.get('kind')}")
    if license_id:
        licensing.require(license_id, subject=f"motion/{motion_key}")

    ref = MotionReference(
        id=st.new_id("mot"),
        identity_id=identity_id,
        motion_key=motion_key,
        label=label or MOTION_CATALOG[motion_key]["label"],
        description=description,
        asset_id=asset_id,
        supported_params=supported_params or _default_params(motion_key),
        approval_state="pending",
        loop_seconds=loop_seconds,
        license_id=license_id,
        note=note,
    )
    st.put("motion_references", ref.id, ref.model_dump(), {"identity_id": identity_id})
    return ref.model_dump()


def _default_params(motion_key: str) -> Dict[str, Any]:
    return {k: {"min": v[0], "max": v[1]} for k, v in MOTION_CATALOG[motion_key]["params"].items()}


def list_references(identity_id: str, approved_only: bool = False) -> List[Dict[str, Any]]:
    rows = st.all_rows("motion_references", "identity_id=?", [identity_id])
    if approved_only:
        rows = [r for r in rows if r.get("approval_state") == "approved"]
    return rows


def approve_reference(ref_id: str) -> Dict[str, Any]:
    row = st.get("motion_references", ref_id)
    if not row:
        raise AssetRejected("MOTION_REF_NOT_FOUND", f"动作参考不存在：{ref_id}")
    row["approval_state"] = "approved"
    st.put("motion_references", ref_id, row, {"identity_id": row.get("identity_id")})
    return row


def _in_range(value: Any, bound: Dict[str, Any]) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    lo = bound.get("min", bound.get("lo"))
    hi = bound.get("max", bound.get("hi"))
    if lo is not None and v < float(lo):
        return False
    if hi is not None and v > float(hi):
        return False
    return True


def validate_imported_reference(ref: Dict[str, Any]) -> None:
    """导入是复用原参考，不是复制一次后永久绕过来源撤销或版本变化。"""
    source_id = ref.get("source_reference_id")
    if not source_id:
        return  # 原有参考保留原有审批语义。
    source = st.get("motion_references", source_id) or {}
    if source.get("approval_state") != "approved" or source.get("is_mock") or source.get("source_reference_id"):
        raise AssetRejected("MOTION_SOURCE_NOT_APPROVED", "原动作参考已撤销或不可用，请重新选择动作")
    fields = ("asset_id", "motion_key", "supported_params", "loop_seconds", "license_id")
    if any(ref.get(field) != source.get(field) for field in fields):
        raise AssetRejected("MOTION_SOURCE_CHANGED", "原动作参考版本或使用范围已变化，请重新检查")
    asset = st.get("avatar_assets", source.get("asset_id") or "") or {}
    if (asset.get("kind") != "motion" or asset.get("is_mock")
            or asset.get("approval_state") == "rejected" or not asset.get("source_evidence")):
        raise AssetRejected("MOTION_SOURCE_INVALID", "原动作素材已不可用")
    if not ref.get("source_asset_hash") or asset.get("file_hash") != ref["source_asset_hash"]:
        raise AssetRejected("MOTION_SOURCE_HASH_CHANGED", "原动作文件版本已变化，不能沿用先前选择")
    for license_id in {source.get("license_id"), asset.get("license_id")} - {None, ""}:
        try:
            licensing.require(license_id, subject=source.get("label") or "动作来源")
        except licensing.LicenseBlocked as exc:
            raise AssetRejected(exc.code, exc.message, exc.detail) from exc


def resolve_motion_intent(identity_id: str, intent: Dict[str, Any],
                          route: str = "M2", as_master: bool = False) -> Dict[str, Any]:
    """把动作意图映射到获准参考；自由描述与越界参数一律拒绝，不做近似兜底。"""
    if not isinstance(intent, dict):
        raise AssetRejected("MOTION_INTENT_INVALID", "动作意图格式非法")
    if intent.get("free_text") and route == "M2":
        raise AssetRejected(
            "MOTION_FREETEXT_UNSUPPORTED",
            "M2 路线不支持任意动作自由描述；请从获准动作模板中选择，或改用 OmniHuman 1.5（T05）",
            {"route": route},
        )
    motion_key = intent.get("motion_key")
    if not motion_key:
        raise AssetRejected("MOTION_REQUIRED", "缺少动作模板选择")
    if motion_key not in MOTION_CATALOG:
        raise AssetRejected("MOTION_UNSUPPORTED", f"动作键未受支持：{motion_key}",
                            {"supported": sorted(MOTION_CATALOG)})

    refs = [r for r in list_references(identity_id)
            if r.get("motion_key") == motion_key and r.get("approval_state") == "approved"]
    declared_ref_id = intent.get("motion_ref_id")
    if declared_ref_id:
        # 意图里点名了具体参考，那它就是绑定而非线索：不得改用同 key 的另一条
        # 已获准参考顶替。否则"登记时看到的那条动作"与"实际执行的动作"可能不是同一条，
        # 而这在输出上看不出来——正是最隐蔽的一类替换。
        declared = st.get("motion_references", declared_ref_id) or {}
        if declared.get("identity_id") and declared.get("identity_id") != identity_id:
            raise AssetRejected("MOTION_REF_IDENTITY_MISMATCH",
                                "点名的动作参考不属于该人物，属换人负例",
                                {"ref_id": declared_ref_id,
                                 "ref_identity": declared.get("identity_id"),
                                 "identity_id": identity_id})
        refs = [r for r in refs if r.get("id") == declared_ref_id]
    if not refs:
        raise AssetRejected("MOTION_NOT_APPROVED",
                            f"该人物下动作 {motion_key} 没有已获准的参考，不得执行",
                            {"identity_id": identity_id, "motion_key": motion_key,
                             "declared_ref_id": declared_ref_id})
    ref = refs[-1]
    validate_imported_reference(ref)

    params = intent.get("params") or {}
    allowed = ref.get("supported_params") or {}
    for k, v in params.items():
        if k not in allowed:
            raise AssetRejected("MOTION_PARAM_UNSUPPORTED", f"动作 {motion_key} 不支持参数 {k}",
                                {"supported": sorted(allowed)})
        if not _in_range(v, allowed[k]):
            raise AssetRejected("MOTION_PARAM_OUT_OF_RANGE",
                                f"参数 {k}={v} 超出 {motion_key} 的支持范围 {allowed[k]}")

    loop = ref.get("loop_seconds")
    master_eligible = loop is None or float(loop) >= MIN_MASTER_LOOP_SECONDS
    if as_master and not master_eligible:
        raise AssetRejected(
            "MOTION_LOOP_TOO_SHORT",
            f"循环时长 {loop}s 短于长期母版下限 {MIN_MASTER_LOOP_SECONDS}s，不得直接转为长期母版",
            {"ref_id": ref.get("id")},
        )
    return {
        "motion_ref_id": ref.get("id"),
        "motion_key": motion_key,
        "params": params,
        "asset_id": ref.get("asset_id"),
        "route": route,
        "master_eligible": master_eligible,
        "note": "意图已映射到获准参考；实际执行仍受 T05 的嘴型与画面门禁约束",
    }
