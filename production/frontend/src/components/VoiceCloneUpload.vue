<script setup lang="ts">
import { computed, onScopeDispose, ref, watch } from 'vue'
import { api } from '@/api'
import { useStudioStore } from '@/stores/studio'
import type { VoiceCloneResult } from '../api/types'

const store = useStudioStore()
const name = ref('')
const sourceEvidence = ref('')
const rightsAck = ref(false)
const activationCostAck = ref(false)
const file = ref<File | null>(null)
const duration = ref<number | null>(null)
const localUrl = ref('')
const requestId = ref('')
const state = ref('')
const submitting = ref(false)
const querying = ref(false)
const error = ref('')
const note = ref('')
const open = ref(false)
const savedVoiceName = ref('')
const localValidationRejected = ref(false)
const fileInputKey = ref(0)
let contextVersion = 0

const fileError = computed(() => {
  if (!file.value) return ''
  if (!/\.(mp3|m4a|wav)$/i.test(file.value.name)) return '请选择 MP3、M4A 或 WAV 音频。'
  if (!file.value.size || file.value.size > 20_000_000) return '音频须非空且不超过 20,000,000 字节。'
  if (duration.value !== null && (!Number.isFinite(duration.value) || duration.value < 10 || duration.value > 300)) return '参考音频须为 10–300 秒。'
  return ''
})
const canSubmit = computed(() => !!store.currentWork && !!name.value.trim() && !!sourceEvidence.value.trim()
  && !!file.value && !fileError.value && duration.value !== null && rightsAck.value && activationCostAck.value
  && !submitting.value && !querying.value && !requestId.value && !store.busy)
const unresolved = computed(() => !!requestId.value && !['succeeded', 'failed', 'blocked'].includes(state.value))
const statusText = computed(() => state.value === 'succeeded' ? '已创建候选音色'
  : ['failed', 'blocked'].includes(state.value) ? '克隆未完成' : submitting.value ? '正在提交克隆' : '结果待核对')
const storageKey = () => `avatar-studio:voice-clone:${store.currentWorkId ?? 'none'}`

function persistRequest() {
  try { sessionStorage.setItem(storageKey(), JSON.stringify({ request_id: requestId.value, state: state.value, name: name.value, local_validation_rejected: localValidationRejected.value })) } catch {
    // The request id remains visible even when browser storage is unavailable.
  }
}

function clearFile() {
  if (localUrl.value) URL.revokeObjectURL(localUrl.value)
  localUrl.value = ''
  file.value = null
  duration.value = null
  fileInputKey.value++
}

function captureContext() {
  const version = contextVersion
  const workId = store.currentWorkId
  const workVersion = store.workLoadVersion
  return { matches: () => version === contextVersion && store.workContextMatches(workId, workVersion) }
}

function resetContext() {
  contextVersion++
  clearFile()
  name.value = ''
  sourceEvidence.value = ''
  rightsAck.value = false
  activationCostAck.value = false
  requestId.value = ''
  state.value = ''
  error.value = ''
  note.value = ''
  savedVoiceName.value = ''
  localValidationRejected.value = false
  submitting.value = false
  querying.value = false
  open.value = false
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey()) ?? 'null')
    if (saved && typeof saved.request_id === 'string' && saved.request_id) {
      requestId.value = saved.request_id
      state.value = typeof saved.state === 'string' ? saved.state : 'unknown'
      name.value = typeof saved.name === 'string' ? saved.name : ''
      localValidationRejected.value = saved.local_validation_rejected === true
      note.value = '已恢复上次克隆作业，可查询原作业查看结果。'
      open.value = true
    }
  } catch { /* A missing local receipt never implies a successful clone. */ }
}

watch(() => [store.currentWorkId, store.workLoadVersion], resetContext, { immediate: true, flush: 'sync' })
onScopeDispose(() => { contextVersion++; clearFile() })

function chooseFile(event: Event) {
  if (requestId.value || submitting.value) return
  const selected = (event.target as HTMLInputElement).files?.[0]
  clearFile()
  error.value = ''
  if (!selected) return
  file.value = selected
  if (!fileError.value) localUrl.value = URL.createObjectURL(selected)
}

function readDuration(event: Event) {
  const audio = event.target as HTMLAudioElement
  if (audio.getAttribute('src') !== localUrl.value) return
  duration.value = audio.duration
}

function audioError(event: Event) {
  if ((event.target as HTMLAudioElement).getAttribute('src') !== localUrl.value) return
  duration.value = null
  error.value = '浏览器无法读取此音频，请换成可播放的 MP3、M4A 或 WAV 文件。'
}

function acceptResult(result: VoiceCloneResult) {
  if (!result?.clone_job || result.clone_job.request_id !== requestId.value) throw new Error('返回的克隆作业编号不一致，请查询原作业。')
  state.value = result.clone_job.state
  note.value = result.note ?? ''
  error.value = ''
  if (state.value === 'succeeded') {
    if (!result.voice?.id) { state.value = 'unknown'; throw new Error('作业返回成功但缺少音色，请查询原作业。') }
    const voice = result.voice
    const index = store.voices.findIndex(item => item.id === voice.id)
    if (index < 0) store.voices.push(voice)
    else store.voices.splice(index, 1, { ...store.voices[index], ...voice })
    savedVoiceName.value = voice.name
    note.value = '候选音色已加入音色库。请生成试听、登记基准，再确认并选用。'
    clearFile()
  } else if (['failed', 'blocked'].includes(state.value)) {
    const failure = result.clone_job.error
    error.value = typeof failure === 'string' ? failure : failure?.message || result.note || '克隆失败，请检查原作业结果。'
  }
  persistRequest()
}

async function submitClone() {
  if (!canSubmit.value || !file.value) return
  const context = captureContext()
  const selected = file.value
  const input = { name: name.value.trim(), filename: selected.name, source_evidence: sourceEvidence.value.trim(), rights_ack: rightsAck.value, activation_cost_ack: activationCostAck.value }
  submitting.value = true
  error.value = ''
  try {
    const bytes = new Uint8Array(await selected.arrayBuffer())
    if (!context.matches()) return
    let binary = ''
    for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768))
    const content_base64 = btoa(binary)
    requestId.value = crypto.randomUUID()
    state.value = 'submitted'
    persistRequest()
    const result = await api.createVoiceClone({ ...input, request_id: requestId.value, content_base64 })
    if (context.matches()) acceptResult(result)
  } catch (cause: any) {
    if (!context.matches()) return
    error.value = cause?.message ?? '克隆提交未完成。'
    localValidationRejected.value = cause?.status === 422
    if (requestId.value) { state.value = 'unknown'; persistRequest() }
  } finally {
    if (context.matches()) submitting.value = false
  }
}

async function queryOriginal() {
  if (!requestId.value || submitting.value || querying.value) return
  const context = captureContext()
  const id = requestId.value
  querying.value = true
  error.value = ''
  try {
    const job = await api.getVoiceClone(id)
    if (!context.matches() || requestId.value !== id) return
    let voice = null
    if (job.state === 'succeeded' && job.voice_asset_id) {
      const voices = await api.listVoices()
      if (!context.matches() || requestId.value !== id) return
      voice = voices.find(item => item.id === job.voice_asset_id) ?? null
    }
    acceptResult({ voice, clone_job: job })
  } catch (cause: any) {
    if (context.matches()) {
      error.value = cause?.message ?? '暂时无法核对，请保留原作业编号后重试查询。'
      if (cause?.status === 404 && localValidationRejected.value) {
        state.value = 'failed'
        note.value = '此前请求未通过本地校验，查询确认没有作业。可修正音频后重新创建。'
        persistRequest()
      }
    }
  } finally {
    if (context.matches()) querying.value = false
  }
}

function startNew() {
  if (unresolved.value || submitting.value || querying.value) return
  try { sessionStorage.removeItem(storageKey()) } catch { /* Local receipt only. */ }
  resetContext()
  open.value = true
}
</script>

<template>
  <details class="clone-panel" :open="open" @toggle="open = ($event.target as HTMLDetailsElement).open">
    <summary>克隆我的音色</summary>
    <div class="clone-content">
      <p class="clone-help">上传一位说话人的清晰录音，创建自己的候选音色。支持 MP3、M4A、WAV，10–300 秒，最多 20 MB。</p>
      <p class="clone-cost">首次使用克隆音色合成语音收取 9.9 元克隆费，另加语音合成费。创建后需在 7 天内正式使用以保留音色；创建本身不会自动合成试听。</p>
      <p v-if="!store.currentWork" class="clone-help">请先选择作品。</p>
      <fieldset :disabled="!!requestId || submitting || querying || !!store.busy || !store.currentWork">
        <label class="clone-field"><span>音色名称</span><input v-model="name" maxlength="80" placeholder="例如：我的口播声音" /></label>
        <label class="clone-field"><span>真人参考音频</span><input :key="fileInputKey" type="file" accept=".mp3,.m4a,.wav,audio/mpeg,audio/mp4,audio/wav" @change="chooseFile" /></label>
        <p v-if="file" class="clone-help">{{ file.name }} · {{ (file.size / 1_000_000).toFixed(2) }} MB<span v-if="duration !== null"> · {{ duration.toFixed(1) }} 秒</span></p>
        <audio v-if="localUrl" :key="localUrl" :src="localUrl" controls preload="metadata" @loadedmetadata="readDuration" @error="audioError"></audio>
        <p v-if="fileError" class="clone-error" role="alert">{{ fileError }}</p>
        <label class="clone-field"><span>音频来源与使用依据</span><textarea v-model="sourceEvidence" rows="2" placeholder="例如：本人录制，或已获得说话人的声音克隆、配音及商业使用授权"></textarea></label>
        <label class="clone-ack"><input v-model="rightsAck" type="checkbox" /><span>这是我本人的声音，或我已取得说话人的声音克隆、本项目配音及商业使用授权。</span></label>
        <label class="clone-ack"><input v-model="activationCostAck" type="checkbox" /><span>我已了解首次使用的 9.9 元克隆费、另计的合成费及 7 天使用期限。</span></label>
      </fieldset>
      <p v-if="error" class="clone-error" role="alert">{{ error }}</p>
      <p v-if="note" class="clone-help" role="status">{{ note }}</p>
      <template v-if="requestId">
        <div class="clone-receipt"><strong>{{ statusText }}{{ savedVoiceName ? ` · ${savedVoiceName}` : '' }}</strong><span>作业编号：{{ requestId }}</span></div>
        <p v-if="unresolved" class="clone-help">请查询原作业核对结果；结果明确前不能再次创建。</p>
        <div class="btn-row">
          <button class="sm" :disabled="submitting || querying" @click="queryOriginal">{{ querying ? '正在查询…' : '查询原克隆作业' }}</button>
          <button v-if="!unresolved" class="sm" :disabled="submitting || querying" @click="startNew">创建另一音色</button>
        </div>
      </template>
      <button v-else class="sm primary-sm" :disabled="!canSubmit" @click="submitClone">{{ submitting ? '正在读取音频…' : '创建候选音色' }}</button>
    </div>
  </details>
</template>

<style scoped>
.clone-panel { border: 1px solid var(--line); border-radius: 8px; margin: 0 0 18px; background: var(--panel); }
summary { padding: 12px 14px; cursor: pointer; font-size: 13px; font-weight: 600; color: var(--fg); }
.clone-content { padding: 0 14px 16px; }
.clone-help, .clone-cost { color: var(--fg-2); font-size: 12px; line-height: 1.7; margin: 0 0 12px; }
.clone-cost { padding: 10px 12px; background: var(--panel-2); border-left: 2px solid var(--line-2); }
fieldset { margin: 0; padding: 0; border: 0; min-width: 0; }
.clone-field { display: grid; gap: 6px; margin-bottom: 12px; font-size: 12px; color: var(--fg-2); }
.clone-field input, textarea { width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--fg); font: inherit; }
.clone-ack { display: flex; align-items: flex-start; gap: 8px; margin: 12px 0; font-size: 12px; line-height: 1.7; color: var(--fg-2); }
.clone-ack input { flex: none; margin-top: 4px; }
audio { width: 100%; margin-bottom: 12px; }
.clone-error { font-size: 12px; line-height: 1.7; color: var(--red, #b63535); margin: 10px 0; }
.clone-receipt { display: grid; gap: 5px; margin: 12px 0; font-size: 12px; overflow-wrap: anywhere; }
.clone-receipt span { font-size: 11px; color: var(--fg-3); }
</style>
