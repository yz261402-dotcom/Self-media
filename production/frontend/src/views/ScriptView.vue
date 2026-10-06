<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useStudioStore } from '@/stores/studio'
import { useContentStore } from '@/stores/content'
import StageProgress from '@/components/StageProgress.vue'
import HeroAction from '@/components/HeroAction.vue'
import ReferencesPanel from '@/components/ReferencesPanel.vue'
import ReferenceDetail from '@/components/ReferenceDetail.vue'
import WritingPromptEditor from '@/components/WritingPromptEditor.vue'
import ScriptCandidateCard from '@/components/ScriptCandidateCard.vue'
import type { ScriptVersion } from '@/api/types'

const store = useStudioStore()
const content = useContentStore()
const router = useRouter()
const topic = ref('')
const materialInput = ref('')
const materialTouched = ref(false)
const editing = ref<{ id: string; text: string } | null>(null)

watch(() => store.currentWorkId, (id) => {
  topic.value = store.currentWork?.brief.topic ?? ''
  materialTouched.value = false
  materialInput.value = (store.currentWork?.brief.materials ?? []).map(m => m.text).join('\n')
  editing.value = null
  // 切作品：作废旧作品的搜索结果与在途响应，并恢复新作品的持久任务（刷新后也能续上）
  if (id) void content.enter(id)
  else content.leave()
}, { immediate: true })

watch(() => store.currentWork?.brief.materials, (materials) => {
  if (!materialTouched.value) materialInput.value = (materials ?? []).map(m => m.text).join('\n')
}, { immediate: true, deep: true })

// 组件卸载：停止轮询、作废在途响应
onBeforeUnmount(() => content.leave())

const batch = computed(() => content.batch ?? store.scriptBatch)
const candidates = computed(() => batch.value?.ready_to_present === true ? batch.value.candidates : [])
const contentRoute = computed(() => content.state?.brief.content_route ?? store.currentWork?.brief.content_route ?? 'talking_script')
const storyboardMode = computed(() => contentRoute.value === 'short_video_storyboard')
const adoptedId = computed(() => store.task?.pending_selection_ids.includes('sel_script')
  ? null : store.selectedScript?.id ?? null)
const isPickable = (c: ScriptVersion) => (c.pickable ?? null) === null
  ? !c.quality_report.checks.some(x => x.hard_gate && x.outcome !== 'pass')
  : !!c.pickable
const passed = computed(() => batch.value?.passed ?? candidates.value.filter(isPickable).length)
const produced = computed(() => batch.value?.produced ?? candidates.value.length)
const requested = computed(() => batch.value?.requested ?? 3)
const materialLines = computed(() => materialInput.value.split('\n').map(s => s.trim()).filter(Boolean))
const sourceLabel = computed(() => {
  if (batch.value?.source_kind === 'llm') return '模型生成'
  if (batch.value?.source_kind === 'mock') return '模拟稿件，不可用于生产'
  return '历史批次，来源未标注'
})
const working = computed(() => content.busy || store.busy === '写稿')
const jobStageLabel = computed(() => {
  const j = content.activeJob
  if (!j) return ''
  const kind: Record<string, string> = { search: '搜索参考', import: '导入并提取转写', reference_revision: '保存转写', prompt: '拆解参考', prompt_revision: '保存提示',
    generation: '写稿与独立审核', repair: '按意见重写', review: '重新审核' }
  const stage: Record<string, string> = { plan: '拆解中', write: '写稿中', review: '独立审核中', repair: '定向返工中', detail: '取详情', transcript: '提取转写', search: '搜索中' }
  return `${kind[j.kind] ?? j.kind}${j.stage && stage[j.stage] ? ' · ' + stage[j.stage] : ''}`
})

function startEdit(c: ScriptVersion) { editing.value = { id: c.id, text: c.text } }
async function saveEdit() {
  if (!editing.value) return
  await store.reviseScript(editing.value.id, editing.value.text)
  editing.value = null
  await content.refresh()
}
async function adopt(c: ScriptVersion) {
  await store.selectScript(c.id, content.revision)
  await content.refresh()
}
async function chooseRoute(route: 'talking_script' | 'short_video_storyboard') {
  await content.setRoute(route)
  await store.syncWork(store.currentWorkId)
}
async function repair(c: ScriptVersion) {
  if (content.prompt) await content.repair(c)
  else { await store.repairScript(c.id); await content.refresh() }
}
async function generateDirect() {
  await store.generateScripts(topic.value || store.currentWork?.brief.topic || '', materialLines.value)
  await content.refresh()
}
async function generateFromPrompt(force: boolean) {
  await content.generate(force)
  await store.syncWork(store.currentWorkId)
}

/** Hero：当前该做什么。 */
const hero = computed(() => {
  if (!storyboardMode.value && !store.currentWork?.identity_id) {
    return { status: 'block' as const, title: '先选人物', desc: '为这期作品选一位主播，再回来写稿。', ctaText: '去选择人物' }
  }
  if (working.value) {
    return { status: 'run' as const, title: '正在处理', desc: jobStageLabel.value || '生成与审核中，请稍候。', ctaText: '处理中', ctaLoading: true }
  }
  if (adoptedId.value && store.selectedScript) {
    return { status: 'done' as const, title: `已采用：${store.selectedScript.title}`, desc: storyboardMode.value ? '分镜已选定，接下来准备镜头图片和 Wan 任务。' : '这篇稿件将用于全文配音。', ctaText: storyboardMode.value ? '下一步：镜头制作' : '下一步：试听配音' }
  }
  if (batch.value) {
    return {
      status: 'todo' as const,
      title: `已产出 ${produced.value}/${requested.value} 篇，${passed.value} 篇通过独立审核`,
      desc: batch.value.ready_to_present ? '三篇均已通过审核，请选择一篇。' : '三篇全部通过审核后展示；可以继续返工或重新生成。',
      ctaText: undefined,
    }
  }
  return { status: 'todo' as const, title: storyboardMode.value ? '写短视频分镜' : '写口播稿', desc: '可先搜索参考再写，也可以直接填主题生成。', ctaText: undefined }
})

function onHeroCta() {
  if (!storyboardMode.value && !store.currentWork?.identity_id) router.push('/avatar')
  else if (adoptedId.value && store.selectedScript) router.push(storyboardMode.value ? '/video' : '/tts')
}
</script>

<template>
  <div class="writing-page">
    <HeroAction
      :status="hero.status"
      :title="hero.title"
      :desc="hero.desc"
      :cta-text="hero.ctaText"
      :cta-loading="hero.ctaLoading"
      @cta="onHeroCta"
    />
    <section class="brief-form section" aria-label="写稿要求">
      <div class="content-route" role="group" aria-label="内容路线">
        <button :class="{ selected: !storyboardMode }" :aria-pressed="!storyboardMode" :disabled="working" @click="chooseRoute('talking_script')">口播稿</button>
        <button :class="{ selected: storyboardMode }" :aria-pressed="storyboardMode" :disabled="working" @click="chooseRoute('short_video_storyboard')">短视频分镜</button>
      </div>
      <label class="field">
        <span>这期讲什么</span>
        <textarea v-model="topic" rows="3" placeholder="例如：给刚接触 AI 的年轻人讲清楚 RAG，用生活中的例子，语气轻松。"></textarea>
      </label>
      <details class="reference-input">
        <summary>补充参考资料 <span class="faint">可选{{ materialLines.length ? ` · ${materialLines.length} 条` : '' }}</span></summary>
        <label class="field">
          <span>可核对的事实或原文，一行一条</span>
          <textarea v-model="materialInput" rows="4" placeholder="将需要引用的数字、价格、产品说明等贴在这里。" @input="materialTouched = true"></textarea>
        </label>
        <p class="field-help">不提供资料时，稿件会避开缺乏依据的数字、价格和机构结论。</p>
      </details>
      <div class="form-actions">
        <button v-if="!content.prompt" class="primary" :disabled="working || (!storyboardMode && !store.currentWork?.identity_id) || !(topic || store.currentWork?.brief.topic || '').trim()" @click="generateDirect">
          {{ working ? '正在生成…' : candidates.length ? '直接按主题重新生成' : '直接按主题生成三稿' }}
        </button>
        <button v-if="content.activeJob" class="ghost" @click="content.cancelActive()">取消当前任务</button>
        <span class="faint">{{ store.currentWork?.brief.target_duration_ms ? `目标 ${store.currentWork.brief.target_duration_ms / 1000} 秒` : '时长不限，按内容自然展开' }}<span v-if="store.currentWork?.brief.audience"> · {{ store.currentWork.brief.audience }}</span></span>
      </div>
      <p class="cost-note">{{ content.costNote }}</p>
      <p v-if="content.notice" class="batch-note" role="status">{{ content.notice }}</p>
      <p v-if="content.error" class="batch-note warning" role="alert">{{ content.error }}</p>
      <p v-if="batch?.shortfall_note" class="batch-note warning">{{ batch.shortfall_note }}</p>
      <p v-if="batch?.repair_note" class="batch-note">{{ batch.repair_note }}</p>
    </section>

    <ReferencesPanel />
    <ReferenceDetail />
    <WritingPromptEditor :blocked="working || (!storyboardMode && !store.currentWork?.identity_id)"
                         :blocked-reason="!storyboardMode && !store.currentWork?.identity_id ? '请先选择主播，再生成口播稿。' : '正在处理任务，请稍候。'"
                         @generate="generateFromPrompt" />

    <section v-if="content.activeJob" class="section" aria-label="写稿进度" aria-live="polite">
      <div class="section-head"><span class="section-title">{{ jobStageLabel || '处理中' }}</span></div>
      <div class="panel panel-pad faint">刷新页面不会丢失进度，也不会重复发起请求。</div>
    </section>
    <section v-else-if="store.task && store.busy === '写稿'" class="section" aria-label="写稿进度" aria-live="polite">
      <div class="section-head"><span class="section-title">正在写稿与检查</span></div>
      <div class="panel"><StageProgress :task="store.task" /></div>
    </section>

    <section class="section" aria-label="候选稿件">
      <div class="section-head">
        <h2 class="section-title">{{ storyboardMode ? '选择一篇分镜剧本' : '选择一篇口播稿' }}</h2>
        <span v-if="batch" class="section-note">已通过 {{ passed }} / 已产出 {{ produced }}（目标 {{ requested }}）</span>
        <span v-if="batch" class="source-note" :class="{ warning: batch.source_kind === 'mock' }">{{ sourceLabel }}</span>
      </div>
      <div v-if="candidates.length" class="script-options">
        <div v-for="(c, index) in candidates" :key="c.id" class="script-slot">
          <template v-if="editing?.id === c.id">
            <div class="edit-box">
              <textarea v-model="editing.text" class="script-editor" :aria-label="`修改${c.title}`"></textarea>
              <div class="btn-row edit-actions">
                <button class="primary sm" :disabled="working" @click="saveEdit">保存为新版本</button>
                <button class="ghost sm" @click="editing = null">取消</button>
              </div>
              <p class="faint">保存后是一个新的稿件版本，需要重新审核才能采用。</p>
            </div>
          </template>
          <ScriptCandidateCard v-else :candidate="c" :index="index" :adopted="adoptedId === c.id" :busy="working"
                               @adopt="adopt" @repair="repair" @edit="startEdit" />
        </div>
      </div>
      <div v-else-if="batch" class="writing-empty">
        <h3>等待三篇全部通过审核</h3>
        <p v-for="target in batch.retry_targets ?? []" :key="target.candidate_id">候选 {{ target.slot ?? '' }}：{{ target.reason }}</p>
        <div class="btn-row"><button :disabled="working" @click="content.repairPending()">继续返工</button><button :disabled="working" @click="content.rereview()">重新审核</button></div>
      </div>
      <div v-else class="writing-empty"><h3>候选稿会显示在这里</h3><p>搜索并选一个参考后准备提示，或直接填写主题生成。</p></div>
    </section>
  </div>
</template>

<style scoped>
.brief-form { padding: 22px 24px; border: 1px solid var(--line); background: var(--panel); }
.content-route { display: inline-flex; margin-bottom: 18px; border: 1px solid var(--line); }
.content-route button { border: 0; border-radius: 0; }
.content-route button.selected { background: var(--fg); color: var(--panel); }
.reference-input { margin: 16px 0; }
.reference-input summary { cursor: pointer; font-size: 13px; color: var(--fg-2); }
.reference-input .field { margin-top: 14px; }
.field-help, .batch-note, .cost-note { font-size: 12px; color: var(--fg-3); margin: 8px 0 0; }
.form-actions { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-top: 18px; }
.form-actions > span { font-size: 12px; }
.source-note { margin-left: auto; font-size: 12px; color: var(--fg-3); }
.warning { color: var(--red); }
.script-options { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--line); }
.script-slot { min-width: 0; display: flex; flex-direction: column; }
.script-slot + .script-slot { border-left: 1px solid var(--line); }
.edit-box { padding: 20px; background: var(--panel); flex: 1; }
.script-editor { width: 100%; min-height: 300px; line-height: 1.85; }
.edit-actions { margin-top: 10px; }
.writing-empty { padding: 44px 24px; border-top: 1px solid var(--line); color: var(--fg-3); }
.writing-empty h3 { font-size: 15px; font-weight: 500; color: var(--fg-2); margin-bottom: 8px; }
.writing-empty p { font-size: 13px; }
@media (max-width: 1200px) {
  .script-options { grid-template-columns: 1fr; }
  .script-slot + .script-slot { border-left: 0; border-top: 1px solid var(--line); }
}
@media (max-width: 600px) {
  .brief-form { padding: 18px 16px; }
  .source-note { width: 100%; margin-left: 0; }
}
</style>
