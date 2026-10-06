<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useStudioStore, type Resolution } from '@/stores/studio'
import { api } from '@/api'
import StatusBadge from '@/components/StatusBadge.vue'
import Note from '@/components/Note.vue'
import VideoDelivery from '@/components/VideoDelivery.vue'
import FoldCard from '@/components/FoldCard.vue'
import HeroAction from '@/components/HeroAction.vue'
import WanStoryboardStudio from '@/components/WanStoryboardStudio.vue'
import type { CostNotice, GenerationRoute, ProviderRun, RouteCapability, VideoAsset } from '@/api/types'

const store = useStudioStore()
const router = useRouter()

/** 提交参数：它们参与调用清单摘要，因此必须由用户显式给出、界面可回溯。 */
const prompt = computed({ get: () => store.thPrompt, set: (value: string) => { void store.updateTalkingHeadOptions({ prompt: value }) } })
const resolution = computed({ get: () => store.thResolution, set: (value: Resolution) => { void store.updateTalkingHeadOptions({ resolution: value }) } })
const durationSeconds = computed({ get: () => store.thDurationSeconds, set: (value: number) => { void store.updateTalkingHeadOptions({ duration_seconds: value }) } })
const nativeReviewAck = reactive<Record<string, boolean>>({})
const isNativeVideo = (video: VideoAsset) => video.generation_mode === 'image_prompt_native_audio'
async function uploadReference(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) await store.uploadWanImage(file)
  input.value = ''
}
const plateVariantId = ref('')
const showRaw = ref(false)

onMounted(() => {
  void refreshPage()
})
let refreshTimer: ReturnType<typeof setInterval> | undefined
let refreshing = false
let disposed = false
async function refreshPage() {
  if (store.currentWork?.brief.content_route === 'short_video_storyboard') return
  if (!store.currentWorkId || refreshing || disposed) return
  const workId = store.currentWorkId
  const workVersion = store.workLoadVersion
  refreshing = true
  try {
    await store.loadTalkingHead(undefined, plateVariantId.value || undefined)
    if (disposed || !store.workContextMatches(workId, workVersion)) return
    await store.pollWaitingRuns()
    if (disposed || !store.workContextMatches(workId, workVersion)) return
    await store.guard('读取成片进度', () => store.refreshPostprocess())
  } finally {
    refreshing = false
    // bootstrap 可能先选中 ID，再 loadWork 清空状态并提升版本；同 ID 也需要补读。
    if (!disposed && !store.workContextMatches(workId, workVersion)) void refreshPage()
  }
}
function scrollToSection(id: string) { document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
function scrollToDelivery() { scrollToSection('delivery') }
watch(() => [store.currentWorkId, store.workLoadVersion] as const, ([id], [previousId]) => {
  if (id !== previousId) {
    store.thPrompt = ''
    store.thResolution = store.currentWork?.route === 'WAN_3_0' ? '720p' : '1080p'
    store.thDurationSeconds = 5
    for (const key of Object.keys(nativeReviewAck)) delete nativeReviewAck[key]
    plateVariantId.value = ''
    review.videoId = ''
    review.reviewAck = false
    noticeFor.value = null
  }
  void refreshPage()
})
onMounted(() => {
  refreshTimer = setInterval(async () => {
    if (store.currentWork?.brief.content_route === 'short_video_storyboard') return
    if (refreshing || store.busy) return
    // 口播与后期是独立阶段：后期 blocked 不能阻止已受理口播的结果回收。
    if (store.waitingVideoRuns.some(run => !!run.remote_job_id)) {
      await refreshPage()
      return
    }
    const state = store.postprocess?.task?.state
    if (state && ['queued', 'running', 'waiting_provider', 'retry_wait'].includes(state)) {
      refreshing = true
      try { await store.guard('更新成片进度', () => store.refreshPostprocess()) }
      finally { refreshing = false }
    }
  }, 3000)
})
onUnmounted(() => { disposed = true; clearInterval(refreshTimer) })

const routeOptions = computed(() => store.thRoutes?.routes ?? [])
const priceOf = (r: GenerationRoute) => store.thRoutes?.price_snapshots.find((p) => p.route === r) ?? null
const currentPrice = computed(() => store.currentRoute ? priceOf(store.currentRoute) : null)
const currentRoute = computed(() => store.currentRoute)

// 三条路线都由用户显式选择；路线决定素材要求、参数与费用。
const ROUTE_PLAIN: GenerationRoute = 'M2'
const ROUTE_BODY: GenerationRoute = 'OMNIHUMAN_1_5'
const ROUTE_CREATIVE: GenerationRoute = 'WAN_3_0'

/** 是否已记录过一次真实选择。与默认指向区分开：默认不是授权。 */
const routeSelected = computed(() => store.routeSelected)
const wantsBodyMotion = computed(() => store.currentRoute === ROUTE_BODY)
const wantsCreative = computed(() => store.currentRoute === ROUTE_CREATIVE)
const wantsPlain = computed(() => store.currentRoute === ROUTE_PLAIN)
const cloudRoute = computed(() => wantsBodyMotion.value || wantsCreative.value)
const bodyOption = computed(() => routeOptions.value.find((r) => r.route === ROUTE_BODY) ?? null)
const creativeOption = computed(() => routeOptions.value.find((r) => r.route === ROUTE_CREATIVE) ?? null)

function choosePlain() {
  if (routeSelected.value && store.currentRoute === ROUTE_PLAIN) return
  void store.confirmRoute(ROUTE_PLAIN)
}

function chooseBodyMotion() {
  const r = bodyOption.value
  if (!r || omniLongBlocked.value) return
  if (routeSelected.value && store.currentRoute === ROUTE_BODY) return
  // 后端硬门禁：高成本路线必须先摆出费用提示并拿到确认，否则选它会被 409 拦下。
  noticeFor.value = r
  noticeAck.value = false
}

function chooseCreative() {
  const option = creativeOption.value
  if (!option || (routeSelected.value && wantsCreative.value)) return
  noticeFor.value = option
  noticeAck.value = false
}

/** 身体动作提示词只对云路线有意义：M2 走已获准的动作参考，没有自由文本入口。 */
const promptApplies = computed(() => cloudRoute.value)

const promptLen = computed(() => prompt.value.length)
const promptLimit = computed(() => store.thPromptMaxChars)
const promptOver = computed(() => promptLen.value > promptLimit.value)

// ---------- 加入肢体动作的费用提示 ----------
// 后端对 OmniHuman 有硬门禁：没确认费用提示就选它会被 409 拦下。
// 这里的弹窗是"把提示摆到用户面前"，不是门禁本身——门禁在服务端。
const noticeFor = ref<RouteCapability | null>(null)
const noticeAck = ref(false)

const notice = computed<CostNotice | null>(() => noticeFor.value?.cost_notice ?? null)
const noticeAmountKnown = computed(() => notice.value?.amount_known === true)

async function acceptNotice() {
  const r = noticeFor.value
  if (!r || !noticeAck.value) return
  await store.confirmRoute(r.route, true)
  if (!store.routeSelected || store.currentRoute !== r.route) return
  noticeFor.value = null
  noticeAck.value = false
}

const audioSeconds = computed(() => ((store.confirmedAudio?.duration_ms ?? 0) / 1000))
const omniLongBlocked = computed(() => !!store.confirmedAudio && audioSeconds.value >= 60)
const inputError = computed(() => routeSelected.value ? store.thInputError : '')
const sourceReady = computed(() => store.thReady && (wantsCreative.value ? !!store.wanImage : !!store.confirmedAudio))
const yuan = (fen: number) => (fen / 100).toFixed(2)

/** 底片源图：默认沿用作品绑定；显式选了才覆盖。
 *  「已审」只筛掉明显不能用的，真正的判定由服务端给（见 admitVariant）。 */
const work = computed(() => store.currentWork)
const approvedVariants = computed(() => store.variants.filter((v) => v.approval_state === 'approved' && v.identity_id === work.value?.identity_id))
const platePreparation = computed(() => store.thPlate?.preparation)
const plateSource = computed(() => platePreparation.value?.source)
const plateSourceUrl = computed(() => {
  const source = plateSource.value
  if (!source) return ''
  if (source.url) return source.url
  return source.storage_uri && !/^https?:/i.test(source.storage_uri)
    ? `/api/v1/media?uri=${encodeURIComponent(source.storage_uri)}` : ''
})
const preparingPlate = computed(() => store.busy === '生成底片')
const canCreatePlate = computed(() => wantsPlain.value && !store.busy && !!store.confirmedAudio && routeSelected.value && platePreparation.value?.ok === true)
let sourceCheckVersion = 0
async function checkPlatePreparation() {
  const workId = store.currentWorkId
  const workVersion = store.workLoadVersion
  const checkVersion = ++sourceCheckVersion
  const variantId = plateVariantId.value
  if (!workId) return
  // 清空旧结论，避免切换源图到新检查返回之间仍允许提交。
  if (store.thPlate) store.thPlate.preparation = undefined
  const result = await store.guard('检查底片素材', () => api.getPlateStatus(workId, variantId || undefined))
  if (result && checkVersion === sourceCheckVersion && store.workContextMatches(workId, workVersion) && plateVariantId.value === variantId) store.thPlate = result
}
watch(plateVariantId, () => { void checkPlatePreparation() })

const estimate = computed(() => routeSelected.value ? store.thCost?.estimate ?? null : null)
const confirmedCost = computed(() => store.thConfirmedCost)
const validConfirmedCost = computed(() => store.thValidConfirmedCost)
const costChanged = computed(() => store.thCostChangedFields.join('、'))

/** 动作要求与构图要匹配：要求手势时，必须能看到手臂且能活动。 */
const gestureRequested = computed(() => /手势|比划|招手|指|挥|拿|举|点赞/.test(prompt.value))
const motionWarning = computed(() =>
  gestureRequested.value && !store.currentRouteSpec?.requires_motion_plate
    ? '你要求了手势/动作。云路线不承诺任意动作，且必须先确认人物构图里手臂与手可见、有活动空间；生成后需核肩臂、身体与手势的自然度和指令遵循程度。'
    : '',
)

// ---------- 原片人工审核表单 ----------
// 本项目没有视频理解能力：四项结论只能由人给出，且每条通过都要绑定引用。
const DIMENSIONS = [
  { key: 'lip_sync', label: '口型/音素同步' },
  { key: 'identity', label: '人物身份保持' },
  { key: 'body_motion', label: '身体动作与颈肩构图' },
  { key: 'expression', label: '眨眼/眉眼/表情自然度' },
] as const

const review = reactive({
  videoId: '',
  dimensions: {
    lip_sync: 'not_run',
    identity: 'not_run',
    body_motion: 'not_run',
    expression: 'not_run',
  } as Record<string, string>,
  refAsset: 'ref_local',
  refStart: 0,
  refEnd: 4000,
  reviewAck: false,
  trackOffset: 0,
  trackEvidence: '解码为 24kHz 单声道后取最佳偏移',
})

function openReview(v: VideoAsset) {
  review.videoId = v.id
  review.dimensions = { lip_sync: 'not_run', identity: 'not_run', body_motion: 'not_run', expression: 'not_run' }
  review.reviewAck = false
  review.refAsset = v.id
  review.refStart = 0
  review.refEnd = Math.min(v.duration_ms, 4000)
}

const allDimensionsDecided = computed(() =>
  DIMENSIONS.every((d) => ['pass', 'fail'].includes(review.dimensions[d.key])),
)
const allDimensionsPass = computed(() => DIMENSIONS.every((d) => review.dimensions[d.key] === 'pass'))

async function submitReview(outcome: 'pass' | 'fail') {
  const refs: Record<string, { asset_id: string; start_ms: number; end_ms: number }> = {}
  for (const d of DIMENSIONS) {
    // 每条结论都指向同一段"我看的那几秒"：没有引用的通过等于没有通过。
    refs[d.key] = { asset_id: review.refAsset, start_ms: review.refStart, end_ms: review.refEnd }
  }
  await store.reviewVideo(review.videoId, { ...review.dimensions }, refs, outcome, '界面人工标注')
}

async function submitTrackCheck(v: VideoAsset) {
  if (!v.audio_asset_id) {
    store.setNotice('danger', '该视频未绑定音频，无法核对音轨保持')
    return
  }
  await store.recordAudioTrackCheck(v.id, {
    audio_asset_id: v.audio_asset_id,
    measured_offset_ms: review.trackOffset,
    evidence: review.trackEvidence,
  })
}

function gateOf(id: string) {
  return store.videoGates[id]?.gate ?? null
}
function reviewStatus(value?: string) {
  const labels: Record<string, string> = {
    not_run: '未检查', pending: '未检查', pass: '通过', approved: '通过',
    fail: '未通过', rejected: '未通过', review: '待复核',
  }
  return labels[value ?? 'not_run'] ?? '待复核'
}
const videos = computed(() => store.videos)
const originals = computed(() => videos.value.filter((v) => v.kind === 'original'))
const plates = computed(() => videos.value.filter((v) => v.kind === 'plate'))
const approvedPlate = computed(() => store.thPlate?.approved === true)

/** 4 个 Tab：设置（路线 / 底片 / 费用）、原片、成片、诊断。
 * 一屏只做一件事，避免单页塞 8 个区块。 */
type VideoTab = 'setup' | 'originals' | 'delivery' | 'diagnostics'
const currentTab = ref<VideoTab>('setup')

/** sticky 底栏状态：主按钮是否可点。
 *
 * 只挡"在飞"的运行（queued/running/waiting_provider…），不挡 `unknown`。
 * 旧口径用的是 unresolvedVideoRuns（含 unknown），一旦有一次查询没拿到结论，
 * 主按钮就永久变灰：用户既不能重提、也没有任何补救入口，只能干瞪眼。
 * unknown 是"还不知道"，不是"不能动"——它的出路是对账或显式重新生成，
 * 那两条路由下面单独开，因此这里必须放行。
 */
const canSubmit = computed(() =>
  sourceReady.value
  && !store.busy
  && validConfirmedCost.value
  && !inputError.value
  && !store.thSubmissionError
  && !store.waitingVideoRuns.length
  && !(wantsCreative.value && store.unresolvedVideoRuns.some(run => run.state === 'unknown'))
  && (!cloudRoute.value || !!store.thCost?.billable_allowed),
)

// ---------- 失败/未知运行的出路：对账 or 显式重新生成 ----------
// 后端 resubmit_guard 只认两种东西：一条有证据的对账结论，或用户显式授权的重提。
// 界面因此只开这两个口，不做任何自动重试。
const blockedRuns = computed(() => store.thBlockedRuns)

/** 对账：只对 unknown 运行开放。改判必须留下"凭什么这么判"。 */
const settleFor = ref<ProviderRun | null>(null)
const settleForm = reactive({ outcome: 'failed' as 'succeeded' | 'failed' | 'cancelled', note: '' })
const settleNoteOk = computed(() => settleForm.note.trim().length >= 4)

/** 后端只接受这三种终态；`unknown` 不是结论，不能写回去。 */
const SETTLE_OUTCOMES: { value: 'succeeded' | 'failed' | 'cancelled'; label: string; hint: string }[] = [
  { value: 'succeeded', label: '已成功', hint: '供应商那边确实产出了视频，只是我们没取回来。' },
  { value: 'failed', label: '已失败', hint: '供应商明确报了错、没产出成片。' },
  { value: 'cancelled', label: '已取消', hint: '任务被取消或超时作废，不会再有结果。' },
]

function openSettle(run: ProviderRun) {
  settleFor.value = run
  settleForm.outcome = 'failed'
  settleForm.note = ''
}

async function confirmSettle() {
  const target = settleFor.value
  if (!target || !settleNoteOk.value) return
  settleFor.value = null
  await store.settleRun(target.id, settleForm.outcome, settleForm.note.trim())
}

/** 重新生成：同一调用清单此前失败/未知过，后端要显式授权才换幂等键重提。 */
const regenOpen = ref(false)

function onPrimarySubmit() {
  if (!canSubmit.value) return
  // 有旧失败/未知记录时不能直接发：先让用户看清"会再次计费"，拿到授权再走。
  if (blockedRuns.value.length) { regenOpen.value = true; return }
  void store.submitTalkingHead(false)
}

async function confirmRegen() {
  regenOpen.value = false
  await store.submitTalkingHead(false, { replacementAuthorized: true })
}

/** Hero：当前该做什么。跟随 Tab 变化。 */
const hero = computed(() => {
  if (routeSelected.value && !wantsCreative.value && !store.confirmedAudio) {
    return {
      status: 'block' as const,
      title: '先确认音频',
      desc: '到语音页试听并确认全文音频，再回来合成视频。',
      ctaText: '去试听与确认',
      ctaDisabled: false,
      ctaLoading: false,
    }
  }
  if (currentTab.value === 'setup') {
    if (!routeSelected.value) {
      return {
        status: 'todo' as const,
        title: '选生成路线',
        desc: '选择 M2 低成本口播、OmniHuman 数字人口播，或 Wan 3.0 创意短视频。',
        ctaText: undefined,
        ctaDisabled: false,
        ctaLoading: false,
      }
    }
    if (inputError.value) {
      return { status: 'block' as const, title: wantsCreative.value ? '完善 Wan 3.0 输入' : '检查生成参数',
        desc: inputError.value, ctaText: undefined, ctaDisabled: true, ctaLoading: false }
    }
    if (!sourceReady.value) {
      return {
        status: 'todo' as const,
        title: '完成准备',
        desc: wantsCreative.value ? '上传一张参考图片，或使用作品已审核图片。' : wantsPlain.value ? '展开下方“准备动作底片”，完成素材检查。' : '确认作品的人物图片与完整原音频，查看下方未满足条件。',
        ctaText: undefined,
        ctaDisabled: false,
        ctaLoading: false,
      }
    }
    if (wantsBodyMotion.value && omniLongBlocked.value) {
      return {
        status: 'block' as const,
        title: 'OmniHuman 不支持 60 秒及以上音频',
        desc: '请回到语音配音页缩短并重新确认最终音频后再使用 OmniHuman。系统不会自动切分或提交长音频。',
        ctaText: undefined,
        ctaDisabled: true,
        ctaLoading: false,
      }
    }
    if (!validConfirmedCost.value) {
      return {
        status: 'todo' as const,
        title: '确认费用',
        desc: '查看当前估算，点"确认调用清单与金额"。',
        ctaText: '确认调用清单与金额',
        ctaDisabled: !!store.busy || !!inputError.value || !estimate.value,
        ctaLoading: store.busy === '确认费用',
      }
    }
    if (store.thSubmissionError) {
      return { status: 'block' as const, title: 'Wan 服务配置待完成', desc: store.thSubmissionError,
        ctaText: undefined, ctaDisabled: true, ctaLoading: false }
    }
    if (blockedRuns.value.length) {
      if (wantsCreative.value && store.unresolvedVideoRuns.some(run => run.state === 'unknown')) {
        return { status: 'block' as const, title: '查询原任务', desc: 'Wan 任务结果未知，请查询原任务或核对供应商记录；不自动重提或更换路线。',
          ctaText: undefined, ctaDisabled: true, ctaLoading: false }
      }
      return {
        status: 'block' as const,
        title: '上次生成没拿到结果',
        desc: `${blockedRuns.value.length} 条运行结果未知或失败：先对账，或确认重新生成（会再次计费）。`,
        ctaText: '重新生成（真实调用，会再次计费）',
        ctaDisabled: !canSubmit.value,
        ctaLoading: !!store.busy,
      }
    }
    return {
      status: 'todo' as const,
      title: wantsCreative.value ? '生成创意短视频' : '生成口播',
      desc: estimate.value ? `本次 API 费用 ¥${yuan(estimate.value.estimated_fen)} · 上限 ¥${yuan(estimate.value.ceiling_fen)}` : '等待服务端报价。',
      ctaText: wantsCreative.value ? '生成创意短视频（真实调用）' : '生成口播（真实调用）',
      ctaDisabled: !canSubmit.value,
      ctaLoading: !!store.busy,
    }
  }
  if (currentTab.value === 'originals') {
    if (store.thGate?.original_confirmed) {
      return {
        status: 'done' as const,
        title: '原片已确认',
        desc: store.finalVideo?.generation_mode === 'image_prompt_native_audio' ? '原声视频已确认，可前往成片页下载。' : '去制作带字幕的成片。',
        ctaText: store.finalVideo?.generation_mode === 'image_prompt_native_audio' ? '查看与下载视频' : '去制作成片',
        ctaDisabled: false,
        ctaLoading: false,
      }
    }
    if (originals.value.length) {
      return {
        status: 'todo' as const,
        title: `检查 ${originals.value.length} 条原片`,
        desc: originals.value.some(isNativeVideo) ? '观看原片并确认画面、台词和声音，保留原声生成下载包。' : '观看后登记画面与音轨检查，确认一条为最终原片。',
        ctaText: undefined,
        ctaDisabled: false,
        ctaLoading: false,
      }
    }
    return {
      status: 'todo' as const,
      title: '等待生成',
      desc: '生成完成后，原片会显示在这里。',
      ctaText: undefined,
      ctaDisabled: false,
      ctaLoading: false,
    }
  }
  if (currentTab.value === 'delivery') {
    if (store.deliveryReady) {
      return {
        status: 'done' as const,
        title: '成片已就绪',
        desc: wantsCreative.value ? '可预览并下载保留原声的视频，当前不含自动字幕。' : '可预览并下载带字幕的成片。',
        ctaText: undefined,
        ctaDisabled: false,
        ctaLoading: false,
      }
    }
    return {
      status: 'todo' as const,
      title: '制作成片',
      desc: wantsCreative.value ? '观看原片并确认画面与声音后，保留原声生成下载包。' : '确认原片后，制作带字幕的成片。',
      ctaText: undefined,
      ctaDisabled: false,
      ctaLoading: false,
    }
  }
  return {
    status: 'todo' as const,
    title: '诊断与排查',
    desc: '适配器、队列、提交记录与原始数据。仅排查时打开。',
    ctaText: undefined,
    ctaDisabled: false,
    ctaLoading: false,
  }
})

function onHeroCta() {
  if (routeSelected.value && !wantsCreative.value && !store.confirmedAudio) { router.push('/tts'); return }
  if (currentTab.value === 'setup') {
    if (!routeSelected.value || inputError.value) return
    if (!validConfirmedCost.value) void store.confirmTalkingHeadCost(currentRoute.value)
    else onPrimarySubmit()
    return
  }
  if (currentTab.value === 'originals' && store.thGate?.original_confirmed) currentTab.value = 'delivery'
}

function blockerLink(code: string) {
  if (/PLATE/.test(code)) return null
  if (/IDENTITY|VARIANT|MOTION/.test(code)) return { to: '/avatar', label: '前往人物设置' }
  if (/AUDIO|VOICE|PLAN|VERSION/.test(code)) return { to: '/tts', label: '前往语音确认' }
  if (/SCRIPT/.test(code)) return { to: '/script', label: '前往选择稿件' }
  return null
}
</script>

<template>
  <WanStoryboardStudio v-if="store.currentWork?.brief.content_route === 'short_video_storyboard'" />
  <div v-else class="video-workbench">
    <HeroAction
      :status="hero.status"
      :title="hero.title"
      :desc="hero.desc"
      :cta-text="hero.ctaText"
      :cta-disabled="hero.ctaDisabled"
      :cta-loading="hero.ctaLoading"
      @cta="onHeroCta"
    />

    <!-- 4 Tab 导航（替代原 page-head + video-nav） -->
    <nav class="video-tabs" aria-label="视频制作">
      <button :class="{ active: currentTab === 'setup' }" @click="currentTab = 'setup'">设置</button>
      <button :class="{ active: currentTab === 'originals' }" @click="currentTab = 'originals'">
        原片<span v-if="originals.length" class="tab-badge">{{ originals.length }}</span>
      </button>
      <button :class="{ active: currentTab === 'delivery' }" @click="currentTab = 'delivery'">
        成片<span v-if="store.deliveryReady" class="tab-dot-ok" aria-hidden="true"></span>
      </button>
      <button :class="{ active: currentTab === 'diagnostics' }" @click="currentTab = 'diagnostics'">诊断</button>
      <div class="tab-spacer"></div>
      <button class="ghost sm" :disabled="!!store.busy" @click="refreshPage">刷新状态</button>
    </nav>

    <!-- ========== Tab 1：设置 ========== -->
    <section v-show="currentTab === 'setup'" class="tab-pane">
      <div class="generation-layout">
        <div class="generation-form">
          <div class="field-heading">生成方式</div>
          <div class="route-choices">
            <button class="route-choice" :class="{ selected: routeSelected && wantsPlain }" :aria-pressed="routeSelected && wantsPlain" :disabled="!!store.busy" @click="choosePlain()"><span class="route-choice-top"><strong>M2 · 低成本口播</strong></span><p>沿用已选动作参考，先生成动作底片，再进行面部表情与对口型。</p><small>不按视频秒数收 API 费；GPU 租用或硬件成本另计。</small></button>
            <button class="route-choice" :class="{ selected: routeSelected && wantsBodyMotion }" :aria-pressed="routeSelected && wantsBodyMotion" :disabled="!!store.busy || omniLongBlocked || !bodyOption" @click="chooseBodyMotion()"><span class="route-choice-top"><strong>OmniHuman · 数字人口播</strong></span><p>根据人物图片与语音生成头肩和身体动作，适合数字人讲述。</p><small>{{ omniLongBlocked ? '当前最终音频达到 60 秒，OmniHuman 仅支持小于 60 秒。' : (bodyOption?.cost_notice?.unit_text || '按生成视频秒数计费') }}</small></button>
            <button class="route-choice" :class="{ selected: routeSelected && wantsCreative }" :aria-pressed="routeSelected && wantsCreative" :disabled="!!store.busy || !creativeOption" @click="chooseCreative()"><span class="route-choice-top"><strong>Wan 3.0 · 创意短视频</strong></span><p>使用一张参考图片和提示词，生成 2–30 秒有声短片。声音由模型生成。</p><small>{{ creativeOption?.cost_summary || '报价按清晰度与生成时长计算，选择前确认费用。' }}</small></button>
          </div>
          <p v-if="omniLongBlocked" class="error-text" role="alert">OmniHuman 仅支持小于 60 秒的最终音频。请缩短并重新确认音频；不会自动分段。</p>
          <p v-if="wantsCreative" class="setup-hint">描述场景、动作、台词和环境音，由模型同时生成画面与声音。默认 5 秒、720p、9:16。图片用于参考生成，非严格首帧；生成后请观看并确认效果。</p>
          <p v-if="wantsCreative && store.thSubmissionError" class="error-text" role="status">{{ store.thSubmissionError }} 已受理的历史任务仍可按原任务号查询。</p>

          <div v-if="wantsCreative" class="wan-image-input">
            <label class="field"><span>上传参考图片</span><input type="file" accept="image/png,image/jpeg,image/webp" :disabled="!!store.busy" @change="uploadReference" /></label>
            <p class="setup-hint">可使用人物、动物、物品或场景图片。未单独上传时，使用作品已审核图片。</p>
            <figure v-if="store.wanImage" class="wan-image-preview">
              <img v-if="store.wanImage.preview_url" :src="store.wanImage.preview_url" alt="Wan 参考图片预览" />
              <figcaption>{{ store.wanImage.label }} · {{ store.wanImage.width }} × {{ store.wanImage.height }}</figcaption>
            </figure>
            <p v-else class="setup-hint">尚未选择参考图片。</p>
          </div>

          <!-- readiness 内联：贴在路线卡下方，一行内交代 -->
          <div v-if="routeSelected && (wantsCreative || store.confirmedAudio)" class="readiness-inline">
            <template v-if="sourceReady">
              <span class="dot-ok" aria-hidden="true"></span>
              <span>素材已就绪，可生成。</span>
            </template>
            <template v-else-if="store.thBlockers.length">
              <span class="dot-warn" aria-hidden="true"></span>
              <ul class="blocker-list inline">
                <li v-for="b in store.thBlockers" :key="b.code">
                  <span>{{ b.message }}</span>
                  <router-link v-if="blockerLink(b.code)" :to="blockerLink(b.code)!.to" class="action-link">{{ blockerLink(b.code)!.label }}</router-link>
                  <button v-else-if="b.code.includes('PLATE')" class="ghost sm" @click="checkPlatePreparation">检查底片素材</button>
                </li>
              </ul>
            </template>
            <template v-else>
              <span class="faint">正在读取生成条件。</span>
            </template>
          </div>

          <!-- 提交状态在“诊断”页展示；长音频不会进入分段续发流程。 -->
          <p v-if="!routeSelected" class="setup-hint">点击上方一种方式，保存这次作品的选择。</p>

          <!-- 动作底片：M2 必经步骤，紧跟路线选择，不再沉在页面下方 -->
          <FoldCard
            v-if="wantsPlain"
            title="准备动作底片"
            :hint="approvedPlate ? '已有审核通过的底片' : 'M2 生成前需要完成'"
            :badge="approvedPlate ? '已就绪' : undefined"
            :tone="approvedPlate ? 'default' : 'warn'"
            :open="routeSelected && !approvedPlate"
          >
            <div class="section-head">
              <span class="section-title">动作底片</span>
              <span class="section-note">先让人物面部动起来，再与语音对口型</span>
            </div>
            <div class="panel">
              <div class="panel-pad">
                <div class="grid-2" style="gap: 10px">
                  <label class="field">
                    <span>用于口播的人物画面</span>
                    <select v-model="plateVariantId" :disabled="!!store.busy">
                      <option value="">{{ work?.variant_id ? '沿用作品已选画面' : `当前人物定妆原图（${store.currentIdentity?.name || '尚未选择人物'}）` }}</option>
                      <option v-for="v in approvedVariants" :key="v.id" :value="v.id">
                        {{ v.preview_label || v.id }} · {{ v.approval_state }}
                      </option>
                    </select>
                  </label>
                  <div class="field">
                    <span>底片素材检查</span>
                    <div class="muted" style="font-size: 12.5px; padding-top: 6px">
                      <template v-if="platePreparation">
                        <StatusBadge :text="platePreparation.ok ? 'pass' : 'fail'" />
                        <span style="margin-left: 6px">{{ platePreparation.ok ? '人物画面、动作参考与全文音频已就绪' : '请先完成下方条件' }}</span>
                      </template>
                      <template v-else>点击「检查底片素材」查看能否生成</template>
                    </div>
                  </div>
                </div>
                <div v-if="plateSource" class="plate-source">
                  <img v-if="plateSourceUrl" :src="plateSourceUrl" alt="底片源图预览" />
                  <p>{{ plateSource.label }}<span v-if="plateSource.kind === 'hero'"> · 直接沿用已审核原图，无需先换服装或背景。</span></p>
                </div>
                <ul v-if="platePreparation?.blockers.length" class="blocker-list"><li v-for="b in platePreparation.blockers" :key="b.code">{{ b.message }} <router-link v-if="blockerLink(b.code)" :to="blockerLink(b.code)!.to" class="action-link">{{ blockerLink(b.code)!.label }}</router-link></li></ul>
                <div class="btn-row" style="margin-top: 10px">
                  <button class="sm" :disabled="!canCreatePlate" @click="store.createPlate(audioSeconds, plateVariantId || undefined)">
                    {{ preparingPlate ? '正在生成动作底片…' : store.confirmedAudio ? `生成底片（${audioSeconds.toFixed(1)}s）` : '先确认音频，再生成底片' }}
                  </button>
                  <button
                    class="sm"
                    :disabled="store.busy !== '' || !work?.identity_id"
                    @click="checkPlatePreparation"
                  >
                    检查底片素材
                  </button>
                </div>
                <div v-if="preparingPlate" class="plate-progress" role="status"><progress aria-label="动作底片生成进度"></progress><span>算力云正在生成动作底片，请勿重复提交。完成后会显示预览。</span></div>
                <p class="setup-hint">沿用已审核的定妆原图，或选用本人物的已审变体。生成后观看底片，再确认人物是否保持一致。缺少画面时，<router-link to="/avatar" class="action-link">前往人物设置</router-link>。</p>
                <div class="faint" style="font-size: 12.5px; margin-top: 6px">
                  底片长度需覆盖全文音频。确认人物无变化后，继续下方费用确认与生成。
                </div>
              </div>
              <template v-if="plates.length">
                <div class="divider"></div>
                <div class="stage-list">
                  <div v-for="p in plates" :key="p.id" class="stage-row">
                    <video v-if="p.url && !p.is_mock" :src="p.url" controls preload="metadata" style="width: 180px; max-height: 220px" aria-label="动作底片预览"></video>
                    <StatusBadge :text="p.approval_state" />
                    <StatusBadge v-if="p.is_mock" text="mock" />
                    <span class="mono faint">{{ p.id.slice(-8) }}</span>
                    <span style="flex: 1">{{ (p.duration_ms / 1000).toFixed(2) }}s</span>
                    <button class="ghost sm" :disabled="store.busy !== ''" @click="store.reviewPlate(p.id, 'pass', '界面人工确认：身份未漂移')">
                      身份未漂移
                    </button>
                    <button class="danger sm" :disabled="store.busy !== ''" @click="store.reviewPlate(p.id, 'fail', '界面人工判定：身份漂移')">
                      身份漂移
                    </button>
                  </div>
                </div>
              </template>
              <div v-else class="empty">尚无底片。</div>
            </div>
          </FoldCard>

          <div class="video-settings">
            <label v-if="cloudRoute" class="field"><span>生成清晰度</span><select v-model="resolution"><option v-if="wantsCreative" value="480p">480p · 较低清晰度</option><option value="720p">720p · 较低资源消耗</option><option value="1080p">1080p · 更高清晰度</option></select></label>
            <label v-if="wantsCreative" class="field"><span>生成时长（秒）</span><input v-model.number="durationSeconds" type="number" min="2" max="30" step="1" /><small>2–30 秒整数 · 单次生成 · 9:16 竖屏</small></label><div v-else class="duration-info"><span class="faint">完整原音频时长</span><strong>{{ store.confirmedAudio ? `${audioSeconds.toFixed(1)} 秒` : '待确认音频' }}</strong><span class="faint">{{ !store.confirmedAudio ? '确认全文音频后显示时长' : wantsBodyMotion && omniLongBlocked ? 'OmniHuman 不支持长音频' : '按全文音频生成' }}</span></div>
          </div>
          <label v-if="promptApplies" class="field motion-field"><span>{{ wantsCreative ? '画面与声音提示词（必填）' : '你希望人物怎样动？' }} <small>{{ promptLen }} / {{ promptLimit }}</small></span><textarea v-model="prompt" rows="3" :required="wantsCreative" :placeholder="wantsCreative ? '描述场景、动作、台词和环境音，例如：小猫走进花园，轻声说“早上好”，伴随鸟鸣和风声' : '例如：自然讲述，少量点头，偶尔用手势强调重点'"></textarea><small>模型不保证准确执行每个动作，生成后需要检查。</small><small v-if="promptOver" class="error-text">超过 {{ promptLimit }} 字，请缩短描述。</small><small v-if="motionWarning">{{ motionWarning }}</small></label>
          <p v-if="inputError" class="error-text" role="alert">{{ inputError }}</p>
          <details class="advanced" v-if="store.currentRouteSpec">
            <summary>当前路线的能力与限制</summary>
            <p>{{ store.currentRouteSpec.label }} · {{ store.currentRouteSpec.cost_summary }}</p>
            <p class="faint">{{ store.currentRouteSpec.body_motion.scope }}</p>
            <p class="faint">实测状态：{{ store.currentRouteSpec.verified_state }}；待核验：{{ store.currentRouteSpec.open_questions.join('、') || '无' }}</p>
          </details>
        </div>
      </div>

      <!-- 费用与提交 -->
      <div v-if="routeSelected" class="generation-submit">
        <div class="field-heading">费用与提交</div>
        <p v-if="store.thCost?.blocker" class="error-text" role="alert">{{ store.thCost.blocker.message }}</p>
        <template v-if="estimate">
          <div class="row">
            <div>
              <div class="sub">{{ cloudRoute ? '本次生成费用估算' : '本次 API 费用（另计 GPU 成本）' }}</div>
              <span class="num" style="font-size: 19px; font-weight: 600">¥{{ yuan(estimate.estimated_fen) }}</span>
              <span class="faint num" style="font-size: 13px; margin-left: 8px">
                上限 ¥{{ yuan(estimate.ceiling_fen) }}
              </span>
              <StatusBadge
                v-if="currentPrice"
                :text="currentPrice.billing_verified ? 'verified' : 'unverified'"
                style="margin-left: 8px"
              />
            </div>
            <div class="topbar-spacer"></div>
            <div class="btn-row">
              <button class="primary" :disabled="store.busy !== '' || !!inputError || !(wantsCreative ? store.wanImage : store.confirmedAudio)" @click="store.confirmTalkingHeadCost(currentRoute)">
                确认调用清单与金额
              </button>
            </div>
          </div>
          <p v-if="wantsPlain" class="setup-hint">M2 的 GPU 租用或硬件成本不包含在上述 API 金额内。</p>
          <div v-if="currentPrice" class="faint" style="font-size: 12.5px; margin-top: 4px">
            {{ currentPrice.rounding_note }}；{{ currentPrice.minimum_note }}；{{ currentPrice.failure_billing_note }}
          </div>
          <details class="advanced"><summary>查看估价依据与实验说明</summary><p class="faint">{{ estimate.price_basis }}</p><p class="faint">{{ estimate.note }}</p></details>
        </template>
        <p v-else class="setup-hint">{{ inputError || '正在等待当前参数的服务端报价；报价未取得前不能确认费用。' }}</p>

        <div v-if="confirmedCost" class="divider" style="margin: 18px 0"></div>
        <template v-if="confirmedCost">
          <div class="sub">{{ validConfirmedCost ? '已确认的调用清单' : '历史确认（当前输入已变化）' }}</div>
          <p v-if="!validConfirmedCost" class="error-text" role="status">{{ costChanged || '调用清单' }}已变化，请重新确认本次调用清单与费用。</p>
          <div class="faint" style="font-size: 12.5px; line-height: 1.8">
            调用次数 {{ confirmedCost.segment_count }} · 分辨率 {{ confirmedCost.resolution }} ·
            估价 ¥{{ yuan(confirmedCost.estimated_fen) }} / 上限 ¥{{ yuan(confirmedCost.ceiling_fen) }}<br />

            {{ confirmedCost.expiry_note ?? '清单变化即失效' }}<br />
            付费授权：<b>{{ confirmedCost.authorizes_payment ? '已授权' : '未授权' }}</b>
            · 实际提交仍需满足预算设置。
          </div>
          <Note v-if="store.thCost && !store.thCost.billable_allowed" kind="warn" label="预算未授权">
            当前预算（单任务 {{ yuan(store.thCost.budget.per_task_limit_fen) }} 元、
            可计费提交 {{ store.thCost.budget.max_billable_submissions }} 次）不允许新增计费任务。
            报价确认不会放宽预算；超额须显式调整预算，而不是重复确认。
          </Note>
        </template>
      </div>
    </section>

    <!-- ========== Tab 2：原片 ========== -->
    <section v-show="currentTab === 'originals'" class="tab-pane">
      <header class="tab-pane-head">
        <h2>检查视频原片</h2>
        <span class="faint">
          {{ store.thGate?.original_confirmed ? '已确认原片' : originals.length ? `${originals.length} 条待检查` : '等待生成' }}
        </span>
      </header>
      <div class="panel">
        <div v-if="store.thGate" class="panel-pad faint" style="font-size: 12.5px">
          {{ store.thGate.original_confirmed ? (store.finalVideo?.generation_mode === 'image_prompt_native_audio' ? '原声视频已确认，可前往成片页预览与下载。' : '已选定一条原片，可继续制作字幕与成片。') : store.thGate.original_count ? '请选择一条原片观看，并完成下方检查。' : '还没有可审核的视频原片。' }}
          <details class="advanced"><summary>原片检查明细</summary><p>{{ store.thGate.note }}</p><p>候选原片 {{ store.thGate.original_count }} 条</p></details>
        </div>

        <template v-for="v in originals" :key="v.id">
          <div class="divider"></div>
          <div class="panel-pad">
            <video v-if="v.url && !v.is_mock" :src="v.url" controls preload="metadata" class="media-preview" :aria-label="`口播原片 ${v.id}`"></video>
            <p v-else class="faint">当前原片没有可播放地址，请先检查文件是否可用，不能据此完成观看确认。</p>
            <div class="row">
              <span class="mono">{{ v.id.slice(-8) }}</span>
              <StatusBadge :text="v.approval_state" />
              <StatusBadge v-if="v.is_mock" text="mock" />
              <StatusBadge v-if="v.confirmed" text="approved" />
              <div class="topbar-spacer"></div>
              <span class="faint num" style="font-size: 12.5px">
                {{ (v.duration_ms / 1000).toFixed(2) }}s<span v-if="v.width && v.height"> · {{ v.width }}×{{ v.height }}</span><span v-if="v.fps"> · {{ v.fps }}fps</span>
              </span>
            </div>

            <div v-if="v.hard_failures.length" style="margin-top: 10px">
              <Note v-for="f in v.hard_failures" :key="f.code" kind="danger" :label="f.code">
                {{ f.message }}
              </Note>
            </div>

            <template v-if="!isNativeVideo(v) && gateOf(v.id)">
              <div class="row" style="margin-top: 12px">
                <span class="sub" style="margin: 0">检查结果</span>
                <StatusBadge :text="gateOf(v.id)?.outcome ?? 'not_run'" />
                <span class="faint" style="font-size: 12.5px">
                  画面审核：{{ reviewStatus(gateOf(v.id)?.review_state) }} · 音轨检查：{{ reviewStatus(gateOf(v.id)?.audio_track_state) }}
                </span>
              </div>
              <div
                v-if="(gateOf(v.id)?.reasons ?? []).length"
                class="faint"
                style="font-size: 12.5px; margin-top: 6px; line-height: 1.7"
              >
                未通过原因：{{ (gateOf(v.id)?.reasons ?? []).join('；') }}
              </div>
            </template>

            <div v-if="isNativeVideo(v)" class="review-box">
              <p class="setup-hint">保留模型生成的画面与原声，不含自动字幕。</p>
              <label class="ack-label"><input v-model="nativeReviewAck[v.id]" type="checkbox" :disabled="v.confirmed" />我已观看这条视频，确认画面、台词和声音符合预期</label>
              <div class="btn-row"><button class="primary" :disabled="!!store.busy || !nativeReviewAck[v.id] || !v.url || v.is_mock || v.confirmed || !!v.hard_failures.length" @click="store.confirmWanNativeVideo(v.id, !!nativeReviewAck[v.id])">{{ v.confirmed ? '已确认原声视频' : '保留原声并生成下载包' }}</button></div>
            </div>
            <template v-else-if="review.videoId !== v.id">
              <div class="btn-row" style="margin-top: 12px">
                <button class="primary sm" :disabled="store.busy !== '' || !v.url || v.is_mock" @click="openReview(v)">观看后审核这条原片</button>
                <button class="sm" :disabled="store.busy !== ''" @click="submitTrackCheck(v)">
                  核对音轨保持（偏移 {{ review.trackOffset }}ms）
                </button>
              </div>
            </template>

            <div v-else class="review-box">
              <div class="sub">逐项检查：根据你观看的实际画面选择结果</div>
              <div v-for="d in DIMENSIONS" :key="d.key" class="check-row">
                <span class="check-label">{{ d.label }}</span>
                <select v-model="review.dimensions[d.key]" style="width: 130px">
                  <option value="not_run">未执行</option>
                  <option value="pass">通过</option>
                  <option value="fail">未通过</option>
                </select>
              </div>
              <div class="grid-2" style="margin-top: 12px">
                <label>
                  <div class="sub">检查的视频编号</div>
                  <input v-model="review.refAsset" style="width: 100%" />
                </label>
                <label>
                  <div class="sub">检查区间（毫秒）</div>
                  <div class="row">
                    <input v-model.number="review.refStart" type="number" style="width: 48%" />
                    <input v-model.number="review.refEnd" type="number" style="width: 48%" />
                  </div>
                </label>
              </div>
              <div class="faint" style="font-size: 12.5px; margin-top: 8px">
                记录你检查的视频区间，便于回看。口型是否自然需要观看画面判断。
              </div>
              <div class="btn-row" style="margin-top: 12px">
                <button
                  class="primary"
                  :disabled="store.busy !== '' || !allDimensionsDecided"
                  @click="submitReview(allDimensionsPass ? 'pass' : 'fail')"
                >
                  登记审核结论
                </button>
                <button class="ghost sm" @click="review.videoId = ''">收起</button>
              </div>
              <div class="divider" style="margin: 16px 0"></div>
              <div class="btn-row"><button :disabled="store.busy !== ''" @click="submitTrackCheck(v)">核对音轨保持（偏移 {{ review.trackOffset }}ms）</button><span class="faint">音轨核对通过后，才能确认最终原片。</span></div>
              <div class="sub" style="margin-top: 16px">确认最终原片</div>
              <label class="faint" style="font-size: 13px; display: flex; align-items: center; gap: 8px">
                <input v-model="review.reviewAck" type="checkbox" />
                我已逐帧/慢放观看这条原片，确认口型、身份、表情与穿帮情况
              </label>
              <div class="btn-row" style="margin-top: 10px">
                <button
                  class="primary"
                  :disabled="store.busy !== '' || !review.reviewAck || !v.url || v.is_mock || gateOf(v.id)?.outcome !== 'pass'"
                  @click="store.confirmFinalVideo(v.id, review.reviewAck, '界面确认')"
                >
                  确认这条为最终原片
                </button>
                <span class="faint" style="font-size: 12.5px">
                  底片与占位产物不可确认；确认会替换此前已确认的原片
                </span>
              </div>
            </div>
          </div>
        </template>

        <div v-if="!originals.length" class="empty">
          生成完成后，口播原片会显示在这里。先完成上方的生成设置。
        </div>
      </div>
    </section>

    <!-- ========== Tab 3：成片 ========== -->
    <section v-show="currentTab === 'delivery'" class="tab-pane">
      <header class="tab-pane-head">
        <h2>成片与下载</h2>
        <span class="faint">{{ store.deliveryReady ? '成片已就绪' : '等待确认原片后生成' }}</span>
      </header>
      <VideoDelivery />
    </section>

    <!-- ========== Tab 4：诊断 ========== -->
    <section v-show="currentTab === 'diagnostics'" class="tab-pane">
      <header class="tab-pane-head">
        <h2>诊断与排查</h2>
        <span class="faint">仅排查时使用</span>
      </header>

      <!-- 适配器状态 -->
      <details class="advanced">
        <summary>路线能力与适配器</summary>
        <div class="section-head">
          <span class="section-title">适配器状态</span>
          <span class="section-note">声明口径 ≠ 本项目实测</span>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span style="font-weight: 500; font-size: 14.5px">适配器状态</span>
            <div class="topbar-spacer"></div>
            <span v-if="store.thAdapters" class="faint" style="font-size: 12.5px">
              凭据存在 ≠ 可调用
            </span>
          </div>
          <div v-if="store.thAdapters" class="panel-pad">
            <div v-for="(a, key) in store.thAdapters" :key="key" class="check-row">
              <StatusBadge :text="a.state" />
              <span class="check-label">{{ a.provider }} · {{ a.model }}</span>
              <span class="check-hint">{{ a.detail }}</span>
            </div>
          </div>
          <div v-else class="empty">尚未取得适配器状态。</div>
        </div>
      </details>

      <!-- 队列 -->
      <details class="advanced">
        <summary>运行队列</summary>
        <div class="section-head">
          <span class="section-title">重型作业队列</span>
          <div class="topbar-spacer"></div>
          <StatusBadge :text="store.thQueue?.serialized ? 'pass' : 'fail'" />
        </div>
        <div class="panel">
          <div v-if="store.thQueue" class="panel-pad">
            <div class="row">
              <span v-for="(n, k) in store.thQueue.counts" :key="k" class="faint num" style="font-size: 12.5px">
                {{ k }}: <b>{{ n }}</b>
              </span>
            </div>
            <div class="faint" style="font-size: 12.5px; margin-top: 8px">{{ store.thQueue.note }}</div>
            <div class="faint" style="font-size: 12.5px; margin-top: 4px">
              GPU 忙只是排队，不判失败；排队期不占用租约。
            </div>
          </div>
          <div v-else class="empty">尚未取得队列状态。</div>
        </div>
      </details>

      <!-- 提交记录 -->
      <details class="advanced">
        <summary>提交记录{{ store.thSubmissions?.runs.length ? ` · ${store.thSubmissions.runs.length} 条` : '' }}</summary>
        <template v-if="store.thSubmissions?.runs.length">
          <div class="divider"></div>
          <div class="stage-list">
            <div v-for="r in store.thSubmissions.runs" :key="r.id" class="stage-row">
              <StatusBadge :text="r.state" />
              <span class="mono faint">{{ r.id.slice(-8) }}</span>
              <span style="flex: 1">{{ r.model }}</span>
              <StatusBadge v-if="r.is_mock" text="mock" />
              <button
                v-if="!r.is_mock && r.remote_job_id && ['unknown', 'queued', 'leased', 'submitted', 'running', 'waiting_provider'].includes(r.state)"
                class="ghost sm"
                :disabled="store.busy !== ''"
                @click="store.pollWaitingRuns(r.id)"
              >
                查询原任务
              </button>
              <!-- 查询到底也拿不到结论时，只能由人来判：这里是对账的唯一入口。 -->
              <button
                v-if="!r.is_mock && r.state === 'unknown'"
                class="ghost sm"
                :disabled="store.busy !== ''"
                @click="openSettle(r)"
              >
                对账
              </button>
              <span v-if="!r.is_mock && r.state === 'unknown' && !r.remote_job_id" class="faint">未取得远端任务号，需要核对供应商记录后再处理。</span>
            </div>
          </div>
        </template>
        <div v-else class="empty">尚无提交记录。</div>
        <div v-if="store.thSubmissions" class="panel-pad faint" style="font-size: 12.5px">
          {{ store.thSubmissions.note }}
        </div>
      </details>

      <!-- 原始 JSON -->
      <details class="advanced">
        <summary @click="showRaw = true">原始诊断数据</summary>
        <div class="section-head">
          <span class="section-title">原始视图</span>
          <span class="section-note">排查字段口径用</span>
          <div class="topbar-spacer"></div>
          <button class="ghost sm" @click="showRaw = !showRaw">{{ showRaw ? '收起' : '展开' }}</button>
        </div>
        <div v-if="showRaw" class="panel">
          <pre class="mono" style="padding: 16px 20px; overflow: auto; max-height: 420px; margin: 0">{{ JSON.stringify({
            preflight: store.thPreflight,
            routes: store.thRoutes,
            cost: store.thCost,
            plate: store.thPlate,
            submissions: store.thSubmissions,
            gate: store.thGate,
            videos: store.videos,
          }, null, 2) }}</pre>
        </div>
      </details>
    </section>

    <!-- Sticky 底栏：仅在 setup Tab 可见，且已确认音频 -->
    <footer v-if="currentTab === 'setup' && (wantsCreative || store.confirmedAudio) && routeSelected" class="video-actionbar">
      <div class="actionbar-info">
        <span class="faint">{{ cloudRoute ? '本次生成费用估算' : '本次 API 费用（另计 GPU 成本）' }}</span>
        <strong v-if="estimate" class="num">¥{{ yuan(estimate.estimated_fen) }}</strong>
        <strong v-else class="quote-pending">等待报价</strong>
        <span v-if="estimate" class="faint">上限 ¥{{ yuan(estimate.ceiling_fen) }}</span>
        <StatusBadge
          v-if="currentPrice"
          :text="currentPrice.billing_verified ? 'verified' : 'unverified'"
        />
      </div>
      <div class="actionbar-actions">
        <button :disabled="!sourceReady || !!store.busy || !!inputError" @click="store.submitTalkingHead(true)">检查生成条件</button>
        <!-- 主按钮必须自报"这是真实付费调用"：旁边那个只做检查，两者不能看起来一样。 -->
        <button class="primary" :disabled="!canSubmit" @click="onPrimarySubmit()">
          {{ wantsCreative ? '生成创意短视频（真实调用）' : blockedRuns.length ? '重新生成（真实调用，会再次计费）' : '生成口播（真实调用）' }}
        </button>
      </div>
    </footer>

    <!-- 高成本路线费用提示 modal -->
    <div v-if="noticeFor && notice" class="modal-mask" @click.self="noticeFor = null">
      <div class="modal" role="dialog" aria-modal="true" aria-label="确认视频生成路线费用">
        <div class="modal-head">
          <span class="sub" style="margin: 0">选择生成路线 · {{ notice.cost_tier_label }}</span>
          <div class="topbar-spacer"></div>
          <span class="tier tier-high">{{ noticeFor.label }}</span>
        </div>

        <div class="faint" style="font-size: 13px; line-height: 1.7; margin-top: 10px">
          {{ noticeFor.cost_summary }}。{{ notice.note }}
          <template v-if="noticeFor.route === 'WAN_3_0'">选择后上传图片并填写画面与声音提示词，默认 5 秒、720p、9:16；调整输入后重新确认报价。</template>
        </div>

        <div class="modal-amount">
          <template v-if="noticeAmountKnown">
            <span class="num" style="font-size: 24px; font-weight: 600">
              ¥{{ yuan(notice.estimated_fen ?? 0) }}
            </span>
            <span class="faint num" style="margin-left: 8px; font-size: 13px">
              上限 ¥{{ yuan(notice.ceiling_fen ?? 0) }}（{{ notice.audio_seconds }}s，向上取整为
              {{ notice.billing_seconds }} 计费秒）
            </span>
          </template>
          <template v-else>
            <span class="faint" style="font-size: 13.5px">{{ notice.amount_pending_reason }}</span>
          </template>
        </div>

        <div class="faint" style="font-size: 12.5px; margin-top: 8px">
          计费口径：{{ notice.unit_text }}
        </div>
        <div v-if="notice.price_basis" class="faint" style="font-size: 12.5px; margin-top: 4px">
          {{ notice.price_basis }}
        </div>

        <div class="sub" style="margin-top: 14px">你需要知道的风险</div>
        <ul class="risk-list">
          <li v-for="risk in notice.risks" :key="risk">{{ risk }}</li>
        </ul>
        <div class="faint" style="font-size: 12.5px">
          不含：{{ notice.excluded.join('、') }}
        </div>

        <div class="divider" style="margin: 14px 0"></div>
        <label class="faint" style="font-size: 13px; display: flex; align-items: flex-start; gap: 8px">
          <input v-model="noticeAck" type="checkbox" style="margin-top: 3px" />
          我已阅读并确认上述费用提示：这条路线按生成秒计费，金额可能高于估价，
          效果不满意或重做仍会收费。
        </label>

        <div class="btn-row" style="margin-top: 14px">
          <button class="primary" :disabled="!noticeAck || store.busy !== ''" @click="acceptNotice()">
            确认费用提示并选择此路线
          </button>
          <button class="ghost sm" @click="noticeFor = null">取消</button>
          <div class="topbar-spacer"></div>
          <span class="faint" style="font-size: 12.5px">
            提示版本 {{ notice.notice_version }}
          </span>
        </div>
        <div class="faint" style="font-size: 12.5px; margin-top: 8px">
          这里确认的是"要不要走这条付费路线"；提交前还要再确认一次本次的调用清单与金额。
        </div>
      </div>
    </div>

    <!-- 对账 modal：把一条 unknown 运行判成终态。改判要留证据，后端才收。 -->
    <div v-if="settleFor" class="modal-mask" @click.self="settleFor = null">
      <div class="modal" role="dialog" aria-modal="true" aria-label="对账：登记运行结论">
        <div class="modal-head">
          <span class="sub" style="margin: 0">对账 · 登记这条运行的最终结论</span>
          <div class="topbar-spacer"></div>
          <span class="mono faint">{{ settleFor.id.slice(-8) }}</span>
        </div>

        <div class="faint" style="font-size: 13px; line-height: 1.7; margin-top: 10px">
          这条运行的状态是 <strong>unknown</strong>：既没拿到成功结果，也没拿到失败结论。
          对账是把它判成终态，<strong>不是重提</strong>——不会产生新的调用，也不会自动重新生成。
        </div>
        <div class="faint" style="font-size: 12.5px; line-height: 1.8; margin-top: 8px">
          模型 {{ settleFor.model }}
          <template v-if="settleFor.remote_job_id">
            · 远端任务号 <span class="mono">{{ settleFor.remote_job_id }}</span>
          </template>
          <template v-else>· 未取得远端任务号</template>
        </div>

        <div class="field-heading" style="margin-top: 16px">依据供应商记录，这条运行最终是</div>
        <div class="settle-choices">
          <label
            v-for="opt in SETTLE_OUTCOMES"
            :key="opt.value"
            class="settle-choice"
            :class="{ selected: settleForm.outcome === opt.value }"
          >
            <input v-model="settleForm.outcome" type="radio" :value="opt.value" />
            <span>
              <strong>{{ opt.label }}</strong>
              <span class="faint" style="display: block; font-size: 12.5px; margin-top: 2px">{{ opt.hint }}</span>
            </span>
          </label>
        </div>

        <div class="field-heading" style="margin-top: 16px">证据（在哪里看到的、看到了什么）</div>
        <textarea
          v-model="settleForm.note"
          placeholder="例：登录火山引擎控制台 → 视频生成任务，查到该任务号为「生成失败：音频时长超限」，未产出任何视频。"
        ></textarea>
        <div class="faint" style="font-size: 12.5px; margin-top: 6px">
          至少 4 个字。没有依据的改判会被后端拒掉——这段说明会随结论一起存档。
        </div>

        <div class="btn-row" style="margin-top: 14px">
          <button class="primary" :disabled="!settleNoteOk || store.busy !== ''" @click="confirmSettle()">
            登记结论
          </button>
          <button class="ghost sm" @click="settleFor = null">取消</button>
        </div>
      </div>
    </div>

    <!-- 重新生成二次确认：这是唯一会"再花一次钱"的按钮，必须说清代价 -->
    <div v-if="regenOpen" class="modal-mask" @click.self="regenOpen = null">
      <div class="modal" role="dialog" aria-modal="true" aria-label="确认重新生成">
        <div class="modal-head">
          <span class="sub" style="margin: 0">重新生成 · 会再次计费</span>
        </div>

        <div class="faint" style="font-size: 13px; line-height: 1.7; margin-top: 10px">
          本次作品已有 <strong>{{ blockedRuns.length }}</strong> 条运行是失败或未知状态。
          重新生成会<strong>换一组幂等键、把调用清单重新提交一遍</strong>，云端按生成秒计费——
          <strong>上一次花掉的钱不会退回</strong>。
        </div>

        <ul class="risk-list" style="margin-top: 12px">
          <li v-for="r in blockedRuns.slice(0, 6)" :key="r.id">
            <StatusBadge :text="r.state" />
            <span class="mono faint" style="margin-left: 6px">{{ r.id.slice(-8) }}</span>
            <span style="margin-left: 6px">{{ r.model }}</span>
          </li>
        </ul>
        <div v-if="blockedRuns.length > 6" class="faint" style="font-size: 12.5px">
          另有 {{ blockedRuns.length - 6 }} 条，可在诊断页的提交记录里查看。
        </div>

        <div class="faint" style="font-size: 12.5px; margin-top: 10px; line-height: 1.7">
          如果这些运行其实已经出了片，先点"查询原任务"或"对账"——那样不用再花钱。
        </div>

        <div class="btn-row" style="margin-top: 14px">
          <button class="primary" :disabled="!!store.busy" @click="confirmRegen()">
            确认重新生成（会再次计费）
          </button>
          <button class="ghost sm" @click="regenOpen = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wan-image-input { margin: 18px 0; }
.wan-image-preview { margin: 12px 0; }
.wan-image-preview img { display: block; max-width: 260px; max-height: 300px; object-fit: contain; border-radius: 8px; }
.wan-image-preview figcaption { margin-top: 8px; color: var(--fg-2); font-size: 12px; }
/* 4 Tab 导航（替代原 page-head + video-nav） */
.video-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  border-bottom: 1px solid var(--line);
  padding: 0 0 0;
  margin: 24px 0 20px;
  font-size: 13px;
}
.video-tabs button {
  padding: 8px 14px;
  min-height: 0;
  font-size: inherit;
  font-weight: 500;
  color: var(--fg-2);
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.video-tabs button:hover { color: var(--blue); }
.video-tabs button.active {
  color: var(--fg);
  border-bottom-color: var(--fg);
}
.tab-badge {
  font-size: 11px;
  background: var(--panel-2);
  border-radius: 9px;
  padding: 1px 7px;
  color: var(--fg-2);
}
.tab-dot-ok {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green, #639922);
}
.tab-spacer { flex: 1; }

/* Tab 标题头 */
.tab-pane-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 14px;
}
.tab-pane-head h2 {
  font-size: 16px;
  font-weight: 500;
  margin: 0;
}

/* 阻塞条幅（替代原 step-blocker 大块） */
.banner-blocker {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r-xs);
  background: var(--panel-2);
  font-size: 13px;
  color: var(--fg-2);
  margin-top: 16px;
}

.source-strip {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}

.field-heading { font-size: 14px; font-weight: 500; margin-bottom: 12px; }

.generation-layout { display: block; padding: 8px 0 16px; }
.route-choices { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.route-choice {
  display: block;
  padding: 16px;
  text-align: left;
  white-space: normal;
  height: auto;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--r-xs);
  box-shadow: none;
}
.route-choice.selected { border-color: var(--fg); box-shadow: inset 0 0 0 1px var(--fg); background: var(--panel); }
.route-choice-top { display: flex; gap: 8px; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.route-choice-top strong { font-size: 14px; }
.route-choice-top > span { font-size: 11px; color: var(--fg-3); font-weight: 400; }
.route-choice p { font-size: 13px; font-weight: 400; color: var(--fg-2); line-height: 1.65; margin-bottom: 8px; }
.route-choice small { font-size: 12px; font-weight: 400; color: var(--fg-3); }

/* readiness 内联条：贴在路线卡下方，一行交代 */
.readiness-inline {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--r-xs);
  background: var(--panel-2);
  font-size: 12.5px;
  color: var(--fg-2);
  line-height: 1.7;
}
.readiness-inline .dot-ok {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--green, #639922);
  margin-top: 6px;
  flex-shrink: 0;
}
.readiness-inline .dot-warn {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--amber, #BA7517);
  margin-top: 6px;
  flex-shrink: 0;
}
.blocker-list.inline {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.blocker-list.inline li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
}
.blocker-list .action-link { font-size: 12px; }

.video-settings {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  margin-top: 22px;
}
.duration-info {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  font-size: 12px;
}
.duration-info strong { font-size: 16px; font-weight: 500; }

.motion-field { margin-top: 20px; }
.motion-field > span { display: flex; justify-content: space-between; }
.motion-field small { color: var(--fg-3); font-size: 12px; font-weight: 400; }
.motion-field .error-text { color: var(--red); }

.setup-hint { color: var(--fg-3); font-size: 12px; line-height: 1.8; margin-top: 12px; }

.plate-setup { margin: 16px 0; }
.plate-source { display: flex; gap: 14px; align-items: center; margin-top: 16px; color: var(--fg-2); font-size: 12.5px; }
.plate-source img { width: 90px; max-height: 140px; object-fit: contain; background: var(--bg-2); }
.plate-progress { display: flex; gap: 12px; align-items: center; margin-top: 14px; font-size: 12.5px; color: var(--fg-2); }
.plate-setup .section-head { padding-top: 16px; }

.generation-submit {
  padding: 22px 0 100px;
  border-top: 1px solid var(--line);
  margin-top: 16px;
}
.generation-submit > .panel-pad { padding: 16px 0 0; }

/* Sticky 底栏 */
.video-actionbar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: -84px;
  padding: 12px 20px;
  background: var(--panel);
  border-top: 1px solid var(--line);
  z-index: 20;
}
.actionbar-info {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12.5px;
}
.actionbar-info strong {
  font-size: 19px;
  font-weight: 600;
}
.actionbar-info .quote-pending { font-size: 14px; font-weight: 500; color: var(--fg-3); }
.actionbar-actions {
  display: flex;
  gap: 10px;
}

.review-box { margin-top: 14px; padding: 20px; border: 1px solid var(--line); background: var(--panel-2); border-radius: var(--r-xs); }

.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.3); display: flex; align-items: center; justify-content: center; z-index: 60; padding: 20px; }
.modal { width: 560px; max-width: 100%; max-height: calc(100vh - 40px); overflow: auto; background: var(--panel); border: 1px solid var(--line); border-radius: var(--r-sm); padding: 24px; }
.modal-head { display: flex; align-items: center; gap: 10px; }
.modal-amount { margin-top: 16px; padding: 16px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.tier { font-size: 12px; color: var(--fg-3); }
.risk-list { margin: 8px 0; padding-left: 20px; font-size: 13px; line-height: 1.8; color: var(--fg-2); }
/* 对账：三个终态并排，选中态要一眼看出来，避免误判成"重试" */
.settle-choices { display: flex; flex-direction: column; gap: 8px; }
.settle-choice {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  cursor: pointer;
  font-size: 13px;
  color: var(--fg-2);
}
.settle-choice:hover { background: var(--panel-3); }
.settle-choice.selected { border-color: var(--fg-3); background: var(--panel-3); color: var(--fg); }
.settle-choice input { margin-top: 3px; }

@media (max-width: 1050px) {
  .route-choices { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 720px) {
  .video-tabs { font-size: 12px; }
  .video-tabs button { padding: 6px 10px; }
  .video-settings { grid-template-columns: minmax(0, 1fr); }
  .source-strip { align-items: flex-start; }
  .review-box { padding: 14px; }
  .video-actionbar { flex-direction: column; align-items: stretch; margin-top: -90px; }
  .actionbar-info { justify-content: space-between; }
}
</style>
