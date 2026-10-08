// 前端数据契约镜像，字段命名与后端（04 接口契约）保持一致，避免映射错误。
// 状态枚举的唯一来源是 04 文档，前端不另建同义枚举。

export type PipelineStage =
  | 'topic'
  | 'script'
  | 'content_review'
  | 'avatar_variant'
  | 'tts'
  | 'talking_head'
  | 'postprocess'
  | 'video_review'
  | 'publish_package'

export type TaskState =
  | 'queued'
  | 'running'
  | 'waiting_provider'
  | 'waiting_review'
  | 'waiting_selection'
  | 'retry_wait'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'blocked'

export type GenerationRoute = 'M2' | 'OMNIHUMAN_1_5' | 'WAN_3_0'
export type FeatureState = 'planned' | 'developing' | 'available' | 'blocked'
export type CheckOutcome = 'pass' | 'fail' | 'review' | 'not_run'
export type SelectionKind = 'avatar' | 'script' | 'voice' | 'audio' | 'route'

export interface FeatureEntry {
  feature_id: string
  title: string
  feature_state: FeatureState
  reason: string
  available_actions: string[]
}

export interface Capabilities {
  mode: 'mock' | 'integration' | 'production'
  routes: {
    route: GenerationRoute
    label: string
    state: FeatureState
    note: string
  }[]
  providers: {
    provider: string
    purpose: string
    model: string
    validation_state: 'unverified' | 'usable' | 'invalid'
    detail: string
  }[]
  budget: {
    per_task_limit_fen: number
    daily_limit_fen: number
    max_billable_submissions: number
  }
}

/** 作品当前绑定的动作意图。后端是 `{"motion_ref_id": ...}` 形态的字典，
 *  历史作品可能整键缺失，因此这里全部可选。 */
export interface WorkMotionIntent {
  motion_key?: string
  params?: Record<string, number>
  motion_ref_id?: string
}

export interface Work {
  id: string
  content_revision?: number
  name: string
  brief: {
    topic: string
    content_type: string
    content_route?: 'talking_script' | 'short_video_storyboard'
    audience: string
    keywords: string[]
    target_duration_ms: number
    /** 参考资料：断言判定的唯一依据来源。留空＝无资料，此时生成侧必须避开需要
     *  外部依据的事实断言，否则每条断言都只能是 unverified，三稿会被硬门禁全挡住。 */
    materials?: MaterialEntry[]
  }
  identity_id: string | null
  voice_asset_id: string | null
  script_version_id: string | null
  route: GenerationRoute
  current_task_id: string | null
  version: number
  created_at: string
  // 后端 Work 契约里本来就有这些绑定字段，前端类型此前漏了——漏掉的后果是
  // 界面看不到"这个作品绑的是哪条变体/哪份计划/哪条音轨"，只能靠提交报错反推。
  variant_id?: string | null
  performance_plan_id?: string | null
  final_audio_id?: string | null
  final_audio_duration_ms?: number | null
  video_asset_id?: string | null
  motion_intent?: WorkMotionIntent | null
  tts_controls?: Partial<TtsControls>
  tts_controls_revision?: number
}

export interface DeletionReceipt {
  id: string
  deleted_at: string
}

export interface LicenseRecord {
  id: string
  subject: string
  kind: 'image_source' | 'voice_source' | 'model_weights' | 'service_terms'
  scope: 'unverified' | 'personal_only' | 'commercial_ok' | 'blocked'
  evidence: string
  covers_weights: boolean
  verified_at: string | null
  verified_by: string
  note: string
}

export interface ReferencePolicy {
  mode: 'multi_view' | 'hero_only'
  declared_by: string
  declared_at: string
  reason: string
  acknowledged: string[]
}

export interface AvatarIdentity {
  id: string
  name: string
  persona: string
  hero_asset_id: string
  identity_digest: string
  default_voice_asset_id: string | null
  approval_state: 'pending' | 'approved' | 'rejected' | 'revoked'
  is_preset: boolean
  note: string
  reference_policy?: ReferencePolicy | null
  license_id?: string | null
  frozen?: boolean
  identity_origin?: 'captured' | 'generated'
  // 草稿候选的丢弃（可恢复，非物理删除）。后端默认列表不含已丢弃的，
  // 要看得显式索取；这里带上下文的字段用于在"已丢弃"分组里说明谁在何时因为什么丢的。
  discarded_at?: string | null
  discarded_by?: string
  discard_reason?: string
  discard_history?: DiscardEvent[]
}

export interface DiscardEvent {
  action: 'discard' | 'restore' | string
  actor: string
  at: string
  reason: string
}

/** 定版链的一步。`why` 由后端给出，界面照抄，不自己另编说法。 */
export interface QualifyStep {
  key: 'review' | 'reference_policy' | 'license_attest' | 'freeze' | string
  label: string
  done: boolean
  why: string
}

/**
 * 定版进度。存在的理由是「选用」与「定版」是**前提/引用**关系而非并列两条路，
 * 而四步里用户看不出自己停在哪一步，所以顺序与状态统一由后端给出。
 */
export interface QualifyProgress {
  steps: QualifyStep[]
  done_count: number
  total: number
  /** 下一个待完成步骤的 key；已完成或已冻结（链路闭合）时为 null。 */
  current: string | null
  frozen: boolean
  /** 已冻结但四步留痕不全（早期预设直接冻结）。空档无法事后补齐，只能新建版本重走。 */
  closed_incomplete: boolean
}

/** 批量就绪度：一次拿全部人物的"还缺什么"，避免一屏列表发 N 个请求。 */
export interface IdentityReadinessBrief {
  ready: boolean
  reasons: string[]
  limited_reference: boolean
  qualify: QualifyProgress
}

/** AC-AV-01 的就绪清单。`limited_reference` 为真时不缺参考图，缺的是多角度真值。 */
export interface IdentityReadiness {
  identity_id: string
  ready: boolean
  reasons: string[]
  reference_policy: ReferencePolicy | null
  limited_reference: boolean
  qualify: QualifyProgress
  gate: string
  checked_at: string
}

export interface AttestResult {
  license: LicenseRecord
  attest_token: string
  /** 后端原样给出的措辞，前端必须照抄展示，不得改写成"已核验"。 */
  attest_note: string
}

/**
 * 受限路径的限制项与人工确认标记，由后端给出原文。
 * 前端不得自行另写一份措辞——确认对象必须是后端这份原文。
 */
/** 官方音色目录的覆盖情况与当前确认口径。 */
export interface VoiceCatalogStatus {
  catalog_size: number
  registered: number
  missing: string[]
  approved_ids: string[]
  total_voices_in_store: number
  excluded_note: string
  /** 确认口径版本；措辞一变旧确认即失效，前端必须回传当前值。 */
  attest_token: string
  attest_note: string
}

/** 把真实试听登记为基准证据的结果。登记成功不等于生产就绪。 */
export interface VoiceBaselineResult {
  voice: VoiceAsset
  source_audio_id: string
  work_id: string
  production_ready: boolean
  production_blockers: string[]
  note: string
}

export interface AvatarPolicyOptions {
  reference_policies: {
    mode: 'multi_view' | 'hero_only'
    label: string
    summary: string
    requires_acknowledgement: boolean
  }[]
  hero_only_limitations: string[]
  attest_token: string
  attest_note: string
}

export interface AvatarVariant {
  id: string
  identity_id: string
  outfit: string
  background: string
  approval_state: 'pending' | 'approved' | 'rejected'
  identity_report: { outcome: CheckOutcome; message: string } | null
  preview_label: string
}

/** 已登记的资产（人物图）。`is_mock` 为真的资产永远不允许伪装成真实产出。 */
export interface AvatarAssetView {
  id: string
  identity_id: string
  kind: string
  version: number
  storage_uri: string
  file_hash: string
  width: number | null
  height: number | null
  license_id: string | null
  is_mock: boolean
  approval_state: 'pending' | 'approved' | 'rejected' | 'revoked'
  note: string
}

/** mask 登记（换景合成的前置）。`state === 'draft'` 表示尚未人工目视确认。 */
export interface MaskSpec {
  id: string
  mask_asset_id: string
  identity_id: string
  source_asset_id: string
  source_file_hash: string
  pixel_size: [number, number]
  origin: string
  state: 'draft' | 'confirmed' | 'rejected'
  confirmed_by: string
  confirmed_at: string | null
  confirm_outcome: 'pass' | 'fail' | 'review' | null
  confirm_note: string
  created_at: string
}

/** 自动分割出的 mask 草稿。`usable` 只表示"未触发已知失效条件"，不代表质量合格。 */
export interface MaskEstimateResult {
  mask_spec: MaskSpec
  asset: AvatarAssetView
  image_url: string
  metrics: Record<string, number>
  problems: string[]
  usable: boolean
  state: string
  warnings: string[]
}

/**
 * 确定性合成变体的结果。
 *
 * `pixel_identical` / `max_channel_diff` 是"硬保护区内像素有没有被动过"的唯一可核对依据：
 * 只有**严格为 0** 才是逐像素相同；`null` 表示无法判定，**不得当作 0 展示**。
 * `registered === false` 说明这是一张候选预览，没有登记成资产，不能拿去建底片。
 */
export interface VariantComposeResult {
  identity_id: string
  derived_from: string
  mask_spec_id: string
  mask_state: string
  background_asset_id: string
  edge_mode: string
  pixel_identical: boolean | null
  max_channel_diff: number | null
  production_ready: boolean | null
  candidate: boolean | null
  blocking_reasons: string[]
  output_sha256: string
  registered: boolean
  storage_uri: string
  file_hash: string
  image_url: string
  asset?: AvatarAssetView
  outfit?: string
  background_label?: string
  warnings: string[]
  preview_id?: string
  note: string
}

/**
 * 新建候选 + 真实出图的结果。
 * `identity` 与 `asset` 都仍是 pending：出图不等于合格，定版只能由人做。
 */
export interface GeneratedIdentityCandidate {
  identity: AvatarIdentity
  asset: AvatarAssetView
  prompt: string
  request_digest: string
  usage: Record<string, unknown>
  image_url: string
  warnings: string[]
}

export interface ReferenceImageSource {
  id: string
  storage_uri: string
  preview_url: string
  file_hash: string
  width: number
  height: number
}

export interface ReferenceImageGenerationInput {
  reference_image_id: string
  appearance: string
  free_text: string
  size?: string
  paid_ack: boolean
  request_id: string
}

export interface ReferenceImageGeneration {
  request_id: string
  state: 'running' | 'succeeded' | 'failed' | 'unknown'
  result: GeneratedIdentityCandidate | null
  error: { code: string; message: string } | null
}

export interface VoiceScope {
  provider: string
  model: string
  region: string
  remote_voice_id: string
  scope_key: string
  complete: boolean
}

export interface BaselineSample {
  voice_asset_id: string
  audio_asset_id: string | null
  file_hash: string
  sample_text: string
  source: string
  available: boolean
  note: string
}

export interface VoiceAsset {
  id: string
  name: string
  provider: string
  model: string
  region?: string
  remote_voice_id: string
  approval_state: 'pending' | 'approved' | 'rejected'
  is_preset: boolean
  sample_text: string
  // 官方音色目录里的口播风格描述与目录分组：仅用于筛选，不构成质量验收结论
  description?: string
  catalog_group?: string
  // 权属记录 id：确认商用范围时要用它定位记录
  license_id?: string | null
  supported_emotions: string[]
  note: string
  // 后端 /voices 会补充作用域与生产就绪判定：
  // production_ready=false 时 production_blockers 逐条说明原因（不静默降级）
  scope?: VoiceScope
  baseline_sample?: BaselineSample
  baseline_audio_asset_id?: string | null
  production_ready?: boolean
  production_blockers?: string[]
  synthesis_profile?: Partial<TtsControls>
  clone_metadata?: Record<string, unknown> | null
}

export type TtsEmotion = 'auto' | 'happy' | 'sad' | 'angry' | 'fearful' | 'disgusted' | 'surprised' | 'calm'

export interface TtsControls {
  emotion: TtsEmotion
  speed: number
  vol: number
  pitch: number
  voice_modify: { pitch: number; intensity: number; timbre: number }
}

export interface VoiceCloneInput {
  request_id: string
  name: string
  filename: string
  content_base64: string
  source_evidence: string
  rights_ack: boolean
  activation_cost_ack: boolean
}

export interface VoiceCloneResult {
  voice: VoiceAsset | null
  clone_job: VoiceCloneJob
  note?: string
}

export interface VoiceCloneJob {
  request_id: string
  state: 'processing' | 'succeeded' | 'failed' | 'unknown' | 'blocked' | string
  stage?: string
  voice_asset_id?: string | null
  error?: { code?: string; message?: string } | string | null
  [key: string]: unknown
}

export interface ScriptQualityCheck {
  code: string
  outcome: CheckOutcome
  hard_gate: boolean
  message: string
}

export interface StoryboardShot {
  shot_index: number
  visual: string
  dialogue: string
  sound_effects: string
  duration_seconds: number
  prompt: string
}

export interface ScriptVersion {
  id: string
  angle: string
  title: string
  description: string
  tags: string[]
  text: string
  content_route?: 'talking_script' | 'short_video_storyboard'
  storyboard?: StoryboardShot[]
  duration_estimate_ms: number
  quality_report: {
    checks: ScriptQualityCheck[]
    overall: CheckOutcome
    review_required: boolean
  }
  claims: { id: string; text: string; verdict: 'supported' | 'refuted' | 'unverified' }[]
  // 口播稿升级：均可缺省（升级前写入的历史稿件没有这些字段）。
  // review_state / pickable / unpickable_reasons 是后端按当前审核复算的投影，页面只读、不自行推断。
  slot?: number | null
  parent_id?: string | null
  batch_id?: string | null
  text_digest?: string | null
  review_state?: 'needs_review' | 'reviewing' | 'passed' | 'failed' | 'stale' | null
  pickable?: boolean | null
  unpickable_reasons?: string[]
  creation_source?: 'manual' | 'llm'
  ai_review_status?: 'not_performed' | 'passed' | 'required'
  takeover_reason?: string
  human_review_state?: 'pending' | 'scored_pass' | 'human_edit' | 'manual_takeover'
  human_review?: HumanReviewRecord | null
}

export type HumanReviewAction = 'adopt' | 'edit_adopt' | 'rewrite'
export type HumanReviewRubric = 'core5' | 'extended8'

export interface HumanReviewRecord {
  id?: string
  mode?: 'scored_pass' | 'human_edit' | 'manual_takeover'
  action?: HumanReviewAction
  rubric?: HumanReviewRubric
  scores?: Record<string, number>
  average?: number | null
  notes?: string
  feedback?: string
  created_at?: string
  [key: string]: unknown
}

export interface HumanReviewContext {
  candidate: ScriptVersion
  content_revision: number
  batch_id: string
  eligible: boolean
  reasons: string[]
  review?: HumanReviewRecord | null
  creation_source?: 'manual' | 'llm'
  ai_review_status?: 'not_performed' | 'passed' | 'required'
  takeover_reason?: string
}

export interface HumanReviewSubmission {
  request_id: string
  script_id: string
  expected_content_revision: number
  expected_batch_id: string
  action: HumanReviewAction
  rubric: HumanReviewRubric
  scores: Record<string, number>
  notes?: string
  feedback?: string
  title?: string
  description?: string
  storyboard?: StoryboardShot[]
  facts_ack?: boolean
}

export interface ManualStoryboardInput {
  request_id: string
  expected_content_revision: number
  title: string
  description?: string
  storyboard: StoryboardShot[]
  takeover_reason: string
  manual_ack: boolean
  facts_ack: boolean
}

export interface ManualStoryboardReceipt {
  ok: boolean
  script_id: string
  batch_id: string
  content_revision: number
  candidate: ScriptVersion
}

export interface DirectGenerationInput {
  request_id: string
  expected_content_revision: number
  topic: string
  materials?: Array<string | MaterialEntry>
  content_route?: 'talking_script' | 'short_video_storyboard'
}

export interface HumanReviewReceipt {
  ok: boolean
  action: HumanReviewAction
  script_id: string
  review?: HumanReviewRecord | null
  job?: ContentJobReceipt | null
}

export interface MaterialEntry {
  text: string
  source?: string
  /** 服务端按资料文本复算的哈希与加入时间：断言"有依据"要能回溯到"依据的哪一版资料"。 */
  content_hash?: string
  added_at?: string
}

export interface ScriptBatch {
  id: string
  work_id: string
  candidates: ScriptVersion[]
  content_route?: 'talking_script' | 'short_video_storyboard'
  ready_to_present?: boolean
  retry_targets?: { candidate_id: string; slot?: number; review_state: string; reason: string }[]
  review_targets?: { candidate_id: string; slot?: number; review_state: string; reason: string }[]
  shortfall_note: string | null
  /** 候选稿来源：llm＝真实模型产出，mock＝模拟三稿，null/undefined＝改动前写入的历史批次（未标注，不追认）。 */
  source_kind?: 'llm' | 'mock' | string | null
  rewrite_count?: number | null
  rewrite_history?: { seq: number; reason: string; candidate_id?: string | null }[]
  /** 出稿后自修复留痕：模型按硬门禁结论把稿子自己改到可采用（不是让用户去改）。 */
  repair_note?: string | null
  repair_history?: {
    round: number
    candidate_id: string
    angle?: string
    outcome: string
    before?: string[]
    after?: string[]
    note?: string
  }[]
  /** 出稿时提供的资料条数，0＝无资料。这是"当时到底有没有资料"的唯一留痕。 */
  materials_count?: number | null
  /** 批次实际产出/通过数：页面如实显示"已通过 X/3"，不写死"三稿已出"。缺失＝升级前的历史批次。 */
  requested?: number | null
  produced?: number | null
  passed?: number | null
  prompt_id?: string | null
  content_revision?: number | null
  job_id?: string | null
  set_digest?: string | null
}

// ---- 口播稿升级：参考、提示、任务 ----
export type ContentJobState =
  | 'queued' | 'running' | 'waiting_external' | 'succeeded' | 'partial'
  | 'blocked' | 'failed' | 'unknown' | 'cancelled' | 'superseded'

export interface ContentJobReceipt {
  job_id: string
  kind: string
  state: ContentJobState
  stage: string | null
  replayed: boolean
  poll_url: string
  result_refs: Record<string, any>
  error: { code: string; message: string; detail?: string } | null
  reference_snapshot_id?: string | null
  media_asset_id?: string | null
  content_revision?: number
  prompt_id?: string | null
  batch_id?: string | null
  cancel_requested?: boolean
  actions?: string[]
  calls?: {
    id: string; step: string; provider: string; model: string; state: string; settlement_state: string | null
    reserved_fen: number | null; actual_cost_fen: number | null; has_remote_handle: boolean
  }[]
}

export interface ContentMetrics { likes: number | null; comments: number | null; shares: number | null; collects: number | null }

export interface ReferenceSearchItem {
  platform_work_id: string
  title: string
  caption: string
  author: string
  cover_url: string | null
  source_url: string
  metrics: ContentMetrics
  published_at: string | null
  duration_ms: number | null
  media_kind: 'video' | 'image_set'
  transcribable: boolean
  fetched_at: string
}

export interface ReferenceTranscript {
  state: 'none' | 'pending' | 'ready' | 'failed' | 'unknown'
  text: string
  source: string
  task_id: string | null
  error: string | null
}

export interface VisualScene {
  index: number
  start_ms: number
  end_ms: number
  shot_type: string
  camera_motion: string
  description: string
  actions: string[]
  on_screen_text: string[]
  beat_cues_ms: number[]
  confidence: number | null
}

/** 画面分析结果：只是模型对画面的观察（灵感），不是事实，也不是口播转写。 */
export interface ReferenceVisual {
  state: 'none' | 'pending' | 'ready' | 'failed' | 'unknown'
  provider?: string
  model?: string
  analysis_version?: string
  media_asset_id?: string
  duration_ms?: number
  summary?: string
  speech_presence?: 'none' | 'present' | 'uncertain' | ''
  music_presence?: 'none' | 'music' | 'uncertain' | ''
  audio?: { tempo_bpm: number | null; beat_times_ms: number[]; confidence: number | null }
  scenes?: VisualScene[]
  warnings?: string[]
  error?: { code: string; message: string } | null
}

export interface MediaAssetView {
  id: string
  duration_ms: number
  width: number
  height: number
  mime_type: string
  sha256: string
  byte_size: number
  source_kind: string
}

export interface VisualQuote {
  call_list?: { model?: string; analysis_version?: string }
  timeout_seconds?: number
  retry_unknown_call?: { call_id: string; sent_at?: string; reserved_fen: number } | null
  request_digest: string
  reserve_fen: number
  estimated_fen: number | null
  price_verified: boolean
  max_ceiling_fen: number | null
  note: string
}

export interface MediaFetchQuote {
  request_digest: string
  reserve_fen: number
  estimated_fen: number | null
  price_verified: boolean
  max_ceiling_fen: number | null
  note: string
}

export interface ReferenceSnapshot {
  id: string
  work_id: string
  platform_work_id: string
  source_url: string
  video_url?: string | null
  media_asset_id?: string | null
  visual?: ReferenceVisual | Record<string, never>
  title: string
  caption: string
  author: string
  cover_url: string | null
  metrics: ContentMetrics
  published_at: string | null
  fetched_at: string
  transcript: ReferenceTranscript
  original_transcript?: ReferenceTranscript | null
  parent_id?: string | null
  evidence_kind: 'inspiration' | 'fact'
  content_digest: string
}

export interface WritingPrompt {
  id: string
  version: number
  source: 'generated' | 'edited'
  parent_id?: string | null
  reference_snapshot_id: string
  reference_analysis: Record<string, any>
  angle_plan: { slot: number; angle: string; new_viewpoint?: string; opening_hint?: string }[]
  prompt_text: string
  model_profile?: Record<string, any>
}

export interface ContentState {
  work_id: string
  content_revision: number
  writer_runtime?: { execution_mode: 'http_api' | 'claude_code'; timeout_seconds?: number; profile_id?: string } | null
  writer_error?: { code: string; message: string } | null
  brief: Work['brief']
  reference: ReferenceSnapshot | null
  style: Record<string, any> | null
  prompt: WritingPrompt | null
  batch: ScriptBatch | null
  review_retry?: {
    call_id: string; job_id: string; batch_id: string; reserved_fen: number | null; has_remote_handle: false
  } | null
  selected_script_id: string | null
  active_jobs: ContentJobReceipt[]
  recent_jobs: ContentJobReceipt[]
  references: {
    id: string; platform_work_id: string; caption: string; author: string; transcript_state: string
    parent_id?: string | null; visual_state?: string; media_asset_id?: string | null; has_video_url?: boolean
  }[]
  media_assets?: MediaAssetView[]
  limits: { per_flow_fen: number | null; daily_fen: number | null; max_text_calls: number | null; concurrency: number; max_repair_rounds: number } | null
}

export interface ScriptRepairReceipt {
  id: string
  parent_id: string
  ready_to_present: false
}

export interface WanStoryboardShot {
  shot_index: number
  visual: string
  dialogue: string
  sound_effects: string
  duration_seconds: number
  image_prompt: string
  wan_prompt: string
  image_state: string
  image_job_id?: string
  image_recovery_available?: boolean
  image_regeneration_available?: boolean
  image_attempt?: number
  video_state: string
  video_job_id?: string | null
  video_regeneration_available?: boolean
  video_attempt?: number
  remote_task_id?: string | null
  image?: { preview_url?: string; storage_uri: string } | null
  clip?: { preview_url?: string; storage_uri: string } | null
  error?: { code: string; message: string } | null
}

export interface WanStoryboardIdentityReference {
  kind: string
  asset_id: string
  asset_hash: string
  identity_id: string
  variant_id: string | null
  storage_uri: string
  label: string
  identity_version: number
  identity_digest: string
  preview_url?: string | null
}

export interface WanStoryboardGraph {
  id: string
  content_revision?: number
  image_reuse?: { source_graph_id: string; shot_indexes: number[] }
  schema_version?: number
  identity_reference?: WanStoryboardIdentityReference | null
  outfit_policy?: 'reference_image' | null
  updated_at?: string
  work_id: string
  script_version_id: string
  state: string
  resolution: string
  image_ceiling_fen_per_shot: number
  plan_digest: string
  task_graph_digest: string
  clip_digest: string
  shots: WanStoryboardShot[]
  estimate: {
    currency: string
    image_calls: number
    video_calls: number
    image_ceiling_total_fen: number
    video_total_fen: number
    shot_prices?: { shot_index: number; ceiling_fen: number; price_snapshot_id?: string }[]
    total_ceiling_fen: number
    budget_unlimited?: boolean
    image_price_known: boolean
    note: string
  }
  image_approval?: { plan_digest: string } | null
  video_approval?: { task_graph_digest: string } | null
  clip_approval?: { clip_digest: string } | null
  output?: {
    preview_url?: string
    storage_uri: string
    file_hash: string
    subtitles_burned?: boolean
    subtitle_files?: { kind: 'subtitles_ass' | 'subtitles_srt'; storage_uri: string; file_hash: string; bytes: number; preview_url?: string }[]
    subtitle_checks?: Record<string, unknown>
  } | null
  subtitle_generation_available?: boolean
  subtitles?: { state: 'queued' | 'running' | 'failed' | 'succeeded' | 'unknown'; job_id: string; error?: { code: string; message: string } | null } | null
  video_id?: string
  package_id?: string
  package_error?: { code: string; message: string } | null
  error?: { code: string; message: string } | null
}

export interface SelectionRequest {
  id: string
  work_id: string
  kind: SelectionKind
  state: 'pending' | 'selected' | 'superseded' | 'cancelled'
  options: { ref_id: string; version: number; digest: string; label: string; quality: CheckOutcome }[]
}

export interface AudioAssetInfo {
  id: string
  kind: 'preview' | 'full'
  voice_asset_id: string
  duration_ms: number
  approved: boolean
  confirmed: boolean
  url: string | null
  checks: ScriptQualityCheck[]
  // 以下是 T04 起补的字段：mock 产物、来源哈希与绑定关系都必须对前端可见，
  // 否则界面会把占位产物显示得和真实音频一样。
  work_id?: string
  is_mock?: boolean
  file_hash?: string
  storage_uri?: string
  script_version_id?: string | null
  performance_plan_id?: string | null
  timing_source?: string | null
  sample_rate?: number | null
  channels?: number | null
  synthesis_profile?: Partial<TtsControls>
  tts_controls_digest?: string
  tts_controls_revision?: number
  invalidated?: boolean
}

export interface VoicePreviewResult {
  cached: boolean
  message: string
  note: string
  sample_sentence: string | null
  audio: AudioAssetInfo
}

export interface ControlEntry {
  control: string
  segment_id?: string
  requested?: string
  applied?: unknown
  applied_as: string
  reason: string
}

export interface FullAudioResult {
  audio: AudioAssetInfo
  is_mock: boolean
  duration_ms: number
  timing_source: string | null
  hard_failures: ScriptQualityCheck[]
  control_summary: Record<string, number>
  unsupported_controls: ControlEntry[]
  note: string
}

export interface AlignmentStatus {
  aligned: boolean
  word_count: number
  claimable: boolean
  source?: string
  method?: string
  reason?: string
}

/** POST /works/{id}/alignment/run 的返回：登记结果 + 登记后的对齐状态。 */
export interface AlignmentRunResult {
  ok: boolean
  reused: boolean
  alignment_id: string | null
  word_count: number
  source?: string | null
  method?: string | null
  transcript?: string
  alignment: AlignmentStatus
  note?: string
}

export interface AudioGateSummary {
  work_id: string
  voice_asset_id: string | null
  full_audio_count: number
  preview_count: number
  final_audio_id: string | null
  final_audio_duration_ms: number | null
  audio_confirmed: boolean
  alignment: AlignmentStatus
}

export interface CostEstimate {
  route: GenerationRoute
  estimated_fen: number
  ceiling_fen: number
  currency: 'CNY'
  price_basis: string
  billable: boolean
  note: string
}

export interface TaskEvent {
  seq: number
  type: string
  occurred_at: string
  stage: PipelineStage | null
  state: TaskState | null
  message: string
}

export interface Task {
  id: string
  work_id: string
  mode: 'mock' | 'integration' | 'production'
  stage: PipelineStage
  state: TaskState
  route: GenerationRoute
  route_confirmed: boolean
  percent: number | null
  progress_source: 'provider' | 'measured' | 'indeterminate'
  pending_selection_ids: string[]
  block_reason: string | null
  last_error: { code: string; message: string; action: string } | null
  stage_rows: {
    stage: PipelineStage
    label: string
    state: 'pending' | 'active' | 'done' | 'blocked' | 'failed'
    detail: string
  }[]
  created_at: string
}

export interface PublishPackage {
  generation_mode?: string
  sound_mode?: string
  id: string
  work_id: string
  task_id: string
  mode: string
  state: 'pending' | 'ready'
  title: string
  description: string
  tags: string[]
  ai_label: string
  files: { path: string; bytes: number; sha256: string }[]
  video_url: string | null
  cover_url: string | null
  download_url: string | null
  manifest_digest: string
}

export type PublishTaskState =
  | 'draft' | 'awaiting_login' | 'uploading' | 'ready_to_confirm'
  | 'submitting' | 'submitted' | 'needs_verification' | 'failed' | 'cancelled'

export interface PublishSessionView {
  state: 'awaiting_login' | 'logged_in' | 'unavailable' | 'needs_verification' | 'busy'
  adapter: 'playwright' | 'mock'
  account_id?: string
  account_label?: string
  code?: string
  message?: string
}

export interface PublishTaskView {
  id: string | null
  work_id: string
  platform?: string
  adapter?: string
  state: PublishTaskState
  package_id?: string
  package_digest?: string
  confirmation_digest?: string
  revision?: number
  title?: string
  description?: string
  tags?: string[]
  scheduled_at?: string | null
  provider_ref?: string | null
  provider_url?: string | null
  error?: { code: string; message: string; retryable?: boolean } | null
  requires_user_confirmation?: boolean
  account_id?: string
  account_label?: string
  is_mock?: boolean
  message?: string
}

export interface PostprocessView {
  error?: { code: string; message: string } | null
  task: Task | null
  execution: { id: string; state?: string; error?: { message?: string }; message?: string } | null
  preview_url: string | null
  cover_url: string | null
  files: { path: string; bytes: number; sha256: string }[]
  ready_for_review: boolean
}

export interface ApiErrorBody {
  code: string
  message: string
  retryable: boolean
  action: string | null
}

// ---------- T05 数字人口播 ----------
//
// 这里的字段全部对齐后端 `app/digital_human` 的实际返回。
// 三条不可省略的表达能力：
//   1) declared（服务方文档口径）与 verified（本项目实测）分列——合并成一个
//      "可用"会让没测过的东西看起来能用；
//   2) is_mock 必须逐条可见——占位产物不得被显示得像真产物；
//   3) 门禁是三态（pass / fail / not_run）——"还没审"不能渲染成"审过了"。

export type RouteVerifiedState = 'unverified' | 'partially_verified' | 'verified'
export type WarningKind = 'rejected' | 'blocked'
export type GateOutcome = 'pass' | 'fail' | 'not_run'
export type VideoKind = 'plate' | 'original'
export type RunState =
  | 'intent'
  | 'queued'
  | 'leased'
  | 'submitted'
  | 'waiting_provider'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'unknown'
  | 'blocked'
  | 'cancelled'

/** 成本档位由后端声明，界面不得由路线名称推定免费。 */
export type CostTier = 'low' | 'high'

export interface BodyMotionClaim {
  supported: boolean
  scope: string
  verified: boolean
  verified_note: string
}

/** 选择路线前必须展示给用户的费用提示。没有计费时长时金额为空，界面不得编数。 */
export interface CostNotice {
  route: GenerationRoute
  cost_tier: CostTier
  cost_tier_label: string
  notice_version: string
  requires_acknowledgement: boolean
  unit_text: string
  body_motion: BodyMotionClaim
  estimated_fen: number | null
  ceiling_fen: number | null
  amount_known: boolean
  amount_pending_reason: string
  risks: string[]
  excluded: string[]
  note: string
  audio_seconds?: number
  billing_seconds?: number | null
  unit_fen?: number
  price_basis?: string
}

export interface RouteCapability {
  route: GenerationRoute
  label: string
  provider: string
  model: string
  capability: string
  consumes: string[]
  requires_motion_plate: boolean
  stage_count: number
  heavy: boolean
  cost_tier: CostTier
  cost_tier_label: string
  cost_notice_required: boolean
  cost_summary: string
  body_motion: BodyMotionClaim
  cost_notice: CostNotice
  declared: Record<string, unknown>
  verified: Record<string, unknown>
  evidence: string[]
  open_questions: string[]
  verified_state: RouteVerifiedState
}

export interface PriceSnapshot {
  id: string
  route: GenerationRoute
  provider: string
  unit: string
  unit_fen: number
  price_source: string
  billing_verified: boolean
  rounding_note: string
  minimum_note: string
  failure_billing_note: string
  evidence: string
  created_at: string
}

export interface CostScenario {
  scenario: string
  estimated_fen: number
  ceiling_fen: number
  unit_fen: number
  per_day_fen: number
  monthly_fen: number
  excluded: string[]
  note: string
}

export interface RoutePriceView {
  routes: RouteCapability[]
  price_snapshots: PriceSnapshot[]
  scenarios: CostScenario[]
  cost_notice_version: string
  /** 作品当前的路线（可能是"还没选"时的默认值，见 route_confirmed） */
  selected_route: GenerationRoute | null
  /** 是否已有一次可追溯的人工路线确认：默认值不是授权，界面据此区分两者 */
  route_confirmed: boolean
  note: string
}

export interface PreflightBlocker {
  code: string
  message: string
  kind: WarningKind
}

export interface MotionIntentView {
  state: 'unspecified' | 'requested' | 'unsupported' | 'missing' | 'resolved' | 'rejected' | 'blocked' | 'free_text' | string
  free_text?: string
  reason: string
  code?: string
  motion_key?: string
  motion_ref_id?: string
}

/** `/identities/{id}/motions`：目录给"有哪些动作"，approved 给"这个人现在真能用哪条"。
 *  界面只该列 approved——目录里的键没登记参考时解析会被拒（MOTION_NOT_APPROVED）。 */
export interface MotionReferenceView {
  id: string
  identity_id: string
  motion_key: string
  label: string
  description: string
  asset_id: string
  approval_state: 'pending' | 'approved' | 'rejected' | string
  loop_seconds: number | null
  supported_params?: Record<string, unknown>
  note?: string
}

export interface MotionCatalogView {
  catalog: Record<string, { label: string; params: Record<string, unknown> }>
  approved: MotionReferenceView[]
}

/** 已有真实参考库：只有 available 且可预览的项目才允许用户添加。 */
export interface MotionTemplateView {
  id: string
  label: string
  description: string
  motion_key: string
  loop_seconds: number | null
  preview_url: string | null
  source_identity_name: string
  note: string
  available: boolean
  unavailable_reason: string | null
}

export interface MotionTemplateLibrary {
  templates: MotionTemplateView[]
  note?: string
}

/** 已上传的自定义动作参考。上传完成后仍需用户预览并确认来源，不能把 pending 当成可用动作。 */
export interface MotionUploadView {
  id: string
  identity_id: string
  state: 'pending' | 'confirmed' | string
  original_filename: string
  duration_seconds: number
  width: number
  height: number
  size_bytes: number
  preview_url: string | null
  reference_id: string | null
}

/** 动作意图落库结果。motion_ref_id 是**绑定**：同 key 的另一条参考不得顶替。 */
export interface MotionIntentResult {
  work_id: string
  motion_intent: { motion_key: string; params: Record<string, number>; motion_ref_id: string }
}

/** 变体准入判定的原始结论：不合格时 reasons 逐条给出，便于界面直接展示。 */
export interface VariantAdmitResult {
  ok: boolean
  outcome: string
  asset_id: string
  identity_id: string
  reasons: string[]
}

export interface TalkingHeadPreflight {
  work_id: string
  route: GenerationRoute | string
  ok: boolean
  blockers: PreflightBlocker[]
  rejected: PreflightBlocker[]
  identity_id: string | null
  variant_id: string | null
  script_version_id: string | null
  performance_plan_id: string | null
  audio_asset_id: string | null
  audio_duration_ms: number | null
  plate_id: string | null
  motion_intent: MotionIntentView
  note: string
}

export interface SegmentItem {
  index: number
  segment_id: string
  start_ms: number
  end_ms: number
  duration_ms: number
  global_offset_ms: number
  over_limit: boolean
  text: string
  single_call: boolean
}

export interface SegmentPlanView {
  work_id: string
  planned: boolean
  reason?: string
  plan_id?: string
  audio_asset_id?: string
  segments?: SegmentItem[]
  limit_ms?: number
  note?: string
}

/**
 * POST /talking-head/segments 落库后返回的**计划记录**。
 *
 * 与 GET 的 `SegmentPlanView` 形状不同：记录里没有 `planned`/`plan_id`，
 * 却有 `alignment_id`/`text_digest` 这类追溯字段。两者不可混用——
 * 把记录当视图用会让界面以为计划还没建立（`planned` 缺失）。
 */
export interface SegmentPlanRecord {
  id: string
  work_id: string
  audio_asset_id: string
  audio_digest: string
  audio_duration_ms: number
  alignment_id: string | null
  alignment_source: string | null
  text_digest: string
  limit_ms: number
  segments: SegmentItem[]
  summary: Record<string, unknown>
  created_at: string
  note: string
}

export interface PlateItem {
  id: string
  duration_ms: number
  approval_state: 'pending' | 'approved' | 'rejected'
  is_mock: boolean
  file_hash: string
}

export interface PlateStatusView {
  work_id: string
  plate_count: number
  approved_plate_id: string | null
  approved: boolean
  plates: PlateItem[]
  preparation?: {
    ok: boolean
    blockers: PreflightBlocker[]
    source: {
      kind: 'hero' | 'variant'
      asset_id: string
      asset_hash: string
      variant_id: string | null
      identity_id: string
      storage_uri: string
      url?: string
      label: string
    } | null
  }
}

export interface WanImageSource {
  asset_id: string
  asset_hash: string
  storage_uri: string
  preview_url: string | null
  label: string
  width: number
  height: number
}

export interface TalkingHeadCallList {
  route: GenerationRoute
  audio_id: string
  audio_hash: string
  variant_id: string | null
  motion_ref_id: string | null
  plate_id: string | null
  prompt: string
  resolution: string
  segment_count: number
  digest: string
  identity_id?: string | null
  source_asset_id?: string | null
  source_asset_hash?: string
  model?: string
  generated_seconds?: number
  generation_mode?: string
  audio_mode?: string
  sound_mode?: string
  final_audio_mode?: string
  effective_prompt?: string
  audio_instruction_version?: string
}

export interface CostConfirmation {
  id: string
  work_id: string
  route: GenerationRoute
  provider: string
  model: string
  request_digest: string
  call_list?: TalkingHeadCallList | null
  estimated_fen: number
  ceiling_fen: number
  segment_count: number
  generated_seconds: number
  resolution: string
  accepted_risks: string[]
  actor_id: string
  confirmed_at: string
  valid_until: string | null
  expiry_policy?: string
  expiry_note?: string
  authorizes_payment: boolean
  note: string
}

export interface TalkingHeadCostView {
  work_id: string
  route: GenerationRoute | null
  confirmation: CostConfirmation | null
  current_call_list?: TalkingHeadCallList | null
  confirmation_valid?: boolean
  changed_fields?: string[]
  estimate: CostEstimate & { billing_verified?: boolean; segment_count?: number; billing_seconds?: number | null } | null
  budget: {
    per_task_limit_fen: number
    daily_limit_fen: number
    max_billable_submissions: number
    spent_today_fen: number
    submissions_today: number
  }
  price_snapshots: Record<string, PriceSnapshot>
  billable_allowed: boolean
  blocker?: { code: string; message: string } | null
  note: string
}

export interface ProviderRun {
  id: string
  work_id: string
  task_id: string | null
  stage: string
  route: GenerationRoute
  provider: string
  model: string
  capability: string
  request_digest: string
  idempotency_key: string
  state: RunState
  remote_job_id: string | null
  result_asset_ids: string[]
  cost_reservation_id: string | null
  is_mock: boolean
  provider_params: Record<string, unknown>
  last_error: { code: string; message: string } | null
  created_at: string
}

export interface SubmissionResult {
  submitted: boolean
  is_mock: boolean
  route: GenerationRoute
  runs: ProviderRun[]
  segments: SegmentItem[]
  estimate: CostEstimate
  // 云端并发上限由账号决定（免费试用为 1）。超过上限的分段不在本次请求里提交，
  // 而是由后台推进器按"前一段到终态 → 提交下一段"续发，因此这两个字段要露出来，
  // 否则界面会把"还有 6 段没提交"误读成"只切出了一段"。
  max_concurrency?: number
  pending_segments?: number
  driver_started?: boolean
  note: string
}

export interface SubmissionListView {
  work_id: string
  runs: ProviderRun[]
  auto_fallback: boolean
  note: string
}

/**
 * 分段推进进度（只读快照）。
 *
 * `state` 的四种取值直接对应界面该显示什么：
 * - `IDLE`    没有运行记录：还没提交过，没有可续发的段
 * - `PENDING` 还有段没发：显示"继续生成剩余分段"
 * - `DONE`    全部段已生成
 * - `STOPPED` 有段 failed/unknown：先对账或重新授权，续发不会自动越过
 */
export interface TalkingHeadDriverView {
  work_id: string
  state: 'IDLE' | 'PENDING' | 'DONE' | 'STOPPED'
  total: number
  succeeded: number
  in_flight: number
  pending: number
  failed: number
  unknown: number
  /** 现在点"继续生成剩余分段"是否真的会推进（STOPPED/IDLE/DONE 一律为 false）。 */
  resumable: boolean
  stop_reason: 'UNKNOWN_REQUIRES_RECONCILIATION' | 'SEGMENT_FAILED' | null
  blocked_runs: string[]
  attempt: string | null
  note: string
  /** 仅 resume 接口返回：本次调用是否真的新起了推进线程。 */
  started?: boolean
}

export interface VideoAsset {
  generation_mode?: string
  sound_mode?: string
  id: string
  work_id: string
  kind: VideoKind
  route: GenerationRoute
  provider: string
  model: string
  storage_uri: string
  url?: string | null
  file_hash: string
  duration_ms: number
  width: number
  height: number
  fps: number
  has_video_track: boolean
  has_audio_track: boolean
  is_mock: boolean
  identity_id: string | null
  variant_id: string | null
  audio_asset_id: string | null
  motion_ref_id: string | null
  segment_index: number | null
  segment_count: number | null
  global_offset_ms: number
  approval_state: 'pending' | 'approved' | 'rejected' | 'revoked'
  confirmed: boolean
  checks: ScriptQualityCheck[]
  note: string
  created_at: string
  hard_failures: ScriptQualityCheck[]
}

export interface AudioTrackStatus {
  checked: boolean
  outcome: GateOutcome | 'invalidated'
  reason: string
  recorded_outcome?: string
  measured_offset_ms?: number | null
  method?: string
  audio_asset_id?: string
  final_audio_id?: string | null
  record_id?: string
}

export interface VideoGateView {
  video_id: string
  kind: VideoKind
  gate: {
    ok: boolean
    outcome: GateOutcome
    review_state: GateOutcome
    audio_track_state: GateOutcome
    dimensions: Record<string, string>
    audio_track: AudioTrackStatus
    review_id: string | null
    reasons: string[]
  }
  audio_track: AudioTrackStatus
  hard_failures: ScriptQualityCheck[]
  note: string
}

export interface TalkingHeadGateSummary {
  work_id: string
  route: GenerationRoute
  route_selection_id: string | null
  plate_count: number
  original_count: number
  final_video_id: string | null
  original_confirmed: boolean
  gate: VideoGateView['gate'] | null
  note: string
}

export interface QueueView {
  work_id: string | null
  gpu_holder: string | null
  counts: Record<string, number>
  heavy_jobs: { id: string; work_id: string; capability: string; state: RunState }[]
  serialized: boolean
  note: string
}

export interface AdapterStatus {
  provider: string
  model: string
  endpoint?: string
  configured: boolean
  state: 'not_configured' | 'unverified' | 'ready'
  declared?: Record<string, unknown>
  verified?: Record<string, unknown>
  evidence?: string[]
  detail: string
}

export interface AdapterStatusView {
  omnihuman: AdapterStatus
  m2_worker: AdapterStatus
  wan?: AdapterStatus
  dashscope_wan?: AdapterStatus
}
