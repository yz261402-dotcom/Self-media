<script setup lang="ts">
import { computed } from 'vue'
import type { QualifyProgress } from '@/api/types'

/**
 * 定版链的四步进度。顺序、完成状态与"这一步为什么需要人做"全部来自后端
 * (`registry.qualify_steps`)，前端只负责渲染。
 *
 * 存在的理由：四步此前是四块同质的表单叠在一起，用户看不出自己停在哪里、
 * 下一步该点哪个，也看不出这四步与"选用"是前提/引用的关系而不是两条并列的路。
 */
const props = defineProps<{ qualify: QualifyProgress | null }>()

const steps = computed(() => props.qualify?.steps ?? [])
const currentIndex = computed(() => {
  const key = props.qualify?.current
  if (!key) return -1
  return steps.value.findIndex((s) => s.key === key)
})
const currentStep = computed(() => (currentIndex.value >= 0 ? steps.value[currentIndex.value] : null))
const allDone = computed(() => !!props.qualify && props.qualify.done_count === props.qualify.total)
</script>

<template>
  <div v-if="qualify" class="q">
    <div class="q-track">
      <div
        v-for="(s, i) in steps"
        :key="s.key"
        class="q-step"
        :class="{ done: s.done, now: i === currentIndex }"
      >
        <span class="q-mark">{{ String(i + 1).padStart(2, '0') }}</span>
        <span class="q-label">{{ s.label }}</span>
        <span v-if="s.done" class="q-status">已完成</span>
      </div>
    </div>

    <!-- 说清"你现在停在哪、为什么"——没有这句话，四步只是四块表单 -->
    <div v-if="allDone" class="q-now ok">
      <b>人物确认已完成。</b>
      可以为当前作品选用。
    </div>
    <div v-else-if="qualify.closed_incomplete" class="q-now warn">
      <b>这版人物的确认记录不完整（{{ qualify.done_count }}/{{ qualify.total }}）。</b>
      复制为新草稿后完善，旧作品继续保留原人物。
    </div>
    <div v-else-if="currentStep" class="q-now">
      <b>下一步：{{ currentStep.label }}</b>
      <span>完成下方第 {{ currentIndex + 1 }} 步，再继续确认。</span>
    </div>
  </div>
</template>

<style scoped>
.q-track { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-bottom: 1px solid var(--line); }
.q-step { display: flex; flex-wrap: wrap; align-items: baseline; gap: 5px 9px; padding: 12px 12px 13px 0; color: var(--fg-3); font-size: 12px; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.q-step.done { color: var(--fg-2); }
.q-step.now { border-bottom-color: var(--fg); color: var(--fg); font-weight: 600; }
.q-mark { font-size: 11px; font-variant-numeric: tabular-nums; color: var(--fg-3); font-weight: 400; }
.q-status { font-size: 10px; color: var(--fg-3); }
.q-now { padding-top: 13px; font-size: 12px; color: var(--fg-2); line-height: 1.7; }
.q-now b { font-weight: 500; color: var(--fg); margin-right: 8px; }
@media (max-width: 650px) {
  .q-track { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .q-step { font-size: 11px; padding: 10px 8px 10px 0; }
}
</style>