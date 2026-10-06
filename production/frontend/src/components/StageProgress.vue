<script setup lang="ts">
import type { Task } from '@/api/types'
const props = defineProps<{ task: Task }>()
const labels: Record<string, string> = {
  topic: '准备主题', script: '生成三篇口播稿', content_review: '检查稿件',
  avatar_variant: '准备人物画面', tts: '合成语音', talking_head: '生成口播视频',
  postprocess: '添加字幕与整理画面', video_review: '确认成片', publish_package: '打包下载文件',
}
</script>

<template>
  <div>
    <!-- 无真实百分比时只显示不定态，不做匀速假进度 -->
    <div
      v-if="props.task.state === 'running' || props.task.state === 'queued'"
      class="bar"
      :class="{ indeterminate: props.task.percent === null }"
      style="margin: 12px 14px"
    >
      <i :style="{ width: (props.task.percent ?? 0) + '%' }"></i>
    </div>
    <div class="stage-list">
      <div
        v-for="s in props.task.stage_rows"
        :key="s.stage"
        class="stage-row"
        :class="s.state === 'done' ? 'done' : s.state === 'active' ? 'active' : s.state === 'blocked' ? 'blocked' : s.state === 'failed' ? 'failed' : ''"
      >
        <span class="stage-mark">
          <svg viewBox="0 0 10 10" fill="none">
            <path d="M1.5 5.2 4 7.5 8.5 2.5" stroke="#ffffff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <span style="flex: 1">{{ labels[s.stage] || s.label }}</span>
      </div>
    </div>
  </div>
</template>
