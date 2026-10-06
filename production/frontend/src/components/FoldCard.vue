<script setup lang="ts">
import { ref, watch } from 'vue'

/** 选项按钮：彩色大按钮，一眼看出可点击。
 * 关闭时整个块是彩色大按钮（白字大标题 + 浮起 hover）。
 * 展开时标题区保持彩色，内容区白底。
 * tone 对应不同场景：
 *   accent = 主操作（新建、开始）· 蓝
 *   warn = 待办（必须先做的事）· 橙
 *   purple = 辅助管理（服装、设置）· 紫
 *   danger = 危险/删除区 · 红
 */
const props = defineProps<{
  title: string
  hint?: string
  badge?: string
  tone?: 'accent' | 'warn' | 'purple' | 'danger' | 'default'
  open?: boolean
}>()
const emit = defineEmits<{ toggle: [open: boolean] }>()

const isOpen = ref(props.open ?? false)
watch(() => props.open, (v) => { if (v !== undefined) isOpen.value = v })

function onToggle(e: Event) {
  isOpen.value = (e.target as HTMLDetailsElement).open
  emit('toggle', isOpen.value)
}
</script>

<template>
  <details
    class="fold-card"
    :class="[tone ?? 'default', { 'is-open': isOpen }]"
    :open="isOpen"
    @toggle="onToggle"
  >
    <summary>
      <div class="fold-text">
        <span class="fold-title">{{ title }}</span>
        <span v-if="hint" class="fold-hint">{{ hint }}</span>
      </div>
      <span v-if="badge" class="fold-badge">{{ badge }}</span>
      <span class="fold-chevron" aria-hidden="true"></span>
    </summary>
    <div class="fold-body">
      <slot />
    </div>
  </details>
</template>

<style scoped>
/* 色块按钮：关闭时是彩色大按钮 */
.fold-card {
  border-radius: 12px;
  border: 0;
  margin: 14px 0;
  overflow: hidden;
  cursor: pointer;
  transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
}
.fold-card:hover:not(.is-open) {
  transform: translateY(-1px);
  box-shadow: 0 6px 18px rgba(0,0,0,.10);
}

/* 各 tone 配色（关闭时背景色 + hover 加深） */
.fold-card.accent  { background: #378ADD; }
.fold-card.accent:hover:not(.is-open)  { background: #1e6fc9; }
.fold-card.warn    { background: #BA7517; }
.fold-card.warn:hover:not(.is-open)    { background: #93571a; }
.fold-card.purple  { background: #7F77DD; }
.fold-card.purple:hover:not(.is-open)  { background: #6059c9; }
.fold-card.danger  { background: #A32D2D; }
.fold-card.danger:hover:not(.is-open)  { background: #7c2222; }
.fold-card.default { background: #5F5E5A; }
.fold-card.default:hover:not(.is-open) { background: #464644; }

/* summary：大按钮 */
.fold-card > summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 20px 22px;
  font-size: 17px;
  font-weight: 600;
  color: #fff;
  line-height: 1.3;
}
.fold-card > summary::-webkit-details-marker { display: none; }

.fold-text { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 0; }
.fold-title { font-size: 17px; font-weight: 600; letter-spacing: 0.1px; }
.fold-hint { font-size: 13px; font-weight: 400; opacity: .88; }

.fold-badge {
  flex: none;
  font-size: 11px;
  font-weight: 500;
  padding: 3px 10px;
  border-radius: 10px;
  background: rgba(255,255,255,.22);
  color: #fff;
}

.fold-chevron {
  flex: none;
  width: 9px;
  height: 9px;
  border-right: 2px solid rgba(255,255,255,.85);
  border-bottom: 2px solid rgba(255,255,255,.85);
  transform: rotate(45deg);
  transition: transform .18s ease;
}

/* 展开时：标题区变回面板色，文字变黑 */
.fold-card.is-open {
  background: var(--panel);
  border: 1px solid var(--line);
  transform: none;
  box-shadow: none;
}
.fold-card.is-open > summary {
  color: var(--fg);
  padding: 18px 22px;
  border-bottom: 1px solid var(--line);
}
.fold-card.is-open .fold-hint { opacity: 1; color: var(--fg-3); }
.fold-card.is-open .fold-badge { background: var(--panel-2); color: var(--fg-2); }
.fold-card.is-open .fold-chevron {
  border-color: var(--fg-3);
  transform: rotate(-135deg);
}
.fold-card.is-open.accent { border-color: #378ADD; }
.fold-card.is-open.accent .fold-title { color: #1e6fc9; }
.fold-card.is-open.warn { border-color: #BA7517; }
.fold-card.is-open.warn .fold-title { color: #8f5713; }
.fold-card.is-open.purple { border-color: #7F77DD; }
.fold-card.is-open.purple .fold-title { color: #6059c9; }
.fold-card.is-open.danger { border-color: #A32D2D; }
.fold-card.is-open.danger .fold-title { color: #7c2222; }

/* 内容区 */
.fold-body { padding: 18px 22px 20px; }
.fold-body > :first-child { margin-top: 0; }
</style>