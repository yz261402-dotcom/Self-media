<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { apiMode } from '@/api'
import { ApiError, httpApi } from '@/api/http'
import type { HumanReviewAction, HumanReviewContext, HumanReviewRubric, HumanReviewSubmission, StoryboardShot } from '@/api/types'
import { activeHumanReviewCriteria, calculateHumanReview } from '@/data/humanReviewRubric'

const route = useRoute()
const router = useRouter()
const workId = computed(() => String(route.params.workId ?? ''))
const scriptId = computed(() => String(route.params.scriptId ?? ''))
const context = ref<HumanReviewContext | null>(null)
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const storageWarning = ref('')
const rubric = ref<HumanReviewRubric>('core5')
const scores = ref<Record<string, number>>({})
const notes = ref('')
const feedback = ref('')
const editedFactsAck = ref(false)
const resolution = ref<'edit' | 'rewrite' | null>(null)
const readMode = ref<'original' | 'edit'>('original')
const title = ref('')
const description = ref('')
const storyboard = ref<StoryboardShot[]>([])
const loadedKey = ref('')
const completed = ref(false)
const success = ref('')
const pendingSubmission = ref<HumanReviewSubmission | null>(null)
const pendingKey = computed(() => `avatar-studio:human-review-pending:${workId.value}:${scriptId.value}`)
let loadGeneration = 0

const criteria = computed(() => activeHumanReviewCriteria(rubric.value))
const result = computed(() => calculateHumanReview(rubric.value, scores.value))
const failed = computed(() => result.value.complete && !result.value.qualified)
const manualSource = computed(() => context.value?.creation_source === 'manual' || context.value?.candidate.creation_source === 'manual')
const readOnlyReview = computed(() => ['human_edit', 'manual_takeover'].includes(context.value?.review?.mode ?? '') && !pendingSubmission.value)
const locked = computed(() => submitting.value || completed.value || pendingSubmission.value !== null || readOnlyReview.value)
const eligible = computed(() => !!context.value?.eligible && !loading.value && !completed.value && !readOnlyReview.value)
const recordedCriteria = computed(() => activeHumanReviewCriteria(context.value?.review?.rubric === 'extended8' ? 'extended8' : 'core5'))
const original = computed(() => context.value?.candidate ?? null)
const changed = computed(() => !!original.value && JSON.stringify({ title: title.value.trim(), description: description.value.trim(), storyboard: storyboard.value })
  !== JSON.stringify({ title: original.value.title.trim(), description: original.value.description.trim(), storyboard: original.value.storyboard ?? [] }))
const touched = computed(() => Object.keys(scores.value).length > 0 || !!notes.value || !!feedback.value || changed.value)
const editing = computed(() => resolution.value === 'edit' && readMode.value === 'edit')
watch([title, description, storyboard], () => { editedFactsAck.value = false }, { deep: true })
const editingErrors = computed(() => {
  const problems: string[] = []
  if (!title.value.trim()) problems.push('标题不能为空。')
  if (!storyboard.value.length) problems.push('至少保留一个镜头。')
  for (const [index, shot] of storyboard.value.entries()) {
    if (shot.shot_index !== index + 1) problems.push('镜头编号必须从 1 连续递增。')
    if (!Number.isInteger(shot.duration_seconds) || shot.duration_seconds < 2 || shot.duration_seconds > 30) problems.push(`镜头 ${index + 1} 时长须为 2 到 30 秒的整数。`)
    if (!shot.visual.trim() || !shot.dialogue.trim() || !shot.sound_effects.trim()) problems.push(`镜头 ${index + 1} 的画面、台词和声音不能为空；无台词或音效可明确填写“无”。`)
  }
  return problems
})
const average = computed(() => result.value.average === null ? '待评分' : result.value.average.toFixed(3).replace(/\.?0+$/, ''))
const verdict = computed(() => !result.value.complete ? '待完成评分' : result.value.qualified ? '评分合格，可以采用' : '评分不合格')
const activeScores = computed(() => Object.fromEntries(criteria.value.filter(item => scores.value[item.id] !== undefined).map(item => [item.id, scores.value[item.id]])))

watch(failed, (isFailed) => {
  if (!isFailed && !locked.value) { resolution.value = null; readMode.value = 'original' }
})

function persistPending(): boolean {
  try {
    if (pendingSubmission.value?.script_id === scriptId.value) localStorage.setItem(pendingKey.value, JSON.stringify(pendingSubmission.value))
    else if (!pendingSubmission.value) localStorage.removeItem(pendingKey.value)
    return true
  } catch {
    storageWarning.value = '浏览器无法保存未确认请求。提交结果异常时，请保留当前页面核对。'
    return false
  }
}

function restorePending() {
  pendingSubmission.value = null
  try {
    const cached = localStorage.getItem(pendingKey.value)
    if (!cached) return
    const pending = JSON.parse(cached)
    if (pending && typeof pending.request_id === 'string' && pending.script_id === scriptId.value
      && Number.isInteger(pending.expected_content_revision) && typeof pending.expected_batch_id === 'string'
      && ['adopt', 'edit_adopt', 'rewrite'].includes(pending.action)) pendingSubmission.value = pending
  } catch {
    storageWarning.value = '未确认请求无法读取，请返回内容创作核对当前采用与任务状态。'
  }
}

function persistDraft() {
  const pendingSaved = persistPending()
  if (!loadedKey.value || !context.value || completed.value) return
  try {
    localStorage.setItem(loadedKey.value, JSON.stringify({ rubric: rubric.value, scores: scores.value, notes: notes.value,
      feedback: feedback.value, resolution: resolution.value, readMode: readMode.value,
      title: title.value, description: description.value, storyboard: storyboard.value, pendingSubmission: pendingSubmission.value }))
    if (pendingSaved) storageWarning.value = ''
  } catch {
    storageWarning.value = '浏览器无法保存审核草稿，关闭或刷新页面会丢失未提交内容。'
  }
}

watch([rubric, scores, notes, feedback, resolution, readMode, title, description, storyboard, pendingSubmission], persistDraft, { deep: true })

function restoreDraft() {
  try {
    const value = localStorage.getItem(loadedKey.value)
    if (!value) return
    const draft = JSON.parse(value)
    if (!draft || typeof draft !== 'object') return
    rubric.value = draft.rubric === 'extended8' ? 'extended8' : 'core5'
    scores.value = Object.fromEntries(Object.entries(draft.scores ?? {}).filter(([id, score]) =>
      activeHumanReviewCriteria('extended8').some(item => item.id === id) && typeof score === 'number' && Number.isInteger(score) && score >= 1 && score <= 5)) as Record<string, number>
    notes.value = typeof draft.notes === 'string' ? draft.notes : ''
    feedback.value = typeof draft.feedback === 'string' ? draft.feedback : ''
    resolution.value = draft.resolution === 'edit' || draft.resolution === 'rewrite' ? draft.resolution : null
    readMode.value = draft.readMode === 'edit' && resolution.value === 'edit' ? 'edit' : 'original'
    if (typeof draft.title === 'string') title.value = draft.title
    if (typeof draft.description === 'string') description.value = draft.description
    if (Array.isArray(draft.storyboard) && draft.storyboard.every((shot: unknown) => {
      if (!shot || typeof shot !== 'object') return false
      const item = shot as Record<string, unknown>
      return typeof item.shot_index === 'number' && typeof item.duration_seconds === 'number'
        && ['visual', 'dialogue', 'sound_effects', 'prompt'].every(key => typeof item[key] === 'string')
    })) storyboard.value = draft.storyboard
    const pending = draft.pendingSubmission
    if (!pendingSubmission.value && pending && typeof pending.request_id === 'string' && pending.script_id === scriptId.value
      && pending.expected_content_revision === context.value?.content_revision && pending.expected_batch_id === context.value?.batch_id
      && ['adopt', 'edit_adopt', 'rewrite'].includes(pending.action)) {
      pendingSubmission.value = pending
      error.value = '上次提交的结果尚未确认。请重新确认原提交，系统将沿用同一请求编号。'
    }
  } catch {
    storageWarning.value = '审核草稿无法读取，当前显示原稿。'
  }
}

async function load() {
  if (submitting.value || completed.value) return
  const generation = ++loadGeneration
  const targetWork = workId.value
  const targetScript = scriptId.value
  loading.value = true
  error.value = ''
  loadedKey.value = ''
  context.value = null
  // A successful edit can remove the source candidate before its response reaches this page.
  restorePending()
  try {
    if (apiMode === 'mock') throw new Error('人工审核需要连接生产后端，模拟模式不能确认采用或提交重写。')
    const loaded = await httpApi.getHumanReview(targetWork, targetScript)
    if (generation !== loadGeneration) return
    context.value = loaded
    rubric.value = 'core5'
    scores.value = {}
    notes.value = ''
    feedback.value = ''
    resolution.value = null
    editedFactsAck.value = false
    readMode.value = 'original'
    title.value = loaded.candidate.title
    description.value = loaded.candidate.description
    storyboard.value = (loaded.candidate.storyboard ?? []).map(shot => ({ ...shot }))
    loadedKey.value = `avatar-studio:human-review-draft:${targetWork}:${targetScript}:${loaded.content_revision}:${loaded.batch_id}`
    restoreDraft()
  } catch (cause) {
    if (generation === loadGeneration) error.value = cause instanceof Error ? cause.message : '人工审核页加载失败。'
  } finally {
    if (generation === loadGeneration) {
      loading.value = false
      if (pendingSubmission.value) error.value = '上次提交的结果尚未确认。请重新确认原提交，系统将沿用同一请求编号。'
    }
  }
}

function chooseResolution(value: 'edit' | 'rewrite') {
  if (locked.value || !failed.value) return
  resolution.value = value
  readMode.value = value === 'edit' ? 'edit' : 'original'
}

function updateShot(shot: StoryboardShot, key: 'visual' | 'dialogue' | 'sound_effects', event: Event) {
  shot[key] = (event.target as HTMLTextAreaElement).value
  shot.prompt = `${shot.visual}；${shot.dialogue}；音效：${shot.sound_effects}`
}

function addShot() {
  if (locked.value) return
  storyboard.value.push({ shot_index: storyboard.value.length + 1, duration_seconds: 5, visual: '', dialogue: '', sound_effects: '无', prompt: '' })
}

function moveShot(index: number, offset: number) {
  const next = index + offset
  if (locked.value || next < 0 || next >= storyboard.value.length) return
  const [shot] = storyboard.value.splice(index, 1)
  storyboard.value.splice(next, 0, shot)
  storyboard.value.forEach((item, i) => { item.shot_index = i + 1 })
}

function removeShot(index: number) {
  if (locked.value || !window.confirm(`删除镜头 ${index + 1}？该改动在确认采用前仅保存在当前审核草稿中。`)) return
  storyboard.value.splice(index, 1)
  storyboard.value.forEach((shot, i) => { shot.shot_index = i + 1 })
}

function notifyAndClose(action: HumanReviewAction, adoptedScriptId: string, targetWorkId: string, targetScriptId: string) {
  const payload = { type: 'avatar-studio:human-review-complete', workId: targetWorkId, action, scriptId: adoptedScriptId, timestamp: Date.now() }
  try { window.opener?.postMessage(payload, window.location.origin) } catch { /* The opener may have navigated away. */ }
  try { localStorage.setItem('avatar-studio:human-review-complete', JSON.stringify(payload)) } catch { /* postMessage is the primary channel. */ }
  if (workId.value === targetWorkId && scriptId.value === targetScriptId) window.close()
}

async function submit(action: HumanReviewAction) {
  if (submitting.value || completed.value) return
  const targetWorkId = workId.value
  const targetScriptId = scriptId.value
  if (!pendingSubmission.value) {
    if (!eligible.value || !context.value) return
    if (action === 'adopt' && !result.value.qualified) return
    if (action === 'edit_adopt' && (!failed.value || !changed.value || editingErrors.value.length || (manualSource.value && !editedFactsAck.value))) return
    if (action === 'rewrite' && (!failed.value || !feedback.value.trim())) return
    pendingSubmission.value = {
      request_id: `human-review-${crypto.randomUUID()}`, script_id: scriptId.value,
      expected_content_revision: context.value.content_revision, expected_batch_id: context.value.batch_id,
      action, rubric: rubric.value, scores: { ...activeScores.value }, notes: notes.value.trim(),
      ...(action === 'rewrite' ? { feedback: feedback.value.trim() } : {}),
      ...(action === 'edit_adopt' ? { title: title.value.trim(), description: description.value.trim(),
        storyboard: storyboard.value.map(shot => ({ ...shot })), ...(manualSource.value ? { facts_ack: true } : {}) } : {}),
    }
    // Persist the exact request before sending so a lost response cannot create another rewrite.
    persistDraft()
  }
  const input = pendingSubmission.value
  submitting.value = true
  error.value = ''
  try {
    const receipt = await httpApi.submitHumanReview(targetWorkId, input)
    if (!receipt.ok) throw new Error('服务未确认此次操作，请核对后重新确认原提交。')
    completed.value = true
    success.value = receipt.action === 'rewrite' ? '重写任务已提交。新稿通过智能体审核后，可以再次进入人工审核。'
      : receipt.action === 'edit_adopt' ? '已按人工修订稿采用，可以进入视频合成。'
        : manualSource.value ? '人工接管评分已通过，剧本已采用；未执行模型审核。' : '人工评分已通过，剧本已采用，可以进入视频合成。'
    try {
      localStorage.removeItem(loadedKey.value)
      localStorage.removeItem(`avatar-studio:human-review-draft:${targetWorkId}:${targetScriptId}:${input.expected_content_revision}:${input.expected_batch_id}`)
    } catch { /* The successful receipt remains authoritative. */ }
    pendingSubmission.value = null
    persistPending()
    notifyAndClose(receipt.action, receipt.script_id, targetWorkId, targetScriptId)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '提交结果未确认，请重新确认原提交。'
    if (cause instanceof ApiError && cause.status !== null && cause.status < 500) {
      pendingSubmission.value = null
      if (cause.status === 409 && context.value) {
        context.value.eligible = false
        context.value.reasons = [error.value]
      }
    }
    persistDraft()
  } finally {
    submitting.value = false
  }
}

function beforeUnload(event: BeforeUnloadEvent) {
  if (completed.value || (!touched.value && !pendingSubmission.value)) return
  event.preventDefault()
  event.returnValue = ''
}

function returnToCreation() {
  if (submitting.value) return
  void router.push({ path: '/script', query: { workId: workId.value } })
}

onBeforeRouteUpdate(() => !submitting.value)
onBeforeRouteLeave(() => !submitting.value && (completed.value || (!touched.value && !pendingSubmission.value)
  || window.confirm('此次人工审核尚未提交，审核草稿会保留在当前浏览器。确认返回内容创作？')))
watch(() => [workId.value, scriptId.value], load, { immediate: true })
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => { ++loadGeneration; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <main class="human-review-page">
    <header class="review-header">
      <div><h1>分镜剧本人工审核</h1><p v-if="original">{{ original.title }}</p></div>
      <button type="button" :disabled="submitting" @click="returnToCreation">返回内容创作</button>
    </header>
    <div v-if="loading && !context" class="page-state" role="status">正在读取剧本和审核状态…</div>
    <div v-if="success" class="success-state" role="status"><strong>{{ success }}</strong><button type="button" @click="returnToCreation">返回内容创作</button></div>
    <div v-if="error" class="error-state" role="alert"><p>{{ error }}</p>
      <button v-if="pendingSubmission && !completed" type="button" :disabled="submitting" @click="submit(pendingSubmission.action)">{{ submitting ? '确认中…' : '重新确认原提交' }}</button>
      <button v-else-if="!completed" type="button" :disabled="loading || submitting" @click="load">刷新审核状态</button>
    </div>
    <p v-if="storageWarning" class="storage-warning" role="alert">{{ storageWarning }}</p>
    <div v-if="manualSource" class="takeover-state"><strong>人工接管 · 未执行模型审核</strong>
      <p v-if="context?.takeover_reason || original?.takeover_reason">接管原因：{{ context?.takeover_reason || original?.takeover_reason }}</p>
    </div>
    <div v-if="context && !context.eligible && !completed && !readOnlyReview" class="eligibility-state" role="alert">
      <strong>当前稿件暂不可提交人工审核</strong><p v-for="reason in context.reasons" :key="reason">{{ reason }}</p>
      <button type="button" :disabled="loading || submitting" @click="load">刷新审核状态</button>
    </div>
    <section v-if="context?.review" class="existing-review" aria-label="已有人工审核记录">
      <h2>{{ context.review.mode === 'human_edit' ? '已按人工修订稿确认采用' : context.review.mode === 'manual_takeover' ? '人工接管评分合格，已确认采用' : '已有人工评分通过记录' }}</h2>
      <p>{{ context.review.mode === 'human_edit' ? '采用依据为人工修订确认。以下保留的是修改前原稿评分，不代表修改稿重新评分通过。' : context.review.mode === 'manual_takeover' ? '采用依据为人工评分合格与内容核对；未执行智能体审核。' : '采用依据为人工评分通过。' }}</p>
      <div class="recorded-scores"><span v-for="item in recordedCriteria" :key="item.id">{{ item.title }}：{{ context.review.scores?.[item.id] ?? '未记录' }} 分</span></div>
      <p v-if="typeof context.review.average === 'number'" class="num">{{ context.review.mode === 'human_edit' ? '原稿平均分' : '平均分' }}：{{ context.review.average }}</p>
      <p v-if="context.review.notes" class="recorded-notes">审核意见：{{ context.review.notes }}</p>
      <button v-if="readOnlyReview" type="button" @click="returnToCreation">返回内容创作</button>
    </section>

    <div v-if="original && context" class="review-layout" :aria-busy="submitting">
      <aside class="script-pane" aria-label="分镜剧本">
        <header class="pane-heading"><h2>{{ editing ? '人工修订稿' : '分镜剧本原文' }}</h2>
          <div v-if="resolution === 'edit'" class="view-switch" role="group" aria-label="剧本视图">
            <button type="button" :class="{ selected: readMode === 'original' }" :aria-pressed="readMode === 'original'" @click="readMode = 'original'">原文</button>
            <button type="button" :class="{ selected: readMode === 'edit' }" :aria-pressed="readMode === 'edit'" @click="readMode = 'edit'">修改稿</button>
          </div>
          <span v-else class="faint num">{{ original.storyboard?.length ?? 0 }} 个镜头</span>
        </header>
        <div class="script-scroll">
          <template v-if="editing">
            <fieldset class="editing-fields" :disabled="locked || !eligible">
              <label class="field"><span>标题</span><input v-model="title" type="text" /></label>
              <label class="field"><span>简介</span><textarea v-model="description" rows="3"></textarea></label>
              <section v-for="(shot, index) in storyboard" :key="index" class="shot-editor">
                <header><h3>镜头 {{ shot.shot_index }}</h3><div class="shot-tools">
                  <button type="button" class="sm ghost" :disabled="index === 0" @click="moveShot(index, -1)">上移</button>
                  <button type="button" class="sm ghost" :disabled="index === storyboard.length - 1" @click="moveShot(index, 1)">下移</button>
                  <button type="button" class="sm danger" @click="removeShot(index)">删除</button>
                </div></header>
                <label class="field"><span>镜头时长（秒）</span><input v-model.number="shot.duration_seconds" type="number" min="2" max="30" step="1" /></label>
                <label class="field"><span>画面</span><textarea :value="shot.visual" rows="4" @input="updateShot(shot, 'visual', $event)"></textarea></label>
                <label class="field"><span>台词 / 旁白</span><textarea :value="shot.dialogue" rows="4" @input="updateShot(shot, 'dialogue', $event)"></textarea></label>
                <label class="field"><span>音效 / 环境声</span><textarea :value="shot.sound_effects" rows="2" @input="updateShot(shot, 'sound_effects', $event)"></textarea></label>
                <label class="field"><span>镜头生成提示</span><textarea v-model="shot.prompt" rows="3"></textarea></label>
              </section>
              <button type="button" @click="addShot">新增镜头</button>
            </fieldset>
          </template>
          <template v-else>
            <h3 class="script-title">{{ original.title }}</h3><p v-if="original.description" class="script-description">{{ original.description }}</p>
            <section v-for="shot in original.storyboard ?? []" :key="shot.shot_index" class="original-shot">
              <header><h3>镜头 {{ shot.shot_index }}</h3><span class="num">{{ shot.duration_seconds }} 秒</span></header>
              <dl><dt>画面</dt><dd>{{ shot.visual }}</dd><dt>台词 / 旁白</dt><dd>{{ shot.dialogue }}</dd><dt>音效 / 环境声</dt><dd>{{ shot.sound_effects }}</dd></dl>
              <details v-if="shot.prompt"><summary>镜头生成提示</summary><p>{{ shot.prompt }}</p></details>
            </section>
            <details v-if="original.text" class="plain-original" :open="!original.storyboard?.length"><summary>完整剧本原文</summary><div>{{ original.text }}</div></details>
          </template>
        </div>
      </aside>

      <section v-if="!readOnlyReview" class="assessment-pane" aria-label="人工评分表">
        <div class="assessment-heading"><h2>审查评分</h2><fieldset class="rubric-switch" :disabled="locked || !eligible"><legend class="sr-only">评分方案</legend>
          <label :class="{ selected: rubric === 'core5' }"><input v-model="rubric" type="radio" value="core5" />核心 5 项</label>
          <label :class="{ selected: rubric === 'extended8' }"><input v-model="rubric" type="radio" value="extended8" />完整 8 项</label>
        </fieldset></div>
        <p class="scoring-rule">全部评分点等权平均，平均分须大于 4 分；正好 4 分不合格。</p>
        <div class="score-summary" :class="{ pass: result.qualified, fail: failed }" role="status" aria-live="polite">
          <div><span>已评项目</span><strong class="num">{{ result.filled }} / {{ result.count }}</strong></div>
          <div><span>平均分</span><strong class="num">{{ average }}</strong></div>
          <p>{{ verdict }}</p>
        </div>
        <fieldset class="criteria-fields" :disabled="locked || !eligible"><legend class="sr-only">逐项评分</legend>
          <article v-for="(item, index) in criteria" :key="item.id" class="criterion">
            <header><h3><span class="criterion-index num">{{ String(index + 1).padStart(2, '0') }}</span>{{ item.title }}</h3>
              <div class="score-options" role="group" :aria-label="item.title + '评分'">
                <label v-for="score in 5" :key="score" :class="{ selected: scores[item.id] === score }" :title="item.levels[score - 1]">
                  <input v-model.number="scores[item.id]" type="radio" :name="item.id" :value="score" :aria-label="item.title + ' ' + score + '分'" /><span>{{ score }}</span>
                </label>
              </div>
            </header><p class="criterion-focus">{{ item.focus }}</p>
            <details open class="rating-standards"><summary>评分标准</summary><ol><li v-for="(level, position) in item.levels" :key="position"><strong>{{ position + 1 }} 分</strong><span>{{ level }}</span></li></ol></details>
          </article>
          <label class="field review-notes"><span>评分依据与修改建议（选填）</span><textarea v-model="notes" rows="4" placeholder="对应台词、镜头编号及需调整的位置"></textarea></label>
        </fieldset>

        <div v-if="result.qualified" class="decision-section"><h3>可以采用</h3><p>当前剧本人工评分合格。</p>
          <button class="primary" type="button" :disabled="!eligible || locked" @click="submit('adopt')">{{ submitting ? '确认中…' : '确认采用' }}</button>
        </div>
        <div v-else-if="failed" class="decision-section"><h3>选择修改方式</h3>
          <div class="resolution-options" role="group" aria-label="评分不合格后的处理方式">
            <button type="button" :class="{ selected: resolution === 'edit' }" :aria-pressed="resolution === 'edit'" :disabled="locked || !eligible" @click="chooseResolution('edit')">手工修改剧本</button>
            <button type="button" :class="{ selected: resolution === 'rewrite' }" :aria-pressed="resolution === 'rewrite'" :disabled="locked || !eligible" @click="chooseResolution('rewrite')">返回重新写稿</button>
          </div>
          <template v-if="resolution === 'edit'"><p class="manual-adoption-note">采用依据：人工修订确认。修改稿将直接进入制作，不再进行智能体审核或重新评分。</p>
            <p v-if="!changed" class="faint">修改稿尚未发生变化。</p>
            <ul v-if="editingErrors.length" class="validation-list"><li v-for="problem in editingErrors" :key="problem">{{ problem }}</li></ul>
            <label v-if="manualSource" class="edited-facts-ack"><input v-model="editedFactsAck" type="checkbox" :disabled="locked || !eligible" /><span>已核对修改稿事实、版权与发布规范，对内容负责。</span></label>
            <button class="primary" type="button" :disabled="!eligible || locked || !changed || !!editingErrors.length || (manualSource && !editedFactsAck)" @click="submit('edit_adopt')">{{ submitting ? '确认中…' : '确认采用修改稿' }}</button>
          </template>
          <template v-if="resolution === 'rewrite'"><label class="field rewrite-feedback"><span>给写稿智能体的修改意见</span><textarea v-model="feedback" :disabled="locked || !eligible" rows="5" placeholder="说明需要修改的镜头、台词和目标效果"></textarea></label>
            <p v-if="manualSource" class="faint">人工导入稿没有原模型写作批次。请返回内容创作直接生成新稿，新稿再经过智能体审核和人工评分。</p>
            <p v-else class="faint">确认后提交新的写稿与智能体审核任务；新稿通过后再次进行人工审核。</p>
            <button v-if="manualSource" class="primary" type="button" :disabled="submitting" @click="returnToCreation">返回内容创作生成新稿</button>
            <button v-else class="primary" type="button" :disabled="!eligible || locked || !feedback.trim()" @click="submit('rewrite')">{{ submitting ? '提交中…' : '确认返回重新写稿' }}</button>
          </template>
        </div>
      </section>
      <section v-else class="assessment-pane confirmed-summary"><h2>{{ context.review?.mode === 'manual_takeover' ? '人工接管已确认' : '人工修订已确认' }}</h2><p>该版本已有有效人工确认记录，可以返回内容创作继续视频合成。</p><button type="button" @click="returnToCreation">返回内容创作</button></section>
    </div>
  </main>
</template>

<style scoped>
.human-review-page { max-width: 1520px; min-height: 100%; padding: 0 32px 48px; margin: 0 auto; letter-spacing: 0; }
.review-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 96px; padding: 20px 0; border-bottom: 1px solid var(--line); }
.review-header > div { min-width: 0; }
.review-header h1 { font-size: 22px; }
.review-header p { color: var(--fg-2); margin: 7px 0 0; overflow-wrap: anywhere; }
.review-header button { flex: none; }
.review-layout { display: grid; grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr); gap: 32px; align-items: start; padding-top: 24px; }
.script-pane { position: sticky; top: 20px; min-width: 0; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; }
.pane-heading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; min-height: 64px; padding: 16px 20px; background: var(--panel-2); border-bottom: 1px solid var(--line); }
.pane-heading h2, .assessment-heading h2 { font-size: 17px; }
.script-scroll { max-height: calc(100dvh - 108px); min-height: 250px; overflow-y: auto; padding: 20px; }
.script-title { font-size: 18px; margin-bottom: 12px; overflow-wrap: anywhere; }
.script-description { color: var(--fg-2); white-space: pre-wrap; overflow-wrap: anywhere; margin-bottom: 24px; }
.original-shot { padding: 0 0 20px; margin-bottom: 20px; border-bottom: 1px solid var(--line); }
.original-shot header, .shot-editor header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.original-shot h3, .shot-editor h3 { font-size: 15px; flex: none; }
.original-shot header span { color: var(--fg-3); font-size: 12px; }
.original-shot dl { margin: 12px 0 0; }
.original-shot dt { font-size: 12px; color: var(--fg-3); margin-top: 12px; }
.original-shot dd { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.9; }
.original-shot details { margin-top: 16px; color: var(--fg-2); font-size: 12px; }
.original-shot details p { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
summary { cursor: pointer; }
.plain-original { font-size: 13px; }
.plain-original > div { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 16px; line-height: 1.9; }
.view-switch, .rubric-switch, .resolution-options { display: flex; gap: 6px; flex-wrap: wrap; }
.view-switch button { padding: 4px 12px; font-size: 12px; background: var(--panel); border-color: var(--line); color: var(--fg-2); }
.selected { color: var(--blue) !important; border-color: var(--blue) !important; background: var(--blue-wash) !important; }
.assessment-pane { min-width: 0; }
.assessment-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.rubric-switch label { display: flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 4px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
.rubric-switch input { margin: 0; accent-color: var(--blue); }
.scoring-rule { color: var(--fg-2); font-size: 12px; margin: 14px 0; }
.score-summary { display: flex; align-items: center; gap: 32px; flex-wrap: wrap; padding: 18px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.score-summary > div { display: flex; flex-direction: column; gap: 4px; }
.score-summary span { font-size: 12px; color: var(--fg-3); }
.score-summary strong { font-size: 23px; line-height: 1.3; }
.score-summary p { font-weight: 600; font-size: 13px; margin: 0 0 0 auto; }
.score-summary.pass p { color: var(--green); }
.score-summary.fail p { color: var(--red); }
.criterion { padding: 26px 0 22px; border-bottom: 1px solid var(--line); }
.criterion header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.criterion h3 { display: flex; gap: 10px; font-size: 15px; line-height: 1.6; }
.criterion-index { color: var(--fg-3); }
.score-options { display: flex; gap: 5px; }
.score-options label { display: grid; place-items: center; position: relative; width: 34px; height: 34px; border: 1px solid var(--line-2); border-radius: 4px; cursor: pointer; font-weight: 600; }
.score-options input { position: absolute; width: 100%; height: 100%; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
.score-options label:has(input:focus-visible) { outline: 2px solid var(--blue); outline-offset: 3px; }
fieldset:disabled .score-options label, fieldset:disabled .rubric-switch label { opacity: 0.6; cursor: not-allowed; }
.criterion-focus { margin: 12px 0; font-size: 12px; line-height: 1.8; color: var(--fg-2); }
.rating-standards { color: var(--fg-2); font-size: 12px; }
.rating-standards summary { color: var(--fg); font-size: 12px; }
.rating-standards ol { padding: 0; margin: 12px 0 0; list-style: none; }
.rating-standards li { display: grid; grid-template-columns: 34px minmax(0, 1fr); gap: 9px; padding: 5px 0; line-height: 1.8; }
.rating-standards strong { color: var(--fg); white-space: nowrap; }
.review-notes { margin: 24px 0; }
.decision-section { padding: 24px 0 0; border-top: 1px solid var(--line); }
.decision-section h3 { font-size: 16px; margin-bottom: 12px; }
.decision-section p { font-size: 13px; margin: 12px 0 16px; }
.resolution-options button { background: var(--panel); color: var(--fg-2); border-color: var(--line-2); }
.manual-adoption-note { color: var(--amber); }
.rewrite-feedback { margin-top: 20px; }
.validation-list { color: var(--red); font-size: 12px; padding-left: 20px; }
.editing-fields .field { margin-bottom: 18px; }
.editing-fields textarea { min-height: 60px; }
.shot-editor { border-top: 1px solid var(--line); padding: 20px 0 8px; }
.shot-editor header { margin-bottom: 16px; flex-wrap: wrap; }
.shot-tools { display: flex; gap: 4px; flex-wrap: wrap; }
.shot-tools button { padding: 3px 7px; font-size: 11px; }
.page-state { padding: 48px 0; color: var(--fg-3); }
.error-state, .success-state, .eligibility-state { padding: 18px 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.error-state, .eligibility-state { color: var(--red); }
.success-state { color: var(--green); display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.storage-warning { color: var(--amber); font-size: 12px; padding-top: 12px; }
.takeover-state { padding: 18px 0; border-bottom: 1px solid var(--line); color: var(--amber); font-size: 13px; }
.takeover-state p { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.edited-facts-ack { display: flex; align-items: flex-start; gap: 8px; margin: 18px 0; font-size: 13px; line-height: 1.75; }
.edited-facts-ack input { width: auto; flex: none; margin-top: 5px; }
.eligibility-state strong { display: block; margin-bottom: 10px; }
.existing-review { padding: 24px 0; border-bottom: 1px solid var(--line); }
.existing-review h2, .confirmed-summary h2 { font-size: 17px; margin-bottom: 12px; }
.existing-review p, .confirmed-summary p { color: var(--fg-2); font-size: 13px; }
.recorded-scores { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 14px 0; color: var(--fg-2); font-size: 12px; }
.recorded-notes { white-space: pre-wrap; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; }
@media (max-width: 1000px) {
  .human-review-page { padding: 0 20px 32px; }
  .review-layout { grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr); gap: 20px; }
  .script-scroll { padding: 16px; }
  .score-summary { gap: 20px; }
  .score-summary p { width: 100%; margin-left: 0; }
}
@media (max-width: 720px) {
  .human-review-page { padding: 0 16px 28px; }
  .review-header { gap: 12px; align-items: flex-start; flex-wrap: wrap; }
  .review-header h1 { font-size: 20px; }
  .review-header button { padding: 5px 10px; font-size: 12px; }
  .review-layout { grid-template-columns: minmax(0, 1fr); gap: 28px; }
  .script-pane { position: static; }
  .script-scroll { max-height: 58dvh; }
  .pane-heading { padding: 14px 16px; }
  .criterion header { align-items: flex-start; }
  .score-options label { width: 38px; height: 38px; }
  .assessment-heading { align-items: flex-start; }
}
</style>
