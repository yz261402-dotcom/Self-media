<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useContentStore } from '@/stores/content'
import type { ReferenceVisual } from '@/api/types'

const content = useContentStore()
const ref_ = computed(() => content.inspecting ? null : content.inspectedReference ?? content.selectedReference)
const selectedId = computed(() => content.selectedReference?.id)
const importedList = computed(() => content.state?.references ?? [])
const editing = ref(false)
const draft = ref('')

watch(() => ref_.value?.id, () => { editing.value = false; draft.value = ref_.value?.transcript.text ?? '' }, { immediate: true })

const TRANSCRIPT_LABEL: Record<string, string> = {
  ready: '转写有效', failed: '转写失败', none: '没有转写', pending: '转写中', unknown: '转写结果未确认',
}
const sourceLabel = computed(() => {
  const t = ref_.value?.transcript
  if (!t) return ''
  if (t.source === 'user_edit') return '你修正过的转写'
  if (t.source === 'redfox') return '红狐提取的转写'
  if (t.source === 'mock') return '演示转写（非真实内容）'
  return t.source || '来源未知'
})
const busy = computed(() => content.busy || content.uploading)
const isStoryboard = computed(() => content.state?.brief?.content_route === 'short_video_storyboard')

// ---- 画面分析（独立付费调用，需用户确认费用） ----
const CEILING_DEFAULT = 80
const fileInput = ref<HTMLInputElement | null>(null)
const confirmed = ref(false)
const retryConfirmed = ref(false)
const ceilingYuan = ref(CEILING_DEFAULT / 100)
const media = computed(() => (ref_.value ? content.mediaByRef[ref_.value.id] : undefined))
const mediaId = computed(() => media.value?.id ?? ref_.value?.media_asset_id ?? '')
const visual = computed(() => {
  const v = ref_.value?.visual as { state?: string } | undefined
  return v && v.state ? (v as ReferenceVisual) : null
})
const visualJob = computed(() => ref_.value ? content.visualJobForReference(ref_.value.id, mediaId.value) : null)
const visualPending = computed(() => !!visualJob.value && ['queued', 'running', 'waiting_external'].includes(visualJob.value.state))
const visualError = computed(() => {
  if (!ref_.value) return ''
  if (visualJob.value) return content.visualErrorForReference(ref_.value.id, mediaId.value)
  if (visual.value?.media_asset_id && visual.value.media_asset_id !== mediaId.value) return ''
  return visual.value?.error?.message ?? (visual.value?.state === 'unknown' ? '上次请求可能已经发出，系统不会自动重发，请先核对。' : '')
})
const canAnalyze = computed(() => !!ref_.value)
const mediaChanged = computed(() => visual.value?.state === 'ready' && !!mediaId.value
  && mediaId.value !== visual.value.media_asset_id)
const currentQuote = computed(() => content.visualQuoteBinding?.referenceId === ref_.value?.id
  && content.visualQuoteBinding?.mediaId === mediaId.value ? content.visualQuote : null)
const retryUnknownCall = computed(() => currentQuote.value?.retry_unknown_call)
const analysisModel = computed(() => currentQuote.value?.call_list?.model ?? '')
const ceilingFen = computed(() => Math.round(Number(ceilingYuan.value) * 100))
const ceilingOk = computed(() => Number.isFinite(ceilingFen.value) && ceilingFen.value >= (currentQuote.value?.reserve_fen ?? CEILING_DEFAULT))
function resetVisualConfirmations() { confirmed.value = false; retryConfirmed.value = false }
watch(() => ref_.value?.id, () => { resetVisualConfirmations(); ceilingYuan.value = CEILING_DEFAULT / 100 }, { flush: 'sync' })
watch([() => ref_.value?.id, () => mediaId.value], ([referenceId, id]) => {
  resetVisualConfirmations()
  if (referenceId && id) void content.loadVisualQuote(referenceId, id)
}, { immediate: true, flush: 'sync' })
// 重新读取报价、原调用变化或用户修改费用上限都撤销之前的勾选，避免旧确认授权另一次调用。
watch([() => content.visualQuote, () => content.visualQuote?.retry_unknown_call?.call_id,
  () => content.visualQuote?.request_digest, () => ceilingFen.value], resetVisualConfirmations, { flush: 'sync' })
watch([() => visualJob.value?.job_id, () => visualJob.value?.state], ([, state], previous) => {
  if (!previous || !['unknown', 'failed', 'blocked'].includes(state ?? '')) return
  resetVisualConfirmations()
  if (ref_.value && mediaId.value) void content.loadVisualQuote(ref_.value.id, mediaId.value)
})

async function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file && ref_.value) {
    resetVisualConfirmations()
    fetchConfirmed.value = false
    await content.uploadReferenceMedia(ref_.value.id, file)
  }
  input.value = ''
}
async function analyze() {
  if (busy.value || !ref_.value || !mediaId.value || !currentQuote.value || !confirmed.value || !ceilingOk.value
    || (retryUnknownCall.value && !retryConfirmed.value)) return
  const retry = retryUnknownCall.value
  const input = retry ? { retry_unknown_call_id: retry.call_id, retry_paid_ack: true } : undefined
  try {
    await content.analyzeReferenceVisual(ref_.value.id, mediaId.value, ceilingFen.value, input)
  } finally { resetVisualConfirmations() }
}
const fetchConfirmed = ref(false)
const fetchCeilingYuan = ref(1)
const fetchCeilingFen = computed(() => Math.round(Number(fetchCeilingYuan.value) * 100))
const fetchOk = computed(() => Number.isFinite(fetchCeilingFen.value) && fetchCeilingFen.value >= (content.fetchQuote?.reserve_fen ?? 50))
watch(() => ref_.value?.id, (id) => { fetchConfirmed.value = false; if (id) void content.loadMediaFetchQuote(id) }, { immediate: true })
async function fetchMedia() {
  if (busy.value || !ref_.value || !fetchConfirmed.value || !fetchOk.value) return
  await content.fetchReferenceMedia(ref_.value.id, fetchCeilingFen.value)
  fetchConfirmed.value = false
}
const mmss = (ms: number) => `${(ms / 1000).toFixed(1)}s`
const PRESENCE: Record<string, string> = { none: '没有', present: '有', music: '有音乐', uncertain: '无法判断', '': '未知' }
const VISUAL_LABEL: Record<string, string> = { none: '未分析画面', ready: '画面分析完成', failed: '画面分析失败', unknown: '画面分析结果未确认', pending: '画面分析中' }
type RefRow = { id: string; transcript_state: string; visual_state?: string }
function refBadge(r: RefRow) {
  if (r.transcript_state === 'ready') return '有口播'
  if (r.visual_state === 'ready') return '画面已分析'
  if (r.transcript_state === 'failed' || r.transcript_state === 'none') return '无口播，可分析画面'
  return TRANSCRIPT_LABEL[r.transcript_state] ?? r.transcript_state
}
function canUse(r: RefRow) {
  return r.transcript_state === 'ready' || (isStoryboard.value && r.visual_state === 'ready')
}
function useLabel(r: RefRow) {
  if (selectedId.value === r.id) return '当前主参考'
  if (canUse(r)) return '设为主参考'
  return isStoryboard.value ? '需要口播或画面分析' : '没有有效口播'
}

async function save() {
  if (!ref_.value || !draft.value.trim()) return
  await content.saveTranscript(ref_.value.id, draft.value.trim())
  editing.value = false
}
</script>

<template>
  <section class="section" aria-label="参考详情">
    <div class="section-head">
      <h2 class="section-title">参考详情</h2>
      <span v-if="importedList.length" class="faint">已导入 {{ importedList.length }} 条，选一条作为主参考</span>
    </div>
    <div class="panel panel-pad">
      <ul v-if="importedList.length" class="imported">
        <li v-for="r in importedList" :key="r.id" :class="{ current: selectedId === r.id }">
          <span class="cap">{{ r.caption || r.platform_work_id }}</span>
          <span class="faint">{{ refBadge(r) }}<template v-if="r.parent_id"> · {{ r.visual_state === 'ready' ? '含画面分析' : '修正版' }}</template></span>
          <div class="btn-row">
            <button class="ghost sm" type="button" :disabled="content.inspecting" @click="content.inspectReference(r.id)">查看参考</button>
            <button class="sm" type="button" :disabled="busy || selectedId === r.id || !canUse(r)" @click="content.useReference(r.id)">
              {{ useLabel(r) }}
            </button>
          </div>
        </li>
      </ul>
      <p v-else class="faint">还没有导入参考。先在上方搜索并“导入参考”。也可以不选参考，直接凭主题写。</p>
      <p v-if="content.inspecting" class="faint" role="status">正在读取参考详情</p>

      <template v-if="ref_">
        <div class="cols">
          <div>
            <h3>发布简介</h3>
            <p class="text">{{ ref_.caption || '（无）' }}</p>
            <p class="faint">简介是作者发布时写的文案，和口播转写是两回事，分开保存。</p>
          </div>
          <div>
            <h3>口播转写 <span class="badge">{{ TRANSCRIPT_LABEL[ref_.transcript.state] }}</span></h3>
            <p class="faint">{{ sourceLabel }}<template v-if="ref_.parent_id"> · 原始转写已保留</template></p>
            <template v-if="editing">
              <textarea v-model="draft" rows="8" class="editor" aria-label="修正转写"></textarea>
              <div class="btn-row">
                <button class="primary sm" type="button" :disabled="busy || !draft.trim()" @click="save">保存为新版本</button>
                <button class="ghost sm" type="button" @click="editing = false">取消</button>
              </div>
            </template>
            <template v-else>
              <p v-if="ref_.transcript.text" class="text transcript">{{ ref_.transcript.text }}</p>
              <p v-else class="faint">没有有效口播转写。</p>
              <button class="ghost sm" type="button" :disabled="busy" @click="editing = true; draft = ref_.transcript.text">修正转写</button>
            </template>
          </div>
        </div>
        <div v-if="canAnalyze || visual" class="visual">
          <h3>画面分析 <span class="badge">{{ VISUAL_LABEL[visual?.state ?? 'none'] }}</span></h3>
          <template v-if="visual?.state === 'ready'">
            <p class="text">{{ visual.summary }}</p>
            <p class="faint">
              人声：{{ PRESENCE[visual.speech_presence ?? ''] }} · 音乐：{{ PRESENCE[visual.music_presence ?? ''] }} ·
              BPM：{{ visual.audio?.tempo_bpm ? visual.audio.tempo_bpm : '未识别' }} · 时长 {{ mmss(visual.duration_ms ?? 0) }} ·
              {{ visual.model }}
            </p>
            <ol class="scenes">
              <li v-for="sc in visual.scenes" :key="sc.index">
                <strong>{{ mmss(sc.start_ms) }}–{{ mmss(sc.end_ms) }}</strong>
                <span class="faint"> {{ sc.shot_type || '景别未知' }} · {{ sc.camera_motion || '运镜未知' }}<template v-if="sc.confidence !== null"> · 置信度 {{ Math.round(sc.confidence * 100) }}%</template></span>
                <p class="text">{{ sc.description }}</p>
                <p v-if="sc.actions.length" class="faint">动作：{{ sc.actions.join('、') }}</p>
                <p v-if="sc.on_screen_text.length" class="faint">画面文字：{{ sc.on_screen_text.join('、') }}</p>
              </li>
            </ol>
            <p v-for="w in visual.warnings" :key="w" class="faint">注意：{{ w }}</p>
            <p class="faint">这是模型对画面的观察，只作灵感，不是事实，也不是口播转写。</p>
          </template>
          <template v-if="canAnalyze">
            <p v-if="mediaChanged" class="faint" role="status">视频已更换，当前展示的是旧视频的解析。请重新分析后使用新参考。</p>
            <p v-else-if="visual?.state === 'ready'" class="faint">可以更换完整视频或重新分析，原解析会保留为历史版本。</p>
            <p v-if="!mediaId" class="faint">
              没有可分析的视频文件。抖音页面链接不能当作视频文件，请上传你有权使用的原视频（MP4/MOV/WebM，200 MiB 内，10 分钟内）。
            </p>
            <div class="confirm">
              <p class="faint">也可以让系统通过红狐自动获取这条视频（独立付费调用，只保存到本地用于画面分析；视频版权由你确认）。预留约 ¥{{ ((content.fetchQuote?.reserve_fen ?? 50) / 100).toFixed(2) }}<template v-if="!content.fetchQuote?.price_verified">，实际费用以红狐账单为准</template>。</p>
              <label class="field"><span>获取视频的费用上限（元）</span>
                <input v-model.number="fetchCeilingYuan" type="number" min="0.5" step="0.1" aria-label="获取视频费用上限" />
              </label>
              <label class="check"><input v-model="fetchConfirmed" type="checkbox" aria-label="确认获取视频费用" /> 我确认产生这笔费用，并有权使用这条视频</label>
              <button class="ghost sm" type="button" :disabled="busy || !fetchConfirmed || !fetchOk" @click="fetchMedia">{{ mediaId ? '重新获取视频' : '自动获取视频' }}</button>
            </div>
            <div class="btn-row">
              <input ref="fileInput" type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm" class="hidden"
                     :disabled="busy || content.uploading" aria-label="上传参考视频" @change="onFile" />
              <button class="ghost sm" type="button" :disabled="busy || content.uploading" @click="fileInput?.click()">
                {{ content.uploading ? '上传中…' : mediaId ? '换一个视频' : '上传参考视频' }}
              </button>
              <span v-if="media" class="faint">已保存：{{ mmss(media.duration_ms) }} · {{ media.width }}×{{ media.height }}</span>
            </div>
            <div v-if="mediaId" class="confirm">
              <p v-if="analysisModel" class="faint">本次解析模型：{{ analysisModel }}</p>
              <p class="faint">画面分析是独立的付费调用（抽取关键帧后调用视觉模型），费用和版权由你确认。预留约 ¥{{ ((currentQuote?.reserve_fen ?? CEILING_DEFAULT) / 100).toFixed(2) }}<template v-if="!currentQuote?.price_verified">，实际费用以供应商账单为准</template>。</p>
              <label class="field"><span>费用上限（元）</span>
                <input v-model.number="ceilingYuan" type="number" min="0.8" step="0.1" aria-label="费用上限" />
              </label>
              <label class="check"><input v-model="confirmed" type="checkbox" aria-label="确认费用" /> 我确认产生这笔费用，并有权使用这条视频</label>
              <template v-if="retryUnknownCall">
                <p class="visual-error">上次结果和费用未知，重新分析会产生一笔新费用。</p>
                <label class="check"><input v-model="retryConfirmed" type="checkbox" aria-label="确认重新分析费用" /> 我接受上次可能已计费，并确认重新分析产生的新费用</label>
              </template>
              <p v-if="!currentQuote" class="faint">正在读取本次画面分析的费用信息，请等待。</p>
              <p v-if="visualPending" class="faint" role="status">正在分析画面，系统会继续查询进度。</p>
              <button class="primary sm" type="button" :disabled="busy || !currentQuote || !confirmed || !ceilingOk || (!!retryUnknownCall && !retryConfirmed)" @click="analyze">{{ visualPending ? '分析画面中…' : retryUnknownCall ? '确认费用后重新分析' : visual?.state === 'ready' ? '重新分析画面' : '分析画面' }}</button>
            </div>
            <p v-if="visualError" class="visual-error" role="alert">{{ visualError }}</p>
          </template>
        </div>
        <p class="faint evidence">这条参考只作灵感：其中的数字和结论不会被当成已核实事实；需要引用的事实请放进“补充参考资料”。</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.imported { list-style: none; padding: 0; margin: 0 0 14px; }
.imported li { display: flex; gap: 12px; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--line); }
.imported li.current { font-weight: 600; }
.imported .cap { flex: 1; overflow-wrap: anywhere; font-size: 13px; }
.cols { display: grid; grid-template-columns: 1fr 2fr; gap: 24px; margin-top: 8px; }
h3 { font-size: 13px; margin: 0 0 6px; }
.text { font-size: 14px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 8px; }
.transcript { max-height: 260px; overflow-y: auto; }
.badge { font-weight: 400; font-size: 11px; color: var(--fg-3); margin-left: 6px; }
.editor { width: 100%; line-height: 1.8; }
.evidence { margin-top: 14px; }
.visual { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--line); }
.visual-error { font-size: 13px; line-height: 1.7; color: var(--red); overflow-wrap: anywhere; }
.scenes { padding-left: 18px; margin: 8px 0; }
.scenes li { margin-bottom: 8px; }
.confirm { margin-top: 10px; display: grid; gap: 8px; max-width: 420px; }
.check { font-size: 13px; display: flex; gap: 6px; align-items: center; }
.hidden { display: none; }
@media (max-width: 800px) { .cols { grid-template-columns: 1fr; } }
</style>
