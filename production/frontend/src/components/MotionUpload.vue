<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '@/api'
import type { MotionReferenceView, MotionUploadView } from '@/api/types'
import { useStudioStore } from '@/stores/studio'

const store = useStudioStore()
const identityId = computed(() => store.currentWork?.identity_id ?? '')
const open = ref(false)
const uploads = ref<MotionUploadView[]>([])
const selectedUpload = ref<MotionUploadView | null>(null)
const localFile = ref<File | null>(null)
const localUrl = ref('')
const localDuration = ref<number | null>(null)
const localDimensions = ref({ width: 0, height: 0 })
const progress = ref(0)
const uploading = ref(false)
const loading = ref(false)
const confirming = ref(false)
const acknowledged = ref(false)
const previewPlayed = ref(false)
const previewFailed = ref(false)
const label = ref('')
const motionKey = ref<'smile' | 'nod' | 'head_turn_small' | 'serious'>('smile')
const description = ref('')
const sourceBasis = ref('')
const error = ref('')
const notice = ref('')
const savedReference = ref<MotionReferenceView | null>(null)
const bindingFailed = ref(false)
let contextVersion = 0
let requestVersion = 0
let uploadController: AbortController | null = null
let fileVersion = 0
let cancelRequested = false

const motionOptions = [
  { key: 'smile', label: '自然微笑' },
  { key: 'nod', label: '轻微点头' },
  { key: 'head_turn_small', label: '小幅转头' },
  { key: 'serious', label: '严肃陈述' },
] as const
const isVideoFile = computed(() => !!localFile.value && /\.(mp4|mov)$/i.test(localFile.value.name))
const localReady = computed(() => !!localFile.value && isVideoFile.value && localDuration.value !== null
  && localDuration.value >= 3 && localDuration.value <= 60 && localDimensions.value.width > 0)
const selectedPreview = computed(() => selectedUpload.value?.preview_url || localUrl.value)
const canConfirm = computed(() => selectedUpload.value?.state === 'pending' && !!selectedUpload.value.preview_url
  && !!label.value.trim() && !!sourceBasis.value.trim() && acknowledged.value && previewPlayed.value
  && !previewFailed.value && !confirming.value && !uploading.value)

function validUpload(item: MotionUploadView): boolean {
  return !!item?.id && item.identity_id === identityId.value && ['pending', 'confirmed'].includes(item.state)
    && Number.isFinite(item.duration_seconds) && Number.isFinite(item.width) && Number.isFinite(item.height)
    && Number.isFinite(item.size_bytes)
}

function captureContext() {
  const version = contextVersion
  const workId = store.currentWorkId
  const loadVersion = store.workLoadVersion
  const personId = identityId.value
  return { personId, matches: () => version === contextVersion && store.workContextMatches(workId, loadVersion) && identityId.value === personId }
}

function clearLocal() {
  fileVersion++
  if (localUrl.value) URL.revokeObjectURL(localUrl.value)
  localUrl.value = ''
  localFile.value = null
  localDuration.value = null
  localDimensions.value = { width: 0, height: 0 }
}

function resetForContext() {
  contextVersion++
  requestVersion++
  uploadController?.abort()
  uploadController = null
  clearLocal()
  selectedUpload.value = null
  uploads.value = []
  progress.value = 0
  uploading.value = false
  confirming.value = false
  acknowledged.value = false
  previewPlayed.value = false
  previewFailed.value = false
  label.value = ''
  motionKey.value = 'smile'
  description.value = ''
  sourceBasis.value = ''
  savedReference.value = null
  bindingFailed.value = false
  error.value = ''
  notice.value = ''
  open.value = false
}

watch(() => [store.currentWorkId, store.workLoadVersion, identityId.value], () => {
  resetForContext()
  if (identityId.value) void loadUploads()
}, { flush: 'sync', immediate: true })

onBeforeUnmount(() => { uploadController?.abort(); if (localUrl.value) URL.revokeObjectURL(localUrl.value) })

async function loadUploads() {
  const context = captureContext()
  const request = ++requestVersion
  loading.value = true
  try {
    const result = await api.listMotionUploads(context.personId)
    if (context.matches() && request === requestVersion) {
      uploads.value = (result.uploads ?? []).filter(validUpload)
      if (selectedUpload.value) selectedUpload.value = uploads.value.find(item => item.id === selectedUpload.value?.id) ?? null
    }
  } catch (cause: any) {
    if (context.matches() && request === requestVersion) error.value = cause?.message ?? '自定义动作列表读取失败。'
  } finally {
    if (context.matches() && request === requestVersion) loading.value = false
  }
}

function chooseUpload(upload: MotionUploadView) {
  if (!validUpload(upload) || uploading.value || confirming.value) return
  clearLocal()
  selectedUpload.value = upload
  acknowledged.value = false
  previewPlayed.value = false
  previewFailed.value = false
  savedReference.value = null
  bindingFailed.value = false
  error.value = ''
  notice.value = upload.state === 'pending' ? '视频检查完成，请播放预览并确认。' : '此动作模板已保存，可直接用于本期。'
  label.value = upload.original_filename.replace(/\.(mp4|mov)$/i, '').slice(0, 60)
  sourceBasis.value = ''
  description.value = ''
}

function inspectFile(file: File) {
  clearLocal()
  error.value = ''
  notice.value = ''
  selectedUpload.value = null
  savedReference.value = null
  bindingFailed.value = false
  acknowledged.value = false
  previewPlayed.value = false
  previewFailed.value = false
  if (!/\.(mp4|mov)$/i.test(file.name)) { error.value = '只支持 MP4 或 MOV 文件。'; return }
  if (file.size > 100 * 1024 * 1024) { error.value = '文件不能超过 100 MiB。'; return }
  localFile.value = file
  localUrl.value = URL.createObjectURL(file)
  const video = document.createElement('video')
  video.preload = 'metadata'
  const version = fileVersion
  const context = captureContext()
  const matches = () => context.matches() && version === fileVersion && localFile.value === file
  video.onloadedmetadata = () => {
    if (!matches()) return
    localDuration.value = Number.isFinite(video.duration) ? video.duration : null
    localDimensions.value = { width: video.videoWidth, height: video.videoHeight }
    if (localDuration.value !== null && (localDuration.value < 3 || localDuration.value > 60)) error.value = '动作参考视频时长需在 3–60 秒之间。'
    if (!localDimensions.value.width) error.value = '无法读取视频画面尺寸，请换一个可播放的视频。'
  }
  video.onerror = () => { if (matches()) { error.value = '本地视频无法读取，请选择可播放的 MP4/MOV 文件。'; localDuration.value = null } }
  video.src = localUrl.value
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) inspectFile(file)
}

function closeForm() {
  cancelRequested = true
  uploadController?.abort()
  error.value = ''
  notice.value = ''
  open.value = false
}

async function upload() {
  const context = captureContext()
  const file = localFile.value
  if (!context.personId || !file || !localReady.value || uploading.value || store.busy) return
  uploading.value = true
  cancelRequested = false
  progress.value = 0
  error.value = ''
  notice.value = '正在上传，完成后还要等待后端检查和规范化。'
  uploadController = new AbortController()
  try {
    const result = await api.uploadMotion(context.personId, file, (loaded, total) => {
      if (context.matches()) {
        progress.value = total ? Math.min(100, Math.floor(loaded / total * 100)) : 0
        if (progress.value === 100) notice.value = '视频已传输，正在检查和规范化；请等待后端返回结果。'
      }
    }, uploadController.signal)
    if (!context.matches()) return
    if (!validUpload(result)) throw new Error('上传响应缺少有效的视频信息，请刷新列表检查结果。')
    progress.value = 100
    selectedUpload.value = result
    uploads.value = [result, ...uploads.value.filter(item => item.id !== result.id)]
    clearLocal()
    acknowledged.value = false
    previewPlayed.value = false
    previewFailed.value = false
    notice.value = '视频检查完成，请播放预览并填写动作信息。'
    label.value = result.original_filename.replace(/\.(mp4|mov)$/i, '').slice(0, 60)
  } catch (cause: any) {
    if (context.matches() && !cancelRequested) { error.value = cause?.message ?? '动作视频上传失败，请重试。'; notice.value = '' }
  } finally {
    if (context.matches()) { uploading.value = false; uploadController = null }
  }
}

async function confirmAndAdopt() {
  const context = captureContext()
  const upload = selectedUpload.value
  if (!upload || !canConfirm.value || !context.personId) return
  confirming.value = true
  error.value = ''
  notice.value = ''
  try {
    const reference = await api.confirmMotionUpload(upload.id, {
      label: label.value.trim(), motion_key: motionKey.value, description: description.value.trim(),
      source_basis: sourceBasis.value.trim(), actor_id: 'user_local', confirmed: true,
    })
    if (!context.matches()) return
    if (reference.identity_id !== context.personId || reference.approval_state !== 'approved') throw new Error('动作保存结果与当前人物不符，请刷新动作列表。')
    savedReference.value = reference
    await store.loadMotions()
    if (!context.matches()) return
    const bound = await store.adoptMotion(reference.motion_key, reference.id)
    if (!context.matches()) return
    bindingFailed.value = !bound
    notice.value = bound ? '动作模板已保存，并已用于本期。' : '动作模板已保存，但本期绑定失败；请点击重试。'
    selectedUpload.value = { ...upload, state: 'confirmed', reference_id: reference.id }
  } catch (cause: any) {
    if (context.matches()) error.value = cause?.message ?? '动作确认失败，请检查视频状态后重试。'
  } finally {
    if (context.matches()) confirming.value = false
  }
}

async function useConfirmed() {
  const context = captureContext()
  const referenceId = savedReference.value?.id || selectedUpload.value?.reference_id
  if (!referenceId || confirming.value || store.busy) return
  confirming.value = true
  error.value = ''
  try {
    await store.loadMotions()
    if (!context.matches()) return
    const reference = store.motionCatalog?.approved.find(item => item.id === referenceId && item.identity_id === context.personId)
    if (!reference) { error.value = '此动作不在当前人物的可用动作中，请检查来源或审核状态。'; return }
    const bound = await store.adoptMotion(reference.motion_key, reference.id)
    if (context.matches()) { bindingFailed.value = !bound; notice.value = bound ? '动作模板已用于本期。' : '模板已保存，但本期绑定未成功，请重试。' }
  } finally { if (context.matches()) confirming.value = false }
}
</script>

<template>
  <section v-if="identityId" class="motion-upload">
    <button class="sm" :disabled="!!store.busy || confirming" @click="open ? closeForm() : open = true">
      {{ open ? '收起自定义动作' : '上传自定义动作' }}
    </button>
    <div v-if="open" class="motion-upload-panel">
      <p class="motion-help">上传 3–60 秒、100 MiB 以内的 MP4/MOV 作为表情、眨眼和头部动作参考。画面尽量正面、单人、面部清晰。只迁移头部与面部，不支持挥手等全身手势；长片循环时可能出现接缝。参考视频的音轨不参与成片。</p>
      <label class="upload-file"><span>选择动作参考视频</span><input type="file" accept="video/mp4,video/quicktime,.mp4,.mov" :disabled="uploading || confirming" @change="onFileChange" /></label>
      <div v-if="localFile" class="local-preview">
        <video :src="localUrl" controls playsinline preload="metadata" />
        <p>{{ localFile.name }} · {{ (localFile.size / 1024 / 1024).toFixed(1) }} MiB<span v-if="localDuration !== null"> · {{ localDuration.toFixed(1) }} 秒 · {{ localDimensions.width }}×{{ localDimensions.height }}</span></p>
      </div>
      <button v-if="localFile" class="sm" :disabled="!localReady || uploading || !!store.busy" @click="upload">{{ uploading ? (progress >= 100 ? '正在检查视频…' : `上传中 ${progress}%`) : '上传并检查' }}</button>
      <button v-if="uploading" class="sm" @click="closeForm">取消上传并收起</button>
      <p v-if="uploading" class="motion-help">取消会中止当前请求。若服务已接收视频，稍后可刷新查看上传结果。</p>
      <div v-if="uploading" class="upload-progress" role="progressbar" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${progress}%` }"></span></div>
      <p v-if="notice" class="motion-notice" role="status">{{ notice }}</p>
      <p v-if="error" class="motion-error" role="alert">{{ error }}</p>

      <div v-if="loading" class="motion-help">正在读取已上传动作…</div>
      <button v-if="!loading" class="sm refresh-upload" :disabled="!!store.busy || uploading || confirming" @click="loadUploads">刷新动作列表</button>
      <div v-if="uploads.length" class="upload-list">
        <button v-for="item in uploads" :key="item.id" class="upload-row" :class="{ selected: selectedUpload?.id === item.id }" :disabled="uploading || confirming" @click="chooseUpload(item)">
          <strong>{{ item.original_filename }}</strong><span>{{ item.state === 'pending' ? '待确认' : '已保存' }}</span>
        </button>
      </div>
      <article v-if="selectedUpload" class="upload-confirm">
        <video v-if="selectedPreview" :key="selectedUpload.id" :src="selectedPreview" controls playsinline preload="metadata" @playing="previewPlayed = true" @error="previewFailed = true" />
        <p v-if="previewFailed" class="motion-error" role="alert">预览视频无法播放，暂不能确认，请刷新或重新上传。</p>
        <p v-else-if="!selectedPreview" class="motion-help">后端尚未提供可播放预览，暂不能确认。</p>
        <p class="motion-help">{{ selectedUpload.duration_seconds.toFixed(1) }} 秒 · {{ selectedUpload.width }}×{{ selectedUpload.height }} · {{ (selectedUpload.size_bytes / 1024 / 1024).toFixed(1) }} MiB</p>
        <template v-if="selectedUpload.state === 'pending'">
          <label class="field"><span>模板名称</span><input v-model="label" type="text" maxlength="60" placeholder="例如：自然讲述微笑" /></label>
          <label class="field"><span>动作类型</span><select v-model="motionKey"><option v-for="option in motionOptions" :key="option.key" :value="option.key">{{ option.label }}</option></select></label>
          <label class="field"><span>动作说明（可选）</span><textarea v-model="description" maxlength="300" placeholder="例如：自然微笑，偶尔点头"></textarea></label>
          <label class="field"><span>视频来源依据</span><input v-model="sourceBasis" type="text" maxlength="300" placeholder="自己拍摄 / 已取得授权，写明具体来源" /></label>
          <label class="check-line"><input v-model="acknowledged" type="checkbox" :disabled="!previewPlayed || previewFailed || confirming" /><span>我已预览，确认有权将这段视频用于本项目的动作参考</span></label>
          <p v-if="!previewPlayed" class="motion-help">请先播放上方的视频，再勾选确认。</p>
          <button class="primary sm" :disabled="!canConfirm || !!store.busy" @click="confirmAndAdopt">{{ confirming ? '正在保存…' : '保存并用于本期' }}</button>
        </template>
        <template v-else-if="selectedUpload.state === 'confirmed'">
          <p class="motion-help">此视频已保存为个人动作模板；选用于本期不会重新上传或重复创建。</p>
          <button class="primary sm" :disabled="!selectedUpload.reference_id || !!store.busy || confirming" @click="useConfirmed">{{ confirming ? '正在设置…' : bindingFailed ? '重新用于本期' : '用于本期' }}</button>
        </template>
      </article>
    </div>
  </section>
</template>

<style scoped>
.motion-upload { margin-top: 14px; }
.motion-upload-panel { margin-top: 16px; padding: 16px; border: 1px solid var(--line); background: var(--panel); }
.motion-help, .motion-notice, .motion-error { margin: 10px 0; color: var(--fg-3); font-size: 12px; line-height: 1.7; }
.motion-notice { color: var(--fg-2); }
.motion-error { color: var(--red); }
.upload-file { display: inline-flex; align-items: center; gap: 10px; margin: 8px 0 14px; font-size: 13px; }
.upload-file input { max-width: 235px; font-size: 12px; }
.local-preview, .upload-confirm { margin: 12px 0; }
.local-preview video, .upload-confirm video { display: block; width: min(300px, 100%); max-height: 300px; background: #111; }
.local-preview p { font-size: 12px; color: var(--fg-3); }
.upload-progress { width: min(360px, 100%); height: 4px; margin: 10px 0; background: var(--panel-2); }
.upload-progress span { display: block; height: 100%; background: var(--blue); transition: width .15s ease; }
.upload-list { display: grid; gap: 6px; margin: 14px 0; }
.refresh-upload { margin: 8px 0 2px; }
.upload-row { display: flex; justify-content: space-between; gap: 12px; width: min(500px, 100%); padding: 9px 12px; border: 1px solid var(--line); background: transparent; text-align: left; }
.upload-row.selected { border-color: var(--blue); }
.upload-row span { color: var(--fg-3); font-size: 12px; }
.upload-confirm { padding-top: 14px; border-top: 1px solid var(--line); }
.upload-confirm .field { max-width: 500px; }
.check-line { display: flex; align-items: flex-start; gap: 8px; margin: 14px 0; font-size: 12px; line-height: 1.7; }
.check-line input { flex: none; margin-top: 4px; }
</style>
