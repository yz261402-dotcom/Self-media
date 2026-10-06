// 真实 HTTP 实现：调用 FastAPI 后端（开发期经 Vite 代理）。
import type {
  AdapterStatusView,
  AlignmentRunResult,
  AudioAssetInfo,
  AttestResult,
  AudioGateSummary,
  AvatarAssetView,
  AvatarIdentity,
  AvatarPolicyOptions,
  AvatarVariant,
  GeneratedIdentityCandidate,
  ReferenceImageSource,
  ReferenceImageGenerationInput,
  ReferenceImageGeneration,
  Capabilities,
  CostEstimate,
  DeletionReceipt,
  FeatureEntry,
  FullAudioResult,
  GenerationRoute,
  IdentityReadiness,
  IdentityReadinessBrief,
  MaskEstimateResult,
  MaskSpec,
  MotionCatalogView,
  MotionIntentResult,
  MotionReferenceView,
  MotionTemplateLibrary,
  MotionUploadView,
  PlateStatusView,
  PublishPackage,
  PublishTaskView,
  PublishSessionView,
  PostprocessView,
  QueueView,
  RoutePriceView,
  ContentJobReceipt,
  ContentState,
  MediaAssetView,
  VisualQuote,
  MediaFetchQuote,
  ReferenceSnapshot,
  WanStoryboardGraph,
  ScriptBatch,
  ScriptVersion,
  ScriptRepairReceipt,
  SegmentPlanRecord,
  SegmentPlanView,
  SubmissionListView,
  SubmissionResult,
  TalkingHeadCostView,
  TalkingHeadDriverView,
  TalkingHeadGateSummary,
  TalkingHeadPreflight,
  Task,
  TaskEvent,
  TtsControls,
  VariantAdmitResult,
  VariantComposeResult,
  VideoAsset,
  VideoGateView,
  VoiceAsset,
  VoiceBaselineResult,
  VoiceCatalogStatus,
  VoiceCloneInput,
  VoiceCloneJob,
  VoiceCloneResult,
  VoicePreviewResult,
  Work,
  WanImageSource,
} from './types'

export class ApiError extends Error {
  code: string
  retryable: boolean
  action: string | null
  status: number | null
  /** 仅内容任务超时（CONTENT_JOB_PENDING）等场景有：指向原持久任务，客户端据此轮询，不要重复提交。 */
  jobId: string | null
  pollUrl: string | null
  constructor(code: string, message: string, retryable = false, action: string | null = null, status: number | null = null,
              jobId: string | null = null, pollUrl: string | null = null) {
    super(message)
    this.code = code
    this.retryable = retryable
    this.action = action
    this.status = status
    this.jobId = jobId
    this.pollUrl = pollUrl
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError('NETWORK_ERROR', '无法连接后端服务，请确认 FastAPI 已启动（127.0.0.1:8000）', true)
  }
  const text = await res.text()
  // 不能直接 JSON.parse：后端未启动时 Vite 代理会回纯文本 500（"Internal Server Error"），
  // 未捕获异常的 uvicorn 同理。裸解析会把这两种情况变成无信息的 SyntaxError。
  let data: any = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      const head = text.trim().replace(/\s+/g, ' ').slice(0, 120)
      throw new ApiError(
        'NON_JSON_RESPONSE',
        `后端未返回 JSON（HTTP ${res.status}）：${head}。请确认 FastAPI 正在运行（127.0.0.1:8000）`,
        true,
        '检查后端进程是否存活，然后重试',
      )
    }
  }
  if (!res.ok) {
    const e = data?.error ?? {}
    throw new ApiError(e.code ?? 'UNKNOWN', e.message ?? `请求失败（HTTP ${res.status}）`, !!e.retryable, e.action ?? null, res.status,
      e.job_id ?? null, e.poll_url ?? null)
  }
  return data as T
}

const post = <T>(path: string, body: unknown) =>
  req<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

/** multipart 上传：不能带固定的 JSON Content-Type，边界由浏览器生成。 */
async function upload<T>(path: string, form: FormData): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api/v1${path}`, { method: 'POST', body: form })
  } catch {
    throw new ApiError('NETWORK_ERROR', '无法连接后端服务，请确认 FastAPI 已启动（127.0.0.1:8000）', true)
  }
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch {
    throw new ApiError('NON_JSON_RESPONSE', `后端未返回 JSON（HTTP ${res.status}）`, true)
  }
  if (!res.ok) {
    const e = data?.error ?? data?.detail?.error ?? {}
    throw new ApiError(e.code ?? 'UNKNOWN', e.message ?? `上传失败（HTTP ${res.status}）`, !!e.retryable, e.action ?? null, res.status)
  }
  return data as T
}

export const httpApi = {
  listWanStoryboards: (workId: string) => req<{ graphs: WanStoryboardGraph[] }>(`/works/${encodeURIComponent(workId)}/wan/storyboards`),
  createWanStoryboard: (workId: string, input: { resolution: string; image_ceiling_fen_per_shot: number }) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards`, input),
  getWanStoryboard: (workId: string, graphId: string) =>
    req<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}`),
  approveWanStoryboardImages: (workId: string, graphId: string, input: { plan_digest: string; total_ceiling_fen: number; paid_ack: boolean; actor_id: string }) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/images/approve`, input),
  generateWanStoryboardImages: (workId: string, graphId: string) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/images/generate`, {}),
  recoverWanStoryboardImage: (workId: string, graphId: string, shotIndex: number) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/shots/${shotIndex}/image/recover`, {}),
  regenerateWanStoryboardImage: (workId: string, graphId: string, shotIndex: number, input: {
    actor_id: string; paid_ack: boolean; duplicate_charge_ack: boolean; plan_digest: string;
    expected_image_job_id: string; request_id: string; ceiling_fen: number
  }) => post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/shots/${shotIndex}/image/regenerate`, input),
  approveWanStoryboardVideos: (workId: string, graphId: string, input: { task_graph_digest: string; total_ceiling_fen: number; paid_ack: boolean; image_review_ack: boolean; actor_id: string }) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/videos/approve`, input),
  submitWanStoryboardVideos: (workId: string, graphId: string) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/videos/submit`, {}),
  regenerateWanStoryboardVideo: (workId: string, graphId: string, shotIndex: number, input: {
    actor_id: string; paid_ack: boolean; duplicate_charge_ack: boolean; task_graph_digest: string;
    expected_video_job_id: string; request_id: string; ceiling_fen: number
  }) => post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/shots/${shotIndex}/video/regenerate`, input),
  pollWanStoryboardShot: (workId: string, graphId: string, shotIndex: number) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/shots/${shotIndex}/poll`, {}),
  approveWanStoryboardClips: (workId: string, graphId: string, input: { clip_digest: string; review_ack: boolean; actor_id: string }) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/clips/approve`, input),
  queueWanStoryboardStitch: (workId: string, graphId: string) =>
    post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/stitch`, {}),
  generateWanStoryboardSubtitles: (workId: string, graphId: string, input: {
    actor_id: string; compute_ack: boolean; expected_video_id: string; output_digest: string; request_id: string
  }) => post<WanStoryboardGraph>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/subtitles`, input),
  confirmWanStoryboard: (workId: string, graphId: string, input: { actor_id: string; review_ack: boolean; video_id: string; output_digest: string }) =>
    post<{ ok: boolean; graph: WanStoryboardGraph; package: PublishPackage }>(`/works/${encodeURIComponent(workId)}/wan/storyboards/${encodeURIComponent(graphId)}/confirm`, input),
  getPostprocess: (workId: string) => req<PostprocessView>(`/works/${workId}/postprocess`),
  startPostprocess: (workId: string) => post<Task>(`/works/${workId}/postprocess`, {}),
  confirmPostprocess: (workId: string, executionId: string) => post<PublishPackage>(`/works/${workId}/postprocess-confirmation`, {
    review_ack: true, actor_id: 'user_local', execution_id: executionId,
  }),
  getCapabilities: () => req<Capabilities>('/capabilities'),
  getFeatures: () => req<FeatureEntry[]>('/features'),

  listWorks: () => req<Work[]>('/works'),
  deleteWork: (workId: string) => req<DeletionReceipt>(`/works/${encodeURIComponent(workId)}`, {
    method: 'DELETE', body: JSON.stringify({ confirmed: true }),
  }),
  deleteIdentity: (identityId: string) => req<DeletionReceipt>(`/avatar-identities/${encodeURIComponent(identityId)}`, {
    method: 'DELETE', body: JSON.stringify({ confirmed: true }),
  }),
  createWork: (input: {
    name: string
    topic: string
    content_type: string
    audience: string
    keywords: string[]
    target_duration_ms: number
  }) => post<Work>('/works', input),

  // 默认只拿"在工作列表里"的人物；已丢弃的候选要单独索取（记录没被删，只是移出视线）。
  listIdentities: (includeDiscarded = false) =>
    req<AvatarIdentity[]>(`/avatar-identities${includeDiscarded ? '?include_discarded=true' : ''}`),
  // 一次拿全部人物的"还缺什么"：逐条问会让一屏列表发 N 个请求。
  identitiesReadiness: (includeDiscarded = false) =>
    req<Record<string, IdentityReadinessBrief>>(
      `/avatar-identities/readiness${includeDiscarded ? '?include_discarded=true' : ''}`,
    ),
  // 丢弃 ≠ 删除：只移出工作列表并记录谁/何时/为什么，可 restore 回来。
  discardIdentity: (identityId: string, reason: string) =>
    post<AvatarIdentity>(`/avatar-identities/${identityId}/discard`, {
      actor: 'user_local',
      reason,
    }),
  restoreIdentity: (identityId: string) =>
    post<AvatarIdentity>(`/avatar-identities/${identityId}/restore`, { actor: 'user_local' }),
  copyIdentityDraft: (identityId: string) =>
    post<AvatarIdentity>(`/identities/${identityId}/draft-copy`, { actor_id: 'user_local' }),
  createIdentityCandidates: (input: { gender: string; age: string; appearance: string; free_text: string }) =>
    post<AvatarIdentity[]>('/avatar-identities/candidates', input),
  createIdentityCandidateWithImage: (input: {
    gender: string
    age: string
    appearance: string
    free_text: string
    size?: string
  }) => post<GeneratedIdentityCandidate>('/avatar-identities/candidates/with-image', input),
  uploadAvatarReferenceImage: (file: File) =>
    req<ReferenceImageSource>('/avatar-identities/reference-images', {
      method: 'POST', headers: { 'Content-Type': file.type }, body: file,
    }),
  createIdentityCandidateFromImage: (input: ReferenceImageGenerationInput) =>
    post<GeneratedIdentityCandidate>('/avatar-identities/candidates/from-image', input),
  getAvatarImageGeneration: (requestId: string) =>
    req<ReferenceImageGeneration>(`/avatar-identities/image-generations/${encodeURIComponent(requestId)}`),
  selectIdentity: (workId: string, identityId: string) =>
    post<Work>(`/works/${workId}/identity`, { identity_id: identityId }),

  // ---- 人物定版链：审核 → 声明参考依据 → 权属确认 → 冻结 ----
  // 每一步的"判定人"都是 user_local：系统不替人做身份判断，也不替人核发商用通行。
  identityReadiness: (identityId: string) =>
    req<IdentityReadiness>(`/identities/${identityId}/readiness`),
  avatarPolicyOptions: () => req<AvatarPolicyOptions>('/avatar/policy-options'),
  listIdentityAssets: (identityId: string, kind?: string) =>
    req<AvatarAssetView[]>(`/identities/${identityId}/assets${kind ? `?kind=${kind}` : ''}`),
  reviewIdentityAsset: (
    identityId: string,
    assetId: string,
    input: { outcome: 'pass' | 'reject' | 'review'; identity_digest: string; method: string; note?: string },
  ) =>
    post<Record<string, unknown>>(`/identities/${identityId}/assets/${assetId}/review`, {
      reviewer: 'user_local',
      ...input,
    }),
  setReferencePolicy: (
    identityId: string,
    input: { mode: 'multi_view' | 'hero_only'; reason: string; acknowledged: string[] },
  ) =>
    post<AvatarIdentity>(`/identities/${identityId}/reference-policy`, {
      reviewer: 'user_local',
      ...input,
    }),
  freezeIdentity: (identityId: string, input: { hero_asset_id: string; identity_digest: string; license_id: string }) =>
    post<AvatarIdentity>(`/identities/${identityId}/freeze`, { reviewer: 'user_local', ...input }),
  attestLicense: (licenseId: string, basis: string, ackToken: string) =>
    post<AttestResult>(`/avatar-licenses/${licenseId}/attest`, {
      reviewer: 'user_local',
      basis,
      ack_token: ackToken,
    }),

  listVariants: (workId: string) => req<AvatarVariant[]>(`/works/${workId}/variants`),
  // 登记变体。给了 asset_id 就**引用已登记的那张资产**（确定性合成的产物），
  // 不再重复登记一份——同一张图两个 id 会让来源链断掉。
  createVariant: (
    workId: string,
    input: { asset_id?: string; outfit: string; background: string; identity_digest?: string },
  ) => post<AvatarVariant>(`/works/${workId}/variants`, input),

  // ---- 换装/换景变体：mask 草稿 → 人目视确认 → 确定性合成 ----
  listMasks: (identityId: string) =>
    req<{ identity_id: string; masks: MaskSpec[]; note: string }>(`/identities/${identityId}/masks`),
  createMask: (
    identityId: string,
    input: { method?: string; alpha_source?: string; component_mode?: string } = {},
  ) => post<MaskEstimateResult>(`/identities/${identityId}/masks`, input),
  // 确认是一次人工目视动作：后端要求操作者以 user 开头，任何"指标全绿即通过"的路径都不存在。
  confirmMask: (maskAssetId: string, outcome: 'pass' | 'fail' | 'review', note = '') =>
    post<MaskSpec>(`/avatar-masks/${maskAssetId}/confirm`, {
      reviewer: 'user_local',
      outcome,
      note,
    }),
  composeVariant: (
    identityId: string,
    input: {
      mask_asset_id: string
      background_asset_id: string
      edge_mode?: string
      person_scale?: number
      outfit?: string
      background_label?: string
      commit?: boolean
    },
  ) => post<VariantComposeResult>(`/identities/${identityId}/variants/compose`, input),

  listVoices: () => req<VoiceAsset[]>('/voices'),
  /** 官方音色目录覆盖情况 + 当前确认口径（attest_token 随口径版本变化）。 */
  getVoiceCatalogStatus: () => req<VoiceCatalogStatus>('/voices/catalog-status'),
  /** 用户显式选定候选音色即批准。批准 ≠ 通过生产质量验收（基准证据仍需单独补齐）。 */
  approveVoice: (voiceId: string) => post<VoiceAsset>(`/voices/${voiceId}/approve`, {}),
  /** 把该作品里这条音色的真实试听登记为基准证据。Mock 产物会被后端拒收。 */
  registerVoiceBaseline: (voiceId: string, workId: string) =>
    post<VoiceBaselineResult>(`/voices/${voiceId}/baseline`, { work_id: workId }),
  createVoicePreview: (workId: string, voiceId: string, sampleText: string, forceNew = false) =>
    post<VoicePreviewResult>('/voice-previews', { work_id: workId, voice_asset_id: voiceId, sample_text: sampleText, force_new: forceNew }),
  selectVoice: (workId: string, voiceId: string) =>
    post<Work>(`/works/${workId}/voice`, { voice_asset_id: voiceId }),
  updateTtsControls: (workId: string, controls: TtsControls) =>
    req<Work>(`/works/${encodeURIComponent(workId)}/tts-controls`, { method: 'PUT', body: JSON.stringify({ controls }) }),
  getTtsControls: (workId: string) => req<{ controls: TtsControls; digest: string }>(`/works/${encodeURIComponent(workId)}/tts-controls`),
  createVoiceClone: (input: VoiceCloneInput) => post<VoiceCloneResult>('/voice-clones', input),
  getVoiceClone: (requestId: string) => req<VoiceCloneJob>(`/voice-clones/${encodeURIComponent(requestId)}`),

  generateScripts: (workId: string, topic: string, materials: string[] = []) =>
    post<ScriptBatch>(`/works/${workId}/scripts`, {
      topic,
      // 逐行参考资料：留空也行，此时生成侧避开需要外部依据的事实断言。
      materials: materials.map((text) => ({ text })),
    }),
  getScriptBatch: (workId: string) => req<ScriptBatch | null>(`/works/${workId}/scripts`),
  selectScript: (workId: string, scriptId: string, expectedContentRevision?: number) =>
    post<{ ok: boolean; message: string }>(`/works/${workId}/script-selection`, {
      script_id: scriptId,
      ...(expectedContentRevision === undefined ? {} : { expected_content_revision: expectedContentRevision }),
    }),

  // ---- 口播稿升级（content-v2）：写操作 202 + 回执，读操作不触发外部请求 ----
  getContentState: (workId: string) => req<ContentState>(`/works/${encodeURIComponent(workId)}/content/state`),
  getReferenceSnapshot: (workId: string, referenceId: string) =>
    req<ReferenceSnapshot>(`/works/${encodeURIComponent(workId)}/content/references/${encodeURIComponent(referenceId)}`),
  getContentJob: (workId: string, jobId: string) =>
    req<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/jobs/${encodeURIComponent(jobId)}`),
  cancelContentJob: (workId: string, jobId: string) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/jobs/${encodeURIComponent(jobId)}/cancel`, {}),
  refreshContentJob: (workId: string, jobId: string) =>
    post<{ job_id: string; state: string; status: string; message?: string }>(
      `/works/${encodeURIComponent(workId)}/content/jobs/${encodeURIComponent(jobId)}/refresh`, {}),
  searchReferences: (workId: string, input: { request_id: string; query: string; period_days: 7 | 30; sort: 'likes' | 'recent' | 'recommended'; cursor?: string | null; refresh?: boolean }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/searches`, input),
  importReference: (workId: string, input: { request_id: string; search_job_id?: string; platform_work_id?: string; source_url?: string; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/imports`, input),
  selectReference: (workId: string, referenceId: string, expectedContentRevision: number) =>
    req<{ ok: boolean; content_revision: number; reference_snapshot_id: string }>(
      `/works/${encodeURIComponent(workId)}/content/reference`,
      { method: 'PUT', body: JSON.stringify({ reference_snapshot_id: referenceId, expected_content_revision: expectedContentRevision }) }),
  uploadReferenceMedia: (workId: string, referenceId: string, file: File) => {
    const form = new FormData()
    form.append('reference_snapshot_id', referenceId)
    form.append('file', file)
    return upload<{ asset: MediaAssetView }>(`/works/${encodeURIComponent(workId)}/content/media`, form)
  },
  quoteVisualAnalysis: (workId: string, input: { reference_snapshot_id: string; media_asset_id: string }) =>
    post<VisualQuote>(`/works/${encodeURIComponent(workId)}/content/visual-quotes`, input),
  startVisualAnalysis: (workId: string, input: { request_id: string; reference_snapshot_id: string; media_asset_id: string; expected_content_revision: number; paid_ack: boolean; total_ceiling_fen: number; retry_unknown_call_id?: string; retry_paid_ack?: boolean }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/visual-analyses`, input),
  quoteMediaFetch: (workId: string, input: { reference_snapshot_id: string }) =>
    post<MediaFetchQuote>(`/works/${encodeURIComponent(workId)}/content/media-fetch-quotes`, input),
  startMediaFetch: (workId: string, input: { request_id: string; reference_snapshot_id: string; expected_content_revision: number; paid_ack: boolean; total_ceiling_fen: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/media-fetches`, input),
  reviseReferenceTranscript: (workId: string, input: { request_id: string; reference_snapshot_id: string; transcript_text: string; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/reference-revisions`, input),
  saveContentSettings: (workId: string, input: { expected_content_revision: number; brief?: Record<string, unknown>; style_snapshot?: Record<string, unknown>; style_profile_id?: string }) =>
    req<{ ok: boolean; content_revision: number }>(`/works/${encodeURIComponent(workId)}/content/settings`, { method: 'PUT', body: JSON.stringify(input) }),
  preparePrompt: (workId: string, input: { request_id: string; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/prompts`, input),
  revisePrompt: (workId: string, input: { request_id: string; prompt_id: string; prompt_text: string; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/prompt-revisions`, input),
  startGeneration: (workId: string, input: { request_id: string; prompt_id: string; content_route: 'talking_script' | 'short_video_storyboard'; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/generations`, input),
  startRepair: (workId: string, input: { request_id: string; batch_id: string; candidate_ids: string[]; expected_content_revision: number; instruction?: string }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/repairs`, input),
  startReview: (workId: string, input: { request_id: string; batch_id: string; expected_content_revision: number }) =>
    post<ContentJobReceipt>(`/works/${encodeURIComponent(workId)}/content/reviews`, input),
  reviseScript: (workId: string, scriptId: string, text: string) =>
    post<ScriptVersion>(`/works/${workId}/script-revisions`, { script_id: scriptId, text }),
  /** 让模型把这一稿重写到过硬门禁——用户不必自己改字。 */
  repairScript: (workId: string, scriptId: string) =>
    post<ScriptVersion | ScriptRepairReceipt>(`/works/${workId}/script-repairs`, { script_id: scriptId }),

  synthesizeFullAudio: (workId: string, forceNew = false) => post<FullAudioResult>(`/works/${workId}/audio`, { force_new: forceNew }),
  listAudios: (workId: string) => req<AudioAssetInfo[]>(`/works/${workId}/audios`),
  // 确认是一次人工动作：后端要求 actor 以 user 开头且必须带试听确认。
  // 界面上的"确认"按钮点击即为用户的显式听检确认，因此这里显式传 audition_ack。
  confirmAudio: (workId: string, audioId: string, auditionAck = true) =>
    post<{ ok: boolean; message: string; duration_ms: number; revoked_previous: string[]; note: string }>(
      `/works/${workId}/audio-confirmation`,
      { audio_id: audioId, actor_id: 'user_local', audition_ack: auditionAck },
    ),
  getAudioGate: (workId: string) => req<AudioGateSummary>(`/works/${workId}/audio-gate`),

  estimateCost: (workId: string, route: GenerationRoute) =>
    post<CostEstimate>('/cost-estimates', { work_id: workId, route }),
  /**
   * 确认路线。高成本路线（OmniHuman）必须带 `cost_notice_ack`：
   * 后端会拦，前端弹窗只是呈现，不是约束。
   */
  confirmCost: (
    workId: string,
    route: GenerationRoute,
    costNoticeAck = false,
  ): Promise<{ ok: boolean; cost_notice_acknowledged: boolean; message: string }> =>
    post<{ ok: boolean; cost_notice_acknowledged: boolean; message: string }>(`/works/${workId}/route`, {
      route,
      cost_notice_ack: costNoticeAck,
    }),

  submitTask: (workId: string) => post<Task>('/tasks', { work_id: workId }),
  getTask: (taskId: string) => req<Task>(`/tasks/${taskId}`),
  getTaskEvents: (taskId: string, afterSeq = 0) =>
    req<TaskEvent[]>(`/tasks/${taskId}/events?after_seq=${afterSeq}`),
  cancelTask: (taskId: string) => post<Task>(`/tasks/${taskId}/cancel`, {}),

  getPackage: (workId: string) => req<PublishPackage | null>(`/works/${workId}/publish-package`),
  getPublishStatus: (workId: string) => req<PublishTaskView>(`/works/${encodeURIComponent(workId)}/publish/status`),
  openPublishLogin: () => post<PublishSessionView>('/publish/douyin/login', {}),
  getPublishSession: () => req<PublishSessionView>('/publish/douyin/session'),
  startPublish: (workId: string, input: { platform: 'douyin'; auto_submit: false; title?: string; description?: string; tags?: string[] }) =>
    post<PublishTaskView>(`/works/${encodeURIComponent(workId)}/publish/start`, input),
  confirmPublish: (workId: string, taskId: string, packageDigest: string, confirmationDigest: string) =>
    post<PublishTaskView>(`/works/${encodeURIComponent(workId)}/publish/confirm`, {
      task_id: taskId, package_digest: packageDigest, confirmation_digest: confirmationDigest, confirmed: true, actor_id: 'user_local',
    }),
  cancelPublish: (workId: string, taskId: string) =>
    post<PublishTaskView>(`/works/${encodeURIComponent(workId)}/publish/cancel`, { task_id: taskId, reason: '用户取消发布准备' }),
  resumePublish: (workId: string, taskId: string) =>
    post<PublishTaskView>(`/works/${encodeURIComponent(workId)}/publish/resume`, { task_id: taskId }),

  // ---------- T05 数字人口播 ----------
  //
  // 分成三块：**看**（能力与费用视图，只读）、**做**（提交与确认）、**判**（门禁与人工审核）。
  // 判断类接口一律把"谁在判"写进参数：本项目没有视觉理解能力，
  // 口型/身份/表情的结论只能由人给出，接口必须能区分"用户判的"与"系统猜的"。

  // 带 work_id 时后端会把最终音频时长并入费用提示，给出具体估价；
  // 不带则金额留空——没有计费时长就不给数。
  getWanImage: (workId: string) => req<{ source: WanImageSource | null }>(`/works/${encodeURIComponent(workId)}/wan/image`),
  uploadWanImage: (workId: string, file: File) => req<{ source: WanImageSource }>(`/works/${encodeURIComponent(workId)}/wan/image`, {
    method: 'POST', body: file,
    headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) },
  }),
  confirmWanNativeVideo: (workId: string, videoId: string) =>
    post<{ ok: boolean; video: VideoAsset; package: PublishPackage }>(`/works/${encodeURIComponent(workId)}/wan/videos/${encodeURIComponent(videoId)}/confirm`, {
      actor_id: 'user_ui', review_ack: true,
    }),
  getTalkingHeadRoutes: (workId?: string) =>
    req<RoutePriceView>(`/talking-head/routes${workId ? `?work_id=${workId}` : ''}`),
  getTalkingHeadQueue: (workId?: string) =>
    req<QueueView>(`/talking-head/queue${workId ? `?work_id=${workId}` : ''}`),
  getTalkingHeadAdapters: () => req<AdapterStatusView>('/talking-head/adapters'),
  getTalkingHeadPreflight: (workId: string, route?: GenerationRoute) =>
    req<TalkingHeadPreflight>(`/works/${workId}/talking-head/preflight${route ? `?route=${route}` : ''}`),
  getTalkingHeadCost: (workId: string, route?: GenerationRoute, prompt = '', resolution = '1080p', durationSeconds?: number) => {
    const query = new URLSearchParams({ prompt, resolution })
    if (route) query.set('route', route)
    if (route === 'WAN_3_0' && durationSeconds !== undefined) query.set('duration_seconds', String(durationSeconds))
    return req<TalkingHeadCostView>(`/works/${workId}/talking-head/cost?${query}`)
  },
  // 摘要由服务端从当前绑定推导，客户端不自报摘要：否则"确认的清单"与
  // "实际提交的清单"可能不是同一件事，而两者金额完全相同。
  confirmTalkingHeadCost: (
    workId: string,
    input: { route: GenerationRoute; prompt: string; resolution: string; duration_seconds?: number },
  ) =>
    post<TalkingHeadCostView['confirmation']>(`/works/${workId}/talking-head/cost-confirmation`, {
      ...input,
      actor_id: 'user_local',
    }),
  /**
   * 建立分段计划。
   *
   * POST 落库后返回的是"计划记录"（id / segments / summary），而界面要的是
   * "视图"（planned / plan_id / total_ms…），两者形状不同。此前直接把记录塞进
   * 视图状态，结果是"分段其实建立成功了，界面却仍显示尚未建立"。
   * 因此这里以服务端视图为准返回，调用方只需面对一种形状。
   */
  createSegmentPlan: async (workId: string, limitMs?: number) => {
    await post<SegmentPlanRecord>(`/works/${workId}/talking-head/segments`, { limit_ms: limitMs })
    return req<SegmentPlanView>(`/works/${workId}/talking-head/segments`)
  },
  getSegmentPlan: (workId: string) =>
    req<SegmentPlanView>(`/works/${workId}/talking-head/segments`),
  /** 音频确认后即可并行对齐：为已确认最终音频跑真实 ASR/强制对齐并登记。 */
  runAlignment: (workId: string) =>
    post<AlignmentRunResult>(`/works/${workId}/alignment/run`, {}),
  // 动作意图必须**落库**：底片是从作品上读 motion_intent 的，
  // 只在下拉框里选一下不发请求，M2 会在提交时才报 MOTION_REQUIRED。
  listMotions: (identityId: string) => req<MotionCatalogView>(`/identities/${identityId}/motions`),
  listMotionTemplates: () => req<MotionTemplateLibrary>('/motion-templates'),
  importMotionTemplate: (identityId: string, sourceReferenceId: string) =>
    post<MotionReferenceView>(`/identities/${identityId}/motions/import`, {
      source_reference_id: sourceReferenceId, actor_id: 'user_local', confirmed: true,
    }),
  listMotionUploads: (identityId: string) =>
    req<{ uploads: MotionUploadView[] }>(`/identities/${identityId}/motion-uploads`),
  /** 原始参考视频用 XHR 上传，便于给用户真实的传输进度；后端检查/转码结束前保持 pending。 */
  uploadMotion: (
    identityId: string,
    file: File,
    onProgress?: (loaded: number, total: number) => void,
    signal?: AbortSignal,
  ): Promise<MotionUploadView> => new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new ApiError('UPLOAD_ABORTED', '动作视频上传已取消。', true)); return }
    const xhr = new XMLHttpRequest()
    const path = `/api/v1/identities/${encodeURIComponent(identityId)}/motion-uploads`
    let settled = false
    const abort = () => xhr.abort()
    const cleanup = () => signal?.removeEventListener('abort', abort)
    const fail = (error: unknown) => { if (!settled) { settled = true; cleanup(); reject(error) } }
    xhr.open('POST', path)
    xhr.responseType = 'text'
    xhr.timeout = 20 * 60 * 1000
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name))
    xhr.upload.onprogress = event => onProgress?.(event.loaded, event.lengthComputable ? event.total : file.size)
    xhr.onerror = () => fail(new ApiError('NETWORK_ERROR', '动作视频上传失败，请检查后端服务。', true))
    xhr.onabort = () => fail(new ApiError('UPLOAD_ABORTED', '动作视频上传已取消。', true))
    xhr.ontimeout = () => fail(new ApiError('UPLOAD_TIMEOUT', '上传或视频检查超时。请先刷新动作列表检查是否已保存，再决定重试。', true))
    xhr.onload = () => {
      let data: any = null
      try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null } catch {
        fail(new ApiError('NON_JSON_RESPONSE', `后端未返回 JSON（HTTP ${xhr.status}）。`, true)); return
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        const e = data?.error ?? {}
        fail(new ApiError(e.code ?? 'UPLOAD_FAILED', e.message ?? `上传失败（HTTP ${xhr.status}）`, !!e.retryable, e.action ?? null)); return
      }
      if (!settled) { settled = true; cleanup(); resolve(data as MotionUploadView) }
    }
    signal?.addEventListener('abort', abort, { once: true })
    try { xhr.send(file) } catch (error) { fail(error) }
  }),
  confirmMotionUpload: (
    uploadId: string,
    input: { label: string; motion_key: 'smile' | 'nod' | 'head_turn_small' | 'serious'; description: string; source_basis: string; actor_id: string; confirmed: boolean },
  ) => post<MotionReferenceView>(`/motion-uploads/${encodeURIComponent(uploadId)}/confirm`, input),
  setMotionIntent: (
    workId: string,
    input: { motion_key: string; motion_ref_id?: string; params?: Record<string, number> },
  ) => post<MotionIntentResult>(`/works/${workId}/motion-intent`, input),
  // 准入判定：不合格时逐条返回原因，界面照抄展示，不自己另编一套说法
  // 画风必须由调用方显式声明：门禁不接受"未声明画风"，空串会被判 review 而非 pass。
  admitVariant: (workId: string, variantId: string, style = '') =>
    post<VariantAdmitResult>(`/works/${workId}/variants/${variantId}/admit`, { style }),

  getPlateStatus: (workId: string, variantId?: string) => req<PlateStatusView>(`/works/${workId}/talking-head/plate${variantId ? `?variant_id=${encodeURIComponent(variantId)}` : ''}`),
  createPlate: (workId: string, outputSeconds: number, variantId?: string) =>
    post<{ registered: boolean; is_mock: boolean; note: string; duration_ms: number }>(
      `/works/${workId}/talking-head/plate`,
      { output_seconds: outputSeconds, ...(variantId ? { variant_id: variantId } : {}) },
    ),
  // 底片审核：通过必须是用户给出的（身份是否漂移只能由人目检）。
  reviewPlate: (plateId: string, input: { reviewer: string; outcome: string; note?: string }) =>
    post<Record<string, unknown>>(`/talking-head/plates/${plateId}/review`, input),

  submitTalkingHead: (
    workId: string,
    input: {
      route: GenerationRoute
      prompt: string
      resolution: string
      dry_run: boolean
      replacement_authorized?: boolean
      duration_seconds?: number
    },
  ) => post<SubmissionResult>(`/works/${workId}/talking-head/submissions`, input),
  getSubmissions: (workId: string) =>
    req<SubmissionListView>(`/works/${workId}/talking-head/submissions`),
  // 分段推进：读进度是只读的；续发只发"已经授权过的那次清单"里还没发出的段，
  // 因此它**不是**重提，不需要新的费用确认（重提见 replacement_authorized）。
  getTalkingHeadDriver: (workId: string) =>
    req<TalkingHeadDriverView>(`/works/${workId}/talking-head/driver`),
  resumeTalkingHeadDriver: (workId: string) =>
    post<TalkingHeadDriverView>(`/works/${workId}/talking-head/driver/resume`, {}),
  pollRun: (runId: string) =>
    post<Record<string, unknown>>(`/talking-head/runs/${runId}/poll`, {}),
  // 对账：结论只能是 succeeded/failed/cancelled，且必须带证据来源。
  // 只发 {note} 会被后端判为 VALIDATION_FAILED —— 载荷形状必须与后端一致。
  settleRun: (runId: string, outcome: 'succeeded' | 'failed' | 'cancelled',
              evidence: { source: string; note?: string }) =>
    post<Record<string, unknown>>(`/talking-head/runs/${runId}/settle`, { outcome, evidence }),

  getVideos: (workId: string) => req<VideoAsset[]>(`/works/${workId}/talking-head/videos`),
  getVideoGate: (videoId: string) => req<VideoGateView>(`/talking-head/videos/${videoId}/gate`),
  getTalkingHeadGate: (workId: string) =>
    req<TalkingHeadGateSummary>(`/works/${workId}/talking-head/gate`),
  // 四维审核：每条 pass 必须带引用（资产 + 毫秒区间），没有引用的通过等于没有通过。
  reviewVideo: (
    videoId: string,
    input: {
      reviewer: string
      outcome: string
      dimensions: Record<string, string>
      references: Record<string, { asset_id: string; start_ms: number; end_ms: number; method?: string }>
      note?: string
    },
  ) => post<Record<string, unknown>>(`/talking-head/videos/${videoId}/review`, input),
  recordAudioTrackCheck: (
    videoId: string,
    input: {
      audio_asset_id: string
      method: string
      outcome: string
      measured_offset_ms?: number
      evidence: string
      note?: string
    },
  ) => post<Record<string, unknown>>(`/talking-head/videos/${videoId}/audio-track-checks`, input),
  // 确认最终原片。`review_ack` 是用户的声明"我看过这条原片"——
  // 本项目没有视频理解能力，所以界面必须由用户显式勾选，不能替用户默认勾上。
  confirmFinalVideo: (workId: string, videoId: string, reviewAck: boolean, note = '') =>
    post<{ ok: boolean; video_id: string; duration_ms: number; note: string }>(
      `/works/${workId}/talking-head/final-video`,
      { video_id: videoId, actor_id: 'user_local', review_ack: reviewAck, note },
    ),
}
