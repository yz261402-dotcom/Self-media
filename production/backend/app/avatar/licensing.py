"""权属与许可核验（AC-AV-04）。

规则：商业使用范围必须可追溯；未确认的模型/权重许可证阻塞该方案；
算法名称或代码许可证不能替代权重许可。本模块只记录与判定，不伪造法律结论。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import LicenseRecord, now_iso

# 代码/算法层面的证据关键字：命中不足以支撑权重商用许可
_CODE_ONLY_HINTS = ("github", "gitlab", "arxiv", "论文", "algorithm", "repo", "apache", "mit license")


class LicenseBlocked(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def register(subject: str, kind: str, scope: str = "unverified", evidence: str = "",
             verified_by: str = "", covers_weights: bool = False, note: str = "") -> Dict[str, Any]:
    """登记一条权属记录。声明商用必须给出证据与核验人，否则回落 unverified。"""
    if scope == "commercial_ok" and (not evidence or not verified_by):
        scope = "unverified"
        note = (note + "；声称商用但缺证据或核验人，已回落未核验").strip("；")
    rec = LicenseRecord(
        id=st.new_id("lic"),
        subject=subject,
        kind=kind,
        scope=scope,
        evidence=evidence,
        covers_weights=covers_weights,
        verified_at=now_iso() if scope in ("commercial_ok", "blocked") else None,
        verified_by=verified_by,
        note=note,
    )
    st.put("license_records", rec.id, rec.model_dump())
    return rec.model_dump()


def get(license_id: str) -> Optional[Dict[str, Any]]:
    return st.get("license_records", license_id) if license_id else None


def require(license_id: Optional[str], subject: str = "", need_weights: bool = False) -> Dict[str, Any]:
    """生产前置检查。未登记、未核验、被阻塞或权重未覆盖，一律阻塞。"""
    rec = get(license_id or "")
    if not rec:
        raise LicenseBlocked("LICENSE_MISSING", f"缺少权属记录，无法确定商业使用范围：{subject or '未命名对象'}")
    if rec.get("scope") == "blocked":
        raise LicenseBlocked("LICENSE_BLOCKED", f"权属已判定不可商用：{rec.get('subject')}")
    if rec.get("scope") != "commercial_ok":
        raise LicenseBlocked("LICENSE_UNVERIFIED", f"权属未核验为可商用（当前 {rec.get('scope')}）：{rec.get('subject')}")
    if need_weights:
        if rec.get("kind") == "model_weights" and not rec.get("covers_weights"):
            raise LicenseBlocked(
                "WEIGHTS_LICENSE_MISSING",
                "算法名称或代码许可证不能替代模型权重许可：权重商用授权未确认",
                {"license_id": rec.get("id"), "subject": rec.get("subject")},
            )
        if _looks_code_only(rec.get("evidence", "")) and not rec.get("covers_weights"):
            raise LicenseBlocked(
                "WEIGHTS_LICENSE_MISSING",
                "证据指向代码/论文层面，未覆盖权重商用授权",
                {"license_id": rec.get("id")},
            )
    return rec


# 确认标记版本。与 COST_NOTICE_VERSION 同理：确认措辞或适用范围一旦变化，
# 旧版本的确认自动失效，必须重新按下。防止"去年点的同意"一直有效。
ATTEST_TOKEN = "COMMERCIAL_ATTEST_V1"


def attest_commercial(license_id: str, attestor: str, basis: str, ack_token: str) -> Dict[str, Any]:
    """人对商用范围作出确认。**不是核验，更不是法律结论。**

    适用的场景：生成服务的条款允许商用、素材由本人提供或已取得授权这类
    "事实清楚但系统无法自行证实"的情况。系统只负责把谁、何时、依据什么
    这条来源链留下来；判定的责任在人，不在代码。

    刻意不做的两件事：
    · 不改写原有 evidence——那里记着"未取得书面法律意见"，抹掉就是伪造核验；
    · 不接受非 `user` 的操作者——系统不能自己给自己核发通行。
    """
    rec = get(license_id or "")
    if not rec:
        raise LicenseBlocked("LICENSE_MISSING", f"权属记录不存在：{license_id}")
    if not str(attestor or "").startswith("user"):
        raise LicenseBlocked("ACTOR_NOT_USER", "商用范围只能由人确认，系统不得自行核发")
    if ack_token != ATTEST_TOKEN:
        raise LicenseBlocked("LICENSE_ATTEST_NOT_ACKNOWLEDGED",
                             f"确认标记不是当前版本 {ATTEST_TOKEN}，须重新确认")
    if not str(basis or "").strip():
        raise LicenseBlocked("LICENSE_ATTEST_BASIS_MISSING",
                             "须写明依据什么确认商用范围（服务条款/授权书/自有素材）")
    if rec.get("scope") == "blocked":
        raise LicenseBlocked("LICENSE_BLOCKED", "已判定不可商用，不能通过人工确认翻案")

    at = now_iso()
    rec["scope"] = "commercial_ok"
    rec["verified_by"] = attestor
    rec["verified_at"] = at
    rec["evidence"] = (
        (rec.get("evidence", "") + f"；{attestor} 于 {at} 确认商用范围：{basis}"
         "（由人确认，非法律意见，不代表已取得书面法律意见）").strip("；")
    )
    rec["note"] = (rec.get("note", "") + f"；{ATTEST_TOKEN}").strip("；")
    st.put("license_records", license_id, rec)
    return rec


def revoke_commercial(license_id: str, operator: str, reason: str = "") -> Dict[str, Any]:
    """撤销人工确认，回到未核验。记录保留：来源链不能倒回去抹。"""
    rec = get(license_id or "")
    if not rec:
        raise LicenseBlocked("LICENSE_MISSING", f"权属记录不存在：{license_id}")
    if not str(operator or "").startswith("user"):
        raise LicenseBlocked("ACTOR_NOT_USER", "撤销确认也只能由人执行")
    at = now_iso()
    rec["scope"] = "unverified"
    rec["verified_by"] = ""
    rec["verified_at"] = None
    rec["evidence"] = (rec.get("evidence", "") +
                       f"；{operator} 于 {at} 撤销上述人工确认（{reason or '未说明原因'}）").strip("；")
    st.put("license_records", license_id, rec)
    return rec


def assert_registrable(license_id: Optional[str], subject: str = "") -> Optional[Dict[str, Any]]:
    """登记阶段只挡"明确不可商用"。

    待核验（unverified/personal_only）允许登记以保留来源链，
    但会在生产准入时阻塞——登记不等于获准生产。
    """
    if not license_id:
        return None
    rec = get(license_id)
    if not rec:
        raise LicenseBlocked("LICENSE_MISSING", f"引用的权属记录不存在：{license_id}")
    if rec.get("scope") == "blocked":
        raise LicenseBlocked("LICENSE_BLOCKED",
                             f"权属已判定不可商用，不得登记为可用资产：{rec.get('subject')}",
                             {"license_id": license_id})
    return rec


def require_weights(subject: str) -> Dict[str, Any]:
    """按对象名查找权重许可记录；未登记即阻塞，不得默认放行。"""
    rows = [r for r in list_records()
            if r.get("kind") == "model_weights" and r.get("subject") == subject]
    if not rows:
        raise LicenseBlocked("WEIGHTS_LICENSE_MISSING",
                             f"未登记检测模型权重许可：{subject}", {"subject": subject})
    rec = sorted(rows, key=lambda r: r.get("verified_at") or "")[-1]
    return require(rec["id"], subject=subject, need_weights=True)


def _looks_code_only(evidence: str) -> bool:
    low = (evidence or "").lower()
    return any(h in low for h in _CODE_ONLY_HINTS)


def list_records() -> List[Dict[str, Any]]:
    return st.all_rows("license_records")
