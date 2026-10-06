<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useStudioStore } from '@/stores/studio'

defineEmits<{ create: [] }>()
const store = useStudioStore()
const route = useRoute()
const storyboard = computed(() => store.currentWork?.brief.content_route === 'short_video_storyboard')

/** 顶部不再横排 5 步，避免与侧栏 stepHint 重复且占满首屏。
 * 已有作品时只显示一行「下一步」摘要；侧栏已经展示了每步的具体状态。 */
const nativeWan = computed(() => (store.currentRoute ?? store.currentWork?.route) === 'WAN_3_0')
const steps = computed(() => storyboard.value ? [
  { title: '创建作品', done: !!store.currentWork },
  { title: '审核并选定分镜', done: store.scriptReady },
  { title: '镜头制作与下载', done: store.deliveryReady },
] : nativeWan.value ? [
  { title: '创建作品', done: !!store.currentWork },
  { title: '图片与提示词', done: !!store.wanImage && !store.thInputError },
  { title: '观看与下载', done: store.deliveryReady },
] : [
  { title: '创建作品', done: !!store.currentWork },
  { title: '确认人物', done: store.identityReady },
  { title: '三选一稿件', done: store.scriptReady },
  { title: '试听与确认', done: !!store.confirmedAudio },
  { title: '合成与下载', done: store.deliveryReady },
])
const currentStep = computed(() => steps.value.find(s => !s.done) ?? steps.value[steps.value.length - 1])
const doneCount = computed(() => steps.value.filter(s => s.done).length)
const onStepPage = computed(() => route.path === store.nextStep.to)

const summary = computed(() => ({
  work: '新建作品，填写这期要讲的内容。',
  identity: store.currentIdentity ? `${store.currentIdentity.name}：资料待完善。` : '先为这期作品选择人物。',
  script: storyboard.value ? '生成并选定一篇审核通过的分镜剧本。' : '人物已就绪，接下来生成并采用一篇口播稿。',
  tts: '稿件已选定，接下来合成并试听全文。',
  video: storyboard.value ? '确认镜头图片和费用，逐镜头生成并审核成片。' : nativeWan.value ? (store.hasReviewableOriginal ? '有声短片已生成，请观看并确认画面和声音。' : store.unresolvedVideoRuns.length ? '有声短片已提交，等待模型生成画面和声音。' : '上传参考图片，描述画面与声音，确认时长和费用。') : store.finalVideo ? '原片已确认，接下来制作字幕与成片。'
    : store.hasReviewableOriginal ? '口播原片已生成，请观看并审核。'
    : store.unresolvedVideoRuns.length ? '口播任务已提交，正在等待生成结果。'
    : '音频已确认，接下来生成口播视频。',
  download: '成片已完成，可以预览和下载。',
}[store.nextStep.key] || store.nextStep.title))
</script>

<template>
  <section class="workflow" :class="{ 'workflow-empty': !store.currentWork }" aria-label="创作向导">
    <!-- 无作品：大 CTA -->
    <div v-if="!store.currentWork" class="workflow-heading">
      <h1>新建一条口播视频</h1>
      <p>填写主题，依次选择人物、文案和声音，最后生成视频。</p>
      <button class="primary" @click="$emit('create')">创建第一个作品</button>
    </div>

    <!-- 有作品：一行摘要，不再 5 步横排 -->
    <div v-else class="workflow-summary">
      <span class="workflow-pill">{{ doneCount }} / {{ steps.length }}</span>
      <span class="workflow-text">{{ summary }}</span>
      <span class="workflow-spacer"></span>
      <router-link v-if="!onStepPage" :to="store.nextStep.to" class="action-link">前往「{{ store.nextStep.label }}」</router-link>
      <details v-else class="workflow-detail"><summary>查看说明</summary><p>{{ store.nextStep.detail }}</p></details>
    </div>
  </section>
</template>

<style scoped>
.workflow {
  margin-bottom: 12px;
}
.workflow-empty { padding: 8px 0 16px; }
.workflow-heading h1 {
  font-size: 18px;
  font-weight: 500;
  margin: 0 0 6px;
}
.workflow-heading p {
  margin: 0 0 14px;
  color: var(--fg-2);
  font-size: 13px;
}

/* 已有作品时：单行摘要 */
.workflow-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  font-size: 13px;
  color: var(--fg-2);
  border-bottom: 1px solid var(--line);
}
.workflow-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 9px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  font-size: 11px;
  color: var(--fg-2);
  font-weight: 500;
  flex-shrink: 0;
}
.workflow-text {
  color: var(--fg);
  flex: 0 1 auto;
}
.workflow-spacer { flex: 1; }
.workflow-detail summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--fg-2);
}
.workflow-detail p {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--fg-2);
  line-height: 1.7;
}
</style>
