"""复用已存在的真实人物原图，建立独立的待审核草稿。"""
import hashlib
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException
from PIL import Image

from . import storage as st
from .avatar import licensing, person_image, registry
from .contracts import AvatarAsset, AvatarIdentity, LicenseRecord, now_iso

router = APIRouter(prefix="/api/v1")


def _reject(code, message, status=409):
    raise HTTPException(status_code=status, detail={"error": {"code": code, "message": message}})


@router.post("/identities/{identity_id}/draft-copy", response_model=AvatarIdentity)
def copy_identity_draft(identity_id: str, payload: dict):
    actor = str(payload.get("actor_id") or "")
    if not actor.startswith("user"):
        _reject("ACTOR_NOT_USER", "复制草稿须由用户操作", 422)
    original = registry.get_identity(identity_id)
    if not original:
        _reject("IDENTITY_NOT_FOUND", "人物不存在", 404)
    if original.get("deleted_at"):
        _reject("IDENTITY_DELETED", "人物已删除，不能复制草稿", 404)
    hero = registry.get_asset(original.get("hero_asset_id") or "")
    if not hero:
        candidates = registry.list_assets(identity_id, "hero")
        if len(candidates) == 1:
            hero = candidates[0]
    if not hero or hero.get("kind") != "hero" or hero.get("identity_id") != identity_id:
        _reject("HERO_REQUIRED", "缺少可唯一确定的定妆原图，请先补齐原图资产")
    if hero.get("is_mock") or not hero.get("source_evidence"):
        _reject("REAL_HERO_REQUIRED", "草稿必须来自有来源记录的真实原图，模拟资产不能复制")
    path = person_image.resolve_media_path(hero.get("storage_uri") or "")
    if not path or not Path(path).is_file():
        _reject("HERO_FILE_MISSING", "原图文件不存在，不能创建空草稿")
    try:
        raw = Path(path).read_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest != hero.get("file_hash"):
            _reject("HERO_HASH_MISMATCH", "原图文件已变化，无法沿用旧资产记录")
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        _reject("HERO_IMAGE_INVALID", "原图无法作为有效图片读取")

    source_license = licensing.get(original.get("license_id") or hero.get("license_id") or "") or {}
    new_id = st.new_id("av")
    new_hero_id = st.new_id("img")
    at = now_iso()
    license_record = LicenseRecord(
        id=st.new_id("lic"), subject=f"{original['name']} · 新草稿", kind="image_source",
        scope="blocked" if source_license.get("scope") == "blocked" else "unverified",
        evidence=(f"复制来源许可 {source_license.get('id') or '未登记'}；"
                  f"原始来源说明：{source_license.get('evidence') or hero['source_evidence']}"),
        note="新草稿须独立确认使用范围，不继承原人物的人工确认",
    )
    identity = AvatarIdentity(
        id=new_id, name=f"{original['name']} · 新草稿", persona=original.get("persona") or "",
        hero_asset_id=new_hero_id, identity_digest="", license_id=license_record.id,
        default_voice_asset_id=original.get("default_voice_asset_id"),
        identity_origin=original.get("identity_origin") or "captured",
        keywords=original.get("keywords") or {},
        note=f"{actor} 于 {at} 从 {identity_id} 复制；复用原图，需重新审核与定版",
    )
    asset = AvatarAsset(
        id=new_hero_id, identity_id=new_id, kind="hero", storage_uri=hero["storage_uri"],
        file_hash=digest, width=width, height=height, license_id=license_record.id,
        source_evidence=f"复制自人物 {identity_id} / 资产 {hero['id']}；{hero['source_evidence']}",
        note="仅复用图像文件，不继承审核、参考依据、动作或生产资格",
    )
    with st.transaction() as conn:
        if registry.get_identity(identity_id) != original or registry.get_asset(hero["id"]) != hero:
            _reject("SOURCE_CHANGED", "原人物资料已变化，请刷新后重试")
        st.insert_row(conn, "license_records", license_record.id, license_record.model_dump())
        st.insert_row(conn, "identities", new_id, identity.model_dump())
        st.insert_row(conn, "avatar_assets", new_hero_id, asset.model_dump(), {"identity_id": new_id})
    return identity
