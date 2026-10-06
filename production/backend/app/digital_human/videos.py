"""口播视频资产登记与门禁字段（AC-TH-01/03/06）。

与 T04 音频资产同一套取向：**`kind` 就是权限**。
- `plate` 是 M2 的动态底片：只有动作，不含最终音频，永远不能当成交付原片；
- `original` 是提交审核的口播原片，必须绑定已确认最终音频。

登记阶段就要拒绝的东西（不留给"审核时再说"）：
无视频轨、无哈希、时长为 0、缺音频绑定 —— 这些不是"质量差"，
而是"这个文件根本不是我们要的产物"，越晚发现越贵。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .. import storage as st
from ..contracts import VideoAssetInfo, now_iso
from .errors import TalkingHeadRejected

KIND_PLATE = "plate"
KIND_ORIGINAL = "original"
KINDS = (KIND_PLATE, KIND_ORIGINAL)

VIDEO_TABLE = "videos"


def get_video(video_id: str) -> Optional[Dict[str, Any]]:
    return st.get(VIDEO_TABLE, video_id) if video_id else None


def list_videos(work_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = st.all_rows(VIDEO_TABLE, "work_id=?", [work_id])
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return sorted(rows, key=lambda r: r.get("created_at", ""))


def register_video(*, work_id: str, kind: str, route: str, provider: str, model: str,
                   duration_ms: int, file_hash: str = "", storage_uri: str = "",
                   is_mock: bool = False, identity_id: Optional[str] = None,
                   variant_id: Optional[str] = None, audio_asset_id: Optional[str] = None,
                   source_asset_id: Optional[str] = None, source_asset_hash: Optional[str] = None,
                   motion_ref_id: Optional[str] = None,
                   provider_run_id: Optional[str] = None,
                   cost_reservation_id: Optional[str] = None,
                   has_video_track: bool = True, has_audio_track: bool = False,
                   width: Optional[int] = None, height: Optional[int] = None,
                   fps: Optional[float] = None,
                   segment_index: Optional[int] = None, segment_count: Optional[int] = None,
                   global_offset_ms: int = 0, revision_of: Optional[str] = None,
                   checks: Optional[List[Dict[str, Any]]] = None,
                   generation_mode: Optional[str] = None,
                   note: str = "") -> Dict[str, Any]:
    if kind not in KINDS:
        raise TalkingHeadRejected("VIDEO_KIND_INVALID", f"视频种类只能是 {KINDS}，收到 {kind}")
    if not work_id:
        raise TalkingHeadRejected("WORK_REQUIRED", "视频资产必须归属作品")
    if not file_hash:
        raise TalkingHeadRejected(
            "VIDEO_HASH_MISSING",
            "缺少文件哈希：无法证明交付的就是审核过的那个文件，不得登记为可用产物")
    if duration_ms <= 0:
        raise TalkingHeadRejected("VIDEO_DURATION_INVALID",
                                 "时长必须为正：下游时间轴不接受 0 或未知时长")
    if not has_video_track:
        raise TalkingHeadRejected(
            "VIDEO_TRACK_MISSING",
            "结果没有视频轨，不是视频；HTML 错误页/纯音频/空文件都属于此类",
            {"kind": kind})
    native = generation_mode == "image_prompt_native_audio"
    if native:
        from .wan_native import validate_registration
        if kind != KIND_ORIGINAL:
            raise TalkingHeadRejected("WAN_NATIVE_KIND_INVALID", "原生声画不能登记为动作底片")
        validate_registration(work_id=work_id, route=route, provider=provider,
                              provider_run_id=provider_run_id, source_asset_id=source_asset_id,
                              source_asset_hash=source_asset_hash, identity_id=identity_id,
                              variant_id=variant_id, audio_asset_id=audio_asset_id,
                              has_audio_track=has_audio_track, is_mock=is_mock)
    elif generation_mode:
        raise TalkingHeadRejected("VIDEO_GENERATION_MODE_INVALID", "未知的视频生成模式")
    if kind == KIND_ORIGINAL and not native:
        if not audio_asset_id:
            raise TalkingHeadRejected(
                "ORIGINAL_AUDIO_UNBOUND",
                "口播原片必须绑定驱动它的最终音频；否则无法核对是否被换声或改稿")
        if not identity_id:
            raise TalkingHeadRejected("ORIGINAL_IDENTITY_UNBOUND",
                                     "口播原片必须绑定作品选定人物，否则无法核对身份保持")
    if kind == KIND_PLATE:
        if not motion_ref_id:
            raise TalkingHeadRejected("PLATE_MOTION_UNBOUND",
                                     "动态底片必须绑定已获准动作参考")
        if not variant_id and not source_asset_id:
            raise TalkingHeadRejected("PLATE_VARIANT_UNBOUND",
                                     "动态底片必须绑定已审变体或已定版原图作为源图")
        if source_asset_id and not source_asset_hash:
            raise TalkingHeadRejected("PLATE_SOURCE_HASH_MISSING", "底片源图必须保留生成时的文件哈希")
        if not variant_id and (not identity_id or not audio_asset_id):
            raise TalkingHeadRejected("PLATE_INPUT_UNBOUND", "原图底片必须绑定人物身份与目标音频")

    video = VideoAssetInfo(
        id=st.new_id("vid"), work_id=work_id, kind=kind, route=route, provider=provider,
        model=model, storage_uri=storage_uri, file_hash=file_hash, duration_ms=int(duration_ms),
        width=width, height=height, fps=fps, has_video_track=has_video_track,
        has_audio_track=has_audio_track, is_mock=is_mock, identity_id=identity_id,
        variant_id=variant_id, audio_asset_id=audio_asset_id, motion_ref_id=motion_ref_id,
        source_asset_id=source_asset_id, source_asset_hash=source_asset_hash,
        provider_run_id=provider_run_id, cost_reservation_id=cost_reservation_id,
        segment_index=segment_index, segment_count=segment_count,
        global_offset_ms=int(global_offset_ms), revision_of=revision_of,
        checks=checks or [], note=note,
    )
    result = video.model_dump()
    if native:
        result.update(generation_mode=generation_mode, sound_mode="native_audio")
    st.put(VIDEO_TABLE, video.id, result, {"work_id": work_id})
    return result


def store_video_bytes(work_id: str, video_id: str, data: bytes) -> str:
    """真实视频落盘。返回相对位置；文件名就是资产 id，不用 None。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "runtime", "video", work_id)
    os.makedirs(root, exist_ok=True)
    name = f"{video_id}.mp4"
    with open(os.path.join(root, name), "wb") as f:
        f.write(data)
    return f"video/{work_id}/{name}"


# ---------- 门禁 ----------

def hard_failures(video: Dict[str, Any]) -> List[Dict[str, str]]:
    """确定性硬失败。Mock 单独一条：占位产物不得继承任何质量通过结论。"""
    out: List[Dict[str, str]] = []
    if video.get("is_mock"):
        out.append({"code": "VIDEO_IS_MOCK",
                    "message": "占位产物：不得作为原片提交审核，也不得进入后期"})
    if not video.get("has_video_track"):
        out.append({"code": "VIDEO_TRACK_MISSING", "message": "没有视频轨"})
    if not video.get("file_hash"):
        out.append({"code": "VIDEO_HASH_MISSING", "message": "缺少文件哈希"})
    if int(video.get("duration_ms") or 0) <= 0:
        out.append({"code": "VIDEO_DURATION_INVALID", "message": "时长非正"})
    for c in video.get("checks", []):
        if c.get("hard_gate") and c.get("outcome") != "pass":
            out.append({"code": c.get("code", "CHECK_FAILED"),
                        "message": c.get("message", "")})
    return out


def final_video(work_id: str) -> Optional[Dict[str, Any]]:
    """本次作品已确认的原片。同一作品只保留一条确认。"""
    rows = [v for v in list_videos(work_id, KIND_ORIGINAL) if v.get("confirmed")]
    return sorted(rows, key=lambda r: r.get("frozen_at") or "")[-1] if rows else None


def approved_originals(work_id: str) -> List[Dict[str, Any]]:
    return [v for v in list_videos(work_id, KIND_ORIGINAL)
            if v.get("approval_state") == "approved" and not v.get("confirmed")]


def is_auditable(video: Dict[str, Any]) -> tuple:
    reasons = [f["message"] for f in hard_failures(video)]
    if video.get("kind") != KIND_ORIGINAL:
        reasons.append("动态底片不是交付原片，不能作为最终视频")
    return (not reasons), reasons


def revoke(video_id: str, reason: str) -> Dict[str, Any]:
    """撤销确认但保留产物与哈希：失效不是删除（AC-AG-06）。"""
    video = get_video(video_id)
    if not video:
        raise TalkingHeadRejected("VIDEO_NOT_FOUND", f"视频不存在：{video_id}")
    video["confirmed"] = False
    video["approval_state"] = "revoked"
    video["revoked_at"] = now_iso()
    video["revoked_reason"] = reason
    st.put(VIDEO_TABLE, video_id, video, {"work_id": video.get("work_id")})
    return video
