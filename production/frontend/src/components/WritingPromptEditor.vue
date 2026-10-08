<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useContentStore } from '@/stores/content'
import type { ReferenceVisual } from '@/api/types'

const content = useContentStore()
const emit = defineEmits<{ generate: [force: boolean] }>()
const props = defineProps<{ blocked?: boolean; blockedReason?: string }>()
const prompt = computed(() => content.prompt)
const ref_ = computed(() => content.selectedReference)
const draft = ref('')
const dirty = computed(() => !!prompt.value && draft.value.trim() !== prompt.value.prompt_text.trim())
const busy = computed(() => content.busy || !!props.blocked)
const storyboardMode = computed(() => content.state?.brief.content_route === 'short_video_storyboard')
const referenceReady = computed(() => !!ref_.value && (ref_.value.transcript.state === 'ready'
  || (storyboardMode.value && (ref_.value.visual as ReferenceVisual | undefined)?.state === 'ready')))

watch(() => prompt.value?.id, () => { draft.value = prompt.value?.prompt_text ?? '' }, { immediate: true })

const canPrepare = computed(() => referenceReady.value && !busy.value)
const stale = computed(() => !!prompt.value && !!ref_.value && prompt.value.reference_snapshot_id !== ref_.value.id)
const canSave = computed(() => dirty.value && !!draft.value.trim() && !busy.value)
const progressText = computed(() => {
  const names: Record<string, string> = { search: '搜索参考', import: '导入参考', reference_revision: '保存转写',
    prompt: '准备提示', prompt_revision: '保存提示', generation: '生成三篇并审核', repair: '返工', review: '重新审核' }
  if (content.submittingKind) return `正在提交${names[content.submittingKind] ?? '任务'}请求，请稍候。`
  const job = content.activeJob
  if (job) {
    const stages: Record<string, string> = { plan: '拆解参考', write: '写稿', review: '独立审核', repair: '定向返工',
      queued: '等待处理', search: '搜索', detail: '取参考详情', transcript: '提取转写' }
    const stage = job.stage ? stages[job.stage] : ''
    return `正在处理任务：${names[job.kind] ?? '参考处理'}${stage ? ' · ' + stage : ''}，请稍候。`
  }
  return content.loading ? '正在刷新写作状态，请稍候。' : ''
})
const generationBlockedReason = computed(() => {
  if (progressText.value) return progressText.value
  if (props.blocked) return props.blockedReason || '请先完成前置步骤，再生成稿件。'
  if (!prompt.value) return '请先准备写作提示。'
  if (dirty.value && !draft.value.trim()) return '创作要求不能为空，请填写后保存。'
  if (dirty.value) return '有未保存的修改：保存后才会用于写稿。'
  if (stale.value) return '这份提示基于另一个参考，请重新准备后再生成。'
  if (ref_.value && !referenceReady.value) return '参考待解析，请先完成转写或画面分析。'
  if (!ref_.value && prompt.value.reference_snapshot_id) return '这份提示的主参考不可用，请重新选择参考并准备提示。'
  return ''
})
const canGenerate = computed(() => !generationBlockedReason.value)
const unknownGeneration = computed(() => [...(content.state?.recent_jobs ?? [])].reverse().find((job) =>
  job.kind === 'generation' && job.state === 'unknown' && job.prompt_id === prompt.value?.id
  && job.content_revision === content.revision))
const unknownWriteCall = computed(() => unknownGeneration.value?.calls?.find((call) =>
  call.step === 'write' && call.state === 'unknown' && !call.has_remote_handle))
const techniques = computed<string[]>(() => (prompt.value?.reference_analysis?.reusable_techniques as string[]) ?? [])
const hook = computed<string>(() => (prompt.value?.reference_analysis?.hook_function as string) ?? '')

async function saveDraft() { if (canSave.value) await content.savePrompt(draft.value) }
function generate(force: boolean) {
  if (!canGenerate.value) return
  if (force && unknownWriteCall.value) {
    const reserve = unknownWriteCall.value.reserved_fen
    const amount = typeof reserve === 'number' ? `上次预留 ¥${(reserve / 100).toFixed(2)} 仍待核对。` : '上次费用仍待核对。'
    const accepted = window.confirm(`上次写稿请求结果未确认，可能已经产生费用。${amount}重新生成会创建新请求，并按当前费用另行预留，可能重复收费；原任务和费用记录会保留。仍要继续吗？`)
    if (!accepted) return
  }
  emit('generate', force)
}
</script>

<template>
  <section class="section" aria-label="写作提示">
    <div class="section-head">
      <h2 class="section-title">写作提示</h2>
      <span v-if="prompt" class="faint">第 {{ prompt.version }} 版 · {{ prompt.source === 'edited' ? '你修改过' : '系统拆解生成' }}</span>
    </div>
    <div class="panel panel-pad">
      <p v-if="ref_ && !referenceReady" class="warn" role="status">参考待解析</p>
      <template v-if="!prompt">
        <p class="faint">根据所选参考拆解写法，并给出三个不同的原创角度和一份可编辑的写作要求。</p>
        <button class="primary" type="button" :disabled="!canPrepare" @click="content.preparePrompt()">拆解参考并准备提示</button>
        <p v-if="props.blocked && !progressText" class="faint" role="status">{{ props.blockedReason || '请先完成前置步骤，再准备提示。' }}</p>
        <p v-if="!ref_" class="faint">{{ storyboardMode ? '尚未选择可用的分镜主参考' : '尚未选择转写有效的主参考' }}</p>
      </template>
      <template v-else>
        <p v-if="stale" class="warn">这份提示基于另一个参考，需要重新准备后才能用来写稿。</p>
        <div v-if="hook || techniques.length" class="analysis">
          <p v-if="hook"><strong>参考的开头功能：</strong>{{ hook }}</p>
          <p v-if="techniques.length"><strong>可借鉴的手法：</strong>{{ techniques.join('、') }}</p>
        </div>
        <ol class="angles">
          <li v-for="a in prompt.angle_plan" :key="a.slot"><strong>{{ a.angle }}</strong><span v-if="a.new_viewpoint">：{{ a.new_viewpoint }}</span></li>
        </ol>
        <label class="field"><span>创作要求（可以直接改）</span>
          <textarea v-model="draft" rows="6" aria-label="创作要求"></textarea>
        </label>
        <div class="btn-row">
          <button class="sm" type="button" :disabled="!canSave" @click="saveDraft">保存为新版本</button>
          <button class="ghost sm" type="button" :disabled="!dirty" @click="draft = prompt.prompt_text">放弃修改</button>
          <button class="ghost sm" type="button" :disabled="!canPrepare" @click="content.preparePrompt()">按参考重新拆解</button>
        </div>
        <p v-if="!dirty" class="faint">当前提示已自动保存。修改后可保存为新版本。</p>
        <div class="btn-row generation">
          <button class="primary" type="button" :disabled="!canGenerate" @click="generate(false)">{{ storyboardMode ? '生成三篇分镜剧本' : '生成三篇口播稿' }}</button>
          <button v-if="content.batch || unknownWriteCall" class="ghost" type="button" :disabled="!canGenerate" @click="generate(true)">重新生成三篇</button>
        </div>
        <p v-if="generationBlockedReason && !progressText" class="faint" role="status">{{ generationBlockedReason }}</p>
      </template>
      <div class="operation-feedback" aria-live="polite">
        <p v-if="progressText" class="faint" role="status">{{ progressText }}</p>
        <p v-if="content.error" class="warn" role="alert">{{ content.error }}</p>
        <p v-else-if="content.notice" class="faint" role="status">{{ content.notice }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.analysis p { font-size: 13px; margin: 0 0 6px; color: var(--fg-2); }
.angles { padding-left: 18px; font-size: 13px; color: var(--fg-2); }
.warn { color: var(--red); font-size: 13px; }
.field textarea { width: 100%; line-height: 1.8; }
.generation { margin-top: 14px; }
.operation-feedback p { margin: 10px 0 0; font-size: 13px; }
</style>
