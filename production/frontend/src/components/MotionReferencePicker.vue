<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import type { MotionTemplateView } from '@/api/types'
import { useStudioStore } from '@/stores/studio'
import MotionUpload from '@/components/MotionUpload.vue'

const store = useStudioStore()
const identityId = computed(() => store.currentWork?.identity_id)
const approved = computed(() => store.motionCatalog?.approved ?? [])
const intent = computed(() => store.currentWork?.motion_intent)
const currentLabel = computed(() => approved.value.find(item => item.id === intent.value?.motion_ref_id)?.label || intent.value?.motion_key || '')
const selectedRefId = ref('')
const libraryOpen = ref(false)
const templates = ref<MotionTemplateView[]>([])
const libraryNote = ref('')
const selectedTemplateId = ref('')
const selected = computed(() => templates.value.find(item => item.id === selectedTemplateId.value))
const loading = ref(false)
const importing = ref(false)
const acknowledged = ref(false)
const previewFailed = ref(false)
const error = ref('')
let contextVersion = 0
let requestVersion = 0

watch(() => [store.currentWorkId, store.workLoadVersion, identityId.value], () => {
  contextVersion++
  requestVersion++
  selectedRefId.value = store.currentWork?.motion_intent?.motion_ref_id ?? ''
  libraryOpen.value = false
  templates.value = []
  selectedTemplateId.value = ''
  acknowledged.value = false
  previewFailed.value = false
  loading.value = false
  importing.value = false
  error.value = ''
  libraryNote.value = ''
}, { flush: 'sync' })
watch(() => intent.value?.motion_ref_id, id => { selectedRefId.value = id ?? '' }, { immediate: true })
watch(selectedTemplateId, () => { acknowledged.value = false; previewFailed.value = false }, { flush: 'sync' })

function captureContext() {
  const version = contextVersion
  const workId = store.currentWorkId
  const loadVersion = store.workLoadVersion
  const personId = identityId.value
  return {
    personId,
    matches: () => version === contextVersion && store.workContextMatches(workId, loadVersion) && identityId.value === personId,
  }
}

async function openLibrary() {
  if (!store.currentWorkId || !identityId.value) return
  const context = captureContext()
  const request = ++requestVersion
  libraryOpen.value = true
  loading.value = true
  error.value = ''
  acknowledged.value = false
  try {
    const result = await api.listMotionTemplates()
    if (!context.matches() || request !== requestVersion) return
    templates.value = result.templates
    libraryNote.value = result.note ?? ''
    selectedTemplateId.value = ''
  } catch (cause: any) {
    if (context.matches() && request === requestVersion) error.value = cause?.message ?? '动作库读取失败，请重试。'
  } finally {
    if (context.matches() && request === requestVersion) loading.value = false
  }
}

async function adoptExisting() {
  const chosen = approved.value.find(item => item.id === selectedRefId.value)
  if (!chosen || store.busy || importing.value) return
  await store.adoptMotion(chosen.motion_key, chosen.id)
}

async function importAndAdopt() {
  const chosen = selected.value
  const context = captureContext()
  if (!context.personId || !store.currentWorkId || !chosen?.available || !chosen.preview_url || !acknowledged.value || previewFailed.value || importing.value || store.busy) return
  importing.value = true
  error.value = ''
  try {
    const reference = await api.importMotionTemplate(context.personId, chosen.id)
    if (!context.matches()) return
    if (reference.identity_id !== context.personId || reference.approval_state !== 'approved') {
      error.value = '动作添加结果不属于当前人物或仍待确认，请重新加载。'
      return
    }
    await store.loadMotions()
    if (!context.matches()) return
    const saved = await store.adoptMotion(reference.motion_key, reference.id)
    if (!context.matches()) return
    if (saved) {
      selectedRefId.value = reference.id
      libraryOpen.value = false
    }
  } catch (cause: any) {
    if (context.matches()) error.value = cause?.message ?? '动作添加失败，请重试。'
  } finally {
    if (context.matches()) importing.value = false
  }
}
</script>

<template>
  <details class="motion-section" :open="!intent?.motion_ref_id">
    <summary><span>动作参考</span><small>使用本地 M2 方式时设置</small></summary>
    <div class="motion-content">
      <p class="motion-help">选择表情、眨眼和轻微头部动作。口型在确认配音后，由视频合成步骤生成。</p>
      <p v-if="!store.currentWorkId || !identityId" class="motion-help">先在顶部选择作品，并为作品选用一位人物，再设置动作。</p>
      <template v-else>
        <p v-if="intent?.motion_ref_id" class="motion-current"><strong>本期动作：{{ currentLabel }}</strong></p>
        <p v-else class="motion-current"><strong>这期还没有绑定动作。</strong> 从已有动作中预览并添加，无需重新生成参考视频。</p>
        <div v-if="approved.length" class="existing-motion">
          <label class="field"><span>此人物已有的动作</span>
            <select v-model="selectedRefId" :disabled="!!store.busy || importing">
              <option value="">请选择动作</option>
              <option v-for="item in approved" :key="item.id" :value="item.id">{{ item.label || item.motion_key }}</option>
            </select>
          </label>
          <button class="sm" :disabled="!selectedRefId || selectedRefId === intent?.motion_ref_id || !!store.busy || importing" @click="adoptExisting">使用这个动作</button>
        </div>
        <button class="sm" :class="{ primary: !intent?.motion_ref_id }" :disabled="loading || importing || !!store.busy" @click="openLibrary">{{ loading ? '正在读取动作…' : intent?.motion_ref_id ? '从动作库更换' : '从已有动作添加' }}</button>
        <MotionUpload />

        <section v-if="libraryOpen" class="motion-library" aria-label="已有动作库" :aria-busy="loading">
          <div class="library-heading"><h3>已有动作库</h3><button class="sm" :disabled="importing" @click="libraryOpen = false">收起</button></div>
          <p v-if="loading" class="motion-help">正在检查已有动作和预览文件…</p>
          <template v-else>
            <p v-if="libraryNote" class="motion-help">{{ libraryNote }}</p>
            <p v-if="!templates.length && !error" class="motion-help">动作库中还没有可复用的真实参考视频。需要先登记来源明确的动作视频，才能生成 M2 底片。</p>
            <p v-else-if="templates.length && !templates.some(item => item.available)" class="motion-help">已有动作暂时无法使用，原因见下方。请先修复参考文件或来源记录。</p>
            <div class="template-options">
              <button v-for="item in templates" :key="item.id" class="template-option" :class="{ selected: item.id === selectedTemplateId }" :aria-pressed="item.id === selectedTemplateId" :disabled="importing" @click="selectedTemplateId = item.id">
                <strong>{{ item.label || item.motion_key }}</strong><span>{{ item.description }}</span>
                <small v-if="!item.available">暂不可用：{{ item.unavailable_reason || '参考文件或使用依据尚未就绪' }}</small>
              </button>
            </div>
            <article v-if="selected" class="template-detail">
              <div class="template-preview">
                <video v-if="selected.preview_url && selected.available" :key="selected.id" :src="selected.preview_url" controls playsinline preload="metadata" @error="previewFailed = true" />
                <p v-if="previewFailed" class="motion-error" role="alert">预览视频无法播放，暂不能添加。请检查参考文件后重试。</p>
                <p v-if="!selected.available || !selected.preview_url" class="motion-error">{{ selected.unavailable_reason || '此动作没有可播放的真实参考视频。' }}</p>
              </div>
              <div class="template-confirmation">
                <h4>{{ selected.label }}</h4>
                <p>{{ selected.description }}</p>
                <p v-if="selected.loop_seconds">参考片段 {{ selected.loop_seconds }} 秒；较长视频会复用动作片段，可能看出重复。</p>
                <p v-else>该参考没有登记循环时长，生成底片前需检查时长限制。</p>
                <p>这里预览的是参考动作。你的主播会在视频合成中生成底片，之后仍需检查身份与动作效果。</p>
                <details class="source-details"><summary>查看来源与使用说明</summary><p>来源人物：{{ selected.source_identity_name || '未记录' }}</p><p>{{ selected.note || '未附加说明。' }}</p></details>
                <label class="check-line"><input v-model="acknowledged" type="checkbox" :disabled="!selected.available || !selected.preview_url || previewFailed || importing" /><span>我已观看参考视频，确认用于当前人物的动作</span></label>
                <button class="primary sm" :disabled="!acknowledged || !selected.available || !selected.preview_url || previewFailed || importing || !!store.busy" @click="importAndAdopt">{{ importing ? '正在添加并保存…' : '添加并使用此动作' }}</button>
                <p class="motion-help">添加仅保存动作绑定，不调用模型，不产生生成费用。</p>
              </div>
            </article>
          </template>
        </section>
        <p v-if="error" class="motion-error" role="alert">{{ error }}</p>
        <p v-if="intent?.motion_ref_id" class="motion-help">动作已保存。完成配音后，进入「视频合成」→「准备动作底片」。</p>
        <details class="source-details"><summary>动作能力与绑定记录</summary><p>LivePortrait 迁移参考视频的眼睑、表情和头部姿态，MuseTalk 根据音频处理口型。目前不支持通过文字生成任意肢体动作。</p><p v-if="intent?.motion_ref_id">当前绑定：{{ intent.motion_ref_id }}</p></details>
      </template>
    </div>
  </details>
</template>

<style scoped>
.motion-section {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel-2);
  margin: 10px 0;
  transition: background .15s ease, border-color .15s ease;
}
.motion-section:hover { background: var(--panel); border-color: var(--line-2); }
.motion-section > summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  font-size: 15px;
  font-weight: 500;
  color: var(--fg);
  line-height: 1.3;
}
.motion-section > summary::-webkit-details-marker { display: none; }
.motion-section > summary::after {
  content: '';
  flex: none;
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-right: 1.5px solid var(--fg-3);
  border-bottom: 1.5px solid var(--fg-3);
  transform: rotate(45deg);
  transition: transform .18s ease;
}
.motion-section[open] > summary::after { transform: rotate(-135deg); }
.motion-section[open] > summary { border-bottom: 1px solid var(--line); }
.motion-section > summary small { margin-left: 0; color: var(--fg-3); font-size: 12px; font-weight: 400; }
.motion-content { padding: 18px; }
.motion-help { margin: 12px 0; color: var(--fg-3); font-size: 12px; line-height: 1.7; }
.motion-current { margin: 0 0 16px; font-size: 13px; line-height: 1.7; }
.existing-motion { display: flex; align-items: flex-end; gap: 12px; margin-bottom: 16px; }
.existing-motion .field { margin: 0; min-width: 220px; }
.motion-library { margin-top: 20px; border: 1px solid var(--line); background: var(--panel); padding: 20px; }
.library-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.library-heading h3 { margin: 0; font-size: 15px; }
.template-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin-top: 16px; }
.template-option { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; text-align: left; white-space: normal; padding: 14px; height: auto; }
.template-option.selected { border-color: var(--blue); }
.template-option span, .template-option small { font-size: 12px; color: var(--fg-3); line-height: 1.7; }
.template-detail { display: grid; grid-template-columns: minmax(160px, 240px) minmax(0, 1fr); gap: 24px; margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--line); }
.template-preview video { display: block; width: 100%; max-height: 330px; background: #111; }
.template-confirmation h4 { margin: 0 0 10px; font-size: 15px; }
.template-confirmation p, .source-details { font-size: 12px; color: var(--fg-2); line-height: 1.7; }
.source-details { margin: 16px 0; }
.source-details summary { cursor: pointer; color: var(--fg-3); }
.motion-error { color: var(--red); font-size: 12px; line-height: 1.7; margin: 12px 0; }
.check-line { margin: 16px 0; }
@media (max-width: 650px) {
  .motion-section > summary small { display: block; margin: 5px 0 0 16px; }
  .existing-motion { flex-wrap: wrap; }
  .existing-motion .field { min-width: 0; width: 100%; }
  .motion-library { padding: 14px; }
  .template-detail { grid-template-columns: 1fr; gap: 16px; }
  .template-preview video { max-width: 240px; }
}
</style>
