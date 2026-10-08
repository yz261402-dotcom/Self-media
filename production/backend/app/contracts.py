"""数据契约（Pydantic）。字段与状态枚举严格对齐 specs/0001-douyin-ai-studio/04-接口契约与数据字典.md。

业务状态枚举的唯一来源是 04 文档，本文件不另建同义枚举。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "2026.09.23-t05"


class StrictModel(BaseModel):
    """严格契约基类：未知字段一律拒绝，避免调用方拼错字段被静默忽略。

    T01 要求：先测非法枚举/未知字段。契约不吸收未声明字段，
    新增字段必须显式进入本文件并同步 04 文档，不允许靠宽松解析"兼容"。
    """

    model_config = ConfigDict(extra="forbid", strict=False)

PipelineStage = Literal[
    "topic",
    "script",
    "content_review",
    "avatar_variant",
    "tts",
    "talking_head",
    "postprocess",
    "video_review",
    "publish_package",
]

TaskState = Literal[
    "queued",
    "running",
    "waiting_provider",
    "waiting_review",
    "waiting_selection",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
]

GenerationRoute = Literal["M2", "OMNIHUMAN_1_5", "WAN_3_0"]
CheckOutcome = Literal["pass", "fail", "review", "not_run"]
ApprovalState = Literal["pending", "approved", "rejected", "revoked"]
FeatureState = Literal["planned", "developing", "available", "blocked"]
Mode = Literal["mock", "integration", "production"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApiError(StrictModel):
    code: str
    message: str
    retryable: bool = False
    action: Optional[str] = None
    # 仅内容任务超时（CONTENT_JOB_PENDING）等场景输出，指向原持久任务供客户端轮询；其他错误不带这两个字段
    job_id: Optional[str] = None
    poll_url: Optional[str] = None


class ErrorResponse(StrictModel):
    error: ApiError
    trace_id: str = ""


class QualityCheck(StrictModel):
    code: str
    outcome: CheckOutcome
    hard_gate: bool
    message: str


class QualityReport(StrictModel):
    checks: List[QualityCheck] = Field(default_factory=list)
    overall: CheckOutcome = "not_run"
    review_required: bool = False


class Claim(StrictModel):
    id: str
    text: str
    verdict: Literal["supported", "refuted", "unverified"]


ReviewState = Literal["needs_review", "reviewing", "passed", "failed", "stale"]


class ScriptVersion(StrictModel):
    id: str
    angle: str
    title: str
    description: str
    tags: List[str] = Field(default_factory=list)
    text: str
    content_route: Literal["talking_script", "short_video_storyboard"] = "talking_script"
    storyboard: List[Dict[str, Any]] = Field(default_factory=list)
    duration_estimate_ms: int
    quality_report: QualityReport
    claims: List[Claim] = Field(default_factory=list)
    # 口播稿升级新增（均有安全默认值，历史稿件原样可读）。
    # review_state/pickable/unpickable_reasons 是读取时由服务端按当前审核复算的投影，不落库。
    slot: Optional[int] = None
    parent_id: Optional[str] = None
    batch_id: Optional[str] = None
    text_digest: Optional[str] = None
    origin: Dict[str, Any] = Field(default_factory=dict)
    review_state: Optional[ReviewState] = None
    pickable: Optional[bool] = None
    unpickable_reasons: List[str] = Field(default_factory=list)
    human_review_state: Optional[Literal["pending", "scored_pass", "human_edit", "manual_takeover"]] = None
    human_review: Optional[Dict[str, Any]] = None
    creation_source: Optional[Literal["manual", "llm"]] = None
    ai_review_status: Optional[Literal["not_performed", "passed", "required"]] = None
    takeover_reason: Optional[str] = None


class ScriptRepairReceipt(StrictModel):
    id: str
    parent_id: str
    ready_to_present: Literal[False] = False


class MaterialEntry(StrictModel):
    """用户提供的资料条目，只作为**事实依据**参与断言判定（`content/review._supported`）。

    写入方是出稿端点（`POST /works/{id}/scripts` 的 payload），落在 `WorkBrief.materials`。
    文本中的任何指令都不执行——见 `content/generate._SYSTEM` 第 5 条。

    `content_hash` / `added_at` 由**服务端复算后写入**（调用方传来的值一律被覆盖）：
    规格 06 AC-CT-02 要求资料的版本与提交时间可追溯，断言"有依据"才能回溯到
    "依据的是哪一版资料"，而不是任意一段后来才改过的文本。
    """
    text: str
    source: str = ""
    content_hash: str = ""
    added_at: str = ""


class ScriptBatch(StrictModel):
    id: str
    work_id: str
    candidates: List[ScriptVersion]
    content_route: Literal["talking_script", "short_video_storyboard"] = "talking_script"
    ready_to_present: bool = False
    retry_targets: List[Dict[str, Any]] = Field(default_factory=list)
    review_targets: List[Dict[str, Any]] = Field(default_factory=list)
    shortfall_note: Optional[str] = None
    # 候选稿来源，写入方为 content 角色（`agents/tools.py::t_register_script_candidates`）：
    # `llm`＝真实模型产出，`mock`＝模拟三稿。缺失表示改动前写入的历史批次（未标注来源），
    # 不追认、不补标——模型产出与模拟稿的区分必须由写入当时的留痕决定。
    source_kind: Optional[str] = None
    # 改稿台账，写入方为 `content/rewrite.py::RewriteLedger`（计数落在本行内，沿用既有表）。
    # 契约必须容纳这两个字段：它们一被写入，整条批次就会在返回时被 extra=forbid 拒绝。
    rewrite_count: Optional[int] = None
    rewrite_history: List[Dict[str, Any]] = Field(default_factory=list)
    # 生成侧自修复留痕，写入方为 `content/generate.py::repair_candidates`（出稿时）与
    # `POST /works/{id}/script-repairs`（用户要求模型再修一稿时）。记"模型自己改了几轮、
    # 每轮消掉了哪些硬门禁"，好让"这稿能采用"可追溯到具体轮次，而不是凭空判定。
    repair_note: Optional[str] = None
    repair_history: List[Dict[str, Any]] = Field(default_factory=list)
    # 本批出稿时提供的资料条数（0＝无资料）。无资料出稿的稿件不得含需要外部依据的事实断言，
    # 这个数字是"当时到底有没有资料"的唯一留痕，事后据此判断门禁结论是否被误读。
    materials_count: Optional[int] = None
    # 口播稿升级：批次的实际产出、通过数与来源绑定。requested/produced/passed 让页面如实显示
    # "已通过 X/3"，而不是写死"三稿已出"。缺失＝升级前写入的历史批次。
    requested: Optional[int] = None
    produced: Optional[int] = None
    passed: Optional[int] = None
    prompt_id: Optional[str] = None
    content_revision: Optional[int] = None
    job_id: Optional[str] = None
    set_digest: Optional[str] = None
    set_review_id: Optional[str] = None
    review_state: Optional[ReviewState] = None


class WorkBrief(StrictModel):
    topic: str = ""
    content_route: Literal["talking_script", "short_video_storyboard"] = "talking_script"
    content_type: str = "AI 科普"
    audience: str = "对 AI 感兴趣的年轻受众"
    keywords: List[str] = Field(default_factory=list)
    target_duration_ms: int = Field(default=0, ge=0)  # 0 表示不限定写稿目标；实际时长来自音频
    # 事实依据来源。留空＝无资料：此时生成侧必须走"不依赖外部事实"的表达方式，
    # 否则每条断言都只能是 unverified，三稿会被 FACT_EVIDENCE 硬门禁全部挡住。
    materials: List[MaterialEntry] = Field(default_factory=list)


class Work(StrictModel):
    id: str
    name: str
    brief: WorkBrief
    identity_id: Optional[str] = None
    voice_asset_id: Optional[str] = None
    tts_controls: Dict[str, Any] = Field(default_factory=dict)
    tts_controls_revision: int = 0
    script_version_id: Optional[str] = None  # 用户显式采用的稿件，重新生成三稿时清空
    route: GenerationRoute = "M2"
    current_task_id: Optional[str] = None
    version: int = 1
    created_at: str = Field(default_factory=now_iso)
    # 作品当前绑定：契约必须容纳它们，否则路由在返回作品时会因未知字段直接报错
    # （extra=forbid 是刻意的——不允许"写入时放开、读取时崩掉"）
    variant_id: Optional[str] = None
    performance_plan_id: Optional[str] = None
    final_audio_id: Optional[str] = None
    final_audio_duration_ms: Optional[int] = None
    video_asset_id: Optional[str] = None
    motion_intent: Dict[str, Any] = Field(default_factory=dict)
    # 高成本路线选择时由服务端写入；刷新作品仍需保留这条用户确认记录。
    cost_notice_ack: Optional[Dict[str, Any]] = None
    # 口播稿升级：共享创作输入（主题/资料/参考/风格/提示/模型配置）每次变更 content_revision+1，
    # 单调递增；旧任务回写靠它与 current_content_job_id 做 CAS，A→B→A 也不会复活旧响应。
    content_revision: int = 0
    reference_snapshot_id: Optional[str] = None
    writing_prompt_id: Optional[str] = None
    writer_profile_id: Optional[str] = None
    style_snapshot: Dict[str, Any] = Field(default_factory=dict)
    current_content_job_id: Optional[str] = None
    deleted_at: Optional[str] = None
    deleted_by: str = ""


# ---- T03 人物资产分层 ----
# 资产种类必须区分：动作参考被误当人物原片是 T03 明确要求拦截的负例。
AssetKind = Literal["hero", "reference", "variant", "motion", "mask", "background"]

# 权属核验结论。算法名称或代码许可证不能替代模型权重许可。
LicenseScope = Literal["personal_only", "commercial_ok", "unverified", "blocked"]

# 身份审核结论。review 表示无法判断，与 fail 一样不进入生产白名单。
IdentityOutcome = Literal["pass", "fail", "review", "not_run"]


class LicenseRecord(StrictModel):
    """权属/许可记录。未确认的许可阻塞该方案，不在报告中伪造法律结论。"""

    id: str
    subject: str  # 资产 id 或模型/权重名称
    kind: Literal["image_source", "voice_source", "model_weights", "service_terms"]
    scope: LicenseScope = "unverified"
    evidence: str = ""  # 条款链接、授权书、核验来源
    # 算法名称或代码许可证不能替代模型权重许可：未显式声明覆盖权重即视为未核验
    covers_weights: bool = False
    verified_at: Optional[str] = None
    verified_by: str = ""
    note: str = ""


class AvatarAsset(StrictModel):
    """人物资产。登记必须带文件哈希与来源证据；缺图、哈希不符、来源不清均阻止生产。"""

    id: str
    identity_id: str
    kind: AssetKind
    version: int = 1
    storage_uri: str = ""  # 为空即缺图，视为未登记完成
    file_hash: str = ""  # sha256，空值不允许进入生产
    width: Optional[int] = None
    height: Optional[int] = None
    source_evidence: str = ""  # 来源与权属说明，空值不允许进入生产
    license_id: Optional[str] = None
    is_mock: bool = False  # Mock 后代不得进入生产，也不得继承质量通过结论
    supersedes_id: Optional[str] = None
    approval_state: ApprovalState = "pending"
    created_at: str = Field(default_factory=now_iso)
    note: str = ""


class IdentityReview(StrictModel):
    """身份审核。摘要绑定资产版本与文件哈希：任一变化后旧审核自动失效。"""

    id: str
    asset_id: str
    reviewer: str
    outcome: IdentityOutcome
    asset_version: int
    file_hash: str
    identity_digest: str  # 与冻结身份基准比对所得
    method: str = ""
    calibration_id: Optional[str] = None
    reviewed_at: str = Field(default_factory=now_iso)
    note: str = ""


class MotionReference(StrictModel):
    """动作/表情参考。M2 只能把 motion 意图映射到已获准的参考与受支持参数。"""

    id: str
    identity_id: str
    motion_key: str  # 受支持的动作键，如 nod / gesture_open
    label: str
    description: str = ""
    asset_id: str
    supported_params: Dict[str, Any] = Field(default_factory=dict)
    approval_state: ApprovalState = "pending"
    loop_seconds: Optional[float] = None  # EXP-001 短循环不直接转长期母版
    license_id: Optional[str] = None
    note: str = ""
    # 从共享动作库选用时保留来源与选择时的文件版本，不继承人物审核。
    source_reference_id: Optional[str] = None
    source_asset_hash: Optional[str] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None


# 走受限路径（`hero_only`）时必须逐条确认的限制。文案变化即视为旧确认失效，
# 与费用提示按 COST_NOTICE_VERSION 版本化同理。
REFERENCE_POLICY_VERSION = "hero_only_v1"

HERO_ONLY_LIMITATIONS: List[str] = [
    "无多角度真值：无法交叉验证是不是同一个人",
    "换装换景只能由原图确定性派生，不得用生成模型另起一张脸",
    "每次出片须由人对照原图给出身份结论，系统不自动判定",
]


class IdentityReferencePolicy(StrictModel):
    """参考集政策：这个身份用什么作为"是同一个人"的依据。

    `multi_view`（默认）：有多张经审核的参考图，可交叉验证。
    `hero_only`：**受限路径**——只有一张原图作锚，不存在多角度真值。
    后者必须写清是谁、何时、因为什么这么定，并逐条确认限制项。
    模式不得由数据自动推断：推断等于让系统替人决定可以用什么证据。
    """

    mode: Literal["multi_view", "hero_only"] = "multi_view"
    declared_by: str
    declared_at: str
    reason: str = ""
    acknowledged: List[str] = Field(default_factory=list)


class AvatarIdentity(StrictModel):
    id: str
    name: str
    persona: str
    hero_asset_id: str
    identity_digest: str
    default_voice_asset_id: Optional[str] = None
    approval_state: ApprovalState = "pending"
    is_preset: bool = False
    note: str = ""
    # T03：冻结身份不可覆盖。换人必须产生新版本并重新验收。
    identity_version: int = 1
    frozen: bool = False
    reference_asset_ids: List[str] = Field(default_factory=list)
    # 身份来源：拍摄/受控素材 vs 由 generative 服务凭空生成。后者天生拿不到多角度真值。
    identity_origin: Literal["captured", "generated"] = "captured"
    # 参考集政策。None=尚未声明；生产准入要求已声明（否则连走哪条路都不清楚）。
    reference_policy: Optional[IdentityReferencePolicy] = None
    # 人设关键词。`create_identity_candidate` 一直会写入它，却从未在契约里声明，
    # 导致新建的人物行无法反序列化（同 reviewed_by 那一类故障）。
    keywords: Dict[str, Any] = Field(default_factory=dict)
    license_id: Optional[str] = None
    base_approved_at: Optional[str] = None
    # 定版审核人。`freeze_identity` 会写入它；契约缺字段会让该身份行在反序列化时
    # 被 extra_forbidden 拒掉（曾导致 /avatar-identities 500、前端卡在"正在连接后端"）。
    reviewed_by: Optional[str] = None
    # 草稿候选的丢弃。刻意**不是物理删除**：规格要求资产/授权/审核记录长期保留，
    # 下游也可能已有引用。丢弃只是把它移出工作列表，并留下"谁、何时、为什么"。
    # discard_history 为只追加事件流（discard / restore），恢复不改写任何历史事件。
    discarded_at: Optional[str] = None
    discarded_by: str = ""
    discard_reason: str = ""
    discard_history: List[Dict[str, Any]] = Field(default_factory=list)
    deleted_at: Optional[str] = None
    deleted_by: str = ""


class AvatarVariant(StrictModel):
    id: str
    identity_id: str
    outfit: str
    background: str
    approval_state: ApprovalState = "pending"
    identity_report: Optional[Dict[str, Any]] = None
    preview_label: str = ""
    # 画风由登记方声明。留空不是"默认写实"，而是"无法判断，需人工复核"——
    # 身份门禁不接受未声明的画风，因此这里必须是一个显式字段。
    declared_style: str = ""
    # T03：变体必须落在具体资产上，审核结论随资产版本与哈希失效
    asset_id: Optional[str] = None
    identity_version: int = 1
    review_id: Optional[str] = None
    motion_ref_id: Optional[str] = None


class VoiceAsset(StrictModel):
    """音色资产（AC-TTS-01）。

    `provider`/`model`/`region`/`remote_voice_id` 四者作为**组合**解释作用域：
    仅凭一个字符串 ID 复用是明确负例（不同服务的同名 ID 语义不同）。
    """

    id: str
    name: str
    provider: str
    model: str
    region: str = "cn"
    remote_voice_id: str
    approval_state: ApprovalState = "pending"
    is_preset: bool = False
    sample_text: str
    # 官方目录里的口播风格描述与目录分组：只用于选音色时做筛选，
    # 不参与作用域判定，也不能被当成"音色质量已验收"的证据。
    description: str = ""
    catalog_group: str = ""
    supported_emotions: List[str] = Field(default_factory=list)
    clone_metadata: Dict[str, Any] = Field(default_factory=dict)
    # 冻结试听音频与基础参数；不支持 seed 的服务不得写"已固定 seed"
    synthesis_profile: Dict[str, Any] = Field(default_factory=dict)
    profile_digest: str = ""
    # 预设音色选择与使用权，或声音创建/注册的来源证据
    source_record_id: Optional[str] = None
    license_id: Optional[str] = None
    # 本项目基准合成证据：真实音频资产与哈希（缺此项即不算完成基准验证）
    baseline_audio_asset_id: Optional[str] = None
    baseline_audio_hash: str = ""
    supersedes_id: Optional[str] = None
    version: int = 1
    note: str = ""


# 对齐来源。未完成对齐时为空，绝不用"均分时长"冒充已完成字词对齐。
TimingSource = Literal["provider", "forced_alignment", "asr_alignment", "mock"]

# 表达标注类型：只允许已定义值，意图统一来自绑定的 PerformancePlan
SpeechAnnotationType = Literal["pause", "emphasis", "rate", "emotion", "pronunciation"]


class SpeechAnnotation(StrictModel):
    """供编译器读取的表达标注派生视图。停顿用零长度位置（start_char == end_char）。"""

    type: SpeechAnnotationType
    start_char: int
    end_char: int
    value: str = ""


class WordTiming(StrictModel):
    """字/词级时间。时间均相对于对应的**实际音频文件起点**。"""

    segment_id: str
    start_char: int
    end_char: int
    start_ms: int
    end_ms: int


class TTSResult(StrictModel):
    audio_asset_id: str
    voice_asset_id: str
    script_version_id: str
    performance_plan_id: str
    duration_ms: int
    spoken_text_mapping: List[Dict[str, Any]] = Field(default_factory=list)
    timing_source: Optional[TimingSource] = None
    words: List[WordTiming] = Field(default_factory=list)
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    audio_format: str = "wav"
    usage_characters: Optional[int] = None
    provider_run_id: Optional[str] = None
    is_mock: bool = False
    note: str = ""


class VoicePreview(StrictModel):
    """短句试听。缓存只按「声音/模型/参数/文本摘要」精确命中；试听片不是正文音频。"""

    work_id: Optional[str] = None
    voice_asset_id: str
    sample_text_digest: str
    synthesis_profile_digest: str
    audio_asset_id: str
    provider_run_id: Optional[str] = None
    cost_reservation_id: Optional[str] = None
    cached: bool = False
    created_at: str = Field(default_factory=now_iso)


# 来源类型。测试素材始终为模拟；来源不明或权利材料不完整时不能批准为生产根资产。
SourceType = Literal["project_generated", "preset_voice_selection",
                     "newly_licensed", "project_test_material"]


class SourceRecord(StrictModel):
    """资产来源记录（AC-TTS-01 的来源侧）。

    由新系统的生成、预设音色选择、创建/注册或受控素材登记流程建立，
    不能靠客户端自行声明来源通过。预设音色以「选择记录 + 官方使用权 + 基准合成」
    为依据，不要求不存在的声音克隆/注册回执。
    """

    id: str
    subject: str  # 资产 id
    source_type: SourceType
    provider: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    provider_run_id: Optional[str] = None
    prompt_asset_id: Optional[str] = None
    seed: Optional[int] = None
    rights_evidence_asset_id: Optional[str] = None
    license_review_state: Literal["unreviewed", "pending", "reviewed", "blocked"] = "unreviewed"
    reviewed_at: Optional[str] = None
    # 权属审核人，见 `tts.sources.mark_reviewed`；与 AvatarIdentity.reviewed_by 同源问题
    reviewed_by: Optional[str] = None
    note: str = ""
    created_at: str = Field(default_factory=now_iso)


class AlignmentRecord(StrictModel):
    """对齐产物版本。缺可靠时间戳时另行补齐并登记新版本，不原地伪填、不均分。"""

    id: str
    audio_asset_id: str
    audio_digest: str = ""
    source: TimingSource
    method: str = ""
    rule_version: str = ""
    words: List[WordTiming] = Field(default_factory=list)
    note: str = ""
    created_at: str = Field(default_factory=now_iso)


class AudioAssetInfo(StrictModel):
    sample_text: str = ""
    synthesis_profile: Dict[str, Any] = Field(default_factory=dict)
    tts_controls_digest: str = ""
    tts_controls_revision: int = 0
    invalidated: bool = False
    invalidated_at: Optional[str] = None
    invalidated_reason: str = ""
    id: str
    kind: Literal["preview", "full"]
    voice_asset_id: str
    duration_ms: int
    approved: bool = False
    confirmed: bool = False
    url: Optional[str] = None
    checks: List[QualityCheck] = Field(default_factory=list)
    # T04：产物必须能追溯到具体文件与合成运行，且 Mock 不得被误当真实音频
    storage_uri: str = ""
    file_hash: str = ""
    is_mock: bool = False
    script_version_id: Optional[str] = None
    performance_plan_id: Optional[str] = None
    provider_run_id: Optional[str] = None
    cost_reservation_id: Optional[str] = None
    timing_source: Optional[TimingSource] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    revision_of: Optional[str] = None
    frozen_at: Optional[str] = None
    note: str = ""


# ---- T05 数字人口播 ----
# plate = M2 的动态底片（只有动作，不含最终音频）；original = 提交给审核的口播原片
VideoKind = Literal["plate", "original"]

# 外部作业运行状态。unknown 是独立状态：提交丢响应时既不算成功也不算失败。
ProviderRunState = Literal["intent", "blocked", "submitted", "waiting_provider",
                           "succeeded", "failed", "unknown", "cancelled"]


class ProviderRun(StrictModel):
    """一次真实外部调用的运行记录（AC-TH-01/06）。

    `request_digest` 与 `idempotency_key` 是"不重复计费"的依据：
    同一摘要不得静默重提，`remote_job_id` 必须回写以便按原任务对账。
    """

    id: str
    work_id: str
    task_id: Optional[str] = None
    stage: PipelineStage = "talking_head"
    route: Optional[str] = None
    provider: str
    model: str
    capability: str
    capability_snapshot_id: str = ""
    request_digest: str = ""
    idempotency_key: str = ""
    state: ProviderRunState = "intent"
    remote_job_id: Optional[str] = None
    request_trace_id: str = ""
    poll_after: Optional[str] = None
    deadline_at: Optional[str] = None
    result_asset_ids: List[str] = Field(default_factory=list)
    cost_reservation_id: Optional[str] = None
    is_mock: bool = False
    provider_params: Dict[str, Any] = Field(default_factory=dict)
    last_error: Optional[ApiError] = None
    created_at: str = Field(default_factory=now_iso)


class VideoAssetInfo(StrictModel):
    """口播视频资产。

    `has_video_track` 是显式字段而不是"能不能解码"的推断：
    无视频轨的结果、HTML 错误页、空文件都必须在这里被挡住，
    不允许因为"文件存在、大小不为 0"就当成功。
    """

    id: str
    work_id: str
    kind: VideoKind
    route: GenerationRoute
    provider: str
    model: str
    storage_uri: str = ""
    file_hash: str = ""
    duration_ms: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    has_video_track: bool = False
    has_audio_track: bool = False
    is_mock: bool = False
    identity_id: Optional[str] = None
    variant_id: Optional[str] = None
    source_asset_id: Optional[str] = None
    source_asset_hash: Optional[str] = None
    audio_asset_id: Optional[str] = None
    motion_ref_id: Optional[str] = None
    provider_run_id: Optional[str] = None
    cost_reservation_id: Optional[str] = None
    # 分段调用：记录分段序号与在统一音轨上的全局偏移
    segment_index: Optional[int] = None
    segment_count: Optional[int] = None
    global_offset_ms: int = 0
    approval_state: ApprovalState = "pending"
    confirmed: bool = False
    revision_of: Optional[str] = None
    frozen_at: Optional[str] = None
    checks: List[QualityCheck] = Field(default_factory=list)
    note: str = ""
    created_at: str = Field(default_factory=now_iso)


class PriceSnapshot(StrictModel):
    """价格快照。用户报价不是账单核验结论，必须分开记录。"""

    id: str
    route: Optional[str] = None
    provider: str = ""
    unit: str = "fen_per_generated_second"
    unit_fen: int = 0
    price_source: Literal["user_confirmed_rate", "provider_quote",
                          "invoice", "unverified"] = "unverified"
    billing_verified: bool = False
    rounding_note: str = ""
    minimum_note: str = ""
    failure_billing_note: str = ""
    evidence: str = ""
    created_at: str = Field(default_factory=now_iso)


class StageRow(StrictModel):
    stage: PipelineStage
    label: str
    state: Literal["pending", "active", "done", "blocked", "failed"]
    detail: str = ""


class Task(StrictModel):
    id: str
    work_id: str
    mode: Mode = "mock"
    stage: PipelineStage = "topic"
    state: TaskState = "queued"
    route: GenerationRoute = "M2"
    route_confirmed: bool = False
    percent: Optional[int] = None
    progress_source: Literal["provider", "measured", "indeterminate"] = "indeterminate"
    pending_selection_ids: List[str] = Field(default_factory=list)
    # T01 冻结字段：路线选择必须可追溯到一条真实人工决定
    route_selection_id: Optional[str] = None
    input_snapshot_digest: str = ""
    block_reason: Optional[str] = None
    last_error: Optional[ApiError] = None
    stage_rows: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)


class TaskEvent(StrictModel):
    seq: int
    type: str
    occurred_at: str
    stage: Optional[PipelineStage] = None
    state: Optional[TaskState] = None
    message: str


class CostEstimate(StrictModel):
    route: GenerationRoute
    estimated_fen: int
    ceiling_fen: int
    currency: Literal["CNY"] = "CNY"
    price_basis: str
    billable: bool = False
    note: str = ""


class PublishPackage(StrictModel):
    id: str
    work_id: str
    task_id: str
    mode: Mode = "mock"
    state: Literal["pending", "ready"] = "pending"
    title: str = ""
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    ai_label: str = ""
    files: List[Dict[str, Any]] = Field(default_factory=list)
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    download_url: Optional[str] = None
    manifest_digest: str = ""


class FeatureEntry(StrictModel):
    feature_id: str
    title: str
    feature_state: FeatureState
    reason: str
    available_actions: List[str] = Field(default_factory=list)


class Capabilities(StrictModel):
    mode: Mode = "mock"
    routes: List[Dict[str, Any]] = Field(default_factory=list)
    providers: List[Dict[str, Any]] = Field(default_factory=list)
    budget: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# T01：人工选择（Selection）与质量审批（Review）必须彼此独立
# ---------------------------------------------------------------------------

SelectionKind = Literal["script", "identity", "variant", "voice", "audio", "route"]
SelectionState = Literal["pending", "decided", "expired", "cancelled"]


class SelectionOption(StrictModel):
    """一个待选项。ref_id 指向真实产物，digest 锁定其内容版本。"""

    option_id: str
    kind: SelectionKind
    ref_id: str
    label: str
    digest: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SelectionRequest(StrictModel):
    """面向用户的待选请求。Agent 只能创建请求，不能替用户决定。"""

    id: str
    work_id: str
    task_id: Optional[str] = None
    kind: SelectionKind
    prompt: str
    options: List[SelectionOption] = Field(default_factory=list)
    required: bool = True
    state: SelectionState = "pending"
    created_at: str = Field(default_factory=now_iso)

    def option_ids(self) -> List[str]:
        return [o.option_id for o in self.options]


class SelectionDecision(StrictModel):
    """人工选择决定。

    红线：actor 恒为 "user"。Agent、质量审核通过、默认推荐都不能产生本对象；
    任何 actor != user 的构造直接拒绝，避免"质量通过替代用户选择"。
    """

    id: str
    request_id: str
    work_id: str
    kind: SelectionKind
    selected_option_id: str
    selected_ref: str
    selected_digest: str = ""
    actor: Literal["user"] = "user"
    reason: str = ""
    decided_at: str = Field(default_factory=now_iso)
    supersedes_decision_id: Optional[str] = None

    def is_user_decision(self) -> bool:
        return self.actor == "user"


class ReviewDecision(StrictModel):
    """质量审批结论。与 SelectionDecision 无继承关系、不含任何 selected_* 字段。

    审批通过只表示"通过门禁"，不构成对候选的采用；采用必须由 SelectionDecision 表达。
    """

    id: str
    work_id: str
    subject_id: str
    subject_digest: str = ""
    ruleset_version: str
    outcome: CheckOutcome
    hard_gate_failed: bool = False
    report_ids: List[str] = Field(default_factory=list)
    reviewer: Literal["agent", "human"] = "agent"
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    note: str = ""
    created_at: str = Field(default_factory=now_iso)


class BindingSet(StrictModel):
    """一次运行绑定的版本集合：身份、声音、模型、Skill、规则。

    任一绑定缺失摘要即视为未定版；调用方必须阻断而不是以空值继续。
    """

    identity_id: Optional[str] = None
    identity_digest: Optional[str] = None
    voice_asset_id: Optional[str] = None
    voice_digest: Optional[str] = None
    model_id: Optional[str] = None
    model_digest: Optional[str] = None
    ruleset_version: str = ""
    skill_refs: List[Dict[str, str]] = Field(default_factory=list)

    def missing(self) -> List[str]:
        """返回"已设 ID 却缺摘要"的绑定项——这类不一致必须阻断。"""
        out = []
        if self.identity_id and not self.identity_digest:
            out.append("identity")
        if self.voice_asset_id and not self.voice_digest:
            out.append("voice")
        if self.model_id and not self.model_digest:
            out.append("model")
        if not self.ruleset_version:
            out.append("ruleset")
        for r in self.skill_refs:
            if not r.get("skill_id") or not r.get("version") or not r.get("digest"):
                out.append(f"skill:{r.get('skill_id', '?')}")
        return out
