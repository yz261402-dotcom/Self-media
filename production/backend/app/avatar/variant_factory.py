"""确定性变体生产：把 `compose.py` 的算法接成一条界面能调用的链路。

为什么需要这一层（而不是把算法直接挂到端点上）：
  · 规格 F02 要求"基于原人物参考图编辑衣景 → 人物一致性与画面审核 → 用户确认 →
    保存为同一人物下的新变体"；
  · 但 gpt-image 系列中转忽略 mask、整图重绘人脸（身份漂移，不满足 AC-AV-02），
    所以**唯一**能满足"身份零漂移"的变体来源是 `compose.py` 的确定性合成——
    人物像素 1:1 复制，硬保护区内最大通道差严格为 0。
  · `compose.py` 本身只是一组算法函数，此前没有任何 HTTP 入口：界面上的「生成变体」
    因此从来产不出合格资产，后面的底片必然卡 `VARIANT_NOT_APPROVED`。
    本模块补的就是这段接线，**不新增任何算法**。

三条不放松的约束（对应本模块的两类异常）：
  1. **mask 必须由人目视确认过**才能产出供定版/生产使用的变体；未确认只能产出候选预览。
     这是"条件不具备"→ `VariantBlocked`（409，可重试），不是"请求不成立"。
  2. **背景权属必须可商用**；未核验同样按 409 处理，人工确认权属后即可继续。
  3. 产物以 `kind="variant"` 登记，且来源链里**必须**写明派生自哪张定妆原图——
     受限身份只允许这一类确定性派生，另起一张脸属于换人，不能登记为变体。

本模块不做身份识别，也不声称产物"就是同一人物"。它保证的是可验证的数学事实：
硬保护区内与源图逐像素相同。整体身份结论仍由人工审核门禁承担。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Sequence

from .. import storage as st
from ..contracts import now_iso
from . import compose, licensing, person_image, registry
from .compose import ComposeRejected


class VariantBlocked(Exception):
    """当前条件不具备（等条件），不是请求本身不成立。→ HTTP 409 + retryable。

    与 T04 的 `TtsBlocked`、T05 的 `TalkingHeadBlocked` 同语义：
    重提不是误操作，等人工确认 / 权属核验 / 上游变化后即可继续。
    """

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None,
                 retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.retryable = retryable


MASK_REL_ROOT = "assets/遮罩"
VARIANT_REL_ROOT = "assets/变体"
PREVIEW_REL_DIRNAME = "候选"


def _media_url(uri: str) -> str:
    import urllib.parse

    return f"/api/v1/media?uri={urllib.parse.quote(uri)}" if uri else ""


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _max_channel_diff(check: Dict[str, Any]) -> Optional[int]:
    """硬保护区逐像素比对的最大通道差。

    取各区域 `max_channel_diff` 的最大值。**任一区域为 None（越界）时整体返回 None**——
    宁可显示"无法判定"，也不能把"没算出来"写成 0：那会把未知当成了零漂移。
    """
    diffs = [r.get("max_channel_diff") for r in (check.get("regions") or [])]
    if not diffs or any(d is None for d in diffs):
        return None
    return max(int(d) for d in diffs)


def _write_bytes(rel_dir: str, name: str, data: bytes) -> Dict[str, str]:
    """落盘到工作区并返回相对 URI。

    必须是**工作区相对路径**：`/api/v1/media` 明确拒绝绝对路径与 `..`，
    写成绝对路径的资产在界面上会变成永远加载不出来的图。
    """
    root = person_image.workspace_root()
    abs_dir = os.path.join(root, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    abs_path = os.path.join(abs_dir, name)
    with open(abs_path, "wb") as f:
        f.write(data)
    return {"storage_uri": f"{rel_dir}/{name}", "abs_path": abs_path, "file_hash": _sha(data)}


# ---------------------------------------------------------------- mask 草稿

def estimate_and_register_mask(*, identity_id: str, source_asset_id: str = "",
                               method: str = "bgmodel", alpha_source: str = "matting",
                               component_mode: str = "all",
                               hard_protect: Optional[Sequence[Sequence[int]]] = None,
                               origin: str = "algorithm_draft") -> Dict[str, Any]:
    """从定妆原图自动分割出 mask 草稿并登记。

    登记即 `state="draft"`：**不存在自动通过路径**，必须再由人目视确认
    （`confirm_mask`）才能用于定版产出。这里的 `usable` 只表示"没触发已知失效条件"。
    """
    ident = registry.get_identity(identity_id)
    if not ident:
        raise ComposeRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    src_id = source_asset_id or (ident.get("hero_asset_id") or "")
    if not src_id:
        raise ComposeRejected("ASSET_NOT_FOUND", "该人物尚无定妆原图，无法派生 mask")
    src = registry.get_asset(src_id)
    if not src:
        raise ComposeRejected("ASSET_NOT_FOUND", f"源资产不存在：{src_id}")
    if src.get("identity_id") != identity_id:
        raise ComposeRejected("ASSET_IDENTITY_MISMATCH", "源资产不属于该人物")
    if src.get("kind") != "hero":
        raise ComposeRejected(
            "ASSET_KIND_MISMATCH",
            f"mask 必须从定妆原图（hero）派生，当前资产种类为 {src.get('kind')}")
    if not src.get("storage_uri"):
        raise ComposeRejected("ASSET_NO_FILE", "源资产缺图，无法派生 mask")

    est = compose.estimate_mask(
        compose.read_asset_bytes(src["storage_uri"]), method,
        hard_protect=hard_protect, alpha_source=alpha_source,
        component_mode=component_mode,
    )

    # 先落盘再登记：文件名就是资产 id，追溯链不断（与音频、生成候选同一约定）。
    asset_id = st.new_id("img")
    saved = _write_bytes(f"{MASK_REL_ROOT}/{identity_id}", f"{asset_id}.png",
                         est["mask_alpha_png"])
    spec = compose.register_mask(
        identity_id=identity_id,
        source_asset_id=src_id,
        mask_binary_png=est["mask_binary_png"],
        mask_alpha_png=est["mask_alpha_png"],
        params=est["params"],
        origin=origin,
        storage_uri=saved["storage_uri"],
        asset_id=asset_id,
        source_evidence=(
            f"由定妆原图 {src_id} 自动分割派生（method={method}，"
            f"alpha_source={alpha_source}，component_mode={component_mode}）；"
            "算法草稿，未经人工目视确认，只能产出候选"
        ),
    )
    return {
        "mask_spec": spec,
        "asset": registry.get_asset(asset_id),
        "image_url": _media_url(saved["storage_uri"]),
        "metrics": est["metrics"],
        "problems": est["problems"],
        "usable": est["usable"],
        "pixel_size": est["pixel_size"],
        "precheck": est["precheck"],
        "state": spec["state"],
        "warnings": _mask_warnings(est, alpha_source, component_mode),
    }


def list_mask_specs(identity_id: str) -> List[Dict[str, Any]]:
    """列出该身份下的 mask 登记（含未确认的草稿），供界面选一条来确认或合成。"""
    rows = [r for r in st.all_rows("mask_specs") if r.get("identity_id") == identity_id]
    return sorted(rows, key=lambda r: r.get("created_at") or "")


def confirm_mask(*, mask_asset_id: str, reviewer: str, outcome: str,
                 note: str = "") -> Dict[str, Any]:
    """人工目视确认 mask。**不接受系统/agent 身份**。

    刻意不做的两件事：不给"指标全绿就自动通过"的路径（指标只能排除已知失效，
    不能证明 mask 正确）；不把确认写成"已核验身份"（确认的是蒙版边缘，
    不是"这就是同一个人"）。
    """
    if not str(reviewer or "").startswith("user"):
        raise VariantBlocked(
            "ACTOR_NOT_USER",
            "mask 目视确认必须由人给出：操作者须以 user 开头，系统不得自行核发",
            retryable=False,
        )
    return compose.confirm_mask(mask_asset_id, reviewer, outcome, note)


def _mask_warnings(est: Dict[str, Any], alpha_source: str, component_mode: str) -> List[str]:
    """把算法参数翻译成人要看的风险提示，不美化。

    这些提示的存在意义是让人在目视确认时知道**该重点看哪里**，
    而不是让系统替人下结论。
    """
    out: List[str] = ["自动 mask 只是草稿：未经人工目视确认，不得用于定版产出"]
    if est.get("problems"):
        out.append("自动分割已触发失效条件，必须人工判断是否可用：" + "；".join(est["problems"]))
    if alpha_source != "matting":
        out.append(
            "当前 alpha 来自羽化：羽化出的透明度与真实混合比例无关，细发丝处会恒接近不透明，"
            "换成深色/异色背景时轮廓外会留下一圈原背景灰晕；如要换深色背景请改用 matting"
        )
    if component_mode == "seed":
        out.append(
            "只保留了含人脸区的连通块（有损）：被丢弃的前景块必须逐个人工核对，"
            "否则衣物或配饰可能整块丢失"
        )
    edges = (est.get("params") or {}).get("border_touching_edges") or []
    if edges:
        out.append(
            "前景贴住画面边缘（" + "/".join(edges) + "）：这类素材把人物缩小置入场景时"
            "会露出源图裁断的断面，需用下移画框的方式规避"
        )
    return out


# ---------------------------------------------------------------- 合成变体

def compose_variant(*, identity_id: str, mask_asset_id: str, background_asset_id: str,
                    edge_mode: str = "unmix", person_scale: float = 1.0,
                    anchor: Optional[Sequence[float]] = None,
                    frame: Optional[Sequence[int]] = None,
                    allow_frame_reveal: bool = False,
                    outfit: str = "", background_label: str = "",
                    declared_style: str = "", commit: bool = True,
                    note: str = "") -> Dict[str, Any]:
    """确定性合成一个变体画面。

    `commit=True`（默认）：要求 mask 已人工确认，产物落盘并登记为 `kind="variant"` 资产。
    `commit=False`：不要求确认，只产出候选预览**不登记**资产，便于先看构图再决定。

    返回里同时带 `hard_protect_check`（硬保护区逐像素比对）与 `production_ready`，
    让界面能直接展示"哪块像素没被动过"，而不是只能说一句"算法合成的"。
    """
    ident = registry.get_identity(identity_id)
    if not ident:
        raise ComposeRejected("IDENTITY_NOT_FOUND", f"人物不存在：{identity_id}")
    hero_id = ident.get("hero_asset_id") or ""
    if not hero_id:
        raise ComposeRejected("ASSET_NOT_FOUND",
                              "该人物尚无定妆原图；变体只能由已定版的原图确定性派生")

    # 背景：先做人话版的"等条件"判断，再交给 compose 做硬校验。
    # 顺序有讲究——权属未核验是"等人确认"，不是"请求写错了"。
    bg = registry.get_asset(background_asset_id)
    if not bg:
        raise ComposeRejected("ASSET_NOT_FOUND", f"背景资产不存在：{background_asset_id}")
    if bg.get("kind") != "background":
        raise ComposeRejected("BACKGROUND_KIND_INVALID",
                              f"背景资产 kind={bg.get('kind')}，应为 background")
    if not bg.get("license_id"):
        raise VariantBlocked("BACKGROUND_LICENSE_MISSING",
                             "背景资产未登记权属记录，无法确认可商用；登记权属后即可继续")
    try:
        licensing.require(bg["license_id"], subject=f"背景资产 {bg['id']}", need_weights=False)
    except licensing.LicenseBlocked as e:
        raise VariantBlocked(e.code, e.message, e.detail)

    spec = compose.get_mask_spec(mask_asset_id)
    if not spec:
        raise ComposeRejected("MASK_NOT_FOUND", f"mask 未登记：{mask_asset_id}")

    if commit:
        ok, reasons = compose.mask_is_usable(spec["id"], hero_id)
        if not ok:
            raise VariantBlocked("MASK_NOT_CONFIRMED", "；".join(reasons),
                                 {"mask_id": spec["id"], "state": spec.get("state")})

    result = compose.compose(
        identity_id=identity_id, hero_asset_id=hero_id,
        mask_asset_id=spec["id"], background_asset_id=background_asset_id,
        require_confirmed=commit, edge_mode=edge_mode, person_scale=person_scale,
        anchor=anchor, frame=frame, allow_frame_reveal=allow_frame_reveal,
    )

    png = result["composed_png"]
    check = result.get("hard_protect_check") or {}
    base = {
        "identity_id": identity_id,
        "derived_from": hero_id,
        "mask_spec_id": spec["id"],
        "mask_state": spec.get("state"),
        "background_asset_id": background_asset_id,
        "edge_mode": edge_mode,
        "hard_protect_check": check,
        # 单独拎出来给界面直接显示：这两个数是"像素有没有被动过"的唯一可核对依据
        "pixel_identical": check.get("pixel_identical"),
        "max_channel_diff": _max_channel_diff(check),
        "frame": result.get("frame"),
        "identity_claim": result.get("identity_claim"),
        "identity_claim_note": result.get("identity_claim_note"),
        "mask_quality_warning": result.get("mask_quality_warning", ""),
        "production_ready": result.get("production_ready"),
        "candidate": result.get("candidate"),
        "blocking_reasons": result.get("blocking_reasons") or [],
        "pixel_size": result.get("pixel_size"),
        "output_sha256": result.get("output_sha256"),
        "registered": False,
        "warnings": _compose_warnings(result),
    }

    if not commit:
        preview_id = st.new_id("prev")
        saved = _write_bytes(f"{VARIANT_REL_ROOT}/{identity_id}/{PREVIEW_REL_DIRNAME}",
                             f"{preview_id}.png", png)
        base.update({
            "preview_id": preview_id,
            "storage_uri": saved["storage_uri"],
            "file_hash": saved["file_hash"],
            "image_url": _media_url(saved["storage_uri"]),
            "note": "候选预览：未登记为资产，不得作为变体使用；确认 mask 后再 commit 正式产出",
        })
        return base

    asset_id = st.new_id("img")
    saved = _write_bytes(f"{VARIANT_REL_ROOT}/{identity_id}", f"{asset_id}.png", png)
    evidence = (
        f"确定性合成：派生自定妆原图 {hero_id}；mask={spec['id']}"
        f"（{spec.get('state')}，确认人 {spec.get('confirmed_by') or '未记录'}，"
        f"确认时间 {spec.get('confirmed_at') or '未记录'}）；"
        f"背景资产 {background_asset_id}；edge_mode={edge_mode}；"
        f"硬保护区最大通道差={check.get('max_abs_diff')}"
        f"（0 表示逐像素相同）；输出 {result.get('output_sha256')}。"
        "本产物未做任何身份识别，身份一致性结论须由人工审核给出"
    )
    try:
        asset = registry.register_asset(
            identity_id=identity_id, kind="variant",
            storage_uri=saved["storage_uri"], file_hash=saved["file_hash"],
            source_evidence=evidence,
            license_id=ident.get("license_id"),
            width=(result.get("pixel_size") or [None, None])[0],
            height=(result.get("pixel_size") or [None, None])[1],
            note=(note or f"确定性合成变体（{edge_mode}）；"
                         f"{outfit or '未指定着装'}/{background_label or '未指定背景'}；"
                         "须经身份审核后才能定版"),
            asset_id=asset_id,
        )
    except registry.AssetRejected as e:
        # 登记阶段的拒绝（缺哈希/来源不清/权属不可商用）是请求与数据的问题 → 422
        raise ComposeRejected(e.code, e.message, e.detail)

    base.update({
        "asset": asset,
        "registered": True,
        "storage_uri": saved["storage_uri"],
        "file_hash": saved["file_hash"],
        "image_url": _media_url(saved["storage_uri"]),
        "outfit": outfit,
        "background_label": background_label or bg.get("note") or "",
        "declared_style": declared_style,
        "note": "变体资产已登记为 pending：须经身份审核（pass）后才可用于底片",
    })
    return base


def _compose_warnings(result: Dict[str, Any]) -> List[str]:
    out: List[str] = [
        "画面由算法确定性合成（人物像素 1:1 复制），未使用任何生成模型重绘",
        "本产物不代表身份已被识别；它只证明硬保护区内像素未被改动",
    ]
    check = result.get("hard_protect_check") or {}
    if check:
        mx = _max_channel_diff(check)
        out.append(
            f"硬保护区比对：最大通道差 {mx}，逐像素相同={check.get('pixel_identical')}"
            f"（共 {len(check.get('regions') or [])} 个区域）；只有该值严格为 0 才是逐像素相同"
        )
    if result.get("mask_quality_warning"):
        out.append(result["mask_quality_warning"])
    if result.get("edge_mode") != "unmix" and (result.get("edge_decontam") or {}).get("method") == "none":
        out.append("未做边缘去污：换深色/异色背景时轮廓外会保留一圈原背景灰晕")
    bg_warn = ((result.get("frame") or {}).get("background_soft_warning") or "")
    if bg_warn:
        out.append(bg_warn)
    if (result.get("frame") or {}).get("revealed_cut"):
        out.append("画框内出现了源图裁断的断面（已在记录中量化）；该产物只用于对照，不宜进入生产")
    if result.get("candidate"):
        out.append("该产物被标记为候选（candidate）。blocking_reasons："
                   + "；".join(result.get("blocking_reasons") or []))
    return out


__all__ = [
    "VariantBlocked",
    "estimate_and_register_mask",
    "list_mask_specs",
    "confirm_mask",
    "compose_variant",
]
