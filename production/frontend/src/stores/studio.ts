import { defineStore } from 'pinia'
import { api } from '@/api'
import { controlsError, controlsKey, effectiveTtsControls, normalizeTtsControls } from '../api/tts-controls'
import type {
  AdapterStatusView,
  AlignmentRunResult,
  AudioAssetInfo,
  AudioGateSummary,
  AvatarAssetView,
  AvatarIdentity,
  AvatarVariant,
  Capabilities,
  CostEstimate,
  CostConfirmation,
  FeatureEntry,
  GenerationRoute,
  IdentityReadinessBrief,
  MaskEstimateResult,
  MaskSpec,
  MotionCatalogView,
  PlateStatusView,
  PublishPackage,
  PostprocessView,
  QueueView,
  RoutePriceView,
  ScriptBatch,
  ScriptVersion,
  SegmentPlanView,
  SubmissionListView,
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
  Work,
  WanImageSource,
} from '@/api/types'

export type Resolution = '480p' | '720p' | '1080p'

const RUNNING_VIDEO_STATES = ['queued', 'leased', 'submitted', 'running', 'waiting_provider']
const pollingStores = new WeakSet<object>()

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

/**
 * 可选的补充读取：拿不到就当没有，绝不把主流程带崩。
 *
 * 必须用 `Promise.resolve().then(fn)` 包一层，不能写成 `fn().catch(...)`：
 * 端点缺失时调用本身会**同步**抛 TypeError，`.catch` 根本接不住，
 * 整条提交流程会被一个"锦上添花"的进度快照打挂——驱动快照就这么挂过一次。
 */
const optional = <T>(fn: () => Promise<T> | T): Promise<T | null> =>
  Promise.resolve().then(fn).catch(() => null)

export const useStudioStore = defineStore('studio', {
  state: () => ({
    apiReady: false as boolean,
    workLoadVersion: 0,
    deletionPending: false,
    deletedWorkIds: [] as string[],
    deletedIdentityIds: [] as string[],
    capabilities: null as Capabilities | null,
    features: [] as FeatureEntry[],
    works: [] as Work[],
    currentWorkId: null as string | null,
    identities: [] as AvatarIdentity[],
    // 已丢弃的草稿候选：记录仍在后端，只是被移出工作列表。界面单独成组并给"恢复"。
    discardedIdentities: [] as AvatarIdentity[],
    // 每条人物"还缺什么"（来自后端 readiness 的 reasons，界面不自己另编一套说法）
    readinessMap: {} as Record<string, IdentityReadinessBrief>,
    voices: [] as VoiceAsset[],
    variants: [] as AvatarVariant[],
    scriptBatch: null as ScriptBatch | null,
    audios: [] as AudioAssetInfo[],
    ttsControlsDraft: null as TtsControls | null,
    task: null as Task | null,
    events: [] as TaskEvent[],
    pkg: null as PublishPackage | null,
    postprocess: null as PostprocessView | null,
    busy: '' as string,
    error: null as string | null,
    notice: null as { kind: 'ok' | 'warn' | 'danger'; text: string } | null,

    // ---------- T05 数字人口播 ----------
    thRoutes: null as RoutePriceView | null,
    thPreflight: null as TalkingHeadPreflight | null,
    thCost: null as TalkingHeadCostView | null,
    thSegments: null as SegmentPlanView | null,
    thPlate: null as PlateStatusView | null,
    thSubmissions: null as SubmissionListView | null,
    /**
     * 分段推进进度（只读）。
     *
     * 并发上限按账号而定（免费试用为 1），一次请求只发得出一段，余下的段靠后台
     * 推进器续发。没有这个快照，界面就答不出"还差几段、能不能继续"，用户只能干等。
     */
    thDriver: null as TalkingHeadDriverView | null,
    thGate: null as TalkingHeadGateSummary | null,
    /**
     * 音频门禁：音频一确认就该能看到"对齐有没有、可不可用"。
     * 分段只能来自同一音轨的句边界时间戳，因此对齐状态必须与分段面板同屏。
     */
    thAudioGate: null as AudioGateSummary | null,
    /** 最近一次运行对齐的结果（未运行过为 null，界面据此区分"没跑过"与"跑了没成"） */
    thAlignment: null as AlignmentRunResult | null,
    thQueue: null as QueueView | null,
    thAdapters: null as AdapterStatusView | null,
    /** 该人物已获准的动作参考 + 受支持动作目录（目录≠可用：只有 approved 能解析通过） */
    motionCatalog: null as MotionCatalogView | null,
    /** 变体准入判定的原始结论：不合格时逐条原因，界面照抄 */
    variantGate: null as VariantAdmitResult | null,
    /** 该人物已登记的 mask（含未确认的草稿）：换景合成的前置 */
    masks: [] as MaskSpec[],
    /** 最近一次自动分割的草稿（含预览图与风险提示） */
    maskDraft: null as MaskEstimateResult | null,
    /** 最近一次确定性合成的结果：pixel_identical / max_channel_diff 直接照抄展示 */
    composeResult: null as VariantComposeResult | null,
    /** 该人物可用的背景底片资产。合成要的是 background_asset_id，不是界面自造的场景名 */
    backgroundAssets: [] as AvatarAssetView[],
    /** 准入时声明的画风。留空 = "无法判断"，门禁不会放过，因此必须显式填 */
    variantStyle: 'realistic' as string,
    videos: [] as VideoAsset[],
    videoGates: {} as Record<string, VideoGateView>,
    /** 提交参数：清单摘要由这些值推导，因此它们必须显式可见、可回溯 */
    thPrompt: '' as string,
    thDurationSeconds: 5,
    wanImage: null as WanImageSource | null,
    wanImageRequestVersion: 0,
    thResolution: '1080p' as Resolution,
    thCostRequestVersion: 0,
    thOptionsRestored: false,
  }),
  getters: {
    currentWork(state): Work | null {
      return state.works.find((w) => w.id === state.currentWorkId) ?? null
    },
    savedTtsControls(): TtsControls {
      const base = this.currentVoice?.synthesis_profile ?? {}
      const overrides = this.currentWork?.tts_controls ?? {}
      return effectiveTtsControls(base, overrides)
    },
    ttsControlsDirty(state): boolean {
      return !!state.ttsControlsDraft && controlsKey(state.ttsControlsDraft) !== controlsKey(this.savedTtsControls)
    },
    currentIdentity(state): AvatarIdentity | null {
      const id = this.currentWork?.identity_id
      return state.identities.find((i) => i.id === id) ?? null
    },
    identityReady(state): boolean {
      const id = this.currentWork?.identity_id
      return !!id && state.readinessMap[id]?.ready === true
    },
    /** 已定版、可以选用的身份。 */
    usableIdentities(state): AvatarIdentity[] {
      return state.identities.filter((i) => i.approval_state === 'approved')
    },
    /** 草稿候选：还没定版，不能进生产，只能继续走定版或被丢弃。 */
    draftIdentities(state): AvatarIdentity[] {
      return state.identities.filter((i) => i.approval_state !== 'approved')
    },
    /**
     * 当前作品"下一步该做什么"——**全应用唯一的定义**（侧栏与各页都用它）。
     *
     * 存在的理由：界面此前选完人物就没了下文，用户点完「选用」不知道往哪走；
     * 侧栏底部虽然有一行"下一步"，但它是浅灰小字、贴在页面最底部，等于藏起来了。
     *
     * 判定沿用侧栏既有的派生量（scriptReady / confirmedAudio / pkg），
     * 不另起一套原始字段判断——两套判断会给出两个"下一步"，比没有指引更糟。
     * `to` 为空表示"还没有作品"，此时不指向任何页面。
     */
    nextStep(state): { key: string; title: string; detail: string; to: string; label: string } {
      const w = state.works.find((x) => x.id === state.currentWorkId)
      if (!w) {
        return { key: 'work', title: '先创建一个作品',
                 detail: '填写这一期的主题和目标时长，之后按顺序确认人物、稿件、声音与视频。', to: '', label: '创建第一个作品' }
      }
      if (w.brief.content_route === 'short_video_storyboard') {
        if (!this.scriptReady) {
          return { key: 'script', title: '写分镜剧本并选稿',
                   detail: '三篇分镜均通过审核后，选择一篇进入镜头制作。', to: '/script', label: '去写分镜剧本' }
        }
        return this.deliveryReady
          ? { key: 'download', title: '短片已确认：预览并下载', detail: '查看已审核的 Wan 原生声画成片。', to: '/video', label: '查看与下载短片' }
          : { key: 'video', title: '制作分镜短片', detail: '确认逐镜头图片与费用，再生成、审核和拼接。', to: '/video', label: '制作分镜短片' }
      }
      if ((this.currentRoute ?? w.route) === 'WAN_3_0') {
        return this.deliveryReady
          ? { key: 'download', title: '视频已确认：预览并下载', detail: '下载保留模型原声的视频；当前作品包不含自动字幕。', to: '/video', label: '查看与下载视频' }
          : { key: 'video', title: '制作 Wan 有声短片', detail: '上传参考图片，描述场景、动作、台词和环境音，选择 2–30 秒时长并确认费用。生成后观看确认即可下载。', to: '/video', label: '制作有声短片' }
      }
      if (!this.identityReady) {
        return { key: 'identity', title: '第 1 步：选定人物',
                 detail: w.identity_id
                   ? `当前人物还需完善：${state.readinessMap[w.identity_id]?.reasons.join('；') || '尚未取得人物就绪结果'}。到人物页查看并处理。`
                   : '选择已有主播，或新建一个主播并完成图片与使用权确认。',
                 to: '/avatar', label: '回到人物库' }
      }
      if (!this.scriptReady) {
        return { key: 'script', title: '第 2 步：写口播稿并定稿',
                 detail: '人物已选定，接下来要有可朗读、已采用的一稿',
                 to: '/script', label: '去写口播稿' }
      }
      if (!this.confirmedAudio) {
        return { key: 'tts', title: '第 3 步：合成并确认音轨',
                 detail: '稿件已定稿，需要一条经人工确认的最终音轨',
                 to: '/tts', label: '去合成音轨' }
      }
      if (!this.deliveryReady) {
        return { key: 'video', title: '第 4 步：生成并打包口播视频',
                 detail: '基础输入已齐。到视频页检查生成条件、选择方式，确认费用后提交；原片和成片都要由你观看确认。',
                 to: '/video', label: '去生成视频' }
      }
      return { key: 'download', title: '制作完成：预览并下载成片',
               detail: '下载视频、字幕、封面与投稿文案后自行投稿。自动发布仍在开发中。', to: '/video', label: '查看与下载成片' }
    },
    currentVoice(state): VoiceAsset | null {
      const id = this.currentWork?.voice_asset_id
      return state.voices.find((v) => v.id === id) ?? null
    },
    /** 用户显式采用的稿件；以 Work.script_version_id 为准，重新出稿后会被后端清空 */
    selectedScript(state): ScriptVersion | null {
      const id = this.currentWork?.script_version_id
      if (!id) return null
      return state.scriptBatch?.candidates.find((c) => c.id === id) ?? null
    },
    /** 稿件是否已采用：以服务端持久化字段为准，避免刷新后状态丢失 */
    scriptReady(): boolean {
      const batch = this.scriptBatch
      const selected = this.selectedScript
      // Older cached projections omit ready_to_present; only an explicit
      // server-side false may block an otherwise selected historical script.
      return !!selected && batch?.ready_to_present !== false && selected.pickable !== false
    },
    confirmedAudio(state): AudioAssetInfo | null {
      const w = this.currentWork
      if (!w || this.ttsControlsDirty) return null
      return state.audios.find((a) => a.kind === 'full' && a.confirmed && !a.is_mock
        && this.audioMatchesTtsControls(a)
        && (!a.work_id || a.work_id === w.id)
        && (!a.script_version_id || a.script_version_id === w.script_version_id)
        && (!w.voice_asset_id || a.voice_asset_id === w.voice_asset_id)
        && (!w.final_audio_id || a.id === w.final_audio_id)) ?? null
    },
    deliveryReady(state): boolean {
      return !!state.currentWorkId && state.pkg?.work_id === state.currentWorkId
        && state.pkg.state === 'ready' && state.pkg.mode !== 'mock' && !!state.pkg.download_url
    },

    /** 没有人工路线确认时保持未选择态；作品的历史默认字段不是授权。 */
    currentRoute(state): GenerationRoute | null {
      if (!this.routeSelected) return null
      return state.thRoutes?.selected_route ?? this.currentWork?.route ?? null
    },
    /** 是否已经有过一次可追溯的人工路线确认。
     *
     * 界面据此区分"已选 M2"和"还没选"——
     * 后端明确不接受把默认值当授权（`assert_route_selected` 要求
     * `route_confirmed` 与 `route_selection_id` 同时存在）。
     * 判据来自服务端（`/talking-head/routes?work_id=`），不靠作品字段：
     * 作品 `route` 永远非空，拿它判断会永远显示成"已选"。
     */
    routeSelected(state): boolean {
      return state.thRoutes?.route_confirmed === true
    },
    /** 路线能力条目（declared / verified 分列的那一份） */
    currentRouteSpec(state) {
      const r = this.currentRoute
      return state.thRoutes?.routes?.find((x) => x.route === r) ?? null
    },
    thBlockers(state) {
      return state.thPreflight?.blockers ?? []
    },
    /** 前置未通过就不得提交——界面据此禁用按钮，而不是靠提示语 */
    thReady(): boolean {
      return this.routeSelected && (this.currentRoute === 'WAN_3_0' || !this.ttsControlsDirty) && !!this.thPreflight?.ok
    },
    thPromptMaxChars(): number {
      const declared = this.currentRouteSpec?.declared
      return Number(declared?.user_prompt_max_chars ?? declared?.prompt_max_chars
        ?? (this.currentRoute === 'WAN_3_0' ? 20000 : 300))
    },
    /** 供应商配置只阻止新调用，不影响已有任务查询、输入编辑或报价。 */
    thSubmissionError(state): string {
      if (this.currentRoute !== 'WAN_3_0') return ''
      const adapter = state.thAdapters?.wan ?? state.thAdapters?.dashscope_wan
      if (!adapter) return '尚未取得 Wan 配置状态，请刷新状态后再生成。'
      return adapter.configured ? '' : (adapter.detail || 'Wan 服务尚未配置，暂不能提交真实生成。')
    },
    /** 前端即时校验不替代后端门禁，只防止明显无效输入进入费用与提交操作。 */
    thInputError(): string {
      const route = this.currentRoute
      if (!route) return '请先明确选择一种视频生成方式。'
      if (route === 'M2') return ''
      const duration = this.confirmedAudio?.duration_ms
      if (route === 'WAN_3_0') {
        if (!Number.isInteger(this.thDurationSeconds) || this.thDurationSeconds < 2 || this.thDurationSeconds > 30)
          return 'Wan 3.0 时长需要为 2–30 秒之间的整数。'
        if (!this.thPrompt.trim()) return '请填写 Wan 3.0 的画面提示词。'
        if (!['480p', '720p', '1080p'].includes(this.thResolution)) return '请选择 Wan 支持的 480p、720p 或 1080p。'
      } else if (duration != null && duration >= 60000) {
        return 'OmniHuman 仅支持小于 60 秒的最终音频；不会自动分段。'
      }
      const maximum = this.thPromptMaxChars
      return this.thPrompt.length > maximum ? `提示词超过 ${maximum} 字，请缩短描述。` : ''
    },
    /** 已确认的费用记录：确认过清单与金额，但这不等于付费授权 */
    thConfirmedCost(state) {
      return state.thCost?.confirmation ?? null
    },
    /** 历史确认不能直接作为当前输入的提交许可。编辑参数时同步失效，无需等待请求。 */
    thCostChangedFields(state): string[] {
      const view = state.thCost
      const conf = view?.confirmation
      if (!conf) return []
      if (view?.work_id !== state.currentWorkId || conf.work_id !== state.currentWorkId) return ['作品']
      if (conf.route !== this.currentRoute) return ['路线']
      const current = view.current_call_list
      if (!current) return ['调用清单']
      const snapshot = conf.call_list
      const changes = new Set<string>()
      if (view.confirmation_valid === false) {
        for (const field of view.changed_fields?.length ? view.changed_fields : ['调用清单']) changes.add(field)
      }
      if (snapshot) {
        const compared = { ...current, route: this.currentRoute,
          prompt: this.currentRoute === 'M2' ? '' : state.thPrompt,
          resolution: this.currentRoute === 'M2' ? 'native' : state.thResolution,
          ...(this.currentRoute === 'WAN_3_0' ? { generated_seconds: state.thDurationSeconds, generation_mode: 'image_prompt_native_audio', sound_mode: 'native_audio' } : {}) }
        const fields = { route: '路线', audio_id: '配音', audio_hash: '配音', variant_id: '人物图片',
          identity_id: '人物', source_asset_id: '人物图片', source_asset_hash: '人物图片',
          motion_ref_id: '动作参考', plate_id: '动作底片', prompt: this.currentRoute === 'WAN_3_0' ? '画面提示词' : '动作描述',
          resolution: '分辨率', segment_count: '调用次数', model: '模型',
          generation_mode: '生成模式', generated_seconds: '生成时长', audio_mode: '声音模式', sound_mode: '声音模式',
          final_audio_mode: '最终音轨', effective_prompt: '生成提示词', audio_instruction_version: '音轨规则' } as const
        for (const key of Object.keys(fields) as (keyof typeof fields)[]) {
          if ((snapshot[key] ?? null) !== (compared[key] ?? null)) changes.add(fields[key])
        }
      } else {
        if (!view.confirmation_valid) for (const field of view.changed_fields?.length ? view.changed_fields : ['调用清单']) changes.add(field)
        if (this.currentRoute !== 'M2') {
          if (state.thPrompt !== current.prompt) changes.add(this.currentRoute === 'WAN_3_0' ? '画面提示词' : '动作描述')
          if (state.thResolution !== current.resolution) changes.add('分辨率')
          if (this.currentRoute === 'WAN_3_0' && state.thDurationSeconds !== current.generated_seconds) changes.add('生成时长')
        }
      }
      return [...changes]
    },
    thValidConfirmedCost(): CostConfirmation | null {
      return this.thConfirmedCost && !this.thCostChangedFields.length ? this.thConfirmedCost : null
    },
    waitingVideoRuns(state) {
      return (state.thSubmissions?.runs ?? []).filter(run => !run.is_mock
        && run.work_id === state.currentWorkId && RUNNING_VIDEO_STATES.includes(run.state))
    },
    unresolvedVideoRuns(state) {
      return (state.thSubmissions?.runs ?? []).filter(run => !run.is_mock
        && run.work_id === state.currentWorkId && [...RUNNING_VIDEO_STATES, 'unknown'].includes(run.state))
    },
    /**
     * 阻断重提的运行：本作品里 failed / unknown 的记录。
     *
     * 后端 `resubmit_guard` 正是用这两个状态判定"这是重做、要显式授权"。
     * 界面据此显示「重新生成」——否则旧失败记录会让「生成口播」永久禁用，
     * 用户除了改稿之外没有任何出路。
     */
    thBlockedRuns(state) {
      return (state.thSubmissions?.runs ?? []).filter(run => !run.is_mock
        && run.work_id === state.currentWorkId && ['failed', 'unknown'].includes(run.state))
    },
    /** 还有分段没发且没有阻塞原因：这才是"可以续发"。 */
    thCanResumeSegments(state): boolean {
      return !!state.thDriver?.resumable
    },
    /** 已确认的最终原片（本作品唯一一条） */
    hasReviewableOriginal(state): boolean {
      return state.videos.some(v => v.work_id === state.currentWorkId && v.kind === 'original'
        && !v.is_mock && ['pending', 'approved'].includes(v.approval_state))
    },
    finalVideo(state): VideoAsset | null {
      return state.videos.find((v) => v.kind === 'original' && v.confirmed) ?? null
    },
    pendingPlates(state): VideoAsset[] {
      return state.videos.filter((v) => v.kind === 'plate' && !v.confirmed)
    },
  },
  actions: {
    workContextMatches(id: string | null, version: number, taskId?: string | null): boolean {
      return this.currentWorkId === id && this.workLoadVersion === version
        && (taskId === undefined || this.currentWork?.current_task_id === taskId)
    },
    setNotice(kind: 'ok' | 'warn' | 'danger', text: string) {
      this.notice = { kind, text }
    },
    clearNotice() {
      this.notice = null
    },
    async guard<T>(label: string, fn: () => Promise<T>): Promise<T | null> {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      this.busy = label
      this.error = null
      try {
        const r = await fn()
        return this.workContextMatches(workId, version) ? r : null
      } catch (e: any) {
        if (this.workContextMatches(workId, version)) this.error = e?.message ?? String(e)
        return null
      } finally {
        if (this.workContextMatches(workId, version) && this.busy === label) this.busy = ''
      }
    },

    async bootstrap() {
      await this.guard('加载', async () => {
        this.capabilities = await api.getCapabilities()
        this.features = await api.getFeatures()
        await this.reloadIdentities()
        this.voices = await api.listVoices()
        this.works = this.visibleWorks(await api.listWorks())
        let remembered = ''
        try { remembered = localStorage.getItem('avatar-studio:last-work') ?? '' } catch { /* storage may be unavailable */ }
        if (!this.currentWorkId && this.works.length) {
          this.currentWorkId = this.works.find(w => w.id === remembered)?.id ?? this.works[0].id
        }
        this.apiReady = true
      })
      if (this.currentWorkId) await this.loadWork(this.currentWorkId)
    },

    async createWork(input: {
      name: string
      topic: string
      content_type: string
      audience: string
      keywords: string[]
      target_duration_ms: number
    }) {
      const w = await this.guard('创建作品', () => api.createWork(input))
      if (w) {
        this.works.push(w)
        this.currentWorkId = w.id
        this.rememberWork(w.id)
        await this.loadWork(w.id)
      }
      return w
    },

    async selectWork(id: string) {
      this.currentWorkId = id
      this.rememberWork(id)
      await this.loadWork(id)
    },

    async deleteWork(id: string): Promise<boolean> {
      if (this.deletionPending) return false
      const label = '删除作品'
      this.deletionPending = true
      this.busy = label
      this.error = null
      try {
        await api.deleteWork(id)
        this.deletedWorkIds.push(id)
        this.works = this.works.filter(work => work.id !== id)
        if (this.currentWorkId === id) {
          ++this.workLoadVersion
          this.clearWorkState()
          this.currentWorkId = null
          try { localStorage.removeItem('avatar-studio:last-work') } catch { /* storage may be unavailable */ }
          if (this.works.length) await this.selectWork(this.works[0].id)
        }
        this.setNotice('ok', '作品已删除')
        return true
      } catch (cause: any) {
        this.error = cause?.message ?? String(cause)
        return false
      } finally {
        this.deletionPending = false
        if (this.busy === label) this.busy = ''
      }
    },

    async deleteIdentity(id: string): Promise<boolean> {
      if (this.deletionPending) return false
      const label = '删除人物'
      this.deletionPending = true
      this.busy = label
      this.error = null
      try {
        await api.deleteIdentity(id)
        this.deletedIdentityIds.push(id)
        this.identities = this.identities.filter(identity => identity.id !== id)
        this.discardedIdentities = this.discardedIdentities.filter(identity => identity.id !== id)
        delete this.readinessMap[id]
        try {
          await this.reloadIdentities()
          this.setNotice('ok', '人物已删除')
        } catch {
          this.setNotice('warn', '人物已删除，素材库刷新失败，请刷新页面')
        }
        return true
      } catch (cause: any) {
        this.error = cause?.message ?? String(cause)
        return false
      } finally {
        this.deletionPending = false
        if (this.busy === label) this.busy = ''
      }
    },

    rememberWork(id: string) {
      try { localStorage.setItem('avatar-studio:last-work', id) } catch { /* selection still works without storage */ }
    },

    async loadWork(id: string) {
      const version = ++this.workLoadVersion
      this.clearWorkState()
      await this.guard('读取作品', async () => {
        const [works, scriptBatch, variants, pkg, audios] = await Promise.all([
          api.listWorks(), api.getScriptBatch(id), api.listVariants(id),
          api.getPackage(id), api.listAudios(id),
        ])
        if (version !== this.workLoadVersion || id !== this.currentWorkId) return
        this.works = this.visibleWorks(works)
        this.scriptBatch = scriptBatch
        this.variants = variants
        this.pkg = pkg?.work_id === id ? pkg : null
        this.audios = audios
        const w = works.find((x) => x.id === id)
        if (w?.current_task_id) {
          const taskId = w.current_task_id
          const [task, events] = await Promise.all([
            api.getTask(taskId), api.getTaskEvents(taskId),
          ])
          if (!this.workContextMatches(id, version, taskId) || task.work_id !== id || task.id !== taskId) return
          this.task = task
          this.events = events
        }
      })
      if (version !== this.workLoadVersion || id !== this.currentWorkId) return
      await this.loadMotions()
      if (!this.workContextMatches(id, version)) return
      await this.loadVariantInputs()
    },

    /** Keep derived data scoped to one work, including while the next request is pending. */
    clearWorkState() {
      this.busy = ''
      this.error = null
      this.notice = null
      this.scriptBatch = null
      this.variants = []
      this.audios = []
      this.ttsControlsDraft = null
      this.task = null
      this.events = []
      this.pkg = null
      this.postprocess = null
      this.videos = []
      this.videoGates = {}
      this.thRoutes = null
      this.thPreflight = null
      this.thCost = null
      this.thSegments = null
      this.thPlate = null
      this.thSubmissions = null
      this.thDriver = null
      this.thGate = null
      this.thAudioGate = null
      this.thAlignment = null
      this.thQueue = null
      this.motionCatalog = null
      this.variantGate = null
      this.masks = []
      this.maskDraft = null
      this.composeResult = null
      this.backgroundAssets = []
      this.thPrompt = ''
      this.thResolution = '1080p'
      this.thDurationSeconds = 5
      this.wanImage = null
      this.wanImageRequestVersion++
      this.thOptionsRestored = false
    },

    /** Re-read authoritative invalidation after changing a dependency. */
    async syncWork(id: string) {
      if (this.currentWorkId !== id) return
      await this.loadWork(id)
    },

    /** 已确认删除的对象不能被删除前发出的列表请求重新放回页面。 */
    visibleWorks(works: Work[]): Work[] {
      return works.filter(work => !this.deletedWorkIds.includes(work.id))
        .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    },

    async refreshPostprocess() {
      const id = this.currentWorkId
      if (!id) return
      const version = this.workLoadVersion
      const taskId = this.currentWork?.current_task_id
      const view = await api.getPostprocess(id)
      if (!this.workContextMatches(id, version, taskId)) return
      this.postprocess = view
      if (view?.task) {
        this.task = view.task
        const w = this.works.find((x) => x.id === id)
        if (w) w.current_task_id = view.task.id
      }
    },

    async startPostprocess() {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      const taskId = this.currentWork?.current_task_id
      if (!id) return
      const task = await this.guard('生成字幕与成片', () => api.startPostprocess(id))
      if (!task || !this.workContextMatches(id, version, taskId) || task.work_id !== id) return
      this.task = task
      const w = this.works.find((x) => x.id === id)
      if (w) w.current_task_id = task.id
      await this.guard('读取成片进度', () => this.refreshPostprocess())
    },

    async confirmPostprocess() {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      const taskId = this.currentWork?.current_task_id
      const executionId = this.postprocess?.execution?.id
      if (!id || !this.postprocess?.ready_for_review || !executionId) return
      const pkg = await this.guard('生成下载包', () => api.confirmPostprocess(id, executionId))
      if (!pkg || !this.workContextMatches(id, version, taskId) || pkg.work_id !== id
        || this.postprocess?.execution?.id !== executionId) return
      this.pkg = pkg
      this.setNotice('ok', '成片已确认，可以下载视频、字幕、封面与投稿文案')
      await this.guard('读取交付状态', () => this.refreshPostprocess())
    },

    /** 该人物**现在真能用**的动作 = 已获准参考，而不是整个目录。
     *  目录里没登记参考的动作键，解析阶段会被 MOTION_NOT_APPROVED 拒掉，
     *  界面上不该给一个选了必然失败的选项。 */
    async loadMotions() {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      const id = this.currentWork?.identity_id
      if (!id) {
        this.motionCatalog = null
        return
      }
      const catalog = await api.listMotions(id).catch(() => null)
      if (this.workContextMatches(workId, version) && this.currentWork?.identity_id === id) this.motionCatalog = catalog
    },

    /** 登记动作意图（落库）。只在下拉框里选一下是不够的：
     *  底片是从作品上读 motion_intent 的，不落库就会在提交时才报 MOTION_REQUIRED。 */
    async adoptMotion(motionKey: string, motionRefId?: string) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      const identityId = this.currentWork?.identity_id
      const matches = () => this.workContextMatches(id, version) && this.currentWork?.identity_id === identityId
      if (!id || !identityId || !motionKey || !motionRefId) return false
      const label = '保存本期动作'
      this.busy = label
      this.error = null
      try {
        const r = await api.setMotionIntent(id, {
          motion_key: motionKey,
          motion_ref_id: motionRefId,
        })
        if (!matches() || r.work_id !== id) return false
        this.currentWork!.motion_intent = r.motion_intent
        this.setNotice('ok', '本期动作已保存。完成配音后，到视频合成中生成动作底片。')
        const preflight = await api.getTalkingHeadPreflight(id).catch(() => null)
        if (matches()) this.thPreflight = preflight
        return matches()
      } catch (e: any) {
        if (matches()) this.error = e?.message ?? String(e)
        return false
      } finally {
        if (matches() && this.busy === label) this.busy = ''
      }
    },

    /** 变体准入判定：拿服务端逐条原因，界面不自己另编一套说法 */
    async admitVariant(variantId: string) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      // 画风必须显式声明：门禁不接受"未声明画风"，空字符串会被判 review 而不是 pass。
      const r = await api.admitVariant(id, variantId, this.variantStyle).catch(() => null)
      if (!this.workContextMatches(id, version)) return
      this.variantGate = r
      if (r && !r.ok) {
        this.setNotice('danger', `变体不可用于生产：${r.reasons.join('；')}`)
      } else if (r) {
        this.setNotice('ok', '变体通过生产准入判定')
      }
    },

    /**
     * 换景合成的前置输入：已登记的 mask + 可用的背景底片。
     * 两者都从后端取——合成要的是真实资产 id，界面自己编一个场景名是合成不出来的。
     */
    async loadVariantInputs() {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      const id = this.currentWork?.identity_id
      this.maskDraft = null
      this.composeResult = null
      if (!id) {
        this.masks = []
        this.backgroundAssets = []
        return
      }
      const [masks, backgrounds] = await Promise.all([
        api.listMasks(id).catch(() => null),
        api.listIdentityAssets(id, 'background').catch(() => [] as AvatarAssetView[]),
      ])
      if (!this.workContextMatches(workId, version) || this.currentWork?.identity_id !== id) return
      this.masks = masks?.masks ?? []
      this.backgroundAssets = backgrounds
    },

    /** 自动分割 mask **草稿**。界面上不提供"自动通过"：指标只能排除已知失效。 */
    async createMaskDraft() {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      const id = this.currentWork?.identity_id
      if (!id) return
      const r = await this.guard('生成蒙版草稿', () => api.createMask(id))
      if (!r) return
      this.setNotice(
        'warn',
        '已生成蒙版草稿：它必须由人目视确认后才能用于产出变体。指标全绿只说明没触发已知失效条件，不等于蒙版正确',
      )
      await this.loadVariantInputs()
      if (!this.workContextMatches(workId, version)) return
      this.maskDraft = r
    },

    /** 人工目视确认 mask —— 权限来自"人"本身，系统不替人核发。 */
    async confirmMask(maskAssetId: string, outcome: 'pass' | 'fail' | 'review', note: string) {
      const r = await this.guard('确认蒙版', () => api.confirmMask(maskAssetId, outcome, note))
      if (!r) return
      if (r.state === 'confirmed') this.setNotice('ok', '蒙版已由人工确认通过，可用于产出变体')
      else if (r.state === 'rejected') this.setNotice('danger', '蒙版已标记为不通过，需重新生成')
      else this.setNotice('warn', '蒙版结论记为"待复核"：仍不能用于产出变体')
      await this.loadVariantInputs()
    },

    /**
     * 生成变体 = **确定性合成**，不是生成模型重绘。
     *
     * 顺序固定且不可跳：先要一条已人工确认的 mask，再选背景底片。
     * 之所以不给"直接出图"的捷径：gpt-image 系列中转会整图重绘，
     * 换景必然连着把脸一起重画（身份漂移），只有逐像素合成能守住身份。
     */
    async createVariant(outfit: string, backgroundAssetId: string, backgroundLabel = '') {
      const workId = this.currentWorkId
      const identityId = this.currentWork?.identity_id
      if (!workId || !identityId) return

      const confirmed = this.masks.find((m) => m.state === 'confirmed')
      if (!confirmed) {
        this.setNotice('danger', '还没有已确认的蒙版：先在下面生成蒙版草稿，并由人目视确认通过')
        return
      }
      if (!backgroundAssetId) {
        this.setNotice('danger', '该人物还没有可用的背景底片资产，无法合成')
        return
      }

      const composed = await this.guard('确定性合成变体', () =>
        api.composeVariant(identityId, {
          mask_asset_id: confirmed.mask_asset_id,
          background_asset_id: backgroundAssetId,
          edge_mode: 'unmix',
          outfit,
          background_label: backgroundLabel,
        }),
      )
      if (!composed) return
      this.composeResult = composed

      const v = await this.guard('登记变体', () =>
        api.createVariant(workId, {
          // 引用刚合成的那张资产，不再重复登记一份（同一张图两个 id 会让来源链断掉）
          asset_id: composed.asset?.id,
          outfit,
          background: backgroundLabel || backgroundAssetId,
          identity_digest: this.currentIdentity?.identity_digest || undefined,
        }),
      )
      if (!v) return
      this.variants.push(v)
      const mx = composed.max_channel_diff
      this.setNotice(
        'warn',
        `变体已登记为待审。硬保护区最大通道差 ${mx === null ? '无法判定' : mx}` +
          '（严格为 0 才是逐像素相同）；未通过身份审核前不得进入生产',
      )
    },

    /** 变体身份审核：走资产审核端点，审核摘要必须与冻结基准一致，否则按换脸拒收。 */
    async reviewVariantAsset(assetId: string, outcome: 'pass' | 'fail' | 'review') {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      const identityId = this.currentWork?.identity_id
      if (!identityId || !assetId) return
      const digest = this.currentIdentity?.identity_digest || ''
      const r = await this.guard('变体身份审核', () =>
        api.reviewIdentityAsset(identityId, assetId, {
          // 资产审核的结论取值是 pass/reject/review；本方法的 fail 语义等同于 reject。
          outcome: outcome === 'fail' ? 'reject' : outcome,
          identity_digest: digest,
          method: '人工对照定妆原图',
          note: '变体身份审核：人工对照冻结基准',
        }),
      )
      if (!r) return
      this.setNotice(
        outcome === 'pass' ? 'ok' : 'warn',
        outcome === 'pass'
          ? '变体身份审核记为通过：它现在可以通过准入判定（仍需核对画风与种类）'
          : '变体审核未记为通过，不能用于生产',
      )
      if (workId) {
        const variants = await api.listVariants(workId)
        if (this.workContextMatches(workId, version)) this.variants = variants
      }
    },

    async refreshTask() {
      const id = this.currentWork?.current_task_id
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      if (!id || !workId) return
      const t = await api.getTask(id).catch(() => null)
      if (!t || t.work_id !== workId || t.id !== id || !this.workContextMatches(workId, version, id)) return
      this.task = t
      const w = this.works.find((x) => x.id === t.work_id)
      if (w) w.current_task_id = t.id
      const evs = await api.getTaskEvents(id, this.events.length).catch(() => [])
      if (!this.workContextMatches(workId, version, id)) return
      if (evs.length) this.events.push(...evs)
      if (t.state === 'succeeded' && !this.pkg) {
        const pkg = await api.getPackage(workId).catch(() => null)
        if (!this.workContextMatches(workId, version, id)) return
        if (pkg?.work_id === workId) this.pkg = pkg
      }
    },

    /** 轮询直到任务离开运行态；只展示真实阶段，不伪造百分比。 */
    async pollTask(times = 40) {
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      const taskId = this.currentWork?.current_task_id
      for (let i = 0; i < times; i++) {
        await wait(1200)
        if (!this.workContextMatches(workId, version, taskId)) return
        await this.refreshTask()
        if (!this.workContextMatches(workId, version, taskId)) return
        const s = this.task?.state
        if (!s) break
        if (['waiting_selection', 'succeeded', 'failed', 'cancelled', 'blocked'].includes(s)) break
      }
    },

    async generateScripts(topic: string, materials: string[] = []) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      const clean = materials.map((m) => m.trim()).filter(Boolean)
      this.setNotice(
        'ok',
        clean.length
          ? `已提交写稿任务：将按你给的 ${clean.length} 条资料生成三个不同角度，并要求模型交付可采用稿`
          : '已提交写稿任务：未提供资料，模型会避开需要外部依据的事实断言，交付三个可采用的角度',
      )
      const batch = await this.guard('写稿', async () => {
        const b = await api.generateScripts(id, topic, clean)
        if (!this.workContextMatches(id, version)) return null
        // 新一版候选意味着旧采用失效
        const w = this.works.find((x) => x.id === id)
        if (w) w.script_version_id = null
        // 资料落进作品 brief：重新生成时沿用，刷新后也能看到当时给了什么
        if (w) w.brief.materials = clean.map((text) => ({ text }))
        await this.pollTask(12)
        return b
      })
      if (batch && this.currentWorkId === id) await this.syncWork(id)
    },

    /** 让模型把这一稿重写到过硬门禁。这是出稿方返工，不是让用户去改字。 */
    async repairScript(scriptId: string) {
      const id = this.currentWorkId
      if (!id) return null
      const version = this.workLoadVersion
      const fixed = await this.guard('重写稿件', () => api.repairScript(id, scriptId))
      if (!fixed) return null
      const batch = await this.guard('读取审核结果', () => api.getScriptBatch(id))
      if (!this.workContextMatches(id, version)) return null
      this.scriptBatch = batch
      this.setNotice(batch?.ready_to_present ? 'ok' : 'warn', batch?.ready_to_present
        ? '三篇稿件均已通过审核，可以选择采用'
        : '返工完成；三篇全部通过审核后才展示候选稿')
      return fixed
    },

    async selectScript(scriptId: string, expectedContentRevision?: number) {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('选稿', () => api.selectScript(id, scriptId, expectedContentRevision))
      // 采用被后端拒绝（缺审核/审核未过/稿件已被替换）时 error 已由 guard 记录；同步一次让页面显示最新审核状态
      if (!r) { await this.syncWork(id); return }
      if (!r.ok) {
        this.setNotice('danger', r.message)
        return
      }
      const w = this.works.find((x) => x.id === id)
      if (w) w.script_version_id = scriptId
      this.setNotice('ok', r.message)
      await this.syncWork(id)
    },

    async reviseScript(scriptId: string, text: string) {
      const id = this.currentWorkId
      if (!id) return
      const v = await this.guard('重审', () => api.reviseScript(id, scriptId, text))
      if (v) {
        this.setNotice('ok', '已保存修订稿并重新审核；下游音频与口型依赖已失效')
        await this.syncWork(id)
      }
    },

    /** 重新拉人物列表与就绪度：丢弃 / 恢复之后必须重取，否则分组与"还缺什么"会不同步。 */
    async reloadIdentities() {
      this.identities = (await api.listIdentities()).filter(identity => !this.deletedIdentityIds.includes(identity.id))
      this.discardedIdentities = (await api.listIdentities(true)).filter(identity => !!identity.discarded_at && !this.deletedIdentityIds.includes(identity.id))
      this.readinessMap = Object.fromEntries(Object.entries(await api.identitiesReadiness())
        .filter(([id]) => !this.deletedIdentityIds.includes(id)))
    },

    /**
     * 丢弃草稿候选：移出工作列表。**不是删除**——记录与历史都保留，可随时恢复。
     * 拒绝的原因由后端给（已定版 / 被作品或变体引用），界面直接转述，不自己另编。
     */
    async discardCandidate(identityId: string, reason: string) {
      const r = await this.guard('丢弃候选', () => api.discardIdentity(identityId, reason))
      if (!r) return
      await this.reloadIdentities()
      this.setNotice('ok', '候选已移出工作列表（记录仍保留，可随时恢复）')
    },

    async restoreCandidate(identityId: string) {
      const r = await this.guard('恢复候选', () => api.restoreIdentity(identityId))
      if (!r) return
      await this.reloadIdentities()
      this.setNotice('ok', '候选已放回工作列表')
    },

    async selectIdentity(identityId: string) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      await this.guard('选择人物', async () => {
        const updated = await api.selectIdentity(id, identityId)
        if (!this.workContextMatches(id, version)) return
        const w = this.works.find((x) => x.id === id)
        if (w) Object.assign(w, updated)
      })
      if (!this.error && this.workContextMatches(id, version)) await this.syncWork(id)
    },

    audioMatchesTtsControls(audio: AudioAssetInfo): boolean {
      if (audio.invalidated) return false
      if ((audio.tts_controls_revision ?? 0) !== (this.currentWork?.tts_controls_revision ?? 0)) return false
      const target = audio.kind === 'preview'
        ? effectiveTtsControls(this.voices.find(voice => voice.id === audio.voice_asset_id)?.synthesis_profile, this.currentWork?.tts_controls)
        : this.savedTtsControls
      if (audio.synthesis_profile) return controlsKey(audio.synthesis_profile) === controlsKey(target)
      // Historical audio without a parameter snapshot can only be used before controls were explicitly saved.
      return !Object.keys(this.currentWork?.tts_controls ?? {}).length
    },

    requireAppliedTtsControls(): boolean {
      if (!this.ttsControlsDirty) return true
      this.error = '请先应用配音参数，再试听、合成或确认音频。'
      return false
    },

    async saveTtsControls(): Promise<boolean> {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id || this.busy) return false
      const controls = normalizeTtsControls(this.ttsControlsDraft ?? this.savedTtsControls)
      const invalid = controlsError(controls)
      if (invalid) { this.error = invalid; return false }
      const updated = await this.guard('应用配音参数', () => api.updateTtsControls(id, controls))
      if (!updated || !this.workContextMatches(id, version)) return false
      const work = this.works.find(item => item.id === id)
      if (work) Object.assign(work, updated)
      this.ttsControlsDraft = null
      await this.syncWork(id)
      if (this.currentWorkId !== id || this.error) return false
      this.setNotice('ok', '配音参数已应用。请按新参数试听并重新确认全文音频。')
      return true
    },

    async voicePreview(voiceId: string, sampleText: string, forceNew = false) {
      if (!this.requireAppliedTtsControls()) return
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      const r = await this.guard('试听', () => api.createVoicePreview(id, voiceId, sampleText, forceNew))
      if (!r) return
      this.audios = [...this.audios.filter(audio => audio.id !== r.audio.id), r.audio]
      // 区分"已缓存"与"需要付费的新合成"：两者都不等于试听合格
      const head = r.cached ? '命中已缓存试听（未产生新费用）' : '已生成试听'
      const tail = r.audio.is_mock
        ? '（占位试听，未提交真实供应商）'
        : `（${Math.round(r.audio.duration_ms / 100) / 10}s）`
      this.setNotice('ok', `${head}${tail}：试听片不能当作正文音频`)
      if (this.voices.find(voice => voice.id === voiceId)?.clone_metadata) {
        const voices = await optional(() => api.listVoices())
        if (voices && this.workContextMatches(id, version)) this.voices = voices
      }
    },

    /** 用户显式选定候选音色即批准。批准 ≠ 通过生产质量验收（基准证据仍需单独补齐）。 */
    async approveVoice(voiceId: string): Promise<boolean> {
      const updated = await this.guard('确认音色', () => api.approveVoice(voiceId))
      if (!updated) return false
      const i = this.voices.findIndex((v) => v.id === voiceId)
      if (i >= 0) this.voices.splice(i, 1, { ...this.voices[i], ...updated })
      this.setNotice('ok', `已确认音色「${updated.name}」。确认为选定，不代表已通过生产质量验收。`)
      return true
    },

    /** 把该作品里这条音色的真实试听登记为基准证据。Mock 产物会被后端拒收。 */
    async registerVoiceBaseline(voiceId: string): Promise<boolean> {
      const id = this.currentWorkId
      if (!id) return false
      const r = await this.guard('登记基准证据', () => api.registerVoiceBaseline(voiceId, id))
      if (!r) return false
      const i = this.voices.findIndex((v) => v.id === voiceId)
      if (i >= 0) {
        this.voices.splice(i, 1, {
          ...this.voices[i], ...r.voice,
          production_ready: r.production_ready,
          production_blockers: r.production_blockers,
        })
      }
      if (r.production_ready) this.setNotice('ok', '基准证据已登记，这条音色现在可用于视频合成。')
      else this.setNotice('warn', `基准证据已登记。仍有阻塞：${r.production_blockers.join('；')}`)
      return true
    },

    /** 由人确认商用范围。确认对象是后端给出的当前口径，系统不代替人做判断。 */
    async attestVoiceLicense(licenseId: string, basis: string, ackToken: string): Promise<boolean> {
      const r = await this.guard('确认商用范围', () => api.attestLicense(licenseId, basis, ackToken))
      if (!r) return false
      this.setNotice('ok', '已保存使用范围确认（由人确认，非法律意见）。')
      // 权属状态变了，生产阻塞项要重新算一遍，否则界面还停在旧的结论上
      await this.guard('刷新音色', async () => { this.voices = await api.listVoices() })
      return true
    },

    async selectVoice(voiceId: string) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      await this.guard('选声', async () => {
        const updated = await api.selectVoice(id, voiceId)
        if (!this.workContextMatches(id, version)) return
        const w = this.works.find((x) => x.id === id)
        if (w) Object.assign(w, updated)
      })
      if (!this.error && this.workContextMatches(id, version)) await this.syncWork(id)
    },

    async synthesizeFull(forceNew = false) {
      if (!this.requireAppliedTtsControls()) return
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('全文合成', () => api.synthesizeFullAudio(id, forceNew))
      if (!r) return
      this.audios = [...this.audios.filter(a => a.id !== r.audio.id), r.audio]
      if (r.is_mock) {
        // 占位产物不继承任何通过结论：必须说清楚它不能进入生产
        this.setNotice('warn', r.note)
        return
      }
      const unsupported = r.control_summary?.unsupported ?? 0
      const extra = unsupported ? `，${unsupported} 项表达控制未生效（已在调用前报告）` : ''
      this.setNotice('ok', `全文音频已合成，实际时长 ${Math.round(r.duration_ms / 100) / 10}s${extra}，请试听后确认`)
    },

    async confirmAudio(audioId: string) {
      if (!this.requireAppliedTtsControls()) return
      const id = this.currentWorkId
      if (!id) return
      const audio = this.audios.find(item => item.id === audioId)
      if (audio && !this.audioMatchesTtsControls(audio)) {
        this.error = '这条音频的配音参数已失效，请按当前参数重新合成。'
        return
      }
      // 点击"确认"即用户的显式听检确认：本项目没有音频识别能力，
      // 术语读法正确与否只能由人听过之后承担，因此这里必须传 audition_ack=true。
      const r = await this.guard('确认音频', () => api.confirmAudio(id, audioId, true))
      if (!r) return
      if (!r.ok) return this.setNotice('danger', r.message)
      this.audios.forEach((a) => (a.confirmed = a.id === audioId))
      this.setNotice('ok', r.message)
      await this.syncWork(id)
    },

    async estimateCost(route: GenerationRoute) {
      const id = this.currentWorkId
      if (!id) return null
      return await this.guard('估价', () => api.estimateCost(id, route))
    },

    /**
     * 确认路线。高成本路线（OmniHuman）必须先确认费用提示：
     * 后端的门禁是硬性的，这里只是把用户那一次确认如实传上去。
     */
    async confirmRoute(route: GenerationRoute, costNoticeAck = false) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      const r = await this.guard('确认路线', () => api.confirmCost(id, route, costNoticeAck))
      if (!r || !this.workContextMatches(id, version)) return
      const changed = this.currentRoute !== route
      const w = this.works.find((x) => x.id === id)
      if (w) w.route = route
      if (changed) {
        this.thCostRequestVersion++
        this.thCost = null
        this.thPreflight = null
        this.thOptionsRestored = true
        this.thPrompt = ''
        this.thResolution = route === 'WAN_3_0' ? '720p' : '1080p'
        this.thDurationSeconds = 5
      }
      if (this.task) this.task.route_confirmed = true
      this.setNotice('ok', r.message)
      const [routes, preflight] = await Promise.all([
        api.getTalkingHeadRoutes(id).catch(() => null), api.getTalkingHeadPreflight(id).catch(() => null),
      ])
      if (!this.workContextMatches(id, version)) return
      this.thRoutes = routes
      this.thPreflight = preflight
      if (this.routeSelected && !this.thInputError) await this.refreshTalkingHeadCost()
    },

    async submitVideo() {
      if (!this.requireAppliedTtsControls()) return
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      const t = await this.guard('提交合成', async () => {
        const task = await api.submitTask(id)
        if (!this.workContextMatches(id, version)) return null
        this.task = task
        const w = this.works.find((x) => x.id === id)
        if (w) w.current_task_id = task.id
        return task
      })
      if (t) await this.pollTask(40)
    },

    async cancelTask() {
      const id = this.task?.id
      const workId = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      await this.guard('取消', async () => {
        const task = await api.cancelTask(id)
        if (this.workContextMatches(workId, version, id)) this.task = task
      })
    },

    // ---------- T05 数字人口播 ----------

    /**
     * 读取口播相关的全部只读视图。刻意分开取：某项不存在（例如还没分段计划）
     * 是正常状态，不应该让整页加载失败。
     */
    async loadTalkingHead(workId?: string, plateVariantId?: string) {
      const id = workId ?? this.currentWorkId
      if (!id) return
      const version = this.workLoadVersion
      const costVersion = ++this.thCostRequestVersion
      const prompt = this.thPrompt
      if (!this.thOptionsRestored && !this.thPrompt && this.currentWork?.route === 'WAN_3_0') this.thResolution = '720p'
      const resolution = this.thResolution
      const durationSeconds = this.thDurationSeconds
      const imageVersion = this.wanImageRequestVersion
      // 只读查询可用历史 route 定位报价；是否选中仍以服务端 route_confirmed 为准。
      const route = this.currentRoute ?? this.currentWork?.route
      await this.guard('读取口播状态', async () => {
        // 音频门禁单独取：取不到就留空（界面显示"未对齐"，不宣称任何结论），
        // 不让它把整页只读视图一起拖失败——与"某项不存在是正常状态"的既有取向一致。
        const audioGate = route === 'WAN_3_0' ? Promise.resolve(null) : Promise.resolve()
          .then(() => api.getAudioGate(id))
          .catch(() => null)
        const [routes, preflight, cost, segments, plate, submissions, gate, gateSummary, queue, adapters, videos, driver, wanImage] = await Promise.all([
          api.getTalkingHeadRoutes(id), api.getTalkingHeadPreflight(id), optional(() => api.getTalkingHeadCost(id, route, prompt, resolution, route === 'WAN_3_0' ? durationSeconds : undefined)),
          api.getSegmentPlan(id), api.getPlateStatus(id, plateVariantId), api.getSubmissions(id),
          api.getTalkingHeadGate(id), audioGate,
          api.getTalkingHeadQueue(id), api.getTalkingHeadAdapters(), api.getVideos(id),
          // 进度取不到就留空（界面不显示续发入口），不把整页只读视图拖失败。
          optional(() => api.getTalkingHeadDriver(id)),
          optional(() => api.getWanImage(id)),
        ])
        // 门禁逐条取：列表里可能有多条候选原片，各自结论不同。
        const gates: Record<string, VideoGateView> = {}
        for (const v of videos) {
          try {
            gates[v.id] = await api.getVideoGate(v.id)
          } catch {
            // 取不到就留空——界面显示"未取得结论"，绝不推断成通过。
          }
        }
        if (this.currentWorkId !== id || this.workLoadVersion !== version) return
        this.thRoutes = routes
        this.thPreflight = preflight
        let restoredOptions = false
        if (this.thCostRequestVersion === costVersion) {
          const saved = cost?.confirmation?.call_list
          if (saved && !this.thOptionsRestored && this.thPrompt === prompt && this.thResolution === resolution && this.thDurationSeconds === durationSeconds
            && prompt === '' && saved.route === route
            && route !== 'M2' && ['480p', '720p', '1080p'].includes(saved.resolution)) {
            this.thPrompt = saved.prompt
            this.thResolution = saved.resolution as Resolution
            if (route === 'WAN_3_0' && saved.generation_mode === 'image_prompt_native_audio' && Number.isInteger(saved.generated_seconds) && saved.generated_seconds! >= 2 && saved.generated_seconds! <= 30) this.thDurationSeconds = saved.generated_seconds!
            restoredOptions = true
          }
          this.thOptionsRestored = true
          this.thCost = this.routeSelected ? cost : null
        }
        this.thSegments = segments
        this.thPlate = plate
        this.thSubmissions = submissions
        this.thDriver = driver
        this.thGate = gate
        this.thAudioGate = gateSummary
        this.thQueue = queue
        this.thAdapters = adapters
        this.videos = videos
        this.videoGates = gates
        if (imageVersion === this.wanImageRequestVersion && wanImage) this.wanImage = wanImage.source
        if (restoredOptions) await this.refreshTalkingHeadCost()
      })
    },

    async uploadWanImage(file: File) {
      const id = this.currentWorkId, version = this.workLoadVersion
      if (!id || this.busy) return
      const imageVersion = ++this.wanImageRequestVersion
      this.thCostRequestVersion++
      if (this.thCost) this.thCost = { ...this.thCost, estimate: null, confirmation_valid: false, changed_fields: ['参考图片'] }
      const result = await this.guard('上传参考图片', () => api.uploadWanImage(id, file))
      if (!result || !this.workContextMatches(id, version) || imageVersion !== this.wanImageRequestVersion) return
      this.wanImage = result.source
      const preflight = await optional(() => api.getTalkingHeadPreflight(id))
      if (!this.workContextMatches(id, version) || imageVersion !== this.wanImageRequestVersion) return
      this.thPreflight = preflight
      await this.refreshTalkingHeadCost()
    },

    async confirmWanNativeVideo(videoId: string, reviewAck: boolean) {
      const id = this.currentWorkId, version = this.workLoadVersion
      if (!id || !reviewAck || this.busy) return
      const result = await this.guard('确认原声视频', () => api.confirmWanNativeVideo(id, videoId))
      if (!result?.ok || !this.workContextMatches(id, version)) return
      await this.loadWork(id)
      if (this.currentWorkId !== id) return
      this.pkg = result.package
      this.setNotice('ok', '已确认视频并保留模型原声，可下载作品包；当前不含自动字幕。')
      await this.guard('读取交付状态', () => this.refreshPostprocess())
    },

    async refreshTalkingHeadCost() {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      const route = this.currentRoute
      if (!id || !route) return
      const prompt = this.thPrompt, resolution = this.thResolution
      const durationSeconds = this.thDurationSeconds
      const costVersion = ++this.thCostRequestVersion
      const cost = await optional(() => api.getTalkingHeadCost(id, route, prompt, resolution, route === 'WAN_3_0' ? durationSeconds : undefined))
      if (this.workContextMatches(id, version) && this.thCostRequestVersion === costVersion
          && this.currentRoute === route && this.thPrompt === prompt && this.thResolution === resolution && this.thDurationSeconds === durationSeconds) {
        this.thCost = cost ?? (this.thCost ? { ...this.thCost, estimate: null,
          current_call_list: null, confirmation_valid: false, changed_fields: ['调用清单'] } : null)
      }
    },

    async updateTalkingHeadOptions(options: { prompt?: string; resolution?: Resolution; duration_seconds?: number }) {
      if (options.prompt !== undefined) this.thPrompt = options.prompt
      if (options.resolution !== undefined) this.thResolution = options.resolution
      if (options.duration_seconds !== undefined) this.thDurationSeconds = options.duration_seconds
      this.thOptionsRestored = true
      this.thCostRequestVersion++
      if (this.thCost) this.thCost = { ...this.thCost, estimate: null, confirmation_valid: false }
      await this.refreshTalkingHeadCost()
    },

    /** 费用确认：摘要由服务端按当前绑定推导，前端只提交输入（描述、分辨率）。 */
    async confirmTalkingHeadCost(route: GenerationRoute | null) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id || !route) return
      if (this.thInputError) { this.error = this.thInputError; return }
      const prompt = this.thPrompt
      const resolution = this.thResolution
      const durationSeconds = this.thDurationSeconds
      const costVersion = ++this.thCostRequestVersion
      const rec = await this.guard('确认费用', () =>
        api.confirmTalkingHeadCost(id, {
          route,
          prompt,
          resolution,
          ...(route === 'WAN_3_0' ? { duration_seconds: durationSeconds } : {}),
        }),
      )
      if (!rec) return
      this.setNotice(
        'warn',
        `已确认调用清单与金额（估价 ¥${(rec.estimated_fen / 100).toFixed(2)}，上限 ¥${(rec.ceiling_fen / 100).toFixed(2)}）：`
          + '这是清单确认，不是付费授权；实际扣费仍由预算策略与台账决定',
      )
      const cost = await api.getTalkingHeadCost(id, route, prompt, resolution, route === 'WAN_3_0' ? durationSeconds : undefined).catch(() => null)
      if (this.workContextMatches(id, version) && this.thCostRequestVersion === costVersion) {
        this.thOptionsRestored = true
        this.thCost = cost
      }
    },

    /**
     * 音频确认后即可并行对齐：为已确认的最终音频取真实字词时间轴并登记。
     *
     * 分段只能来自同一音轨的句边界时间戳，而对齐此前只在"已有通过审核的原片"之后才跑，
     * 于是"分段需要对齐、对齐需要原片、原片需要分段"绕成了死结。把对齐提前到音频确认之后，
     * 与规格一致：音频确认后即可并行对齐字幕，不必等原片。
     */
    async runAlignment() {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('运行字词对齐', () => api.runAlignment(id))
      if (!r) return
      this.thAlignment = r
      this.setNotice(
        'ok',
        r.reused
          ? (r.note ?? '已有可追溯的对齐产物，未重复调用远端模型')
          : `已登记 ${r.word_count} 个字词时间戳（来源 ${r.source ?? '未知'}，方法 ${r.method ?? '未记录'}）`,
      )
      // 对齐登记后门禁会变，重新取一遍只读视图，别让界面停在旧结论上。
      await this.loadTalkingHead()
    },

    async createSegmentPlan() {
      const id = this.currentWorkId
      if (!id) return
      const p = await this.guard('建立分段计划', () => api.createSegmentPlan(id))
      if (!p) return
      this.thSegments = p
      const count = p.segments?.length ?? 0
      this.setNotice(
        p.planned ? 'ok' : 'warn',
        p.planned
          ? `分段计划已建立：${count} 段，单段上限 ${((p.limit_ms ?? 0) / 1000).toFixed(0)} 秒，未加速、未截字`
          : (p.reason ?? '服务端未给出可用的分段计划'),
      )
    },

    async createPlate(outputSeconds: number, variantId?: string) {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id) return
      // 不带 variant_id 时后端回退到作品绑定的变体；这里显式传是为了让界面选的那条
      // 与实际用的那条一致——否则用户改了选择却仍用旧的，看不出来。
      const r = await this.guard('生成底片', () => api.createPlate(id, outputSeconds, variantId))
      if (!r) return
      // 占位产物不登记为资产：这里必须说清楚，否则用户会以为底片已经可用了。
      this.setNotice(r.registered ? 'ok' : 'warn', r.note)
      // 显式选图会由后端更新作品绑定与版本，先取回实际选择再刷新后续门禁。
      const works = await this.guard('同步作品绑定', () => api.listWorks())
      if (!works) return
      if (!this.workContextMatches(id, version)) return
      this.works = this.visibleWorks(works)
      await this.loadTalkingHead(id, variantId)
    },

    /** 底片审核：通过只能由用户给出——身份是否漂移只能由人目检。 */
    async reviewPlate(plateId: string, outcome: 'pass' | 'fail', note = '') {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('底片审核', () =>
        api.reviewPlate(plateId, { reviewer: 'user_local', outcome, note }),
      )
      if (!r) return
      this.setNotice('ok', `已登记底片审核结论：${outcome}`)
      await this.loadTalkingHead(id)
    },

    /**
     * 提交口播生成。
     *
     * `replacementAuthorized` 只应在用户**显式确认"重新生成会再次计费"**之后传 true。
     * 它不是普通开关：同一调用清单此前失败/未知过时，后端会要求重新确认费用并换一组
     * 幂等键，否则以 `RESUBMIT_REQUIRES_*` 拦下——这是防"点两下就重复扣费"的闸，
     * 界面不得悄悄绕过，只负责把用户的明确意图如实传下去。
     */
    async submitTalkingHead(dryRun: boolean, opts: { replacementAuthorized?: boolean } = {}) {
      if (this.currentRoute !== 'WAN_3_0' && !this.requireAppliedTtsControls()) return
      const id = this.currentWorkId
      const version = this.workLoadVersion
      const replacementAuthorized = !!opts.replacementAuthorized
      if (!id || this.busy || pollingStores.has(this)) return
      const route = this.currentRoute
      if (!route || this.thInputError) { this.error = this.thInputError; return }
      if (!dryRun && this.thSubmissionError) { this.error = this.thSubmissionError; return }
      if (route === 'WAN_3_0' && this.unresolvedVideoRuns.some(run => run.state === 'unknown')) {
        this.setNotice('warn', '已有结果未知的任务，请查询原任务或核对供应商记录，不能重新生成。')
        return
      }
      if (!dryRun && !replacementAuthorized && this.unresolvedVideoRuns.length) {
        this.setNotice('warn', '已有口播任务尚未取得结果，请查询原任务，不要重复生成。')
        return
      }
      if (!dryRun && !this.thValidConfirmedCost) {
        this.setNotice('warn', this.thConfirmedCost
          ? `${this.thCostChangedFields.join('、') || '调用清单'}已变化，请重新确认本次调用清单与费用。`
          : '请先确认本次调用清单与费用，再生成口播。')
        return
      }
      const input = { route, prompt: this.thPrompt,
        resolution: this.thResolution, dry_run: dryRun,
        ...(route === 'WAN_3_0' ? { duration_seconds: this.thDurationSeconds } : {}),
        ...(replacementAuthorized ? { replacement_authorized: true } : {}) }
      const r = await this.guard('提交口播', async () => {
        try {
          return await api.submitTalkingHead(id, input)
        } catch (error: any) {
          if (error?.code === 'COST_LIST_CHANGED' && this.workContextMatches(id, version)) {
            const costVersion = ++this.thCostRequestVersion
            const updated = await api.getTalkingHeadCost(id, input.route, input.prompt, input.resolution, input.duration_seconds).catch(() => null)
            if (this.workContextMatches(id, version) && this.thCostRequestVersion === costVersion) {
              // 查询失败时保留历史记录，但不能继续把它当作当前输入的有效确认。
              this.thCost = updated ?? (this.thCost ? { ...this.thCost, current_call_list: null,
                confirmation_valid: false, changed_fields: ['调用清单'] } : null)
            }
          }
          throw error
        }
      })
      if (!r) return
      // 先保留本次受理回执，查询期间也能阻止连续点击提交第二份任务。
      if (!r.is_mock && r.runs?.length) this.thSubmissions = {
        work_id: id, runs: r.runs, auto_fallback: false, note: r.note,
      }
      if (r.is_mock || !r.submitted) {
        // 占位提交不产生任何媒体：不得显示成"已生成"，也不得留下产物。
        this.setNotice('warn', r.note)
      } else {
        // 并发上限内的段在本次请求里提交，超出的段由后台推进器续发；
        // 不说清楚的话，界面会把"还有 6 段没提交"误读成"只切出了一段"。
        const pending = r.pending_segments ?? 0
        this.setNotice('ok', pending > 0
          ? `已提交 ${r.runs.length} 个调用；云端并发上限为 ${r.max_concurrency ?? 1}，`
            + `其余 ${pending} 段会在前一段完成后自动续发`
          : `已提交 ${r.runs.length} 个调用；外部作业需按原任务号查询，未知结果先核对`)
      }
      const [submissions, videos, driver] = await Promise.all([
        optional(() => api.getSubmissions(id)), optional(() => api.getVideos(id)),
        // 进度快照取不到只是少一个续发入口，不能让"提交已完成"这个事实跟着丢。
        optional(() => api.getTalkingHeadDriver(id)),
      ])
      if (!this.workContextMatches(id, version)) return
      if (submissions) this.thSubmissions = submissions
      if (videos) this.videos = videos
      if (driver) this.thDriver = driver
      if (!r.is_mock && r.submitted) await this.pollWaitingRuns()
    },

    /** 自动查询已受理的云端作业：查询不会重提，也不会把运行中误判为失败。 */
    async pollWaitingRuns(runId?: string): Promise<boolean> {
      const id = this.currentWorkId
      const version = this.workLoadVersion
      if (!id || pollingStores.has(this)) return false
      const runs = (this.thSubmissions?.runs ?? []).filter((r) =>
        !r.is_mock && r.work_id === id && !!r.remote_job_id
        && (runId ? r.id === runId && [...RUNNING_VIDEO_STATES, 'unknown'].includes(r.state) : RUNNING_VIDEO_STATES.includes(r.state)),
      )
      if (!runs.length) return false
      pollingStores.add(this)
      try {
        let changed = false
        const remoteIds = new Set<string>()
        for (const run of runs) {
          if (!this.workContextMatches(id, version)) return false
          const remoteKey = `${run.provider}:${run.remote_job_id}`
          if (remoteIds.has(remoteKey)) continue
          remoteIds.add(remoteKey)
          const result = await this.guard('查询口播进度', () => api.pollRun(run.id))
          if (!result || !this.workContextMatches(id, version)) return false
          const state = String(result.state ?? '')
          if (['succeeded', 'failed', 'unknown', 'cancelled'].includes(state)) changed = true
        }
        if (changed) {
          // 原片回收后同时刷新可播放地址和审核门禁，避免只更新视频列表。
          await this.loadTalkingHead(id)
        } else {
          const submissions = await this.guard('更新口播状态', () => api.getSubmissions(id))
          if (submissions && this.workContextMatches(id, version)) this.thSubmissions = submissions
        }
        return changed && this.workContextMatches(id, version)
      } finally {
        pollingStores.delete(this)
      }
    },

    /**
     * 续发剩余分段。
     *
     * 与"重提"是两件不同的事：重提是**新的付费调用**（要重新确认费用），
     * 续发只是把已经授权过的那次调用清单里还没发出的段按序发出去，
     * 因此不需要任何新的费用确认。有段 unknown/failed 时后端不会硬推，
     * 会返回 STOPPED —— 这里如实把原因显示出来，不假装已经继续。
     */
    async resumeTalkingHeadSegments() {
      const id = this.currentWorkId
      if (!id || this.busy) return
      const r = await this.guard('继续生成剩余分段', () => api.resumeTalkingHeadDriver(id))
      if (!r) return
      this.thDriver = r
      if (r.resumable) {
        this.setNotice('ok', `已交回后台推进：剩余 ${r.pending} 段会在前一段完成后依次提交`)
      } else {
        this.setNotice('warn', r.note)
      }
      await this.loadTalkingHead(id)
    },

    /** 未知结果先对账：不盲目重提，也不把未知写成失败。 */
    async settleRun(runId: string, outcome: 'succeeded' | 'failed' | 'cancelled', note: string) {
      const id = this.currentWorkId
      if (!id) return
      // 后端要"结论 + 证据来源"；无证据改判会被 EVIDENCE_REQUIRED 拒掉。
      const r = await this.guard('对账', () =>
        api.settleRun(runId, outcome, { source: 'user_manual', note }))
      if (!r) return
      this.setNotice('ok', '已登记对账结论；重提需你重新授权，不会自动发生')
      await this.loadTalkingHead(id)
    },

    async reviewVideo(
      videoId: string,
      dimensions: Record<string, string>,
      references: Record<string, { asset_id: string; start_ms: number; end_ms: number }>,
      outcome: 'pass' | 'fail',
      note = '',
    ) {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('原片审核', () =>
        api.reviewVideo(videoId, { reviewer: 'user_local', outcome, dimensions, references, note }),
      )
      if (!r) return
      this.setNotice('ok', '已登记人工原片审核结论（口型/身份/身体/表情四项）')
      await this.loadTalkingHead(id)
    },

    async recordAudioTrackCheck(
      videoId: string,
      input: { audio_asset_id: string; measured_offset_ms: number; evidence: string },
    ) {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('核对音轨', () =>
        api.recordAudioTrackCheck(videoId, {
          audio_asset_id: input.audio_asset_id,
          method: 'decode_align',
          outcome: 'pass',
          measured_offset_ms: input.measured_offset_ms,
          evidence: input.evidence,
        }),
      )
      if (!r) return
      this.setNotice('ok', '已登记音轨保持核对记录（与画面审核分别记录）')
      await this.loadTalkingHead(id)
    },

    async confirmFinalVideo(videoId: string, reviewAck: boolean, note = '') {
      const id = this.currentWorkId
      if (!id) return
      const r = await this.guard('确认原片', () => api.confirmFinalVideo(id, videoId, reviewAck, note))
      if (!r) return
      this.setNotice('ok', r.note || '已确认最终原片')
      await this.loadTalkingHead(id)
    },
  },
})
