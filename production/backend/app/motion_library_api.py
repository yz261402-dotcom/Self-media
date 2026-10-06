"""真实动作参考库：预览后由用户选给另一人物，不复制人物身份审批。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import storage as st
from .avatar import licensing, motion, person_image, registry
from .contracts import MotionReference, now_iso

router = APIRouter(prefix="/api/v1")


def _reject(code: str, message: str, status: int = 409):
    raise HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


def _local_file(asset: dict) -> Path:
    """只读已登记资产；远端 URI 仅用于匹配受控动作目录，不发起下载。"""
    uri = str(asset.get("storage_uri") or "")
    path = person_image.resolve_media_path(uri)
    if path is None:
        parsed = urlsplit(uri)
        # 历史驱动登记为 TOS URI，本地已保存同名副本。名字只是定位线索，
        # 文件必须再与该资产登记的 SHA256 完全一致，不能换用别的同名视频。
        if parsed.scheme == "https" and parsed.netloc:
            name = unquote(parsed.path.rsplit("/", 1)[-1])
            if name and not any(c in name for c in ("/", "\\", ":")):
                path = person_image.resolve_media_path(f"assets/动作参考/{name}")
    if not path or Path(path).suffix.lower() != ".mp4" or not Path(path).is_file():
        _reject("MOTION_FILE_MISSING", "动作参考的本地视频缺失，暂时无法预览或选用")
    expected = str(asset.get("file_hash") or "")
    try:
        with Path(path).open("rb") as stream:
            digest = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        _reject("MOTION_FILE_UNREADABLE", "动作参考文件暂时无法读取")
    if digest != expected:
        _reject("MOTION_HASH_MISMATCH", "动作文件与已登记版本不一致，不能沿用旧审核")
    return Path(path)


def _source(reference_id: str) -> tuple[dict, dict, Path]:
    ref = st.get("motion_references", reference_id)
    if not ref:
        _reject("MOTION_REF_NOT_FOUND", "动作模板不存在", 404)
    if ref.get("source_reference_id"):
        _reject("MOTION_SOURCE_DERIVED", "请选择动作库中的原始模板，不重复导入派生记录")
    if ref.get("approval_state") != "approved" or ref.get("is_mock"):
        _reject("MOTION_NOT_APPROVED", "动作参考尚未获准或属于模拟素材，不能选用")
    if ref.get("motion_key") not in motion.MOTION_CATALOG:
        _reject("MOTION_UNSUPPORTED", "当前 M2 尚不支持该动作类型")
    asset = registry.get_asset(ref.get("asset_id") or "")
    if not asset or asset.get("kind") != "motion":
        _reject("MOTION_ASSET_MISSING", "动作模板未绑定有效的动作资产")
    if asset.get("is_mock") or not str(asset.get("source_evidence") or "").strip():
        _reject("MOTION_SOURCE_UNVERIFIED", "动作素材必须是真实文件并有可追溯来源")
    # 动作 approval 由 motion_reference 表达。历史 motion asset 为 pending
    # 不等于人物身份审核失败，也不能为了复用动作把它改成 approved。
    if asset.get("approval_state") == "rejected":
        _reject("MOTION_ASSET_REJECTED", "动作来源资产已被拒绝，不能选用")
    if asset.get("identity_id") and asset["identity_id"] != ref.get("identity_id"):
        _reject("MOTION_SOURCE_MISMATCH", "动作参考与其登记来源不一致")
    for license_id in {ref.get("license_id"), asset.get("license_id")} - {None, ""}:
        try:
            licensing.require(license_id, subject=ref.get("label") or "动作参考")
        except licensing.LicenseBlocked as exc:
            _reject(exc.code, exc.message)
    return ref, asset, _local_file(asset)


def _note(ref: dict, asset: dict) -> str:
    parts = [ref.get("note") or "", asset.get("source_evidence") or "",
             "仅迁移动作，不采用参考人物的脸；新主播底片仍须检查身份与动作。"]
    if not ref.get("license_id") and not asset.get("license_id"):
        parts.append("原参考未单独登记动作许可，沿用已有参考的使用限制；不授予新人物商用许可。")
    return "；".join(part.strip("；") for part in parts if part)


@router.get("/motion-templates")
def list_motion_templates():
    templates = []
    for ref in st.all_rows("motion_references"):
        # catalog 是支持能力清单，不是素材；只有真实登记且已获准的原始参考
        # 才有卡片。文件缺失或许可变化时保留卡片并说明当前不可用原因。
        if ref.get("source_reference_id") or ref.get("approval_state") != "approved" or ref.get("is_mock"):
            continue
        asset = registry.get_asset(ref.get("asset_id") or "") or {}
        if asset.get("is_mock"):
            continue
        unavailable_reason = ""
        try:
            _source(ref["id"])
        except HTTPException as exc:
            unavailable_reason = exc.detail["error"]["message"]
        source_identity = registry.get_identity(ref.get("identity_id") or "") or {}
        templates.append({
            "id": ref["id"], "label": ref.get("label") or "动作参考",
            "description": ref.get("description") or "", "motion_key": ref.get("motion_key"),
            "loop_seconds": ref.get("loop_seconds"),
            "preview_url": None if unavailable_reason else f'/api/v1/motion-templates/{ref["id"]}/preview',
            "source_identity_name": source_identity.get("name") or "公共动作参考",
            "note": _note(ref, asset), "available": not unavailable_reason,
            "unavailable_reason": unavailable_reason,
        })
    return {"templates": templates}


@router.get("/motion-templates/{source_reference_id}/preview")
def preview_motion_template(source_reference_id: str):
    _, _, path = _source(source_reference_id)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.post("/identities/{identity_id}/motions/import")
def import_motion_template(identity_id: str, payload: dict):
    actor = str(payload.get("actor_id") or "")
    if not actor.startswith("user"):
        _reject("ACTOR_NOT_USER", "动作选用须由用户确认", 422)
    if payload.get("confirmed") is not True:
        _reject("MOTION_CONFIRMATION_REQUIRED", "请先预览并确认采用该动作", 422)
    source_reference_id = str(payload.get("source_reference_id") or "")
    # BEGIN IMMEDIATE 同时保护重复导入和审批变化。只插入目标人物的动作
    # 引用，不更新 source asset、identity、identity_review 或 work。
    with st.transaction() as conn:
        target = registry.get_identity(identity_id)
        if not target:
            _reject("IDENTITY_NOT_FOUND", "人物不存在", 404)
        if target.get("discarded_at"):
            _reject("IDENTITY_DISCARDED", "请先恢复人物，再选择动作")
        ref, asset, _ = _source(source_reference_id)
        if ref.get("identity_id") == identity_id:
            return ref
        existing = next((row for row in motion.list_references(identity_id)
                         if row.get("source_reference_id") == source_reference_id), None)
        if existing:
            if existing.get("approval_state") != "approved" or existing.get("asset_id") != asset["id"]:
                _reject("MOTION_IMPORT_CHANGED", "已导入动作的状态发生变化，请重新检查参考")
            try:
                motion.validate_imported_reference(existing)
            except registry.AssetRejected as exc:
                _reject(exc.code, exc.message)
            return existing
        at = now_iso()
        copied = MotionReference(
            id=st.new_id("mot"), identity_id=identity_id, asset_id=asset["id"],
            motion_key=ref["motion_key"], label=ref["label"], description=ref.get("description") or "",
            supported_params=ref.get("supported_params") or {}, approval_state="approved",
            loop_seconds=ref.get("loop_seconds"), license_id=ref.get("license_id"),
            note=f'{_note(ref, asset)}；{actor} 于 {at} 确认从 {source_reference_id} 选用该动作',
            source_reference_id=source_reference_id, source_asset_hash=asset["file_hash"],
            confirmed_by=actor, confirmed_at=at,
        ).model_dump()
        st.insert_row(conn, "motion_references", copied["id"], copied, {"identity_id": identity_id})
    return copied
