import { defineStore } from 'pinia'
import { api } from '@/api'
import type {
  ContentJobReceipt, ContentState, MediaAssetView, ReferenceSearchItem, ReferenceSnapshot, MediaFetchQuote, ScriptVersion, VisualQuote,
} from '@/api/types'

const TERMINAL = ['succeeded', 'partial', 'blocked', 'failed', 'unknown', 'cancelled', 'superseded']
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

function importAwaitingResult(job: ContentJobReceipt): boolean {
  const paused = job.state === 'unknown' && ['REFERENCE_POLL_TIMEOUT', 'REFERENCE_QUERY_FAILED'].includes(job.error?.code ?? '')
  return job.kind === 'import' && (job.state === 'waiting_external' || paused)
    && !!job.calls?.some(call => call.provider === 'redfox' && call.step === 'transcript_submit'
      && call.state === 'waiting_external' && call.has_remote_handle)
}

function jobErrorMessage(job: ContentJobReceipt): string {
  if (job.state === 'failed' || job.state === 'blocked') return job.error?.message ?? '任务未成功'
  if (job.state === 'unknown') {
    if (['PROVIDER_RESPONSE_TIMEOUT', 'PROVIDER_CONNECTION_CLOSED', 'VISUAL_PREVIOUS_CALL_UNKNOWN',
      'REFERENCE_POLL_TIMEOUT', 'REFERENCE_QUERY_FAILED'].includes(job.error?.code ?? '') && job.error?.message) return job.error.message
    return '这一步的结果未确认（可能已发出请求），系统不会自动重发，请先核对。'
  }
  return ''
}

export type SearchSort = 'likes' | 'recent' | 'recommended'
export type ContentRoute = 'talking_script' | 'short_video_storyboard'
type WriterJobTracking = { jobId: string; fingerprint: string; runtimeKey: string }
type ReviewJobTracking = { jobId: string; fingerprint: string; batchKey: string }

function needsRepair(candidate: ScriptVersion): boolean {
  return candidate.review_state === 'failed'
    || !!candidate.quality_report?.checks?.some(check => check.hard_gate && check.outcome !== 'pass')
}

/** 每类操作的 request_id 只在“同一份意图”里稳定：刷新后还能取回原任务，不会因重复点击多收一次费。 */
const RID_PREFIX = 'avatar-studio:content-rid:'

function stored(key: string): string | null {
  try { return localStorage.getItem(RID_PREFIX + key) } catch { return null }
}
function remember(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(RID_PREFIX + key)
    else localStorage.setItem(RID_PREFIX + key, value)
  } catch { /* 无存储时仍可工作，只是刷新后不能复用 */ }
}
function newRid(): string {
  const c: any = globalThis.crypto
  return c?.randomUUID ? c.randomUUID() : `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export const useContentStore = defineStore('content', {
  state: () => ({
    workId: '' as string,
    /** 每次切作品/卸载都递增：晚到的响应先核对世代再落地。 */
    generation: 0,
    state: null as ContentState | null,
    loading: false,
    refreshRequest: 0,
    submittingKind: null as string | null,
    directRequestPending: false,
    error: '' as string,
    notice: '' as string,
    inspectedReference: null as ReferenceSnapshot | null,
    inspecting: false,
    inspectionRequest: 0,
    visualQuoteRequest: 0,
    fetchQuoteRequest: 0,
    // 搜索
    query: '' as string,
    periodDays: 7 as 7 | 30,
    sort: 'likes' as SearchSort,
    searchJob: null as ContentJobReceipt | null,
    searchItems: [] as ReferenceSearchItem[],
    nextCursor: null as string | null,
    searchedAt: '' as string,
    sortedScope: '' as string,
    // 进行中的任务（按 kind 索引）
    jobs: {} as Record<string, ContentJobReceipt>,
    // 写稿等待超出页面同步期限后，仍只追踪本次回执，避免历史错误混入当前请求。
    writerJobs: {} as Record<string, WriterJobTracking>,
    reviewJob: null as ReviewJobTracking | null,
    refreshingImportJobId: '',
    polling: false,
    pollTimer: null as ReturnType<typeof setTimeout> | null,
    // 画面分析：上传的原视频（按参考快照索引）与最近一次报价
    mediaByRef: {} as Record<string, MediaAssetView>,
    visualQuote: null as VisualQuote | null,
    visualQuoteBinding: null as { referenceId: string; mediaId: string } | null,
    fetchQuote: null as MediaFetchQuote | null,
    uploading: false,
  }),

  getters: {
    revision: (s) => s.state?.content_revision ?? 0,
    activeJob: (s): ContentJobReceipt | null =>
      s.state?.active_jobs?.[0] ?? (Object.values(s.jobs) as ContentJobReceipt[]).find((j) => !TERMINAL.includes(j.state)) ?? null,
    waitingImportJob: (s): ContentJobReceipt | null =>
      [...(s.state?.active_jobs ?? []), ...Object.values(s.jobs), ...(s.state?.recent_jobs ?? []).slice().reverse()]
        .find(importAwaitingResult) ?? null,
    busy(): boolean { return this.loading || !!this.submittingKind || !!this.activeJob },
    selectedReference: (s) => s.state?.reference ?? null,
    /** 每条参考的反馈也绑定媒体，避免显示其他步骤或换视频之前的失败。 */
    visualJobForReference: (s) => (referenceId: string, mediaId: string): ContentJobReceipt | null => {
      if (!referenceId || !mediaId) return null
      // recent_jobs 按创建时间升序返回，历史反馈必须取最后一次操作。
      return [...(s.state?.active_jobs ?? []), ...Object.values(s.jobs).filter((job) => !TERMINAL.includes(job.state)).reverse(),
        ...(s.state?.recent_jobs ?? []).slice().reverse(), ...Object.values(s.jobs).reverse()]
        .find((job) => job.kind === 'visual_analysis' && job.reference_snapshot_id === referenceId && job.media_asset_id === mediaId) ?? null
    },
    visualErrorForReference(): (referenceId: string, mediaId: string) => string {
      return (referenceId, mediaId) => {
        const job = this.visualJobForReference(referenceId, mediaId)
        return job ? jobErrorMessage(job) : ''
      }
    },
    prompt: (s) => s.state?.prompt ?? null,
    batch: (s) => s.state?.batch ?? null,
    passedCount: (s): number => s.state?.batch?.passed ?? (s.state?.batch?.candidates ?? []).filter((c) => c.pickable).length,
    visibleCandidates: (s): ScriptVersion[] => s.state?.batch?.candidates ?? [],
    writerWaitMs: (s): number => {
      const runtime = s.state?.writer_runtime
      const configured = runtime?.timeout_seconds
      const seconds = Number.isFinite(configured) && (configured ?? 0) > 0
        ? configured! : runtime?.execution_mode === 'claude_code' ? 600 : 120
      return (seconds + 90) * 1000
    },
    /** 面向普通用户的费用说明：不暴露 token/协议细节。 */
    costNote: (s): string => {
      if (!s.state) return '正在读取费用信息。'
      return '内容创作不设每日、单流程或调用次数额度上限，费用按实际调用记录。'
    },
  },

  actions: {
    async setRoute(route: ContentRoute): Promise<boolean> {
      if (!this.state || this.state.brief?.content_route === route || this.busy) return false
      const id = this.workId
      const gen = this.generation
      this.submittingKind = 'settings'
      this.error = ''; this.notice = ''
      try {
        await api.saveContentSettings(id, {
          expected_content_revision: this.revision, brief: { content_route: route },
        })
        if (gen === this.generation && id === this.workId) {
          this.notice = '内容路线已更新，请重新准备写作提示。'
          await this.refresh(gen)
          return gen === this.generation && id === this.workId && this.state?.brief.content_route === route
        }
      } catch (e: any) {
        if (gen === this.generation && id === this.workId) this.error = e?.message ?? String(e)
      } finally {
        if (gen === this.generation && id === this.workId && this.submittingKind === 'settings') this.submittingKind = null
      }
      return false
    },
    /** 进入某作品：清空旧作品的一切本地状态并恢复（含刷新后的任务）。 */
    async enter(workId: string) {
      if (this.workId === workId && this.state) return
      this.leave()
      this.workId = workId
      this.directRequestPending = !!stored(`${workId}:direct-pending`)
      this.restoreWriterJobs()
      this.restoreReviewJob()
      const gen = this.generation
      await this.refresh(gen)
      if (gen === this.generation && this.state?.active_jobs?.length) this.startPolling()
    },

    /** 切走或卸载：停止轮询，作废在途响应。 */
    leave() {
      this.generation++
      this.stopPolling()
      this.workId = ''
      this.directRequestPending = false
      this.state = null
      this.error = ''
      this.notice = ''
      this.inspectedReference = null
      this.inspecting = false
      this.inspectionRequest++
      this.visualQuoteRequest++
      this.fetchQuoteRequest++
      this.jobs = {}
      this.writerJobs = {}
      this.reviewJob = null
      this.refreshingImportJobId = ''
      this.mediaByRef = {}
      this.visualQuote = null
      this.visualQuoteBinding = null
      this.fetchQuote = null
      this.uploading = false
      this.searchJob = null
      this.searchItems = []
      this.nextCursor = null
      this.loading = false
      this.refreshRequest++
      this.submittingKind = null
    },

    async refresh(gen = this.generation) {
      const id = this.workId
      if (!id || gen !== this.generation) return
      const request = ++this.refreshRequest
      this.loading = true
      try {
        const s = await api.getContentState(id)
        if (gen !== this.generation || id !== this.workId || request !== this.refreshRequest || s.work_id !== id) return
        this.state = s
        const byId = new Map((s.media_assets ?? []).map((m) => [m.id, m] as const))
        for (const r of s.references ?? []) {
          const m = r.media_asset_id ? byId.get(r.media_asset_id) : undefined
          if (m && !this.mediaByRef[r.id]) this.mediaByRef[r.id] = m
        }
        // 以后端为准：不在 active_jobs 里的本地回执已经结束（提交时的 queued 回执不会自己变成终态）
        const active = new Set((s.active_jobs ?? []).map((j) => j.job_id))
        for (const [kind, tracked] of Object.entries(this.writerJobs) as [string, WriterJobTracking][]) {
          if (tracked.runtimeKey !== this.writerFingerprint('')) { this.forgetWriterJob(kind); continue }
          if (active.has(tracked.jobId)) continue
          const terminal = (s.recent_jobs ?? []).find((j) => j.job_id === tracked.jobId) ?? this.jobs[kind]
          if (terminal?.job_id === tracked.jobId && TERMINAL.includes(terminal.state)) this.settleWriterJob(terminal)
        }
        const trackedReview = this.reviewJob
        if (trackedReview && trackedReview.batchKey !== this.reviewBatchKey()) this.forgetReviewJob()
        else if (trackedReview && !active.has(trackedReview.jobId)) {
          const terminal = (s.recent_jobs ?? []).find(job => job.job_id === trackedReview.jobId) ?? this.jobs.review
          if (terminal?.job_id === trackedReview.jobId && TERMINAL.includes(terminal.state)) this.settleReviewJob(terminal)
        }
        for (const [k, j] of Object.entries(this.jobs) as [string, ContentJobReceipt][]) if (TERMINAL.includes(j.state) || !active.has(j.job_id)) delete this.jobs[k]
      } catch (e: any) {
        if (gen === this.generation && id === this.workId && request === this.refreshRequest) this.error = e?.message ?? String(e)
      } finally {
        if (gen === this.generation && id === this.workId && request === this.refreshRequest) this.loading = false
      }
    },

    // ---- 轮询：卸载/切作品/任务终态都停止；已终态不再轮询 ----
    startPolling() {
      if (this.polling || !this.workId) return
      this.polling = true
      const gen = this.generation
      const tick = async () => {
        if (gen !== this.generation || !this.polling) return
        await this.refresh(gen)
        if (gen !== this.generation) return
        const active = this.state?.active_jobs ?? []
        if (!active.length) { this.stopPolling(); return }
        this.pollTimer = setTimeout(tick, 1500)
      }
      this.pollTimer = setTimeout(tick, 600)
    },
    stopPolling() {
      this.polling = false
      if (this.pollTimer) clearTimeout(this.pollTimer)
      this.pollTimer = null
    },

    /** 取同一意图的 request_id；意图变化（payload 摘要变了）才换新，以便“换个关键词再搜”不被当成重放。 */
    rid(kind: string, fingerprint: string): string {
      const key = `${this.workId}:${kind}:${fingerprint}`
      let v = stored(key)
      if (!v) { v = newRid(); remember(key, v) }
      return v
    },
    /** 任务落到终态后释放 request_id：下一次同样的操作是新意图（例如“重新生成”）。 */
    releaseRid(kind: string, fingerprint: string) {
      remember(`${this.workId}:${kind}:${fingerprint}`, null)
    },

    writerFingerprint(fingerprint: string): string {
      const runtime = this.state?.writer_runtime
      return runtime ? `${fingerprint}|writer:${JSON.stringify([runtime.execution_mode, runtime.profile_id ?? ''])}` : fingerprint
    },

    writerJobCurrent(jobId: string): boolean {
      return (Object.values(this.writerJobs) as WriterJobTracking[]).some((tracked) => tracked.jobId === jobId && tracked.runtimeKey === this.writerFingerprint(''))
    },

    restoreWriterJobs() {
      for (const kind of ['generation', 'repair']) {
        try {
          const parsed = JSON.parse(stored(`${this.workId}:writer-job:${kind}`) ?? 'null')
          if (parsed && typeof parsed.jobId === 'string' && typeof parsed.fingerprint === 'string' && typeof parsed.runtimeKey === 'string') this.writerJobs[kind] = parsed
        } catch { /* 无效的本地缓存不能改变请求或费用状态。 */ }
      }
    },

    forgetWriterJob(kind: string) {
      delete this.writerJobs[kind]
      remember(`${this.workId}:writer-job:${kind}`, null)
    },

    settleWriterJob(job: ContentJobReceipt) {
      const entry = (Object.entries(this.writerJobs) as [string, WriterJobTracking][]).find(([, tracked]) => tracked.jobId === job.job_id)
      if (!entry || !TERMINAL.includes(job.state)) return
      const [kind, tracked] = entry
      if (tracked.runtimeKey !== this.writerFingerprint('')) { this.forgetWriterJob(kind); return }
      this.forgetWriterJob(kind)
      this.error = jobErrorMessage(job)
      this.notice = job.state === 'superseded' ? '创作内容在生成期间被修改，这次结果已归档，没有覆盖当前内容。' : ''
      if (['succeeded', 'partial'].includes(job.state)) {
        const produced = job.result_refs?.produced, passed = job.result_refs?.passed
        if (Number.isInteger(produced) && Number.isInteger(passed) && produced >= 0 && passed >= 0 && passed <= produced) {
          this.notice = produced ? `${kind === 'repair' ? '重写新版本' : '新稿'}已生成，当前批次 ${passed}/${produced} 篇通过独立审核`
            + (passed < produced ? `，${produced - passed} 篇尚未通过。` : '。')
            : '本次未产出新稿，请查看任务详情。'
        }
      }
      if (job.state !== 'unknown') this.releaseRid(kind, tracked.fingerprint)
    },

    reviewBatchKey(): string {
      return `${this.state?.batch?.id ?? ''}@${this.revision}`
    },

    reviewJobCurrent(jobId: string): boolean {
      return this.reviewJob?.jobId === jobId && this.reviewJob.batchKey === this.reviewBatchKey()
    },

    restoreReviewJob() {
      try {
        const parsed = JSON.parse(stored(`${this.workId}:review-job`) ?? 'null')
        if (parsed && typeof parsed.jobId === 'string' && typeof parsed.fingerprint === 'string' && typeof parsed.batchKey === 'string') this.reviewJob = parsed
      } catch { /* 缓存只恢复任务追踪，不决定是否重新收费。 */ }
    },

    forgetReviewJob() {
      this.reviewJob = null
      remember(`${this.workId}:review-job`, null)
    },

    settleReviewJob(job: ContentJobReceipt) {
      const tracked = this.reviewJob
      if (!tracked || !this.reviewJobCurrent(job.job_id) || !TERMINAL.includes(job.state)) return
      this.forgetReviewJob()
      this.error = jobErrorMessage(job)
      this.notice = job.state === 'superseded' ? '稿件在审核期间已修改，这次审核结果已归档，请审核当前版本。' : ''
      if (job.state !== 'unknown') this.releaseRid('review', tracked.fingerprint)
    },

    async submit(kind: string, fingerprint: string, call: (rid: string) => Promise<ContentJobReceipt>, okNote = '') {
      const id = this.workId
      const gen = this.generation
      const writerRuntimeKey = this.writerFingerprint('')
      const reviewBatchKey = this.reviewBatchKey()
      if (!id || this.submittingKind || this.loading) return null
      this.submittingKind = kind
      this.error = ''
      this.notice = ''
      try {
        const receipt = await call(this.rid(kind, fingerprint))
        if (gen !== this.generation || id !== this.workId) return null
        this.jobs[kind] = receipt
        if (kind === 'generation' || kind === 'repair') this.writerJobs[kind] = {
          jobId: receipt.job_id, fingerprint, runtimeKey: writerRuntimeKey,
        }
        if (kind === 'generation' || kind === 'repair') remember(`${id}:writer-job:${kind}`, JSON.stringify(this.writerJobs[kind]))
        if (kind === 'review') {
          this.reviewJob = { jobId: receipt.job_id, fingerprint, batchKey: reviewBatchKey }
          remember(`${id}:review-job`, JSON.stringify(this.reviewJob))
        }
        if (okNote && !receipt.replayed) this.notice = okNote
        if (receipt.replayed) this.notice = '这项操作已经提交过，正在沿用原任务，不会重复发起或重复计费。'
        await this.refresh(gen)
        if (gen !== this.generation || id !== this.workId) return null
        this.startPolling()
        return receipt
      } catch (e: any) {
        if (gen === this.generation && id === this.workId) {
          const stale = e?.code === 'CONTENT_REVISION_STALE'
          this.error = stale ? '内容在别处已被修改，已为你刷新，请确认后再操作。' : (e?.message ?? String(e))
          if (stale) await this.refresh(gen)
        }
        return null
      } finally {
        if (gen === this.generation && id === this.workId) this.submittingKind = null
      }
    },

    async search(more = false) {
      const q = this.query.trim()
      if (!q) { this.error = '请先填写要搜索的主题。'; return }
      const fp = `${q}|${this.periodDays}|${this.sort}|${more ? this.nextCursor : ''}`
      const receipt = await this.submit('search', fp, (request_id) => api.searchReferences(this.workId, {
        request_id, query: q, period_days: this.periodDays, sort: this.sort, cursor: more ? this.nextCursor : null,
      }))
      if (receipt) await this.waitForJob(receipt.job_id, (job) => {
        const refs = job.result_refs || {}
        const items = (refs.items ?? []) as ReferenceSearchItem[]
        this.searchItems = more ? [...this.searchItems, ...items.filter((i) => !this.searchItems.some((o) => o.platform_work_id === i.platform_work_id))] : items
        this.nextCursor = refs.next_cursor ?? null
        this.searchedAt = refs.fetched_at ?? ''
        this.sortedScope = refs.sorted_scope ?? ''
        this.searchJob = job
        this.releaseRid('search', fp)
      })
    },

    /** 等待某个任务到终态（受世代保护），到终态后回调一次。 */
    async waitForJob(jobId: string, onDone: (job: ContentJobReceipt) => void | Promise<void>, maxMs = 120000, isCurrent?: () => boolean) {
      const gen = this.generation
      const id = this.workId
      const start = Date.now()
      while (Date.now() - start < maxMs) {
        if (gen !== this.generation || id !== this.workId || (isCurrent && !isCurrent())) return
        let job: ContentJobReceipt
        try { job = await api.getContentJob(id, jobId) } catch (e: any) {
          if (gen === this.generation && id === this.workId && (!isCurrent || isCurrent())) this.error = e?.message ?? String(e)
          return
        }
        if (gen !== this.generation || id !== this.workId || (isCurrent && !isCurrent())) return
        if (TERMINAL.includes(job.state)) {
          const message = jobErrorMessage(job)
          if (message) { this.error = message; this.notice = '' }
          else if (job.state === 'superseded') this.notice = '创作内容在生成期间被修改，这次结果已归档，没有覆盖当前内容。'
          await onDone(job)
          await this.refresh(gen)
          return
        }
        await wait(1200)
      }
      if (gen !== this.generation || id !== this.workId || (isCurrent && !isCurrent())) return
      await this.refresh(gen)
      if (gen !== this.generation || id !== this.workId) return
      const running = this.state?.active_jobs?.find((job) => job.job_id === jobId)
      if (running) this.startPolling()
    },

    async refreshImportResult(jobId: string) {
      const id = this.workId
      const gen = this.generation
      if (!id || !jobId || this.refreshingImportJobId) return
      this.refreshingImportJobId = jobId
      this.error = ''
      try {
        const job = await api.getContentJob(id, jobId)
        if (gen !== this.generation || id !== this.workId) return
        if (job.job_id !== jobId || !importAwaitingResult(job) || !job.actions?.includes('refresh')) {
          await this.refresh(gen)
          return
        }
        const receipt = await api.refreshContentJob(id, jobId)
        if (gen !== this.generation || id !== this.workId) return
        this.notice = receipt.status === 'needs_verification'
          ? receipt.message ?? '转写结果需要核对，未重新提交请求。'
          : '已查询原转写任务。'
        await this.refresh(gen)
        if (gen === this.generation && id === this.workId && this.state?.active_jobs?.length) this.startPolling()
      } catch (e: any) {
        if (gen === this.generation && id === this.workId) this.error = e?.message ?? String(e)
      } finally {
        if (gen === this.generation && id === this.workId) this.refreshingImportJobId = ''
      }
    },

    async importReference(item: { platform_work_id: string }) {
      const fp = item.platform_work_id
      const receipt = await this.submit('import', fp, (request_id) => api.importReference(this.workId, {
        request_id, search_job_id: this.searchJob?.job_id, platform_work_id: item.platform_work_id,
        expected_content_revision: this.revision,
      }), '已开始导入：先取详情，再提取口播转写（只对你选中的这一条收费）。')
      if (receipt) await this.waitForJob(receipt.job_id, async (job) => {
        this.releaseRid('import', fp)
        const referenceId = job.result_refs?.reference_id
        if (job.state === 'succeeded' && typeof referenceId === 'string') {
          const kind = this.state?.brief?.content_route === 'short_video_storyboard' ? '分镜剧本' : '口播稿'
          this.notice = `参考已导入，尚未生成${kind}。`
          await this.inspectReference(referenceId)
        }
      })
    },

    async inspectReference(referenceId: string) {
      const id = this.workId
      const gen = this.generation
      if (!id) return
      const request = ++this.inspectionRequest
      this.visualQuoteRequest++
      this.fetchQuoteRequest++
      this.visualQuote = null
      this.visualQuoteBinding = null
      this.fetchQuote = null
      this.inspecting = true
      try {
        const ref = await api.getReferenceSnapshot(id, referenceId)
        if (gen !== this.generation || id !== this.workId || request !== this.inspectionRequest) return
        if (ref.work_id === id && ref.id === referenceId) this.inspectedReference = ref
      } catch (e: any) {
        if (gen === this.generation && request === this.inspectionRequest) this.error = e?.message ?? String(e)
      } finally {
        if (gen === this.generation && request === this.inspectionRequest) this.inspecting = false
      }
    },

    async useReference(referenceId: string) {
      const id = this.workId
      const gen = this.generation
      try {
        await api.selectReference(id, referenceId, this.revision)
        if (gen === this.generation) {
          this.notice = '已选为主参考，之前的写作提示需要重新准备。'
          await this.refresh(gen)
          if (gen === this.generation) await this.inspectReference(referenceId)
        }
      } catch (e: any) {
        if (gen === this.generation) { this.error = e?.message ?? String(e); if (e?.code === 'CONTENT_REVISION_STALE') await this.refresh(gen) }
      }
    },

    /** 上传原视频：只保存并探测，不调用任何付费模型。 */
    async uploadReferenceMedia(referenceId: string, file: File) {
      const id = this.workId
      const gen = this.generation
      if (!id) return null
      this.error = ''
      this.uploading = true
      try {
        const { asset } = await api.uploadReferenceMedia(id, referenceId, file)
        if (gen !== this.generation || id !== this.workId) return null
        this.visualQuoteRequest++
        this.visualQuote = null
        this.visualQuoteBinding = null
        this.mediaByRef[referenceId] = asset
        this.notice = `视频已保存（${(asset.duration_ms / 1000).toFixed(1)} 秒，${asset.width}×${asset.height}）。分析画面前需要你确认费用。`
        return asset
      } catch (e: any) {
        if (gen === this.generation) this.error = e?.message ?? String(e)
        return null
      } finally {
        if (gen === this.generation) this.uploading = false
      }
    },

    /** 自动获取视频：RedFox 下载器是独立付费调用，页面确认费用后才发起；完成后把新视频挂到该参考上。 */
    async loadMediaFetchQuote(referenceId: string) {
      const gen = this.generation
      const request = ++this.fetchQuoteRequest
      try {
        const q = await api.quoteMediaFetch(this.workId, { reference_snapshot_id: referenceId })
        if (gen === this.generation && request === this.fetchQuoteRequest) this.fetchQuote = q
      } catch (e: any) {
        if (gen === this.generation && request === this.fetchQuoteRequest) { this.fetchQuote = null; this.error = e?.message ?? String(e) }
      }
    },

    async fetchReferenceMedia(referenceId: string, ceilingFen: number) {
      const fp = `${referenceId}|${ceilingFen}`
      const receipt = await this.submit('media_fetch', fp, (request_id) => api.startMediaFetch(this.workId, {
        request_id, reference_snapshot_id: referenceId, expected_content_revision: this.revision,
        paid_ack: true, total_ceiling_fen: ceilingFen,
      }), '已开始获取视频：先向红狐取下载地址，再保存到本地（这是独立的付费调用，已按你确认的上限预留）。')
      if (receipt) await this.waitForJob(receipt.job_id, (job) => {
        this.releaseRid('media_fetch', fp)
        const refs = job.result_refs as { media_asset_id?: string; duration_ms?: number; byte_size?: number } | undefined
        if (job.state === 'succeeded' && refs?.media_asset_id) {
          this.visualQuoteRequest++
          this.visualQuote = null
          this.visualQuoteBinding = null
          this.mediaByRef[referenceId] = {
            id: refs.media_asset_id, duration_ms: refs.duration_ms ?? 0, width: 0, height: 0, mime_type: 'video/mp4',
            sha256: '', byte_size: refs.byte_size ?? 0, source_kind: 'provider_url',
          }
          this.notice = '视频已保存到本地。分析画面前还需要你确认费用。'
        }
      }, 180000)
    },

    async loadVisualQuote(referenceId: string, mediaId: string) {
      const id = this.workId
      const gen = this.generation
      const request = ++this.visualQuoteRequest
      this.visualQuote = null
      this.visualQuoteBinding = { referenceId, mediaId }
      try {
        const q = await api.quoteVisualAnalysis(id, { reference_snapshot_id: referenceId, media_asset_id: mediaId })
        if (gen === this.generation && id === this.workId && request === this.visualQuoteRequest) this.visualQuote = q
      } catch (e: any) {
        if (gen === this.generation && request === this.visualQuoteRequest) { this.visualQuote = null; this.error = e?.message ?? String(e) }
      }
    },

    /** 用户勾选费用确认后才调用：paid_ack 与费用上限由页面交给后端，后端绑定到本次调用清单。 */
    async analyzeReferenceVisual(referenceId: string, mediaId: string, ceilingFen: number,
      retry?: { retry_unknown_call_id: string; retry_paid_ack: boolean }) {
      if (this.busy || this.uploading) return
      const bound = this.visualQuoteBinding?.referenceId === referenceId && this.visualQuoteBinding?.mediaId === mediaId
      const previous = bound ? this.visualQuote?.retry_unknown_call?.call_id : undefined
      if ((previous || retry) && (!bound || !previous || retry?.retry_unknown_call_id !== previous || retry?.retry_paid_ack !== true)) {
        this.error = '请核对当前视频的费用，并单独确认上次可能已计费和本次重新分析的费用。'
        return
      }
      // 未知原调用与本次追加授权是不同意图；未知回执仍保留同一request_id供刷新后重放。
      const quoteDigest = bound ? this.visualQuote?.request_digest : undefined
      const fp = `${referenceId}|${mediaId}|${ceilingFen}${quoteDigest ? `|quote:${quoteDigest}` : ''}${previous ? `|retry:${previous}` : ''}`
      const gen = this.generation
      const maxMs = ((this.visualQuote?.timeout_seconds ?? 600) + 90) * 1000
      const receipt = await this.submit('visual_analysis', fp, (request_id) => api.startVisualAnalysis(this.workId, {
        request_id, reference_snapshot_id: referenceId, media_asset_id: mediaId,
        expected_content_revision: this.revision, paid_ack: true, total_ceiling_fen: ceilingFen,
        ...(previous ? { retry_unknown_call_id: previous, retry_paid_ack: true } : {}),
      }), '已开始分析画面：抽取关键帧并调用视觉模型（这是独立的付费调用，已按你确认的上限预留）。')
      if (receipt) await this.waitForJob(receipt.job_id, async (job) => {
        if (job.state !== 'unknown') this.releaseRid('visual_analysis', fp)
        if (job.state === 'succeeded') {
          this.notice = '画面分析完成，新参考尚未设为主参考。'
          const referenceId = job.result_refs?.reference_id
          if (typeof referenceId === 'string') await this.inspectReference(referenceId)
        }
        if (gen === this.generation && this.visualQuoteBinding?.referenceId === referenceId && this.visualQuoteBinding?.mediaId === mediaId) {
          await this.loadVisualQuote(referenceId, mediaId)
        }
      }, maxMs)
      else if (gen === this.generation && this.visualQuoteBinding?.referenceId === referenceId && this.visualQuoteBinding?.mediaId === mediaId) {
        await this.loadVisualQuote(referenceId, mediaId)
      }
    },

    async saveTranscript(referenceId: string, text: string) {
      const fp = `${referenceId}|${text.length}|${text.slice(0, 40)}`
      const receipt = await this.submit('reference_revision', fp, (request_id) => api.reviseReferenceTranscript(this.workId, {
        request_id, reference_snapshot_id: referenceId, transcript_text: text, expected_content_revision: this.revision,
      }), '已保存修正后的转写为新的参考版本，原始转写保留。')
      if (receipt) await this.waitForJob(receipt.job_id, async (job) => {
        this.releaseRid('reference_revision', fp)
        const referenceId = job.result_refs?.reference_id
        if (job.state === 'succeeded' && typeof referenceId === 'string') await this.inspectReference(referenceId)
      })
    },

    async preparePrompt() {
      const fp = `${this.state?.reference?.id ?? ''}@${this.revision}`
      const receipt = await this.submit('prompt', fp, (request_id) => api.preparePrompt(this.workId, {
        request_id, expected_content_revision: this.revision,
      }), '正在拆解参考并准备写作提示。')
      if (receipt) await this.waitForJob(receipt.job_id, () => this.releaseRid('prompt', fp))
    },

    async savePrompt(promptText: string) {
      const p = this.state?.prompt
      if (!p) return
      const fp = `${p.id}|${promptText.length}|${promptText.slice(0, 40)}`
      const receipt = await this.submit('prompt_revision', fp, (request_id) => api.revisePrompt(this.workId, {
        request_id, prompt_id: p.id, prompt_text: promptText, expected_content_revision: this.revision,
      }), '已保存为新的提示版本。')
      if (receipt) await this.waitForJob(receipt.job_id, () => this.releaseRid('prompt_revision', fp))
    },

    /** 已确认的终态会释放 request_id；“重新生成”不能绕过在途或未知调用。 */
    async generateDirect(topic: string, materials: string[] = []) {
      if (this.busy || !this.state) return
      if (typeof api.startDirectGeneration !== 'function') { this.error = '当前模式不支持直接生成，请连接生产后端。'; return }
      const id = this.workId
      const gen = this.generation
      const desired = { topic: topic.trim(), materials, content_route: this.state.brief.content_route ?? 'talking_script' as ContentRoute }
      if (!desired.topic) { this.error = '请先填写这期主题。'; return }
      const fp = this.writerFingerprint(`direct:${JSON.stringify(desired)}`)
      const intentKey = `${id}:direct-pending`
      type DirectInput = typeof desired & { request_id: string; expected_content_revision: number }
      let input: DirectInput | null = null
      try {
        const prior = JSON.parse(stored(intentKey) ?? 'null') as DirectInput | null
        if (prior && typeof prior.request_id === 'string' && Number.isInteger(prior.expected_content_revision)) {
          if (JSON.stringify({ topic: prior.topic, materials: prior.materials, content_route: prior.content_route }) !== JSON.stringify(desired)) {
            this.error = '上次直接生成的提交结果尚未确认，请先按原主题和资料核对原任务，再修改输入。'
            return
          }
          input = prior
          remember(`${id}:generation:${fp}`, input.request_id)
        }
      } catch { /* Malformed local state cannot authorize a model call. */ }
      const maxMs = this.writerWaitMs
      const receipt = await this.submit('generation', fp, async (request_id) => {
        input ??= { ...desired, request_id, expected_content_revision: this.revision }
        remember(intentKey, JSON.stringify(input))
        this.directRequestPending = true
        try {
          return await api.startDirectGeneration(id, input)
        } catch (cause: any) {
          if (cause?.status != null && cause.status < 500) {
            remember(intentKey, null)
            remember(`${id}:generation:${fp}`, null)
            if (gen === this.generation && id === this.workId) this.directRequestPending = false
          }
          throw cause
        }
      }, '正在按主题写稿并执行模型审核。')
      if (receipt && gen === this.generation && id === this.workId) {
        remember(intentKey, null)
        this.directRequestPending = false
        await this.waitForJob(receipt.job_id, job => this.settleWriterJob(job), maxMs,
          () => this.writerJobCurrent(receipt.job_id))
      }
    },

    async generate(force = false) {
      if (this.submittingKind || this.loading) return
      const p = this.state?.prompt
      if (!p) { this.error = '请先准备并确认写作提示。'; return }
      const content_route = this.state?.brief?.content_route ?? 'talking_script'
      const unknown = [...(this.state?.recent_jobs ?? [])].reverse().find((job) =>
        job.kind === 'generation' && job.state === 'unknown' && job.prompt_id === p.id
        && job.content_revision === this.revision)
      const retryCall = force ? unknown?.calls?.find((call) =>
        call.step === 'write' && call.state === 'unknown' && !call.has_remote_handle) : undefined
      const fp = this.writerFingerprint(`${p.id}@${this.revision}:${content_route}`)
        + (retryCall ? `|retry:${retryCall.id}` : '')
      const maxMs = this.writerWaitMs
      const receipt = await this.submit('generation', fp, (request_id) => api.startGeneration(this.workId, {
        request_id, prompt_id: p.id, content_route, expected_content_revision: this.revision,
        ...(retryCall ? { retry_unknown_call_id: retryCall.id, retry_paid_ack: true } : {}),
      }), this.state?.writer_runtime?.execution_mode === 'claude_code'
        ? '已开始通过 Claude Code 写三个不同角度的稿，完成后逐稿独立审核。' : '已开始写三个不同角度的稿并逐稿独立审核。')
      if (receipt) await this.waitForJob(receipt.job_id, (job) => this.settleWriterJob(job), maxMs,
        () => this.writerJobCurrent(receipt.job_id))
    },

    async repair(candidate: ScriptVersion, instruction = '') {
      const b = this.state?.batch
      if (!b) return
      if (!needsRepair(candidate)) { this.notice = '这篇稿件尚无明确返工意见，请先重新审核。'; return }
      const fp = this.writerFingerprint(`${b.id}|${candidate.id}|${instruction}`)
      const maxMs = this.writerWaitMs
      const receipt = await this.submit('repair', fp, (request_id) => api.startRepair(this.workId, {
        request_id, batch_id: b.id, candidate_ids: [candidate.id], expected_content_revision: this.revision,
        ...(instruction ? { instruction } : {}),
      }), this.state?.writer_runtime?.execution_mode === 'claude_code'
        ? '已开始通过 Claude Code 按审稿意见重写这一稿。' : '正在按审稿意见重写这一稿，其余稿保持不变。')
      if (receipt) await this.waitForJob(receipt.job_id, (job) => this.settleWriterJob(job), maxMs,
        () => this.writerJobCurrent(receipt.job_id))
    },

    async repairPending() {
      const b = this.state?.batch
      if (!b) return
      const ids = (b.retry_targets ?? []).filter(target => target.review_state === 'failed'
        || b.candidates.some(candidate => candidate.id === target.candidate_id && needsRepair(candidate)))
        .map(target => target.candidate_id)
      if (!ids.length) { this.notice = '没有需要按意见重写的稿件，请重新审核待审稿件。'; return }
      const fp = this.writerFingerprint(`${b.id}|${ids.join(',')}`)
      const maxMs = this.writerWaitMs
      const receipt = await this.submit('repair', fp, (request_id) => api.startRepair(this.workId, {
        request_id, batch_id: b.id, candidate_ids: ids, expected_content_revision: this.revision,
      }), this.state?.writer_runtime?.execution_mode === 'claude_code'
        ? '已开始通过 Claude Code 按审核意见返工未通过的稿件。' : '正在按审核意见返工未通过的稿件。')
      if (receipt) await this.waitForJob(receipt.job_id, (job) => this.settleWriterJob(job), maxMs,
        () => this.writerJobCurrent(receipt.job_id))
    },

    async rereview() {
      if (this.busy) return
      const b = this.state?.batch
      if (!b) return
      const retry = this.state?.review_retry
      if (retry && retry.batch_id !== b.id) { this.error = '审核恢复信息已失效，请刷新后重试。'; return }
      if (retry) {
        const reserve = retry.reserved_fen == null ? '未确认' : `¥${(retry.reserved_fen / 100).toFixed(2)}`
        if (!window.confirm(`上次审核可能已经产生费用，结果尚未确认（上次预留 ${reserve}）。重新审核会发起新的付费请求，可能重复收费。是否继续？`)) return
      }
      const fp = `${b.id}@${this.revision}` + (retry ? `|retry:${retry.call_id}` : '')
      const receipt = await this.submit('review', fp, (request_id) => api.startReview(this.workId, {
        request_id, batch_id: b.id, expected_content_revision: this.revision,
        ...(retry ? { retry_unknown_call_id: retry.call_id, retry_paid_ack: true } : {}),
      }), '正在重新审核尚未有有效审核的稿件。')
      if (receipt) await this.waitForJob(receipt.job_id, job => this.settleReviewJob(job), 210000,
        () => this.reviewJobCurrent(receipt.job_id))
    },

    async cancelActive() {
      const job = this.activeJob
      if (!job) return
      try { await api.cancelContentJob(this.workId, job.job_id) } catch (e: any) { this.error = e?.message ?? String(e) }
      await this.refresh()
    },

    async saveStyle(style: Record<string, unknown>) {
      const id = this.workId
      const gen = this.generation
      try {
        await api.saveContentSettings(id, { expected_content_revision: this.revision, style_snapshot: style })
        if (gen === this.generation) { this.notice = '账号风格已保存，之前的提示与审核需要重新确认。'; await this.refresh(gen) }
      } catch (e: any) {
        if (gen === this.generation) this.error = e?.message ?? String(e)
      }
    },
  },
})
