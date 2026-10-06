"""写作/审核共用的输入上下文与摘要：审核绑定和“审核是否仍有效”都以它为准。"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .. import storage as st
from . import contracts as cc
from . import repository as repo
from .errors import ContentRejected


def materials_for(brief: Dict[str, Any]) -> List[Dict[str, Any]]:
    """brief.materials → 带稳定 evidence_id 的事实资料。视频参考不在此列（它默认只是灵感）。"""
    out = []
    for m in (brief or {}).get("materials") or []:
        text = str(m.get("text", "") if isinstance(m, dict) else m).strip()
        if not text:
            continue
        h = (m.get("content_hash") if isinstance(m, dict) else "") or hashlib.sha256(text.encode("utf-8")).hexdigest()
        out.append({"evidence_id": "ev_" + h[:10], "text": text,
                    "source": (m.get("source", "") if isinstance(m, dict) else ""), "content_hash": h})
    return out


def brief_core(brief: Dict[str, Any]) -> Dict[str, Any]:
    b = brief or {}
    return {"topic": str(b.get("topic") or "").strip(), "audience": b.get("audience") or "",
            "content_route": b.get("content_route") or "talking_script",
            "content_type": b.get("content_type") or "", "keywords": list(b.get("keywords") or []),
            "target_duration_ms": int(b.get("target_duration_ms") or 0)}


def evidence_digest(materials: List[Dict[str, Any]]) -> str:
    return cc.digest([[m["evidence_id"], m["text"]] for m in materials])


def identity_snapshot(work: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the selected identity fields allowed in content prompts.

    The writer needs a stable, human-readable character anchor, while image
    bytes and asset paths stay in the later Wan task graph.  Keeping the
    snapshot small also prevents unrelated registry metadata from becoming a
    hidden writing input.
    """
    identity_id = str((work or {}).get("identity_id") or "")
    if not identity_id:
        return None
    row = st.get("identities", identity_id)
    if row and row.get("deleted_at"):
        raise ContentRejected("人物已删除，不能继续制作", "IDENTITY_DELETED")
    if not row:
        return {"identity_id": identity_id, "name": "", "persona": "", "keywords": {},
                "identity_version": 0, "identity_digest": ""}
    return {"identity_id": identity_id, "name": str(row.get("name") or ""),
            "persona": str(row.get("persona") or ""), "keywords": row.get("keywords") or {},
            "identity_version": int(row.get("identity_version") or 1),
            "identity_digest": str(row.get("identity_digest") or "")}


def identity_digest(identity: Optional[Dict[str, Any]]) -> str:
    return cc.digest(identity or {})


def load_context(work_id: str, *, require_prompt: bool = False, require_reference: bool = False) -> Dict[str, Any]:
    work = st.get("works", work_id)
    if work is None:
        raise ContentRejected(f"作品不存在：{work_id}", "WORK_NOT_FOUND")
    if work.get("deleted_at"):
        raise ContentRejected("作品已删除，不能继续制作", "WORK_DELETED")
    brief = work.get("brief") or {}
    materials = materials_for(brief)
    identity = identity_snapshot(work)
    reference = repo.get_reference(work.get("reference_snapshot_id") or "") if work.get("reference_snapshot_id") else None
    prompt = repo.get_prompt(work.get("writing_prompt_id") or "") if work.get("writing_prompt_id") else None
    if require_reference or (require_prompt and reference is not None):
        if reference is None:
            raise ContentRejected("请先选择一个可用的参考（口播稿需要有效转写，分镜可用画面分析）", "REFERENCE_REQUIRED")
        from .references import usable_for_route
        blocked = usable_for_route(reference, brief_core(brief)["content_route"])
        if blocked is not None:
            raise blocked
    if require_prompt and prompt is None:
        raise ContentRejected("请先生成并确认写作提示", "PROMPT_REQUIRED")
    style = work.get("style_snapshot") or None
    style_digest = (style or {}).get("digest") or (cc.style_digest(style) if style else "")
    return {
        "work": work, "brief": brief, "brief_core": brief_core(brief), "materials": materials,
        "reference": reference, "prompt": prompt, "style": style, "identity": identity,
        "digests": {
            "reference": reference["content_digest"] if reference else "",
            "evidence": evidence_digest(materials), "brief": cc.digest(brief_core(brief)),
            "style": style_digest, "identity": identity_digest(identity),
        },
        "content_revision": int(work.get("content_revision") or 0),
    }
