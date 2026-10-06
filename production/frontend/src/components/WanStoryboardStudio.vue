<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, apiMode } from '@/api'
import { useStudioStore } from '@/stores/studio'
import type { WanStoryboardGraph, WanStoryboardShot } from '@/api/types'

const studio = useStudioStore()
const graph = ref<WanStoryboardGraph | null>(null)
const busy = ref(false)
const error = ref('')
const resolution = ref('720p')
const imageBudget = ref(0)
const imagePaidAck = ref(false)
const videoPaidAck = ref(false)
const imageReviewAck = ref(false)
const clipReviewAck = ref(false)
const finalReviewAck = ref(false)
const confirming = ref(false)
const subtitleComputeAck = ref(false)
const subtitleRequest = ref<{ graphId: string; videoId: string; outputDigest: string; requestId: string } | null>(null)
const imageRegeneration = ref<{
  graphId: string; shotIndex: number; planDigest: string; imageJobId: string; requestId: string; ceilingFen: number
} | null>(null)
const imageRegenerationAck = ref(false)
const videoRegeneration = ref<{
  graphId: string; shotIndex: number; taskGraphDigest: string; videoJobId: string; requestId: string; ceilingFen: number
} | null>(null)
const videoRegenerationAck = ref(false)
let generation = 0
let timer: ReturnType<typeof setTimeout> | undefined
const money = (fen: number) => `¥${(fen / 100).toFixed(2)}`
const budgetUnlimited = computed(() => graph.value?.estimate.budget_unlimited ?? true)
const allImages = computed(() => !!graph.value && graph.value.shots.every(s => s.image_state === 'succeeded'))
const allClips = computed(() => !!graph.value && graph.value.shots.every(s => s.video_state === 'succeeded'))
const legacyWithoutReference = computed(() => !!graph.value && (graph.value.schema_version ?? 2) < 3 && !!studio.currentWork?.identity_id)
const referenceChanged = computed(() => {
  if (!graph.value || legacyWithoutReference.value) return false
  const reference = graph.value.identity_reference
  const identityId = studio.currentWork?.identity_id ?? null
  return identityId !== (reference?.identity_id ?? null)
    || (!!identityId && (studio.currentWork?.variant_id ?? null) !== (reference?.variant_id ?? null))
})
const needsReprepare = computed(() => legacyWithoutReference.value || referenceChanged.value)
const unresolved = computed(() => graph.value?.shots.some(s => ['unknown', 'failed', 'blocked', 'sending'].includes(s.video_state)) ?? false)
const invalidSelection = computed(() => ['STORYBOARD_INPUT_CHANGED', 'STORYBOARD_SELECTION_REQUIRED'].includes(graph.value?.error?.code ?? ''))
const subtitlesProcessing = computed(() => ['queued', 'running'].includes(graph.value?.subtitles?.state ?? ''))
const subtitlesUnresolved = computed(() => subtitlesProcessing.value || graph.value?.subtitles?.state === 'unknown')
const subtitleGenerationBlocked = computed(() => busy.value || confirming.value || needsReprepare.value || invalidSelection.value
  || apiMode !== 'http' || subtitlesProcessing.value || graph.value?.subtitles?.state === 'unknown'
  || graph.value?.subtitle_generation_available !== true || !graph.value.video_id || !graph.value.output?.file_hash)
const videoStarted = computed(() => !!graph.value && (
  graph.value.shots.some(s => s.video_state !== 'pending' || !!s.remote_task_id || !!s.video_job_id || !!s.clip)
  || !!graph.value.video_id || !!graph.value.output
  || ['stitch_queued', 'stitching', 'stitch_failed', 'succeeded'].includes(graph.value.state)
))
const imageRegenerationBlocked = computed(() => busy.value || needsReprepare.value || videoStarted.value || invalidSelection.value || apiMode !== 'http')
const imageRegenerationCurrent = computed(() => {
  const selection = imageRegeneration.value; const g = graph.value
  if (!selection || !g || g.id !== selection.graphId || g.plan_digest !== selection.planDigest
    || g.image_ceiling_fen_per_shot !== selection.ceilingFen) return false
  const shot = g.shots.find(s => s.shot_index === selection.shotIndex)
  return shot?.image_regeneration_available === true && shot.image_job_id === selection.imageJobId
})
const videoRegenerationBlocked = computed(() => busy.value || confirming.value || needsReprepare.value || invalidSelection.value
  || apiMode !== 'http' || !!graph.value?.video_id || !!graph.value?.output || !!graph.value?.package_id
  || ['stitch_queued', 'stitching', 'stitch_failed', 'succeeded'].includes(graph.value?.state ?? ''))
const videoRegenerationCurrent = computed(() => {
  const selection = videoRegeneration.value; const g = graph.value
  if (!selection || !g || g.id !== selection.graphId || g.task_graph_digest !== selection.taskGraphDigest
    || videoRegenerationPrice(selection.shotIndex) !== selection.ceilingFen) return false
  const shot = g.shots.find(s => s.shot_index === selection.shotIndex)
  return !!shot && videoRegenerationEligible(shot) && shot.video_job_id === selection.videoJobId
})
const stitchQueueStalled = computed(() => {
  if (graph.value?.state !== 'stitch_queued' || !graph.value.updated_at) return false
  const queuedAt = Date.parse(graph.value.updated_at)
  return Number.isFinite(queuedAt) && Date.now() - queuedAt > 60_000
})
const labels: Record<string, string> = { pending: '待提交', sending: '提交中', succeeded: '已完成', failed: '失败',
  unknown: '结果待核对', blocked: '已阻断', waiting_provider: '生成中', waiting_image_approval: '待确认图片预算',
  images_ready: '镜头图已就绪', waiting_clip_review: '待观看原片', ready_to_stitch: '可拼接', stitch_queued: '拼接排队中', stitching: '拼接中', stitch_failed: '拼接失败' }

async function action(call: () => Promise<WanStoryboardGraph>) {
  if (busy.value) return
  const current = generation
  busy.value = true; error.value = ''
  try { const next = await call(); if (current === generation) { graph.value = next; pollLater() } }
  catch (e: any) { if (current === generation) error.value = e?.message ?? String(e) }
  finally { if (current === generation) busy.value = false }
}
async function load() {
  const current = generation; const workId = studio.currentWorkId
  if (!workId || apiMode !== 'http') return
  try {
    const result = await api.listWanStoryboards(workId)
    if (current !== generation) return
    graph.value = [...result.graphs].reverse().find(g => g.script_version_id === studio.currentWork?.script_version_id) ?? null
    pollLater()
  } catch (e: any) { if (current === generation) error.value = e?.message ?? String(e) }
}
function pollLater() {
  clearTimeout(timer)
  if (!graph.value || (!subtitlesProcessing.value && !['waiting_provider', 'stitch_queued', 'stitching'].includes(graph.value.state))) return
  const current = generation
  timer = setTimeout(async () => {
    if (current !== generation || busy.value || !graph.value) { pollLater(); return }
    const workId = studio.currentWorkId; const id = graph.value.id
    if (subtitlesProcessing.value) { await action(() => api.getWanStoryboard(workId, id)); return }
    const waiting = graph.value.shots.find(s => s.remote_task_id && s.video_state === 'waiting_provider')
    const next = graph.value.shots.some(s => s.video_state === 'pending')
    await action(() => waiting ? api.pollWanStoryboardShot(workId, id, waiting.shot_index)
      : next && graph.value!.video_approval && !unresolved.value && !needsReprepare.value ? api.submitWanStoryboardVideos(workId, id)
      : api.getWanStoryboard(workId, id))
  }, 5000)
}
watch(() => [studio.currentWorkId, studio.currentWork?.script_version_id, studio.currentWork?.identity_id, studio.currentWork?.variant_id], () => {
  generation++; clearTimeout(timer); graph.value = null; busy.value = false; error.value = ''
  resetImageRegeneration()
  resetVideoRegeneration()
  resetSubtitleRequest()
  imagePaidAck.value = videoPaidAck.value = imageReviewAck.value = clipReviewAck.value = finalReviewAck.value = false
  void load()
}, { immediate: true })
watch(() => [graph.value?.id, graph.value?.video_id, graph.value?.output?.file_hash, graph.value?.output?.preview_url], (next, previous) => {
  if (next.some((value, index) => value !== previous[index])) {
    finalReviewAck.value = false
    resetSubtitleRequest()
  }
}, { flush: 'sync' })
watch(() => [graph.value?.subtitles?.job_id, graph.value?.subtitles?.state], (next, previous) => {
  if (next.some((value, index) => value !== previous[index]) && ['failed', 'succeeded'].includes(next[1] ?? '')) {
    resetSubtitleRequest()
    if (next[1] === 'succeeded') finalReviewAck.value = false
  }
}, { flush: 'sync' })
onBeforeUnmount(() => { generation++; clearTimeout(timer) })
const workId = () => studio.currentWorkId
function create() { void action(() => api.createWanStoryboard(workId(), { resolution: resolution.value, image_ceiling_fen_per_shot: budgetUnlimited.value ? 0 : Math.round(imageBudget.value * 100) })) }
function approveImages() {
  if (needsReprepare.value) return
  const g = graph.value!; void action(() => api.approveWanStoryboardImages(workId(), g.id, { actor_id: 'user_local', paid_ack: imagePaidAck.value, plan_digest: g.plan_digest, total_ceiling_fen: g.estimate.image_ceiling_total_fen }))
}
function approveVideos() {
  if (needsReprepare.value) return
  const g = graph.value!; void action(() => api.approveWanStoryboardVideos(workId(), g.id, { actor_id: 'user_local', paid_ack: videoPaidAck.value, image_review_ack: imageReviewAck.value, task_graph_digest: g.task_graph_digest, total_ceiling_fen: g.estimate.video_total_fen }))
}
function generateImages() {
  if (needsReprepare.value || !graph.value) return
  const id = graph.value.id; void action(() => api.generateWanStoryboardImages(workId(), id))
}
function resetImageRegeneration() {
  imageRegeneration.value = null; imageRegenerationAck.value = false
}
function requestImageRegeneration(shotIndex: number) {
  if (imageRegenerationBlocked.value) return
  const g = graph.value; const shot = g?.shots.find(s => s.shot_index === shotIndex)
  if (!g || shot?.image_regeneration_available !== true || !shot.image_job_id
    || !Number.isFinite(g.image_ceiling_fen_per_shot) || g.image_ceiling_fen_per_shot < 0) return
  if (imageRegenerationCurrent.value && imageRegeneration.value?.shotIndex === shotIndex) return
  imageRegenerationAck.value = false
  imageRegeneration.value = { graphId: g.id, shotIndex, planDigest: g.plan_digest,
    imageJobId: shot.image_job_id, requestId: globalThis.crypto.randomUUID(), ceilingFen: g.image_ceiling_fen_per_shot }
}
function cancelImageRegeneration() {
  if (!busy.value) resetImageRegeneration()
}
async function confirmImageRegeneration() {
  if (imageRegenerationBlocked.value || !imageRegenerationAck.value || !imageRegenerationCurrent.value) return
  const selection = imageRegeneration.value!; const current = generation; const currentWorkId = workId()
  await action(async () => {
    const next = await api.regenerateWanStoryboardImage(currentWorkId, selection.graphId, selection.shotIndex, {
      actor_id: 'user_local', paid_ack: true, duplicate_charge_ack: true,
      plan_digest: selection.planDigest, expected_image_job_id: selection.imageJobId,
      request_id: selection.requestId, ceiling_fen: selection.ceilingFen,
    })
    if (current === generation) {
      resetImageRegeneration()
      imageReviewAck.value = videoPaidAck.value = clipReviewAck.value = finalReviewAck.value = false
    }
    return next
  })
}
function submitVideos() {
  if (needsReprepare.value || !graph.value) return
  const id = graph.value.id; void action(() => api.submitWanStoryboardVideos(workId(), id))
}
function videoRegenerationPrice(shotIndex: number) {
  return graph.value?.estimate.shot_prices?.find(price => price.shot_index === shotIndex)?.ceiling_fen
}
function videoRegenerationEligible(shot: WanStoryboardShot) {
  const ceiling = videoRegenerationPrice(shot.shot_index)
  return shot.video_regeneration_available === true && !!shot.video_job_id && shot.image_state === 'succeeded'
    && ['failed', 'unknown', 'blocked', 'succeeded'].includes(shot.video_state)
    && typeof ceiling === 'number' && Number.isFinite(ceiling) && ceiling >= 0
}
function resetVideoRegeneration() {
  videoRegeneration.value = null; videoRegenerationAck.value = false
}
function requestVideoRegeneration(shotIndex: number) {
  if (videoRegenerationBlocked.value) return
  const g = graph.value; const shot = g?.shots.find(s => s.shot_index === shotIndex)
  if (!g || !shot || !videoRegenerationEligible(shot)) return
  if (videoRegenerationCurrent.value && videoRegeneration.value?.shotIndex === shotIndex) return
  videoRegenerationAck.value = false
  videoRegeneration.value = { graphId: g.id, shotIndex, taskGraphDigest: g.task_graph_digest,
    videoJobId: shot.video_job_id!, requestId: globalThis.crypto.randomUUID(), ceilingFen: videoRegenerationPrice(shotIndex)! }
}
function cancelVideoRegeneration() {
  if (!busy.value) resetVideoRegeneration()
}
async function confirmVideoRegeneration() {
  if (videoRegenerationBlocked.value || !videoRegenerationAck.value || !videoRegenerationCurrent.value) return
  const selection = videoRegeneration.value!; const current = generation; const currentWorkId = workId()
  await action(async () => {
    const next = await api.regenerateWanStoryboardVideo(currentWorkId, selection.graphId, selection.shotIndex, {
      actor_id: 'user_local', paid_ack: true, duplicate_charge_ack: true,
      task_graph_digest: selection.taskGraphDigest, expected_video_job_id: selection.videoJobId,
      request_id: selection.requestId, ceiling_fen: selection.ceilingFen,
    })
    if (current === generation) {
      resetVideoRegeneration()
      clipReviewAck.value = finalReviewAck.value = false
    }
    return next
  })
}
function approveClips() {
  const g = graph.value!; void action(() => api.approveWanStoryboardClips(workId(), g.id, { actor_id: 'user_local', review_ack: clipReviewAck.value, clip_digest: g.clip_digest }))
}
function resetSubtitleRequest() {
  subtitleRequest.value = null; subtitleComputeAck.value = false
}
async function generateSubtitles() {
  const g = graph.value
  if (subtitleGenerationBlocked.value || !subtitleComputeAck.value || !g?.video_id || !g.output?.file_hash) return
  let request = subtitleRequest.value
  if (!request || request.graphId !== g.id || request.videoId !== g.video_id || request.outputDigest !== g.output.file_hash) {
    request = { graphId: g.id, videoId: g.video_id, outputDigest: g.output.file_hash, requestId: globalThis.crypto.randomUUID() }
    subtitleRequest.value = request
  }
  const currentWorkId = workId(); const selected = request
  finalReviewAck.value = false
  await action(() => api.generateWanStoryboardSubtitles(currentWorkId, selected.graphId, {
    actor_id: 'user_local', compute_ack: true, expected_video_id: selected.videoId,
    output_digest: selected.outputDigest, request_id: selected.requestId,
  }))
}
function refreshSubtitles() {
  const g = graph.value
  if (!g || apiMode !== 'http') return
  void action(() => api.getWanStoryboard(workId(), g.id))
}
async function confirmFinal() {
  const g = graph.value
  if (!g?.video_id || !g.output || !finalReviewAck.value || confirming.value || busy.value || subtitlesUnresolved.value) return
  const current = generation
  confirming.value = true; error.value = ''
  try {
    const result = await api.confirmWanStoryboard(workId(), g.id, {
      actor_id: 'user_local', review_ack: true, video_id: g.video_id, output_digest: g.output.file_hash,
    })
    if (current !== generation) return
    graph.value = { ...result.graph, package_id: result.package.id }
    await studio.syncWork(workId())
    await studio.refreshPostprocess()
  } catch (e: any) { if (current === generation) error.value = e?.message ?? String(e) }
  finally { if (current === generation) confirming.value = false }
}
</script>

<template>
  <div class="storyboard-studio">
    <header class="section-head"><h1>Wan 分镜制作</h1><button :disabled="busy" @click="load">刷新状态</button></header>
    <p v-if="apiMode !== 'http'" class="warning">演示模式不提交镜头生成。</p>
    <p v-if="!studio.scriptReady">请先在脚本页完成审核并采用一篇分镜剧本。</p>
    <p v-if="error" class="warning" role="alert">{{ error }}</p>
    <p v-if="graph?.error" class="warning" role="alert">{{ graph.error.message }}</p>
    <p v-if="invalidSelection" class="warning" role="alert">当前任务图已失效，请返回脚本页重新采用审核通过的分镜，再重新准备任务图。</p>
    <p v-if="legacyWithoutReference" class="warning" role="alert">旧任务未使用人物图。请按当前人物重新准备任务图，再确认图片预算；原有结果仍可查看。</p>
    <p v-else-if="referenceChanged" class="warning" role="alert">人物参考已变化，请重新准备任务图，再确认图片预算；原有结果仍可查看。</p>
    <section v-if="!graph || !graph.image_approval || needsReprepare" class="section graph-setup">
      <label class="field"><span>清晰度</span><select v-model="resolution"><option>480p</option><option>720p</option><option>1080p</option></select></label>
      <label v-if="!budgetUnlimited" class="field"><span>每张图片预算预留（元）</span><input v-model.number="imageBudget" type="number" min="0" step="0.01" /></label>
      <button class="primary" :disabled="busy || apiMode !== 'http' || (!studio.scriptReady && !needsReprepare) || !Number.isFinite(imageBudget) || imageBudget < 0" @click="create">{{ needsReprepare ? '按当前人物重新准备任务图' : graph ? '更新预算与任务图' : '准备任务图' }}</button>
    </section>
    <template v-if="graph">
      <figure v-if="graph.identity_reference" class="identity-reference">
        <img v-if="graph.identity_reference.preview_url" :src="graph.identity_reference.preview_url" alt="人物参考图" />
        <figcaption><strong>人物参考图</strong><span>{{ graph.identity_reference.label }}</span></figcaption>
      </figure>
      <p v-else-if="!needsReprepare" class="faint">未选择人物，镜头图片将按画面描述生成。</p>
      <section class="section graph-summary">
        <strong>{{ labels[graph.state] ?? graph.state }}</strong><span>{{ graph.shots.length }} 镜头 · {{ graph.shots.reduce((n, s) => n + s.duration_seconds, 0) }} 秒 · {{ graph.resolution }}</span>
        <span>{{ budgetUnlimited ? '图片按实际账单' : `图片预算 ${money(graph.estimate.image_ceiling_total_fen)}` }} · Wan 估算 {{ money(graph.estimate.video_total_fen) }}</span>
        <p class="faint">图片金额是预算预留，当前没有已核验的供应商单价。已受理作业可能计费。</p>
      </section>
      <ol class="shot-list">
        <li v-for="shot in graph.shots" :key="shot.shot_index" class="shot-row">
          <header><strong>镜头 {{ shot.shot_index }}</strong><span>{{ shot.duration_seconds }} 秒</span></header>
          <img v-if="shot.image?.preview_url" :src="shot.image.preview_url" :alt="`镜头 ${shot.shot_index} 参考图`" />
          <video v-if="shot.clip?.preview_url" :src="shot.clip.preview_url" controls preload="metadata"></video>
          <div class="shot-content"><p>{{ shot.visual }}</p><p>台词 / 旁白：{{ shot.dialogue }}</p><p>声音：{{ shot.sound_effects }}</p>
            <details><summary>镜头提示词</summary><p>{{ shot.wan_prompt }}</p></details>
            <p class="faint">图片：{{ labels[shot.image_state] ?? shot.image_state }} · 视频：{{ labels[shot.video_state] ?? shot.video_state }}</p>
            <p v-if="shot.image_job_id" class="faint">图片作业号：{{ shot.image_job_id }}</p>
            <template v-if="['unknown', 'sending'].includes(shot.image_state)">
              <button v-if="shot.image_recovery_available" :disabled="busy || needsReprepare" @click="action(() => api.recoverWanStoryboardImage(workId(), graph!.id, shot.shot_index))">回收已有图片</button>
              <p v-else-if="shot.image_state === 'unknown'" class="faint">尚未取得图片回执，请在供应商后台核对原请求或确认追加费用后重新生成。</p>
            </template>
            <button v-if="shot.image_regeneration_available" :disabled="imageRegenerationBlocked" @click="requestImageRegeneration(shot.shot_index)">重新生成此镜头</button>
            <section v-if="imageRegeneration?.shotIndex === shot.shot_index" class="image-regeneration-confirm" :aria-label="`重新生成镜头 ${shot.shot_index} 确认`">
              <p class="warning">原请求可能已计费，重新生成会发起新的图片请求，可能再次计费</p>
              <p class="faint">{{ budgetUnlimited ? '新请求费用按实际账单' : `本次图片预算预留 ${money(imageRegeneration.ceilingFen)}` }}</p>
              <label><input v-model="imageRegenerationAck" type="checkbox" :disabled="imageRegenerationBlocked" /> 我确认重新生成此镜头可能再次计费</label>
              <div class="image-regeneration-actions">
                <button :disabled="busy" @click="cancelImageRegeneration">取消</button>
                <button :disabled="imageRegenerationBlocked || !imageRegenerationAck || !imageRegenerationCurrent" @click="confirmImageRegeneration">确认重新生成</button>
              </div>
            </section>
            <p v-if="shot.remote_task_id" class="faint">任务号：{{ shot.remote_task_id }}</p><p v-if="shot.error" class="warning">{{ shot.error.message }}</p>
            <button v-if="shot.remote_task_id && ['waiting_provider', 'unknown'].includes(shot.video_state)" :disabled="busy" @click="action(() => api.pollWanStoryboardShot(workId(), graph!.id, shot.shot_index))">查询原作业</button>
            <p v-if="shot.video_job_id" class="faint">视频作业号：{{ shot.video_job_id }}</p>
            <button v-if="shot.video_regeneration_available" :disabled="videoRegenerationBlocked || !videoRegenerationEligible(shot)" @click="requestVideoRegeneration(shot.shot_index)">重新生成视频</button>
            <section v-if="videoRegeneration?.shotIndex === shot.shot_index" class="video-regeneration-confirm" :aria-label="`重新生成镜头 ${shot.shot_index} 视频确认`">
              <p class="warning">原视频请求可能已计费，重新生成可能再次计费</p>
              <p class="faint">本镜头预计 {{ money(videoRegeneration.ceilingFen) }}，费用按实际账单</p>
              <label><input v-model="videoRegenerationAck" type="checkbox" :disabled="videoRegenerationBlocked" /> 我确认重新生成本镜头视频可能再次计费</label>
              <div class="video-regeneration-actions">
                <button :disabled="busy" @click="cancelVideoRegeneration">取消</button>
                <button :disabled="videoRegenerationBlocked || !videoRegenerationAck || !videoRegenerationCurrent" @click="confirmVideoRegeneration">确认重新生成视频</button>
              </div>
            </section>
          </div>
        </li>
      </ol>
      <section class="section graph-actions">
        <template v-if="!graph.image_approval">
          <label><input v-model="imagePaidAck" type="checkbox" /> 确认 {{ graph.estimate.image_calls }} 次生图{{ budgetUnlimited ? '（费用按实际账单）' : `及 ${money(graph.estimate.image_ceiling_total_fen)} 预算预留` }}</label>
          <button :disabled="busy || needsReprepare || !imagePaidAck || (!budgetUnlimited && graph.estimate.image_ceiling_total_fen <= 0)" @click="approveImages">{{ budgetUnlimited ? '确认图片生成' : '确认图片预算' }}</button>
        </template>
        <button v-else-if="!allImages" class="primary" :disabled="busy || needsReprepare || graph.state === 'unknown' || graph.state === 'blocked'" @click="generateImages">生成镜头图片</button>
        <template v-else-if="!graph.video_approval">
          <label><input v-model="imageReviewAck" type="checkbox" /> 已查看所有镜头图片</label>
          <label><input v-model="videoPaidAck" type="checkbox" /> 确认 {{ graph.estimate.video_calls }} 次 Wan 调用，预计 {{ money(graph.estimate.video_total_fen) }}</label>
          <button :disabled="busy || needsReprepare || !imageReviewAck || !videoPaidAck" @click="approveVideos">确认任务图与视频费用</button>
        </template>
        <button v-else-if="!allClips" class="primary" :disabled="busy || needsReprepare || unresolved || graph.shots.some(s => s.video_state === 'waiting_provider')" @click="submitVideos">开始逐镜头生成</button>
        <template v-else-if="!graph.clip_approval">
          <label><input v-model="clipReviewAck" type="checkbox" /> 已观看全部镜头，确认画面、声音和衔接</label>
          <button :disabled="busy || !clipReviewAck" @click="approveClips">确认原片</button>
        </template>
        <button v-else-if="graph.state === 'ready_to_stitch'" class="primary" :disabled="busy" @click="action(() => api.queueWanStoryboardStitch(workId(), graph!.id))">拼接成片</button>
        <button v-else-if="graph.state === 'stitch_failed' && !invalidSelection" class="primary" :disabled="busy" @click="action(() => api.queueWanStoryboardStitch(workId(), graph!.id))">重试本地拼接</button>
        <p v-if="['stitch_queued', 'stitching'].includes(graph.state)" role="status">{{ labels[graph.state] }}</p>
        <p v-if="stitchQueueStalled" class="warning" role="alert">拼接排队超过 60 秒，本地 Worker 可能未运行或已退出。请检查 production/runtime/storyboard-worker.err.log，并重新启动项目。</p>
      </section>
      <section v-if="graph.output?.preview_url" class="section graph-output">
        <h2>成片</h2><video :src="graph.output.preview_url" controls preload="metadata"></video>
        <section v-if="graph.subtitle_generation_available || graph.subtitles" class="subtitle-generation" aria-label="成片字幕">
          <p class="faint">根据视频实际对白生成字幕，使用已连接的云端 GPU；原视频保留。</p>
          <p v-if="subtitlesProcessing" role="status">{{ graph.subtitles?.state === 'queued' ? '字幕生成排队中' : '正在识别对白并生成字幕' }}</p>
          <p v-if="graph.subtitles?.error" class="warning" role="alert">{{ graph.subtitles.error.message }}</p>
          <template v-if="graph.subtitles?.state === 'unknown'">
            <p class="warning" role="status">字幕结果待核对，请刷新已有任务状态。</p>
            <button :disabled="busy || apiMode !== 'http'" @click="refreshSubtitles">刷新字幕状态</button>
          </template>
          <template v-else-if="graph.subtitle_generation_available || subtitlesProcessing">
            <label><input v-model="subtitleComputeAck" type="checkbox" :disabled="subtitleGenerationBlocked" /> 确认使用云端 GPU 生成字幕</label>
            <button :disabled="subtitleGenerationBlocked || !subtitleComputeAck" @click="generateSubtitles">{{ graph.subtitles?.state === 'failed' ? '重试生成字幕' : '生成字幕与成片' }}</button>
          </template>
          <div v-if="graph.output.subtitle_files?.length" class="subtitle-downloads">
            <template v-for="file in graph.output.subtitle_files" :key="file.kind">
              <a v-if="file.preview_url" :href="file.preview_url" download>{{ file.kind === 'subtitles_ass' ? '下载 ASS 字幕' : '下载 SRT 字幕' }}</a>
            </template>
          </div>
        </section>
        <p v-if="graph.package_error" class="warning" role="alert">{{ graph.package_error.message }}</p>
        <template v-if="!graph.package_id">
          <label><input v-model="finalReviewAck" type="checkbox" :disabled="subtitlesUnresolved" /> {{ graph.output.subtitles_burned ? '已观看成片，确认字幕、画面和声音' : '已观看成片，确认画面和声音' }}</label>
          <button class="primary" :disabled="busy || confirming || subtitlesUnresolved || !finalReviewAck" @click="confirmFinal">确认成片并生成发布包</button>
        </template>
        <template v-else><a :href="graph.output.preview_url" download>下载成片</a><router-link to="/publish">进入发布</router-link></template>
      </section>
    </template>
  </div>
</template>

<style scoped>
h1 { font-size: 24px; margin: 0; } h2 { font-size: 18px; }
.graph-setup, .graph-summary, .graph-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; padding: 20px 0; border-bottom: 1px solid var(--line); }
.graph-summary p { width: 100%; margin: 0; font-size: 12px; }
.identity-reference { display: flex; align-items: center; gap: 16px; margin: 0; padding: 20px 0; border-bottom: 1px solid var(--line); }
.identity-reference img { width: 72px; height: 96px; object-fit: contain; background: var(--panel); border-radius: 8px; }
.identity-reference figcaption { display: grid; gap: 8px; font-size: 13px; }
.shot-list { list-style: none; padding: 0; margin: 0; }
.shot-row { display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 16px; padding: 22px 0; border-bottom: 1px solid var(--line); }
.shot-row header { grid-column: 1 / -1; display: flex; justify-content: space-between; }
.shot-row img, .shot-row video { width: 180px; height: 240px; object-fit: contain; background: var(--panel); }
.shot-content { min-width: 0; overflow-wrap: anywhere; } .shot-content p { font-size: 13px; line-height: 1.7; }
.shot-row video + .shot-content { grid-column: 2; }
.graph-actions label { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.graph-output video { display: block; width: min(420px, 100%); max-height: 640px; margin: 16px 0; }
.subtitle-generation { display: grid; justify-items: start; gap: 12px; margin: 16px 0; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.subtitle-generation p { margin: 0; font-size: 13px; line-height: 1.7; }
.subtitle-generation label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.subtitle-downloads { display: flex; flex-wrap: wrap; gap: 16px; }
.warning { color: var(--red); }
.image-regeneration-confirm, .video-regeneration-confirm { margin: 12px 0; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.image-regeneration-confirm label, .video-regeneration-confirm label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.image-regeneration-actions, .video-regeneration-actions { display: flex; gap: 8px; margin-top: 12px; }
@media(max-width:600px) { .shot-row { grid-template-columns: 1fr; } .shot-row img, .shot-row video { width: 100%; height: 300px; } .shot-row video + .shot-content { grid-column: 1; } }
</style>
