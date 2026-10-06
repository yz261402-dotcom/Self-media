"""来源记录（AC-TTS-01 的来源侧）。

规则：来源由**流程**建立，不由客户端声明。测试素材始终为模拟，
且永远不能被审批成生产根资产——否则"测试素材复制一份、改个 mode"就能混进生产。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .. import storage as st
from ..contracts import SourceRecord, now_iso
from .errors import TtsRejected

SOURCE_TABLE = "source_records"

# 各来源类型必须齐备的字段：缺一项即来源不清
REQUIRED_FIELDS: Dict[str, tuple] = {
    "project_generated": ("provider", "model", "provider_run_id"),
    "preset_voice_selection": ("provider", "model"),
    "newly_licensed": ("rights_evidence_asset_id",),
    "project_test_material": (),
}


def register_source(*, subject: str, source_type: str,
                    provider: Optional[str] = None, model: Optional[str] = None,
                    version: Optional[str] = None, provider_run_id: Optional[str] = None,
                    prompt_asset_id: Optional[str] = None, seed: Optional[int] = None,
                    rights_evidence_asset_id: Optional[str] = None,
                    note: str = "") -> Dict[str, Any]:
    if source_type not in REQUIRED_FIELDS:
        raise TtsRejected("SOURCE_TYPE_INVALID", f"来源类型非法：{source_type}")
    if not subject:
        raise TtsRejected("SOURCE_SUBJECT_MISSING", "来源记录必须绑定到具体资产")
    payload = {"provider": provider, "model": model, "provider_run_id": provider_run_id,
               "rights_evidence_asset_id": rights_evidence_asset_id}
    missing = [f for f in REQUIRED_FIELDS[source_type] if not payload.get(f)]
    if missing:
        raise TtsRejected(
            "SOURCE_EVIDENCE_MISSING",
            f"来源类型 {source_type} 缺少必备证据：{missing}。"
            "来源不明或缺少该来源类型应有的合成/生成/登记证据时不能批准为生产根资产",
            {"source_type": source_type, "missing": missing, "subject": subject},
        )
    rec = SourceRecord(
        id=st.new_id("src"), subject=subject, source_type=source_type,  # type: ignore[arg-type]
        provider=provider, model=model, version=version,
        provider_run_id=provider_run_id, prompt_asset_id=prompt_asset_id, seed=seed,
        rights_evidence_asset_id=rights_evidence_asset_id,
        license_review_state="unreviewed",
        note=note or ("测试素材：始终为模拟，不得批准为生产根资产"
                      if source_type == "project_test_material" else ""),
    )
    st.put(SOURCE_TABLE, rec.id, rec.model_dump(), {"subject": subject})
    return rec.model_dump()


def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    return st.get(SOURCE_TABLE, source_id) if source_id else None


def list_sources(subject: Optional[str] = None) -> list:
    rows = st.all_rows(SOURCE_TABLE)
    return [r for r in rows if r.get("subject") == subject] if subject else rows


def mark_license_review(source_id: str, state: str, reviewer: str,
                        note: str = "") -> Dict[str, Any]:
    """登记权属核验结论。测试素材永远不能被核验成"可生产"。"""
    rec = get_source(source_id)
    if not rec:
        raise TtsRejected("SOURCE_NOT_FOUND", f"来源记录不存在：{source_id}")
    if state not in ("unreviewed", "pending", "reviewed", "blocked"):
        raise TtsRejected("SOURCE_REVIEW_STATE_INVALID", f"核验状态非法：{state}")
    if rec.get("source_type") == "project_test_material" and state == "reviewed":
        raise TtsRejected(
            "SOURCE_TEST_MATERIAL_NOT_APPROVABLE",
            "测试素材始终为模拟，不能核验为可用于生产",
            {"source_id": source_id},
        )
    rec["license_review_state"] = state
    rec["reviewed_at"] = now_iso()
    rec["reviewed_by"] = reviewer
    if note:
        rec["note"] = (rec.get("note", "") + "；" + note).strip("；")
    st.put(SOURCE_TABLE, source_id, rec, {"subject": rec.get("subject")})
    return rec


def source_ready(source_id: str) -> tuple:
    """来源是否可用于生产：必须存在、已核验、且不是测试素材。"""
    reasons = []
    rec = get_source(source_id)
    if not rec:
        return False, [f"来源记录不存在：{source_id}"]
    if rec.get("source_type") == "project_test_material":
        reasons.append("来源为测试素材（模拟），不得作为生产根资产")
    if rec.get("license_review_state") != "reviewed":
        reasons.append(f"权属核验未完成（{rec.get('license_review_state')}）")
    return (not reasons), reasons
