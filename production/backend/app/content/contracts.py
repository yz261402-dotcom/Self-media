"""口播稿升级的内容契约：参考快照、风格、提示、批次与审核绑定摘要。

公共 Work/ScriptVersion/ScriptBatch 字段仍在 `app/contracts.py`；这里只放本功能独有的
记录形状与摘要函数。所有摘要都是版本化的确定性函数：同内容同摘要，与 ID、抓取时间无关。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import Field, model_validator

from ..contracts import ScriptVersion, StrictModel, now_iso

DIGEST_VERSION = "content-digest-v1"

TranscriptState = Literal["none", "pending", "ready", "failed", "unknown"]
EvidenceKind = Literal["inspiration", "fact"]


def digest(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def text_digest(text: str) -> str:
    """正文摘要：只忽略首尾空白，正文内部任何字符变化都产生新摘要。"""
    return "sha256:" + hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:32]


def candidate_digest(version: Dict[str, Any]) -> str:
    """Bind all user-visible storyboard fields, not just its derived reading text."""
    if version.get("content_route") == "short_video_storyboard":
        return digest({"route": "short_video_storyboard", "text": version.get("text", ""),
                       "storyboard": version.get("storyboard") or []})
    return text_digest(version.get("text", ""))


def set_digest(members: Sequence[Dict[str, Any]]) -> str:
    """候选集合摘要：排序后的 (slot, candidate_id, text_digest)；替换任意一篇即变化。"""
    rows = sorted(((int(m["slot"]), str(m["candidate_id"]), str(m["text_digest"])) for m in members))
    return digest({"v": DIGEST_VERSION, "members": rows})


class Metrics(StrictModel):
    """互动数据。None 代表上游未提供，绝不补成 0。"""
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    collects: Optional[int] = None


def transcript_view(transcript: Dict[str, Any]) -> Dict[str, Any]:
    """Interpret RedFox's no-speech marker without rewriting stored evidence."""
    result = dict(transcript)
    text = str(result.get("text") or "").strip().rstrip("。.!！")
    if result.get("state") == "ready" and result.get("source") == "redfox" and text == "静音音频":
        result["state"] = "failed"
        result["error"] = result.get("error") or "转写未检测到有效人声（静音音频）；可进行画面分析或手动补全转写"
    return result


def transcript_is_usable(transcript: Dict[str, Any]) -> bool:
    result = transcript_view(transcript)
    return result.get("state") == "ready" and bool(str(result.get("text") or "").strip())


class Transcript(StrictModel):
    state: TranscriptState = "none"
    text: str = ""
    source: str = ""
    task_id: Optional[str] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def _ready_needs_text(self):
        if self.state == "ready" and not self.text.strip():
            raise ValueError("转写标为 ready 但没有正文：缺少转写不能冒充已提取")
        return self


VisualState = Literal["none", "pending", "ready", "failed", "unknown"]
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")


class MediaAsset(StrictModel):
    """参考视频的本地媒体资产：只保存服务端受控存储位置，不接受任意路径。"""
    id: str
    work_id: str
    kind: Literal["reference_video"]
    storage_uri: str
    sha256: str
    mime_type: Literal["video/mp4", "video/quicktime", "video/webm"]
    byte_size: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source_kind: Literal["user_upload", "provider_url"]
    source_url: str = ""
    created_at: str
    reference_snapshot_ids: List[str] = Field(default_factory=list)
    reference_bound_at: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self):
        if not _SHA.match(self.sha256):
            raise ValueError("sha256 必须是 sha256:<64 位十六进制>")
        if not self.storage_uri.strip():
            raise ValueError("storage_uri 不能为空")
        return self


class VisualScene(StrictModel):
    index: int = Field(ge=1)
    start_ms: int = Field(ge=0)
    end_ms: int
    shot_type: str = ""
    camera_motion: str = ""
    description: str = ""
    actions: List[str] = Field(default_factory=list)
    on_screen_text: List[str] = Field(default_factory=list)
    beat_cues_ms: List[int] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)


class VisualAnalysis(StrictModel):
    """`ReferenceSnapshot.visual` 的形状；没有视觉结果时快照里存 `{}`。

    ready 必须能追溯到模型、输入哈希与合法时间线；failed/unknown 必须带 error，不能被当作有效参考。
    """
    state: VisualState
    provider: str = ""
    model: str = ""
    analysis_version: str = ""
    media_asset_id: str = ""
    media_sha256: str = ""
    duration_ms: int = 0
    coverage: List[Dict[str, int]] = Field(default_factory=list)
    summary: str = ""
    speech_presence: Literal["none", "present", "uncertain", ""] = ""
    music_presence: Literal["none", "music", "uncertain", ""] = ""
    audio: Dict[str, Any] = Field(default_factory=dict)
    scenes: List[VisualScene] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[Dict[str, str]] = None
    created_at: str = ""

    @model_validator(mode="after")
    def _check(self):
        if self.state == "ready":
            if not (self.provider and self.model and self.analysis_version):
                raise ValueError("视觉分析 ready 必须记录 provider/model/analysis_version")
            if not (self.media_asset_id and _SHA.match(self.media_sha256)) or self.duration_ms <= 0:
                raise ValueError("视觉分析 ready 必须绑定媒体资产、哈希与时长")
            if not self.scenes:
                raise ValueError("视觉分析 ready 必须至少有一个场景")
            if self.error:
                raise ValueError("ready 的视觉分析不能带 error")
            for s in self.scenes:
                if s.end_ms <= s.start_ms or s.end_ms > self.duration_ms:
                    raise ValueError("场景时间必须为正区间且不超过视频时长")
        elif self.state in ("failed", "unknown"):
            if not (self.error and self.error.get("code")):
                raise ValueError(f"视觉分析 {self.state} 必须带 error.code/message")
        return self


def visual_state(ref: Any) -> str:
    d = ref if isinstance(ref, dict) else ref.model_dump()
    return (d.get("visual") or {}).get("state") or "none"


class ReferenceSnapshot(StrictModel):
    """不可变参考快照。标题、简介、转写、画面分析分别保存，模型只能用实际取得的部分。"""
    id: str
    work_id: str
    provider: str = "redfox"
    platform: str = "douyin"
    platform_work_id: str
    source_url: str = ""
    title: str = ""
    caption: str = ""
    author: str = ""
    cover_url: Optional[str] = None
    video_url: Optional[str] = None
    media_asset_id: Optional[str] = None
    metrics: Metrics = Field(default_factory=Metrics)
    published_at: Optional[str] = None
    fetched_at: str
    transcript: Transcript = Field(default_factory=Transcript)
    visual: Dict[str, Any] = Field(default_factory=dict)
    content_digest: str = ""
    extraction_version: str
    # 视频参考默认 inspiration：不自动升级为可信事实
    evidence_kind: EvidenceKind = "inspiration"
    # 用户修正转写产生新快照时指向原快照，原始转写与来源保留
    parent_id: Optional[str] = None
    original_transcript: Optional[Transcript] = None
    unverified_fields: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _visual_shape(self):
        if self.visual:
            VisualAnalysis(**self.visual)
        return self


def reference_digest(ref: Any) -> str:
    """参考内容摘要：与 ID、抓取时间、互动数无关，只跟可写作依据的内容有关。"""
    d = ref if isinstance(ref, dict) else ref.model_dump()
    t = d.get("transcript") or {}
    return digest({
        "v": DIGEST_VERSION, "platform": d.get("platform"), "id": d.get("platform_work_id"),
        "title": d.get("title", ""), "caption": d.get("caption", ""),
        "transcript": {"state": t.get("state"), "text": (t.get("text") or "").strip()},
        "visual": d.get("visual") or {}, "extraction_version": d.get("extraction_version"),
    })


class StyleProfile(StrictModel):
    """可编辑账号风格。每次保存 revision+1；作品使用快照而非追随共享对象变化。"""
    id: str
    name: str = ""
    persona: str = ""
    attitude: str = ""
    professionalism: str = ""
    humor: str = ""
    phrases: List[str] = Field(default_factory=list)
    banned_phrases: List[str] = Field(default_factory=list)
    own_experience: List[str] = Field(default_factory=list)
    sample_scripts: List[str] = Field(default_factory=list)
    revision: int = 0
    updated_at: str = Field(default_factory=now_iso)


_STYLE_DIGEST_FIELDS = ("persona", "attitude", "professionalism", "humor", "phrases",
                        "banned_phrases", "own_experience", "sample_scripts")


def style_digest(style: Dict[str, Any]) -> str:
    return digest({"v": DIGEST_VERSION, **{k: style.get(k) for k in _STYLE_DIGEST_FIELDS}})


class PromptDocument(StrictModel):
    """不可变的拆解与写作提示版本；prompt_text 是用户可编辑的创作要求。"""
    id: str
    work_id: str
    reference_snapshot_id: str
    reference_digest: str = ""
    brief_digest: str = ""
    style_digest: str = ""
    # 生成提示时绑定的本期数字人物快照；人物变更后旧提示必须失效。
    identity_snapshot: Dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    parent_id: Optional[str] = None
    source: Literal["generated", "edited"] = "generated"
    reference_analysis: Dict[str, Any] = Field(default_factory=dict)
    angle_plan: List[Dict[str, Any]] = Field(default_factory=list)
    prompt_text: str
    template_version: str = "reference-script-v2-identity"
    model_profile: Dict[str, Any] = Field(default_factory=dict)
    content_revision: int = 0
    created_at: str = Field(default_factory=now_iso)


class ScriptVersionRecord(ScriptVersion):
    """落库的稿件版本：ScriptVersion + 归属作品。读取时投影字段不落库。"""
    work_id: str


class BatchMember(StrictModel):
    slot: int
    candidate_id: str
    text_digest: str = ""


class BatchRecord(StrictModel):
    """不可变批次快照：候选版本集合与任务来源；旧 script_batches 仅投影当前版本。"""
    id: str
    work_id: str
    prompt_id: str = ""
    content_revision: int = 0
    job_id: str = ""
    source_kind: str = "llm"
    content_route: Literal["talking_script", "short_video_storyboard"] = "talking_script"
    members: List[BatchMember]
    set_digest: str = ""
    requested: int = 3
    shortfall_note: Optional[str] = None
    repair_note: Optional[str] = None
    repair_history: List[Dict[str, Any]] = Field(default_factory=list)
    materials_count: Optional[int] = None
    created_at: str = Field(default_factory=now_iso)


def is_independent_review(decision: Optional[Dict[str, Any]]) -> bool:
    """一条质量结论是否可算作"独立语义审核"。

    只自称 independent 不够：必须是语义类审核、带审核方（供应商+模型）和输入绑定。
    历史规则型结论（默认 independent=False）永远不满足。
    """
    if not decision or decision.get("independent") is not True:
        return False
    if decision.get("review_kind") not in ("semantic_single", "semantic_set"):
        return False
    reviewer = decision.get("reviewer") or {}
    if not (reviewer.get("provider") and reviewer.get("model")):
        return False
    return bool(decision.get("binding"))
