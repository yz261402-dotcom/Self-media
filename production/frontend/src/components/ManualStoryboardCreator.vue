<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { apiMode } from '@/api'
import { ApiError, httpApi } from '@/api/http'
import type { ManualStoryboardInput, ManualStoryboardReceipt, StoryboardShot } from '@/api/types'
import { parseManualStoryboardJson, validateManualStoryboard } from '@/data/manualStoryboard'

const props = defineProps<{ workId: string; revision: number; busy?: boolean }>()
const emit = defineEmits<{ (e: 'created', receipt: ManualStoryboardReceipt): void; (e: 'cancel'): void }>()
const mode = ref<'structured' | 'json'>('structured')
const title = ref('')
const description = ref('')
const storyboard = ref<StoryboardShot[]>([])
const jsonText = ref('')
const reason = ref('')
const manualAck = ref(false)
const factsAck = ref(false)
const submitting = ref(false)
const completed = ref(false)
const error = ref('')
const storageWarning = ref('')
const pending = ref<ManualStoryboardInput | null>(null)
const storageKey = computed(() => `avatar-studio:manual-storyboard:${props.workId}:${props.revision}`)
const locked = computed(() => submitting.value || !!props.busy || completed.value || !!pending.value)
const validation = computed(() => validateManualStoryboard(title.value, storyboard.value))
const totalDuration = computed(() => storyboard.value.reduce((sum, shot) => sum + (Number.isFinite(shot.duration_seconds) ? shot.duration_seconds : 0), 0))
const canSubmit = computed(() => !locked.value && apiMode !== 'mock' && !validation.value.length
  && !!reason.value.trim() && reason.value.trim().length <= 4000 && manualAck.value && factsAck.value)

function emptyShot(index: number): StoryboardShot {
  return { shot_index: index, duration_seconds: 5, visual: '', dialogue: '', sound_effects: '无', prompt: '' }
}

function saveDraft() {
  if (completed.value) return
  try {
    localStorage.setItem(storageKey.value, JSON.stringify({ mode: mode.value, title: title.value, description: description.value,
      storyboard: storyboard.value, jsonText: jsonText.value, reason: reason.value, manualAck: manualAck.value,
      factsAck: factsAck.value, pending: pending.value }))
    storageWarning.value = ''
  } catch { storageWarning.value = '浏览器无法保存草稿，请保留当前页面直到提交结果确认。' }
}

function restoreDraft() {
  mode.value = 'structured'; title.value = ''; description.value = ''; jsonText.value = ''; reason.value = ''
  storyboard.value = [emptyShot(1)]; manualAck.value = false; factsAck.value = false
  pending.value = null; completed.value = false; error.value = ''; storageWarning.value = ''
  try {
    const cached = localStorage.getItem(storageKey.value)
    if (!cached) return
    const draft = JSON.parse(cached)
    if (!draft || typeof draft !== 'object') return
    mode.value = draft.mode === 'json' ? 'json' : 'structured'
    for (const [field, state] of [['title', title], ['description', description], ['jsonText', jsonText], ['reason', reason]] as const) {
      if (typeof draft[field] === 'string') state.value = draft[field]
    }
    if (Array.isArray(draft.storyboard)) storyboard.value = parseManualStoryboardJson(JSON.stringify(draft.storyboard)).storyboard
    manualAck.value = draft.manualAck === true; factsAck.value = draft.factsAck === true
    const request = draft.pending
    if (request && typeof request.request_id === 'string' && request.request_id
      && request.expected_content_revision === props.revision && typeof request.title === 'string'
      && typeof request.takeover_reason === 'string' && request.takeover_reason.trim()
      && request.manual_ack === true && request.facts_ack === true && Array.isArray(request.storyboard)) {
      const parsed = parseManualStoryboardJson(JSON.stringify(request))
      if (!validateManualStoryboard(parsed.title, parsed.storyboard).length) {
        pending.value = request
        title.value = request.title; description.value = request.description ?? ''; storyboard.value = request.storyboard
        error.value = '上次提交结果尚未确认，请重新确认原提交。'
      }
    }
  } catch { storageWarning.value = '人工创作草稿无法读取，请核对后继续。' }
}

watch(() => [props.workId, props.revision], restoreDraft, { immediate: true })
watch([mode, title, description, storyboard, jsonText, reason, manualAck, factsAck, pending], saveDraft, { deep: true })
watch([title, description, storyboard], () => { if (!pending.value) factsAck.value = false }, { deep: true })

function addShot() {
  if (!locked.value && storyboard.value.length < 30) storyboard.value.push(emptyShot(storyboard.value.length + 1))
}

function moveShot(index: number, direction: number) {
  const next = index + direction
  if (locked.value || next < 0 || next >= storyboard.value.length) return
  const [shot] = storyboard.value.splice(index, 1)
  storyboard.value.splice(next, 0, shot)
  storyboard.value.forEach((item, i) => { item.shot_index = i + 1 })
}

function removeShot(index: number) {
  if (locked.value || !window.confirm(`删除镜头 ${index + 1}？`)) return
  storyboard.value.splice(index, 1)
  storyboard.value.forEach((item, i) => { item.shot_index = i + 1 })
}

function importJson() {
  if (locked.value) return
  try {
    const parsed = parseManualStoryboardJson(jsonText.value, title.value, description.value)
    const problems = validateManualStoryboard(parsed.title, parsed.storyboard)
    if (problems.length) throw new Error(problems.join(' '))
    title.value = parsed.title; description.value = parsed.description; storyboard.value = parsed.storyboard
    mode.value = 'structured'; error.value = ''
  } catch (cause) { error.value = cause instanceof SyntaxError ? 'JSON 格式不正确，请检查括号、逗号和引号。' : cause instanceof Error ? cause.message : 'JSON 导入失败。' }
}

async function readFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file || locked.value) return
  const targetKey = storageKey.value
  try {
    if (file.size > 2 * 1024 * 1024) throw new Error('JSON 文件不能超过 2 MB。')
    const content = await file.text()
    if (targetKey !== storageKey.value || locked.value) return
    jsonText.value = content
    importJson()
  } catch (cause) { if (targetKey === storageKey.value) error.value = cause instanceof Error ? cause.message : '文件读取失败。' }
  finally { (event.target as HTMLInputElement).value = '' }
}

async function submit() {
  if (submitting.value || props.busy || completed.value || apiMode === 'mock') return
  if (!pending.value) {
    if (!canSubmit.value || mode.value !== 'structured') return
    pending.value = { request_id: `manual-storyboard-${crypto.randomUUID()}`, expected_content_revision: props.revision,
      title: title.value.trim(), description: description.value.trim(), takeover_reason: reason.value.trim(),
      storyboard: storyboard.value.map(shot => ({ ...shot, visual: shot.visual.trim(), dialogue: shot.dialogue.trim(),
        sound_effects: shot.sound_effects.trim(), prompt: shot.prompt.trim() || `${shot.visual.trim()}；${shot.dialogue.trim()}；音效：${shot.sound_effects.trim()}` })),
      manual_ack: true, facts_ack: true }
    // Keep the exact intent on an unknown response; retrying must reuse its request ID.
    saveDraft()
  }
  const request = pending.value
  const targetWork = props.workId
  const targetKey = storageKey.value
  submitting.value = true; error.value = ''
  try {
    const receipt = await httpApi.createManualStoryboard(targetWork, request)
    if (!receipt.ok) throw new Error('此次保存结果尚未确认，请重新确认原提交。')
    try { localStorage.removeItem(targetKey) } catch { /* The server receipt confirms creation. */ }
    if (targetKey !== storageKey.value) return
    completed.value = true; pending.value = null
    emit('created', receipt)
  } catch (cause) {
    if (targetKey !== storageKey.value) return
    error.value = cause instanceof Error ? cause.message : '此次保存结果尚未确认，请重新确认原提交。'
    if (cause instanceof ApiError && cause.status !== null && cause.status < 500) pending.value = null
    saveDraft()
  } finally { submitting.value = false }
}
</script>

<template>
  <section class="manual-creator" aria-label="人工创作分镜剧本" :aria-busy="submitting">
    <header class="creator-heading"><div><h2>人工创作分镜剧本</h2><p>人工接管 · 未执行模型写稿与审核</p></div>
      <button class="ghost sm" type="button" :disabled="submitting" @click="emit('cancel')">关闭</button>
    </header>
    <p v-if="apiMode === 'mock'" class="error" role="alert">模拟模式不支持保存人工稿，请连接生产后端。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="storageWarning" class="warning" role="alert">{{ storageWarning }}</p>
    <div v-if="pending" class="pending-state"><p>原提交结果未确认，稿件已锁定。</p>
      <button type="button" class="primary" :disabled="submitting || busy" @click="submit">{{ submitting ? '确认中…' : '重新确认原提交' }}</button>
    </div>
    <fieldset :disabled="locked">
      <div class="mode-switch" role="group" aria-label="人工创作方式">
        <label :class="{ selected: mode === 'structured' }"><input v-model="mode" type="radio" value="structured" />手写分镜</label>
        <label :class="{ selected: mode === 'json' }"><input v-model="mode" type="radio" value="json" />导入 JSON</label>
      </div>
      <div class="metadata-fields"><label class="field"><span>标题</span><input v-model="title" type="text" maxlength="500" /></label>
        <label class="field"><span>简介（选填）</span><textarea v-model="description" rows="2"></textarea></label>
      </div>
      <template v-if="mode === 'json'">
        <label class="field"><span>分镜 JSON</span><textarea v-model="jsonText" class="json-input" rows="10" spellcheck="false"></textarea></label>
        <div class="import-tools"><label class="file-field"><span>选择 JSON 文件</span><input type="file" accept=".json,application/json" @change="readFile" /></label>
          <button type="button" :disabled="!jsonText.trim()" @click="importJson">解析并编辑</button>
        </div>
        <details class="json-fields"><summary>JSON 字段</summary><p><code>{ title, description, storyboard: [...] }</code> 或镜头数组</p>
          <dl><dt>shot_index</dt><dd>从 1 连续递增的镜头编号</dd><dt>duration_seconds</dt><dd>2 到 30 秒的整数</dd>
            <dt>visual / dialogue / sound_effects</dt><dd>画面、台词、声音字符串；没有台词或音效填写“无”</dd><dt>prompt</dt><dd>镜头生成提示字符串（可选）</dd></dl>
        </details>
      </template>
      <template v-else>
        <div class="shot-summary"><strong>{{ storyboard.length }} 个镜头</strong><span>总时长 {{ totalDuration }} 秒</span></div>
        <section v-for="(shot, index) in storyboard" :key="index" class="manual-shot">
          <header><h3>镜头 {{ shot.shot_index }}</h3><div class="shot-tools">
            <button type="button" class="ghost sm" :disabled="index === 0" @click="moveShot(index, -1)">上移</button>
            <button type="button" class="ghost sm" :disabled="index === storyboard.length - 1" @click="moveShot(index, 1)">下移</button>
            <button type="button" class="danger sm" @click="removeShot(index)">删除</button>
          </div></header>
          <label class="field duration-field"><span>时长（秒）</span><input v-model.number="shot.duration_seconds" type="number" min="2" max="30" step="1" /></label>
          <div class="shot-fields"><label class="field"><span>画面</span><textarea v-model="shot.visual" rows="3"></textarea></label>
            <label class="field"><span>台词 / 旁白</span><textarea v-model="shot.dialogue" rows="3"></textarea></label>
            <label class="field"><span>音效 / 环境声</span><textarea v-model="shot.sound_effects" rows="2"></textarea></label>
            <label class="field"><span>镜头生成提示（选填）</span><textarea v-model="shot.prompt" rows="2"></textarea></label>
          </div>
        </section>
        <button type="button" :disabled="storyboard.length >= 30" @click="addShot">新增镜头</button>
      </template>
      <div class="takeover-fields"><label class="field"><span>人工接管原因</span><textarea v-model="reason" rows="2" maxlength="4000"></textarea></label>
        <label class="ack-field"><input v-model="manualAck" type="checkbox" /><span>确认由我接管创作和内容审核，本稿未执行智能体写稿与审核。</span></label>
        <label class="ack-field"><input v-model="factsAck" type="checkbox" /><span>已核对稿件事实、版权与发布规范，对内容负责。</span></label>
      </div>
    </fieldset>
    <ul v-if="mode === 'structured' && validation.length" class="validation-list"><li v-for="problem in validation" :key="problem">{{ problem }}</li></ul>
    <footer class="creator-actions"><button type="button" class="primary" :disabled="!canSubmit || mode !== 'structured'" @click="submit">{{ submitting ? '保存中…' : '保存并进入人工审核' }}</button></footer>
  </section>
</template>

<style scoped>
.manual-creator { min-width: 0; padding: 24px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.creator-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.creator-heading h2 { font-size: 18px; }
.creator-heading p { margin: 7px 0 0; color: var(--amber); font-size: 12px; }
fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.mode-switch { display: flex; gap: 8px; margin-bottom: 20px; }
.mode-switch label { display: flex; align-items: center; gap: 6px; padding: 6px 12px; border: 1px solid var(--line); border-radius: 4px; font-size: 13px; }
.mode-switch input, .ack-field input { width: auto; margin: 0; accent-color: var(--blue); }
.mode-switch .selected { border-color: var(--blue); color: var(--blue); background: var(--blue-wash); }
.metadata-fields, .shot-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.field { min-width: 0; margin-bottom: 14px; }
.field textarea, .field input { width: 100%; box-sizing: border-box; }
.shot-summary { display: flex; align-items: center; gap: 20px; padding: 16px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
.shot-summary span { color: var(--fg-3); }
.manual-shot { padding: 20px 0; border-bottom: 1px solid var(--line); margin-bottom: 18px; }
.manual-shot header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.manual-shot h3 { font-size: 15px; }
.shot-tools { display: flex; gap: 5px; }
.duration-field { max-width: 180px; }
.takeover-fields { padding-top: 24px; }
.ack-field { display: flex; align-items: flex-start; gap: 9px; margin-top: 12px; line-height: 1.75; font-size: 13px; }
.ack-field input { flex: none; margin-top: 5px; }
.creator-actions { padding-top: 20px; }
.creator-actions button { max-width: 100%; }
.error, .validation-list { color: var(--red); font-size: 13px; overflow-wrap: anywhere; }
.validation-list { padding-left: 20px; margin: 18px 0 0; }
.warning { color: var(--amber); font-size: 12px; }
.pending-state { padding: 10px 0 20px; color: var(--amber); }
.json-input { font-family: ui-monospace, monospace; }
.import-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 16px; }
.file-field { display: flex; flex-direction: column; gap: 8px; min-width: 0; font-size: 13px; }
.file-field input { max-width: 100%; }
.json-fields { margin: 16px 0; color: var(--fg-2); font-size: 12px; }
.json-fields dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 10px; }
.json-fields dt, .json-fields dd, .json-fields code { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 720px) { .metadata-fields, .shot-fields { grid-template-columns: minmax(0, 1fr); gap: 0; } .creator-heading { flex-wrap: wrap; } }
</style>
