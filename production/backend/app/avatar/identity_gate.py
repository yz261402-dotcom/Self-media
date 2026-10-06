"""身份门禁与负例拦截（AC-AV-03）。

不同人物、卡通、SVG/CSS/几何人形、无法判断身份、哈希篡改均失败或待复核，
不进入生产白名单；拒绝原因可定位到具体资产。

画风标签由登记方声明，本模块不声称做了图像识别——无法判定的一律判 review。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import registry
from .registry import AssetRejected

# 非写实画风：不在生产白名单内
NEGATIVE_STYLES = {
    "cartoon": "卡通画风",
    "anime": "二次元画风",
    "svg": "SVG 矢量人形",
    "css": "CSS 拼装人形",
    "geometric": "几何人形",
    "illustration": "插画风格",
}


class GateDecision(Dict[str, Any]):
    pass


def kind_matches(asset: Dict[str, Any], expected_kind: str) -> bool:
    return asset.get("kind") == expected_kind


def guard_kind(asset: Dict[str, Any], expected_kind: str) -> None:
    """动作参考被误作人物原片是明确负例：种类不符直接拒收。"""
    if asset.get("kind") != expected_kind:
        raise AssetRejected(
            "ASSET_KIND_MISMATCH",
            f"资产种类不符：需要 {expected_kind}，实际 {asset.get('kind')}",
            {"asset_id": asset.get("id"), "expected": expected_kind, "actual": asset.get("kind")},
        )


def screen_style(declared_style: str) -> Dict[str, Any]:
    key = (declared_style or "").strip().lower()
    if key in NEGATIVE_STYLES:
        return {"outcome": "fail", "reason": f"{NEGATIVE_STYLES[key]}不属于写实人物资产，不得进入生产白名单"}
    if not key:
        return {"outcome": "review", "reason": "未声明画风，无法判断身份，需人工复核"}
    return {"outcome": "pass", "reason": ""}


def check_identity_match(asset: Dict[str, Any], identity: Dict[str, Any],
                         review: Optional[Dict[str, Any]]) -> List[str]:
    """换脸候选拒收：审核摘要必须与冻结身份基准一致。"""
    reasons: List[str] = []
    if not review:
        reasons.append("缺少与当前版本和哈希匹配的身份审核结论")
        return reasons
    if review.get("identity_digest") != identity.get("identity_digest"):
        reasons.append(
            f"身份基准不符：审核摘要 {review.get('identity_digest')} 与冻结基准 "
            f"{identity.get('identity_digest')} 不一致，判定为换脸候选"
        )
    return reasons


def admit(asset_id: str, identity_id: str, expected_kind: str = "hero",
          declared_style: str = "") -> Dict[str, Any]:
    """生产前准入判定。返回结构化结论，不抛异常，便于定位到资产。"""
    reasons: List[str] = []
    asset = registry.get_asset(asset_id)
    identity = registry.get_identity(identity_id)
    if not asset:
        return {"ok": False, "outcome": "fail", "asset_id": asset_id,
                "reasons": [f"资产不存在：{asset_id}"]}
    if not identity:
        return {"ok": False, "outcome": "fail", "asset_id": asset_id,
                "reasons": [f"人物不存在：{identity_id}"]}
    if asset.get("identity_id") != identity_id:
        reasons.append(f"资产不属于该人物：{asset.get('identity_id')}")
    if asset.get("kind") != expected_kind:
        reasons.append(f"资产种类不符：需要 {expected_kind}，实际 {asset.get('kind')}")
    if asset.get("is_mock"):
        reasons.append("Mock 后代不得进入生产")
    if not asset.get("storage_uri"):
        reasons.append("缺图")
    if not asset.get("file_hash"):
        reasons.append("缺少文件哈希")
    if not asset.get("source_evidence"):
        reasons.append("来源不清")
    if asset.get("approval_state") != "approved":
        reasons.append(f"资产未定版（{asset.get('approval_state')}）")

    review = registry.current_review(asset_id)
    reasons.extend(check_identity_match(asset, identity, review))

    style = screen_style(declared_style)
    if style["outcome"] != "pass":
        reasons.append(style["reason"])

    outcome = "fail" if reasons else "pass"
    return {"ok": not reasons, "outcome": outcome, "asset_id": asset_id,
            "identity_id": identity_id, "reasons": reasons}


def admit_for_production(asset_id: str, identity_id: str, expected_kind: str = "hero",
                         declared_style: str = "") -> Dict[str, Any]:
    """强门禁版本：不通过即抛错，阻断后续生产调用。"""
    decision = admit(asset_id, identity_id, expected_kind, declared_style)
    if not decision["ok"]:
        raise AssetRejected("IDENTITY_GATE_REJECTED", "；".join(decision["reasons"]),
                            {"asset_id": asset_id, "reasons": decision["reasons"]})
    return decision


def verify_no_tamper(asset_id: str, actual_hash: str) -> Dict[str, Any]:
    """哈希篡改检查：实际文件哈希必须与登记一致。"""
    ok, msg = registry.verify_hash(asset_id, actual_hash)
    if not ok:
        raise AssetRejected("ASSET_HASH_TAMPERED", msg, {"asset_id": asset_id})
    return {"ok": True, "asset_id": asset_id}
