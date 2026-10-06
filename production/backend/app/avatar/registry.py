"""人物资产受控登记（AC-AV-01）。

规则：登记必须带原图哈希与来源证据；缺图、哈希不符、来源不清均阻止生产；
冻结身份不可被覆盖，显式换人必须产生新版本并重新验收；Mock 后代不得进入生产。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from .. import storage as st
from ..contracts import (HERO_ONLY_LIMITATIONS, ApprovalState, AvatarAsset, AvatarIdentity,
                         IdentityReferencePolicy, now_iso)
from . import licensing

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{16,}$")


class AssetRejected(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def digest_of(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- 身份 ----------

def get_identity(identity_id: str) -> Optional[Dict[str, Any]]:
    return st.get("identities", identity_id) if identity_id else None


def list_identities() -> List[Dict[str, Any]]:
    return st.all_rows("identities")


def create_identity_candidate(name: str, persona: str, keywords: Optional[Dict[str, Any]] = None,
                              note: str = "", origin: str = "captured") -> Dict[str, Any]:
    """首次新建人物的候选。候选一律 pending 且未冻结，必须经审核定版。

    `origin="generated"` 表示该人脸由生成服务凭空产出，天生不存在多角度真值；
    这只是一个事实标记，不构成任何放宽——能否走受限路径仍须人显式声明政策。
    """
    ident = AvatarIdentity(
        id=st.new_id("av"),
        name=name,
        persona=persona,
        hero_asset_id="",
        identity_digest="",
        approval_state="pending",
        is_preset=False,
        note=note or "候选：待真实生成、登记参考集并通过身份审核后定版",
        identity_origin=origin,
        # keywords 必须在这里进模型，不能事后塞进 dict 绕开校验。
        # 绕开的结果是：写入成功、读取时才 extra_forbidden，且表现为整表端点 500
        # （曾导致前端卡在"正在连接后端"）——故障离根因很远，很难查。
        keywords=keywords or {},
    )
    row = ident.model_dump()
    st.put("identities", ident.id, row)
    return row


def set_reference_policy(identity_id: str, mode: str, operator: str, reason: str = "",
                         acknowledged: Optional[List[str]] = None) -> Dict[str, Any]:
    """声明这个身份用哪套参考依据。**只能由人扳的开关**。

    `hero_only` 是受限路径：承认"没有多角度真值"，用它换掉"至少 4 张参考图"的要求。
    代价是把一致性判断压到每次出片的人工对照上——所以限制必须逐条确认，
    系统不得自行推断某个身份该走哪条路（推断等于替人选证据口径）。
    """
    ident = get_identity(identity_id)
    if not ident:
        raise AssetRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    if mode not in ("multi_view", "hero_only"):
        raise AssetRejected("REFERENCE_POLICY_INVALID", f"参考集政策非法：{mode}")
    # 与 T04/T05 同源口径：凡涉及"人来判断"的确认，operator 必须是人
    if not str(operator or "").startswith("user"):
        raise AssetRejected("ACTOR_NOT_USER", "参考集政策只能由人声明")
    if ident.get("frozen"):
        raise AssetRejected("IDENTITY_FROZEN", "身份已定版，参考依据口径不得在定版后变更")

    ack = list(acknowledged or [])
    if mode == "hero_only":
        missing = [x for x in HERO_ONLY_LIMITATIONS if x not in ack]
        if missing:
            raise AssetRejected("REFERENCE_POLICY_NOT_ACKNOWLEDGED",
                                "未确认受限路径的限制：" + "；".join(missing))
        if not (reason or "").strip():
            raise AssetRejected("REFERENCE_POLICY_REASON_MISSING",
                                "须写明为何这个身份拿不到多角度参考图")

    policy = IdentityReferencePolicy(mode=mode, declared_by=operator, declared_at=now_iso(),
                                     reason=reason, acknowledged=ack)
    ident["reference_policy"] = policy.model_dump()
    st.put("identities", identity_id, ident)
    return ident


def freeze_identity(identity_id: str, hero_asset_id: str, identity_digest: str,
                    reviewer: str, license_id: Optional[str] = None) -> Dict[str, Any]:
    """定版冻结。冻结后 hero/参考资产不可被覆盖，换人必须走新版本。"""
    ident = get_identity(identity_id)
    if not ident:
        raise AssetRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    hero = get_asset(hero_asset_id)
    if not hero:
        raise AssetRejected("ASSET_NO_FILE", "缺少定妆原图资产，无法冻结身份")
    if hero.get("identity_id") != identity_id:
        raise AssetRejected("ASSET_IDENTITY_MISMATCH", "原图资产不属于该人物")
    if hero.get("kind") != "hero":
        raise AssetRejected("ASSET_KIND_MISMATCH", f"原片必须是 hero 资产，当前为 {hero.get('kind')}")
    _require_hash(hero)
    if not identity_digest:
        raise AssetRejected("IDENTITY_BASE_MISSING", "缺少身份基准摘要")
    # 参考依据口径必须在定版**之前**定下来：定版后再改等于换证据标准。
    if not ident.get("reference_policy"):
        raise AssetRejected("REFERENCE_POLICY_MISSING",
                            "定版前须先声明参考集政策：multi_view（多角度）或 hero_only（受限，仅原图为锚）")
    # 未经任何身份审核的原片不得作为定版依据（选择审批不等于身份审核）
    if not st.all_rows("identity_reviews", "asset_id=?", [hero_asset_id]):
        raise AssetRejected("ASSET_NOT_REVIEWED",
                            "定妆原片尚无身份审核记录，不得冻结身份")
    if license_id:
        # 定版登记与生产准入分离：此处只挡明确不可商用，准入时再要求可商用
        licensing.assert_registrable(license_id, subject=ident.get("name"))

    # 兼容早期写入的预设记录：T03 字段缺失时补齐，避免下游读到 None
    ident.setdefault("identity_version", 1)
    ident.setdefault("reference_asset_ids", [])
    ident["hero_asset_id"] = hero_asset_id
    ident["identity_digest"] = identity_digest
    ident["frozen"] = True
    ident["approval_state"] = "approved"
    ident["license_id"] = license_id
    ident["base_approved_at"] = now_iso()
    ident["reviewed_by"] = reviewer
    st.put("identities", identity_id, ident)
    return ident


def bump_identity_version(identity_id: str) -> Dict[str, Any]:
    """显式换人：产生新版本并清空审定状态，旧作品仍引用原版本。"""
    ident = get_identity(identity_id)
    if not ident:
        raise AssetRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    ident["identity_version"] = int(ident.get("identity_version", 1)) + 1
    ident["frozen"] = False
    ident["approval_state"] = "pending"
    ident["base_approved_at"] = None
    ident["hero_asset_id"] = ""
    ident["identity_digest"] = ""
    ident["reference_asset_ids"] = []
    ident["note"] = "显式换人产生新版本，必须重新登记原图与参考集并重新验收"
    st.put("identities", identity_id, ident)
    return ident


# ---------- 资产 ----------

def get_asset(asset_id: str) -> Optional[Dict[str, Any]]:
    return st.get("avatar_assets", asset_id) if asset_id else None


def list_assets(identity_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = st.all_rows("avatar_assets", "identity_id=?", [identity_id])
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows


def _require_hash(asset: Dict[str, Any]) -> None:
    h = asset.get("file_hash") or ""
    if not h:
        raise AssetRejected("ASSET_HASH_MISSING", f"资产缺少文件哈希，来源不可追溯：{asset.get('id')}")
    if not _HASH_RE.match(h):
        raise AssetRejected("ASSET_HASH_INVALID", f"文件哈希格式不合法：{h}")


def register_asset(identity_id: str, kind: str, storage_uri: str = "", file_hash: str = "",
                   source_evidence: str = "", is_mock: bool = False, license_id: Optional[str] = None,
                   supersedes_id: Optional[str] = None, note: str = "",
                   width: Optional[int] = None, height: Optional[int] = None,
                   asset_id: Optional[str] = None) -> Dict[str, Any]:
    """登记资产。缺图、哈希缺失、来源不清一律拒绝登记为可用资产。

    `asset_id` 供"文件已经写好、文件名就是资产 id"的场景使用（如 AI 生成主图：
    先落盘再登记）。不给时仍然自动生成，行为不变。
    """
    ident = get_identity(identity_id)
    if not ident:
        raise AssetRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    if not storage_uri:
        raise AssetRejected("ASSET_NO_FILE", "缺图：未提供存储位置，资产不完整")
    if not file_hash:
        raise AssetRejected("ASSET_HASH_MISSING", "缺少原图哈希，无法追溯文件是否被替换")
    if not _HASH_RE.match(file_hash):
        raise AssetRejected("ASSET_HASH_INVALID", f"文件哈希格式不合法：{file_hash}")
    if not source_evidence:
        raise AssetRejected("ASSET_SOURCE_UNCLEAR", "来源不清：未登记来源与权属证据")
    if license_id:
        # 登记保留来源链：待核验权属可以登记，但不能借此进入生产
        licensing.assert_registrable(license_id, subject=f"{ident.get('name')}/{kind}")

    # 冻结身份的原片不可新增替换：定妆照唯一，换人必须走新版本。
    # 参考集允许继续补齐（AC-AV-01 要求至少 4 张），但已定版参考的文件不可被替换。
    if ident.get("frozen") and kind == "hero":
        existing = [a for a in list_assets(identity_id, "hero")
                    if a.get("approval_state") == "approved" and not a.get("supersedes_id")]
        if existing and not supersedes_id:
            raise AssetRejected(
                "IDENTITY_FROZEN",
                "身份已冻结，定妆原片不可覆盖；显式换人须产生新版本并重新验收",
                {"identity_id": identity_id, "existing": [a["id"] for a in existing]},
            )
    prior = get_asset(supersedes_id) if supersedes_id else None
    asset = AvatarAsset(
        id=asset_id or st.new_id("img"),
        identity_id=identity_id,
        kind=kind,
        version=int(prior["version"]) + 1 if prior else 1,
        storage_uri=storage_uri,
        file_hash=file_hash,
        width=width,
        height=height,
        source_evidence=source_evidence,
        license_id=license_id,
        is_mock=is_mock,
        supersedes_id=supersedes_id,
        approval_state="pending",
        note=note,
    )
    st.put("avatar_assets", asset.id, asset.model_dump(), {"identity_id": identity_id})
    if kind == "reference" and asset.id not in (ident.get("reference_asset_ids") or []):
        ident.setdefault("reference_asset_ids", []).append(asset.id)
        st.put("identities", identity_id, ident)
    return asset.model_dump()


def replace_asset_file(asset_id: str, storage_uri: str, file_hash: str,
                       source_evidence: Optional[str] = None) -> Dict[str, Any]:
    """文件被替换：版本递增并重置审核，旧审核摘要随之失效。"""
    asset = get_asset(asset_id)
    if not asset:
        raise AssetRejected("ASSET_NOT_FOUND", f"资产不存在：{asset_id}")
    _require_hash({"id": asset_id, "file_hash": file_hash})
    ident = get_identity(asset.get("identity_id", ""))
    if ident and ident.get("frozen") and asset.get("approval_state") == "approved":
        raise AssetRejected("IDENTITY_FROZEN", "已定版且冻结的资产不可被替换文件")
    asset["storage_uri"] = storage_uri
    asset["file_hash"] = file_hash
    if source_evidence:
        asset["source_evidence"] = source_evidence
    asset["version"] = int(asset.get("version", 1)) + 1
    asset["approval_state"] = "pending"
    asset["updated_at"] = now_iso()
    st.put("avatar_assets", asset_id, asset, {"identity_id": asset.get("identity_id")})
    return asset


def verify_hash(asset_id: str, expected_hash: str) -> Tuple[bool, str]:
    asset = get_asset(asset_id)
    if not asset:
        return False, f"资产不存在：{asset_id}"
    if asset.get("file_hash") != expected_hash:
        return False, "哈希不符：文件已被替换，不得继续生产"
    return True, ""


# ---------- 生产准入 ----------

def production_ready(identity_id: str) -> Tuple[bool, List[str]]:
    """AC-AV-01：缺图、哈希不符、来源不清、Mock、权属未确认均阻止生产。"""
    reasons: List[str] = []
    ident = get_identity(identity_id)
    if not ident:
        return False, [f"人物不存在：{identity_id}"]
    if ident.get("deleted_at"):
        return False, ["人物已删除，不能继续制作"]
    if ident.get("approval_state") != "approved":
        reasons.append("人物未定版")
    if not ident.get("frozen"):
        reasons.append("身份未冻结")
    if not ident.get("identity_digest"):
        reasons.append("缺少身份基准摘要")

    hero = get_asset(ident.get("hero_asset_id", ""))
    if not hero:
        reasons.append("缺少定妆原片资产")
    else:
        if hero.get("kind") != "hero":
            reasons.append("原片资产种类不是 hero")
        if not hero.get("storage_uri"):
            reasons.append("原片缺图")
        if not hero.get("file_hash"):
            reasons.append("原片缺少哈希")
        elif not _HASH_RE.match(hero.get("file_hash", "")):
            reasons.append("原片哈希格式不合法")
        if not hero.get("source_evidence"):
            reasons.append("原片来源不清")
        if hero.get("is_mock"):
            reasons.append("原片为 Mock 后代，不得进入生产")
        if hero.get("approval_state") != "approved":
            reasons.append("原片未通过身份审核")
        if not _current_review(hero):
            reasons.append("原片缺少与当前版本和哈希匹配的审核结论")
        if not hero.get("license_id"):
            reasons.append("原片缺少权属记录，商业使用范围不可追溯")
        else:
            try:
                licensing.require(hero["license_id"], subject=f"{ident.get('name')}/hero")
            except licensing.LicenseBlocked as e:
                reasons.append(e.message)

    # 参考图的作用取决于已声明的政策口径，两条路的证据要求不同。
    policy = ident.get("reference_policy") or {}
    mode = policy.get("mode") or ""
    refs = [a for a in list_assets(identity_id, "reference")]
    usable_refs = [a for a in refs if _current_review(a) and not a.get("is_mock")]
    if not policy:
        reasons.append("参考集政策未声明：须先由人声明走多角度还是受限路径")
    elif mode == "hero_only":
        missing = [x for x in HERO_ONLY_LIMITATIONS if x not in (policy.get("acknowledged") or [])]
        if missing:
            reasons.append("受限路径的限制未逐条确认：" + "；".join(missing))
        if not str(policy.get("declared_by", "")).startswith("user"):
            reasons.append("受限路径须由人声明，不接受系统推断")
        # 已有的参考图如果存在，就必须都审过——不能留一批没审的混在其中
        if len(usable_refs) != len(refs):
            reasons.append(f"存在未通过审核的参考图 {len(refs) - len(usable_refs)} 张")
    else:
        if len(usable_refs) < 4:
            reasons.append(f"审核通过的参考图不足 4 张（当前 {len(usable_refs)}）")

    # AC-AV-04：权属未确认即阻塞。缺记录本身就是未确认，不能默认放行。
    if not ident.get("license_id"):
        reasons.append("身份缺少权属记录，商业使用范围不可追溯")
    else:
        try:
            licensing.require(ident["license_id"], subject=ident.get("name"))
        except licensing.LicenseBlocked as e:
            reasons.append(e.message)

    # 同一许可可能同时挂在身份与原片上，原因列表去重，便于前端直接展示
    reasons = list(dict.fromkeys(reasons))
    return (not reasons), reasons


def qualify_steps(identity_id: str) -> Dict[str, Any]:
    """定版链的进度：走到第几步、卡在哪一步、这一步为什么需要人来做。

    存在的理由：界面上「选用」与「审核 / 定版」是两个并排的按钮，用户看不出
    二者是"前提"与"引用"的关系，也看不出四步里自己停在哪。因此把顺序、完成状态
    与"为什么需要这一步"统一由后端给出，前端只渲染、不自己另编一套说法——
    否则界面与实际门禁会各说各话，这正是之前"看起来能做、点了就被拒"的来源。

    「选用」（`POST /works/{id}/identity`）只是把已定版的人绑到作品上，
    未定版一律拒绝；所以这四个步骤是选用的**前提**，不是并列的另一条路。
    """
    ident = get_identity(identity_id) or {}
    hero = get_asset(ident.get("hero_asset_id", "")) or {}
    policy = ident.get("reference_policy") or {}

    # 第 1 步：原片身份审核。审核摘要绑定资产版本与哈希，图一变结论即失效。
    reviewed = bool(hero) and not hero.get("is_mock") and _current_review(hero) is not None

    # 第 2 步：参考依据口径。受限路径还要逐条确认代价并由人写明理由。
    policy_ok = bool(policy)
    if policy_ok and policy.get("mode") == "hero_only":
        missing = [x for x in HERO_ONLY_LIMITATIONS if x not in (policy.get("acknowledged") or [])]
        policy_ok = (not missing
                     and bool(str(policy.get("reason") or "").strip())
                     and str(policy.get("declared_by") or "").startswith("user"))

    # 第 3 步：商用范围人工确认。blocked 不可翻案，因此同样算未完成。
    rec = licensing.get(ident.get("license_id") or "")
    attested = bool(rec) and rec.get("scope") == "commercial_ok"

    frozen = bool(ident.get("frozen")) and ident.get("approval_state") == "approved"

    steps = [
        {"key": "review", "label": "原片身份审核", "done": reviewed,
         "why": "系统不做人脸比对，只能由人目检原图并留下核对方法；结论绑定这张图的哈希，换图即失效。"},
        {"key": "reference_policy", "label": "声明参考依据口径", "done": policy_ok,
         "why": "定版前必须定下「拿什么当是同一个人」。定版后不许再改，所以只能现在声明。"},
        {"key": "license_attest", "label": "商用范围确认", "done": attested,
         "why": "由人确认这一版权属记录可用于商用并写明依据；系统不做法律判断。"},
        {"key": "freeze", "label": "冻结定版", "done": frozen,
         "why": "把这一版焊死：原图不可替换，此后任何作品都能直接选它；要换人只能新建版本。"},
    ]
    done_count = sum(1 for s in steps if s["done"])
    # 冻结即链路闭合：未完成的步骤此后**不可能**再补（冻结后口径不可再声明、
    # 定版后不接受新的基准）。因此此时不给"下一步"——否则界面会指向一个
    # 点了必定被拒的动作，那比不给提示更糟。留痕不全是早期预设直接冻结造成的，
    # 如实标记，供人判断是否要新建版本重走一遍。
    closed_incomplete = frozen and done_count != len(steps)
    return {
        "steps": steps,
        "done_count": done_count,
        "total": len(steps),
        "current": None if frozen else next((s["key"] for s in steps if not s["done"]), None),
        "frozen": frozen,
        "closed_incomplete": closed_incomplete,
    }


def require_detector_license(subject: str = "身份检测模型") -> Dict[str, Any]:
    """启用检测 Provider 前必须核验权重商用许可（AC-AV-04）。"""
    return licensing.require_weights(subject)


def require_production(identity_id: str) -> Dict[str, Any]:
    ok, reasons = production_ready(identity_id)
    if not ok:
        raise AssetRejected("ASSET_NOT_PRODUCTION_READY", "；".join(reasons),
                            {"identity_id": identity_id, "reasons": reasons})
    return get_identity(identity_id)


# ---------- 草稿候选的丢弃（可恢复，非物理删除） ----------

class DiscardRejected(Exception):
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _discard_blockers(identity_id: str, ident: Dict[str, Any]) -> List[str]:
    """列出"为什么不能丢弃"，空列表表示可丢弃。

    丢弃只是把人移出工作列表，**不删记录**；但一旦被下游引用，丢它会让引用悬空。
    这里宁可挡住并说清原因，也不做级联清理——清理会静默改变别人已确认的东西。
    """
    reasons: List[str] = []
    if ident.get("frozen"):
        reasons.append("身份已冻结：定版是对外承诺，换人须新建版本，不能丢弃")
    if ident.get("approval_state") == "approved":
        reasons.append("身份已定版：换人须新建版本，不能丢弃")

    users = [w for w in st.all_rows("works") if w.get("identity_id") == identity_id]
    if users:
        ids = "、".join(str(w.get("id") or "?") for w in users[:5])
        reasons.append(f"已被 {len(users)} 个作品引用：{ids}" + ("…" if len(users) > 5 else ""))

    variants = [v for v in st.all_rows("variants") if v.get("identity_id") == identity_id]
    if variants:
        reasons.append(f"已有 {len(variants)} 条变体建立在它之上，丢弃会让变体失去归属")

    refs = [m for m in st.all_rows("motion_references") if m.get("identity_id") == identity_id]
    if refs:
        reasons.append(f"已有 {len(refs)} 条动作参考挂在它上面")

    hero = get_asset(ident.get("hero_asset_id", ""))
    if hero and _current_review(hero):
        reasons.append("原片已有身份审核结论，丢弃会留下无归属的已审资产")
    return reasons


def discard_identity(identity_id: str, actor: str, reason: str = "") -> Dict[str, Any]:
    """把草稿候选移出工作列表。幂等：已丢弃的再丢一次直接返回当前状态。

    只有人能丢弃（actor 须以 user 开头）——"这个候选我不要了"是人的判断，
    系统不得替人下这个结论。原字段一字不改，只追加丢弃元数据与历史事件。
    """
    ident = get_identity(identity_id)
    if not ident:
        raise DiscardRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    if ident.get("deleted_at"):
        raise DiscardRejected("IDENTITY_DELETED", "人物已删除，不能继续丢弃")
    if not str(actor or "").startswith("user"):
        raise DiscardRejected("ACTOR_NOT_HUMAN", "丢弃候选须由人操作（操作者须以 user 开头）")
    if not str(reason or "").strip():
        raise DiscardRejected("REASON_REQUIRED", "丢弃须写明理由，便于日后回溯")
    if ident.get("discarded_at"):
        return ident

    blockers = _discard_blockers(identity_id, ident)
    if blockers:
        raise DiscardRejected("IDENTITY_NOT_DISCARDABLE", "；".join(blockers),
                              {"reasons": blockers})

    at = now_iso()
    ident["discarded_at"] = at
    ident["discarded_by"] = actor
    ident["discard_reason"] = reason.strip()
    ident["discard_history"] = list(ident.get("discard_history") or []) + [
        {"action": "discard", "actor": actor, "at": at, "reason": reason.strip()}]
    st.put("identities", identity_id, ident)
    return ident


def restore_identity(identity_id: str, actor: str) -> Dict[str, Any]:
    """把已丢弃的候选放回工作列表。历史事件只追加，不改写也不抹掉原丢弃理由。"""
    ident = get_identity(identity_id)
    if not ident:
        raise DiscardRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    if ident.get("deleted_at"):
        raise DiscardRejected("IDENTITY_DELETED", "人物已删除，不能通过恢复候选重新启用")
    if not str(actor or "").startswith("user"):
        raise DiscardRejected("ACTOR_NOT_HUMAN", "恢复候选须由人操作（操作者须以 user 开头）")
    if not ident.get("discarded_at"):
        return ident

    at = now_iso()
    ident["discard_history"] = list(ident.get("discard_history") or []) + [
        {"action": "restore", "actor": actor, "at": at,
         "reason": f"恢复（原丢弃理由：{ident.get('discard_reason') or '未记录'}）"}]
    ident["discarded_at"] = None
    ident["discarded_by"] = ""
    ident["discard_reason"] = ""
    st.put("identities", identity_id, ident)
    return ident


# ---------- 审核 ----------

def _current_review(asset: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """审核摘要绑定资产版本与文件哈希：任一变化后旧审核即失效。"""
    rows = st.all_rows("identity_reviews", "asset_id=?", [asset.get("id")])
    valid = [r for r in rows
             if r.get("asset_version") == asset.get("version")
             and r.get("file_hash") == asset.get("file_hash")
             and r.get("outcome") == "pass"]
    if not valid:
        return None
    return sorted(valid, key=lambda r: r.get("reviewed_at", ""))[-1]


def current_review(asset_id: str) -> Optional[Dict[str, Any]]:
    asset = get_asset(asset_id)
    if not asset:
        return None
    return _current_review(asset)


def record_review(asset_id: str, reviewer: str, outcome: str, identity_digest: str,
                  method: str = "", calibration_id: Optional[str] = None, note: str = "") -> Dict[str, Any]:
    from ..contracts import IdentityReview

    asset = get_asset(asset_id)
    if not asset:
        raise AssetRejected("ASSET_NOT_FOUND", f"资产不存在：{asset_id}")
    if outcome not in ("pass", "fail", "review", "not_run"):
        raise AssetRejected("REVIEW_OUTCOME_INVALID", f"审核结论非法：{outcome}")
    rev = IdentityReview(
        id=st.new_id("rev"),
        asset_id=asset_id,
        reviewer=reviewer,
        outcome=outcome,
        asset_version=int(asset.get("version", 1)),
        file_hash=asset.get("file_hash", ""),
        identity_digest=identity_digest,
        method=method,
        calibration_id=calibration_id,
        note=note,
    )
    st.put("identity_reviews", rev.id, rev.model_dump(), {"asset_id": asset_id})
    if outcome == "pass":
        asset["approval_state"] = "approved"
    elif outcome == "fail":
        asset["approval_state"] = "rejected"
    else:
        asset["approval_state"] = "pending"
    st.put("avatar_assets", asset_id, asset, {"identity_id": asset.get("identity_id")})
    return rev.model_dump()
