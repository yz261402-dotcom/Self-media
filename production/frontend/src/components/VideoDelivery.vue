<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useStudioStore } from '@/stores/studio'
import StageProgress from './StageProgress.vue'

const store = useStudioStore()
const reviewed = ref(false)
const nativeDelivery = computed(() => store.pkg?.generation_mode === 'image_prompt_native_audio' || store.finalVideo?.generation_mode === 'image_prompt_native_audio' || (!store.finalVideo && store.currentRoute === 'WAN_3_0'))
watch(() => [store.currentWorkId, store.postprocess?.execution?.id, store.postprocess?.preview_url], () => { reviewed.value = false })
const deliveryStages = ['postprocess', 'video_review', 'publish_package']
const postprocessTask = computed(() => {
  const task = store.postprocess?.task
  if (!task || !deliveryStages.includes(task.stage)) return null
  const execution = store.postprocess?.execution
  if (execution && 'task_id' in execution && execution.task_id !== task.id) return null
  return { ...task, stage_rows: (task.stage_rows ?? []).filter((row) => deliveryStages.includes(row.stage)) }
})
const processing = computed(() => ['queued', 'running', 'waiting_provider', 'retry_wait'].includes(postprocessTask.value?.state ?? ''))
const postprocessFailure = computed(() => {
  if (store.postprocess?.error) return null
  const task = postprocessTask.value
  if (!task || task.work_id !== store.currentWorkId || !['blocked', 'failed'].includes(task.state)) return null
  const currentTaskId = store.currentWork?.current_task_id
  if (currentTaskId && currentTaskId !== task.id) return null
  if (task.last_error?.message) return task.last_error.message
  const execution = store.postprocess?.execution
  if (!execution || !('task_id' in execution) || execution.task_id !== task.id) return null
  if ('work_id' in execution && execution.work_id !== task.work_id) return null
  return execution.error?.message || null
})
const preview = computed(() => store.deliveryReady ? store.pkg?.video_url : store.postprocess?.preview_url)
</script>

<template>
  <section id="delivery" class="delivery-stage">
    <header class="delivery-heading"><span class="delivery-index">03</span><div><h2>成片与下载</h2><p>{{ nativeDelivery ? '保留模型生成的画面与原声，观看确认后下载。' : '加字幕、整理画面与音量，最后确认成片。' }}</p></div><span v-if="store.deliveryReady" class="delivery-state">已完成</span></header>
    <p v-if="store.postprocess?.error" role="alert" class="step-blocker">{{ store.postprocess.error.message }}</p>
    <div class="delivery-layout" :class="{ 'has-preview': !!preview }">
      <div v-if="preview" class="delivery-preview"><video :src="preview" controls preload="metadata" aria-label="成片预览"></video><span>成片预览</span></div>
      <div class="delivery-controls">
      <template v-if="store.deliveryReady">
        <h3>{{ store.pkg?.title || '你的作品已完成' }}</h3>
        <p class="muted">{{ nativeDelivery ? '原声视频已整理好，可以下载使用；当前不含自动字幕。' : '视频、字幕、封面和投稿文案已整理好，可以下载使用。' }}</p>
        <a :href="store.pkg?.download_url ?? undefined" class="action-link primary download-main" download>下载完整作品包</a>
        <div class="download-links"><a v-if="store.pkg?.video_url" :href="store.pkg.video_url" class="action-link" download>单独下载视频</a><a v-if="store.pkg?.cover_url" :href="store.pkg.cover_url" class="action-link" download>下载封面</a></div>
        <p class="delivery-note">自动发布正在开发。当前请下载作品后，前往平台投稿。</p>
      </template>
      <template v-else-if="nativeDelivery">
        <h3>观看并确认原声视频</h3>
        <p class="muted">请在“原片”页观看视频并确认画面、台词与声音，即可生成下载包。保留模型原声，不含自动字幕。</p>
      </template>
      <template v-else>
        <h3>{{ processing ? '正在制作成片' : store.postprocess?.ready_for_review ? '观看并确认这版成片' : '生成带字幕的成片' }}</h3>
        <p class="muted">使用上一步确认的口播原片和音频，自动生成字幕、规范画幅与音量。</p>
        <p v-if="!store.finalVideo" class="delivery-prerequisite">还需要一条已审核、已确认的口播原片。请先完成上方第 02 步。</p>
        <div class="btn-row">
          <button class="primary" :disabled="!store.finalVideo || !!store.busy || processing" @click="store.startPostprocess()">
            {{ processing ? '正在制作成片…' : store.postprocess?.preview_url ? '重新生成字幕与成片' : '生成字幕与成片' }}
          </button>
          <button class="sm" :disabled="!!store.busy" @click="store.guard('刷新成片进度', () => store.refreshPostprocess())">刷新进度</button>
        </div>
        <p v-if="postprocessFailure" role="alert" class="step-blocker">{{ postprocessFailure }}</p>
        <p class="delivery-note">字幕识别服务需要能够连接。处理完成后，此处会显示成片。</p>
        <div v-if="processing && postprocessTask" class="bar" :class="{ indeterminate: postprocessTask.percent == null }" role="progressbar" aria-label="成片处理进度" :aria-valuenow="postprocessTask.percent ?? undefined"><i :style="{ width: `${postprocessTask.percent ?? 0}%` }"></i></div>
        <details v-if="postprocessTask" class="advanced"><summary>后期阶段明细</summary><StageProgress :task="postprocessTask" /></details>
      </template>

      <div v-if="store.postprocess?.ready_for_review && !store.deliveryReady && !nativeDelivery" class="delivery-confirmation">
        <label class="ack-label"><input v-model="reviewed" type="checkbox" />我已观看这版成片，确认字幕、画面、声音和片尾没有问题</label>
        <button class="primary" :disabled="!reviewed || !!store.busy" @click="store.confirmPostprocess()">确认成片并生成下载包</button>
      </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.delivery-stage { margin-top: 36px; scroll-margin-top: 24px; }
.delivery-heading { display: flex; align-items: flex-start; gap: 20px; padding-bottom: 20px; border-bottom: 1px solid var(--line); }
.delivery-index { font-size: 30px; line-height: 1.2; font-weight: 400; letter-spacing: -1.5px; color: var(--fg-3); }
.delivery-heading h2 { font-size: 20px; margin-bottom: 5px; }
.delivery-heading p { margin: 0; font-size: 13px; color: var(--fg-3); }
.delivery-state { margin-left: auto; font-size: 12px; color: var(--fg-2); padding-top: 5px; }
.delivery-layout { padding: 24px 0; }
.delivery-layout.has-preview { display: grid; grid-template-columns: minmax(180px,300px) minmax(0,1fr); gap: 30px; }
.delivery-preview video { display: block; width: 100%; max-height: 520px; object-fit: contain; background: #000; }
.delivery-preview > span { display: block; margin-top: 8px; color: var(--fg-3); font-size: 12px; }
.delivery-controls h3 { margin-bottom: 12px; font-size: 18px; }
.delivery-controls > p { font-size: 13px; line-height: 1.8; margin-bottom: 18px; }
.delivery-controls > .delivery-note { margin: 16px 0 0; font-size: 12px; color: var(--fg-3); }
.delivery-prerequisite { padding-left: 12px; border-left: 2px solid var(--line-2); color: var(--fg-2); }
.download-main { display: inline-flex; padding: 11px 18px; }
.download-links { display: flex; flex-wrap: wrap; gap: 20px; margin-top: 18px; font-size: 13px; }
.delivery-confirmation { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--line); }
.delivery-confirmation .ack-label { margin-bottom: 16px; }
@media (max-width: 720px) { .delivery-layout.has-preview { grid-template-columns: minmax(0,1fr); } .delivery-preview { max-width: 280px; } .delivery-heading { gap: 14px; } }
</style>
