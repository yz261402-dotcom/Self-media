<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useStudioStore } from '@/stores/studio'
import { useContentStore } from '@/stores/content'
import StageProgress from '@/components/StageProgress.vue'
import HeroAction from '@/components/HeroAction.vue'
import ReferencesPanel from '@/components/ReferencesPanel.vue'
import ReferenceDetail from '@/components/ReferenceDetail.vue'
import WritingPromptEditor from '@/components/WritingPromptEditor.vue'
import ScriptCandidateCard from '@/components/ScriptCandidateCard.vue'
import ManualStoryboardCreator from '@/components/ManualStoryboardCreator.vue'
import type { ScriptVersion, StoryboardShot } from '@/api/types'

const store = useStudioStore()
const content = useContentStore()
const router = useRouter()
const route = useRoute()
const topic = ref('')
const materialInput = ref('')
const materialTouched = ref(false)
type EditingDraft = { id: string; text: string; title?: string; description?: string; storyboard?: StoryboardShot[] }
const editing = ref<EditingDraft | null>(null)
const showManual = ref(false)

watch(() => store.currentWorkId, (id) => {
  topic.value = store.currentWork?.brief.topic ?? ''
  materialTouched.value = false
  materialInput.value = (store.currentWork?.brief.materials ?? []).map(m => m.text).join('\n')
  editing.value = null
  showManual.value = false
  // 切作品：作废旧作品的搜索结果与在途响应，并恢复新作品的持久任务（刷新后也能续上）
  if (id) void content.enter(id)
  else content.leave()
}, { immediate: true })

watch(() => store.currentWork?.brief.materials, (materials) => {
  if (!materialTouched.value) materialInput.value = (materials ?? []).map(m => m.text).join('\n')
}, { immediate: true, deep: true })

// 组件卸载：停止轮询、作废在途响应
let reviewWindow: Window | null = null
let reviewRefreshTimer: ReturnType<typeof setTimeout> | undefined
let reviewWorkSync: { workId: string; promise: Promise<void> } | null = null
onBeforeUnmount(() => {
  content.leave()
  clearTimeout(reviewRefreshTimer)
  window.removeEventListener('message', onReviewMessage)
  window.removeEventListener('storage', onReviewStorage)
  window.removeEventListener('focus', onReviewFocus)
})

const batch = computed(() => content.batch ?? store.scriptBatch)
const candidates = computed(() => batch.value?.candidates ?? [])
const manualBatch = computed(() => batch.value?.source_kind === 'manual')
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
const batchGuidance = computed(() => {
  if (manualBatch.value) return adoptedId.value ? '人工分镜已采用，可以进入镜头制作。' : '人工分镜已保存，请进入人工审核评分并确认采用。'
  if (batch.value?.ready_to_present) return storyboardMode.value ? '智能体审核已完成，请进入人工审核后采用一篇。' : '三篇均已通过审核，请选择一篇。'
  const unpassed = candidates.value.filter(c => !isPickable(c))
  const failedCount = unpassed.filter(c => c.review_state === 'failed').length
  const reviewingCount = unpassed.filter(c => c.review_state === 'reviewing').length
  const pendingCount = unpassed.length - failedCount - reviewingCount
  const messages = []
  if (candidates.value.some(isPickable)) messages.push(storyboardMode.value ? '已通过稿件可进入人工审核。' : '已通过稿件可以直接采用。')
  if (failedCount) messages.push(`${failedCount} 篇审核未通过，请查看修改项。`)
  if (pendingCount) messages.push(`${pendingCount} 篇待重新审核。`)
  if (reviewingCount) messages.push(`${reviewingCount} 篇独立审核中。`)
  return messages.join('') || '尚未产出稿件，请查看任务进度。'
})
const materialLines = computed(() => materialInput.value.split('\n').map(s => s.trim()).filter(Boolean))
const sourceLabel = computed(() => {
  if (batch.value?.source_kind === 'llm') return '模型生成'
  if (batch.value?.source_kind === 'mock') return '模拟稿件，不可用于生产'
  if (batch.value?.source_kind === 'manual') return '人工创作 · 模型审核未执行'
  return '历史批次，来源未标注'
})
const working = computed(() => content.busy || store.busy === '写稿')
const editingBusy = computed(() => content.loading || !!content.submittingKind || store.busy === '写稿'
  || !!(content.activeJob && content.activeJob.stage !== 'review'))
const jobStageLabel = computed(() => {
  const j = content.activeJob ?? content.waitingImportJob
  if (!j) return ''
  if (j.kind === 'import' && j.state === 'unknown') return '参考转写 · 查询已暂停'
  if (j.kind === 'import' && j.state === 'waiting_external') return '参考导入 · 等待转写结果'
  const kind: Record<string, string> = { search: '搜索参考', import: '导入并提取转写', reference_revision: '保存转写', prompt: '拆解参考', prompt_revision: '保存提示',
    generation: '写稿与独立审核', repair: '重写与独立审核', review: '重新审核' }
  const stage: Record<string, string> = { plan: '拆解中', write: '写稿中', review: '独立审核中', repair: '定向返工中', detail: '取详情', transcript: '提取转写', search: '搜索中' }
  return `${kind[j.kind] ?? j.kind}${j.stage && stage[j.stage] ? ' · ' + stage[j.stage] : ''}`
})

function startEdit(c: ScriptVersion) {
  editing.value = c.content_route === 'short_video_storyboard'
    ? { id: c.id, text: c.text, title: c.title, description: c.description,
        storyboard: (c.storyboard ?? []).map((shot) => ({ ...shot })) }
    : { id: c.id, text: c.text }
}
async function saveEdit() {
  if (!editing.value) return
  const draft = editing.value
  const saved = await store.reviseScript(draft.id, draft.storyboard
    ? { title: draft.title, description: draft.description, storyboard: draft.storyboard }
    : draft.text)
  if (!saved) return
  editing.value = null
  await content.refresh()
}
async function adopt(c: ScriptVersion) {
  if (storyboardMode.value) {
    if (!store.currentWorkId || working.value) return
    const destination = { name: 'human-review', params: { workId: store.currentWorkId, scriptId: c.id } }
    const href = router.resolve(destination).href
    reviewWindow = window.open(href, '_blank')
    if (!reviewWindow) await router.push(destination)
    return
  }
  await store.selectScript(c.id, content.revision)
  await content.refresh()
}

async function refreshAfterHumanReview(workId: string) {
  if (workId !== store.currentWorkId || workId !== content.workId) return
  const generation = content.generation
  await content.refresh(generation)
  if (generation !== content.generation || workId !== store.currentWorkId) return
  await syncReviewWork(workId)
  if (generation === content.generation && content.activeJob) content.startPolling()
}

function syncReviewWork(workId: string): Promise<void> {
  if (reviewWorkSync?.workId === workId) return reviewWorkSync.promise
  const promise = store.syncWork(workId).finally(() => {
    if (reviewWorkSync?.promise === promise) reviewWorkSync = null
  })
  reviewWorkSync = { workId, promise }
  return promise
}

function queueReviewRefresh(value: unknown) {
  if (!value || typeof value !== 'object') return
  const event = value as { type?: string; workId?: string; action?: string }
  if (event.type !== 'avatar-studio:human-review-complete' || event.workId !== store.currentWorkId
    || !['adopt', 'edit_adopt', 'rewrite'].includes(event.action ?? '')) return
  const workId = event.workId!
  clearTimeout(reviewRefreshTimer)
  reviewRefreshTimer = setTimeout(() => { void refreshAfterHumanReview(workId) }, 100)
}

function onReviewMessage(event: MessageEvent) {
  if (event.origin === window.location.origin) queueReviewRefresh(event.data)
}
function onReviewStorage(event: StorageEvent) {
  if (event.key !== 'avatar-studio:human-review-complete' || !event.newValue) return
  try { queueReviewRefresh(JSON.parse(event.newValue)) } catch { /* Ignore unrelated or malformed storage messages. */ }
}
function onReviewFocus() {
  if (reviewWindow && store.currentWorkId) void refreshAfterHumanReview(store.currentWorkId)
}
onMounted(() => {
  window.addEventListener('message', onReviewMessage)
  window.addEventListener('storage', onReviewStorage)
  window.addEventListener('focus', onReviewFocus)
})
watch([() => route.query.workId, () => store.apiReady], ([workId, ready]) => {
  if (ready && typeof workId === 'string' && workId !== store.currentWorkId && store.works.some(work => work.id === workId)) {
    void store.selectWork(workId)
  }
}, { immediate: true })
watch(() => content.state?.selected_script_id, (scriptId) => {
  const workId = store.currentWorkId
  if (workId && content.workId === workId && scriptId !== undefined
    && (scriptId ?? null) !== (store.currentWork?.script_version_id ?? null)) void syncReviewWork(workId)
})
async function chooseRoute(route: 'talking_script' | 'short_video_storyboard') {
  if (route === contentRoute.value || working.value) return
  const workId = store.currentWorkId
  const generation = content.generation
  if (!workId || content.workId !== workId) return
  if ((batch.value || content.state?.selected_script_id || store.selectedScript || store.currentWork?.script_version_id)
    && !window.confirm('切换内容路线会撤销当前采用，已有审核和制作任务将失效。继续创作需重新审核、采用并准备任务图；原稿件、素材和费用记录保留。确定切换吗？')) return
  const saved = await content.setRoute(route)
  if (saved && generation === content.generation && workId === store.currentWorkId && content.workId === workId) {
    await store.syncWork(workId)
  }
}
async function repair(c: ScriptVersion) {
  if (content.prompt) await content.repair(c)
  else { await store.repairScript(c.id); await content.refresh() }
}
async function generateDirect() {
  const workId = store.currentWorkId
  if (!workId) return
  await content.generateDirect(topic.value || store.currentWork?.brief.topic || '', materialLines.value)
  if (workId === store.currentWorkId) await syncReviewWork(workId)
}
async function manualCreated(receipt: { script_id: string }) {
  const workId = store.currentWorkId
  if (!workId || content.workId !== workId) return
  showManual.value = false
  await refreshAfterHumanReview(workId)
  if (workId !== store.currentWorkId) return
  const candidate = content.batch?.candidates.find(item => item.id === receipt.script_id)
  if (candidate) await adopt(candidate)
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
    if (content.waitingImportJob?.state === 'waiting_external') return { status: 'run' as const, title: '等待参考转写结果', desc: '转写请求已提交，结果将自动同步。', ctaText: undefined }
    return { status: 'run' as const, title: '正在处理', desc: jobStageLabel.value || '生成与审核中，请稍候。', ctaText: '处理中', ctaLoading: true }
  }
  if (adoptedId.value && store.selectedScript) {
    return { status: 'done' as const, title: `已采用：${store.selectedScript.title}`, desc: storyboardMode.value ? '分镜已选定，接下来准备镜头图片和 Wan 任务。' : '这篇稿件将用于全文配音。', ctaText: storyboardMode.value ? '下一步：镜头制作' : '下一步：试听配音' }
  }
  if (batch.value) {
    if (manualBatch.value) return { status: 'todo' as const, title: '人工分镜待审核', desc: batchGuidance.value, ctaText: undefined }
    return {
      status: 'todo' as const,
      title: `已产出 ${produced.value}/${requested.value} 篇，${passed.value} 篇通过独立审核`,
      desc: batchGuidance.value,
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
        <button v-if="!content.prompt || manualBatch || content.directRequestPending" class="primary" :disabled="working || (!storyboardMode && !store.currentWork?.identity_id) || !(topic || store.currentWork?.brief.topic || '').trim()" @click="generateDirect">
          {{ working ? '正在生成…' : content.directRequestPending ? '核对原生成任务' : candidates.length ? '直接按主题重新生成' : '直接按主题生成三稿' }}
        </button>
        <button v-if="content.activeJob" class="ghost" @click="content.cancelActive()">取消当前任务</button>
        <button v-if="storyboardMode" type="button" :disabled="working || !store.currentWorkId" @click="showManual = !showManual">{{ showManual ? '收起人工创作' : '导入 / 手写分镜' }}</button>
        <span class="faint">{{ store.currentWork?.brief.target_duration_ms ? `目标 ${store.currentWork.brief.target_duration_ms / 1000} 秒` : '时长不限，按内容自然展开' }}<span v-if="store.currentWork?.brief.audience"> · {{ store.currentWork.brief.audience }}</span></span>
      </div>
      <p class="cost-note">{{ content.costNote }}</p>
      <p v-if="content.notice" class="batch-note" role="status">{{ content.notice }}</p>
      <p v-if="content.error" class="batch-note warning" role="alert">{{ content.error }}</p>
      <p v-if="content.state?.writer_error" class="batch-note warning" role="alert">{{ content.state.writer_error.message }}</p>
      <p v-if="batch?.shortfall_note" class="batch-note warning">{{ batch.shortfall_note }}</p>
      <p v-if="batch?.repair_note" class="batch-note">{{ batch.repair_note }}</p>
    </section>

    <ManualStoryboardCreator v-if="showManual && storyboardMode && store.currentWorkId"
      :key="store.currentWorkId" :work-id="store.currentWorkId" :revision="content.revision" :busy="working"
      @created="manualCreated" @cancel="showManual = false" />

    <ReferencesPanel />
    <ReferenceDetail />
    <WritingPromptEditor :blocked="working || (!storyboardMode && !store.currentWork?.identity_id)"
                         :blocked-reason="!storyboardMode && !store.currentWork?.identity_id ? '请先选择主播，再生成口播稿。' : '正在处理任务，请稍候。'"
                         @generate="generateFromPrompt" />

    <section v-if="content.activeJob || content.waitingImportJob" class="section" aria-label="写稿进度" aria-live="polite">
      <div class="section-head"><span class="section-title">{{ jobStageLabel || '处理中' }}</span></div>
      <div v-if="content.waitingImportJob" class="panel panel-pad">
        <p v-if="content.waitingImportJob.state === 'unknown'" class="warning" role="alert">{{ content.waitingImportJob.error?.message || '转写结果查询已暂停，原任务仍保留。' }}</p>
        <p v-else class="faint">转写已提交，结果将自动同步。</p>
        <button type="button" class="primary sm"
          :disabled="content.refreshingImportJobId === content.waitingImportJob.job_id"
          @click="content.refreshImportResult(content.waitingImportJob.job_id)">
          {{ content.refreshingImportJobId === content.waitingImportJob.job_id ? '查询中…' : content.waitingImportJob.state === 'unknown' ? '继续查询转写' : '查询转写进度' }}
        </button>
      </div>
      <div v-else class="panel panel-pad faint">刷新页面不会丢失进度，也不会重复发起请求。</div>
    </section>
    <section v-else-if="store.task && store.busy === '写稿'" class="section" aria-label="写稿进度" aria-live="polite">
      <div class="section-head"><span class="section-title">正在写稿与检查</span></div>
      <div class="panel"><StageProgress :task="store.task" /></div>
    </section>

    <section class="section" aria-label="候选稿件">
      <div class="section-head">
        <h2 class="section-title">{{ storyboardMode ? '选择一篇分镜剧本' : '选择一篇口播稿' }}</h2>
        <span v-if="batch" class="section-note">{{ manualBatch ? '人工分镜' : `已通过 ${passed}` }} / 已产出 {{ produced }}（目标 {{ requested }}）</span>
        <span v-if="batch" class="source-note" :class="{ warning: batch.source_kind === 'mock' }">{{ sourceLabel }}</span>
      </div>
      <div v-if="candidates.length" class="script-options">
        <div v-for="(c, index) in candidates" :key="c.id" class="script-slot">
          <template v-if="editing?.id === c.id">
            <div class="edit-box">
              <template v-if="editing.storyboard">
                <h3>修改分镜剧本</h3>
                <label class="field"><span>剧本标题</span><input v-model="editing.title" type="text" :aria-label="`修改${c.title}标题`" /></label>
                <label class="field"><span>剧本说明</span><textarea v-model="editing.description" rows="2" aria-label="修改分镜剧本说明"></textarea></label>
                <div v-for="shot in editing.storyboard" :key="shot.shot_index" class="storyboard-edit-shot">
                  <header><strong>镜头 {{ shot.shot_index }}</strong></header>
                  <label class="field"><span>画面描述</span><textarea v-model="shot.visual" rows="3" :aria-label="`镜头${shot.shot_index}画面描述`"></textarea></label>
                  <label class="field"><span>台词 / 旁白</span><textarea v-model="shot.dialogue" rows="3" :aria-label="`镜头${shot.shot_index}台词旁白`"></textarea></label>
                  <label class="field"><span>声音 / 音效</span><textarea v-model="shot.sound_effects" rows="2" :aria-label="`镜头${shot.shot_index}声音音效`"></textarea></label>
                  <label class="field"><span>时长（秒）</span><input v-model.number="shot.duration_seconds" type="number" min="2" max="30" step="1" :aria-label="`镜头${shot.shot_index}时长`" /></label>
                  <label class="field"><span>图片生成提示词</span><textarea v-model="shot.prompt" rows="3" :aria-label="`镜头${shot.shot_index}图片生成提示词`"></textarea></label>
                </div>
              </template>
              <textarea v-else v-model="editing.text" class="script-editor" :aria-label="`修改${c.title}`"></textarea>
              <div class="btn-row edit-actions">
                <button class="primary sm" :disabled="editingBusy" @click="saveEdit">{{ editing.storyboard ? '保存分镜新版本' : '保存为新版本' }}</button>
                <button class="ghost sm" @click="editing = null">取消</button>
              </div>
              <p class="faint">保存后会生成新的稿件版本，需要重新审核才能采用；已有镜头图片和视频不会自动套用这次修改。</p>
            </div>
          </template>
          <ScriptCandidateCard v-else :candidate="c" :index="index" :adopted="adoptedId === c.id" :busy="working" :edit-busy="editingBusy" :human-review="storyboardMode"
                               @adopt="adopt" @repair="repair" @edit="startEdit" @review="content.rereview()" />
        </div>
      </div>
      <div v-else-if="batch" class="writing-empty">
        <h3>还没有生成可展示的稿件</h3>
        <p>请查看任务进度，或使用上方生成入口。</p>
      </div>
      <div v-else class="writing-empty"><h3>候选稿会显示在这里</h3><p>搜索并选一个参考后准备提示，或直接填写主题生成。</p></div>
      <div v-if="batch && !manualBatch" class="batch-review panel panel-pad" aria-label="稿件审核">
        <p v-if="!batch.ready_to_present" class="faint">{{ batchGuidance }}</p>
        <p v-for="target in batch.review_targets ?? []" :key="`review-${target.candidate_id}`" class="faint">候选 {{ target.slot ?? '' }}：{{ target.reason }}</p>
        <p v-for="target in batch.retry_targets ?? []" :key="target.candidate_id" class="warning">候选 {{ target.slot ?? '' }}：{{ target.reason }}</p>
        <div class="btn-row">
          <button :disabled="working || !candidates.length" @click="content.rereview()">重新审核</button>
          <button v-if="batch.retry_targets?.length" :disabled="working" @click="content.repairPending()">重写未通过稿件</button>
        </div>
      </div>
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
.storyboard-edit-shot { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); }
.storyboard-edit-shot header { margin-bottom: 10px; }
.storyboard-edit-shot .field { margin: 10px 0; }
.edit-actions { margin-top: 10px; }
.writing-empty { padding: 44px 24px; border-top: 1px solid var(--line); color: var(--fg-3); }
.writing-empty h3 { font-size: 15px; font-weight: 500; color: var(--fg-2); margin-bottom: 8px; }
.writing-empty p { font-size: 13px; }
.batch-review { margin-top: 16px; }
.batch-review p { font-size: 13px; margin-bottom: 10px; }
@media (max-width: 1200px) {
  .script-options { grid-template-columns: 1fr; }
  .script-slot + .script-slot { border-left: 0; border-top: 1px solid var(--line); }
}
@media (max-width: 600px) {
  .brief-form { padding: 18px 16px; }
  .source-note { width: 100%; margin-left: 0; }
}
</style>
