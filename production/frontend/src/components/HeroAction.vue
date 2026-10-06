<script setup lang="ts">
/** HeroAction：每个制作页顶部的"当前要做的一件事"。
 * 大字号标题 + 一句话说明 + 超大主按钮，让用户一眼看到下一步操作。
 * status:
 *   todo = 还没做（普通态）
 *   run  = 进行中（按钮 loading / 文字带"..."）
 *   done = 已完成（绿色标题 + 跳转下一页的 CTA）
 *   block = 前置未完成（CTA 跳到前置页）
 */
defineProps<{
  title: string
  desc?: string
  ctaText?: string
  ctaDisabled?: boolean
  ctaLoading?: boolean
  status?: 'todo' | 'run' | 'done' | 'block'
  secondaryText?: string
  secondaryDisabled?: boolean
}>()
const emit = defineEmits<{ cta: []; secondary: [] }>()
</script>

<template>
  <section class="hero" :class="status ?? 'todo'">
    <div class="hero-left">
      <span v-if="status === 'done'" class="hero-check" aria-hidden="true">✓</span>
      <span v-else-if="status === 'run'" class="hero-dot run" aria-hidden="true"></span>
      <span v-else-if="status === 'block'" class="hero-dot warn" aria-hidden="true"></span>
      <div class="hero-text">
        <h1 class="hero-title">{{ title }}</h1>
        <p v-if="desc" class="hero-desc">{{ desc }}</p>
      </div>
    </div>
    <div class="hero-actions">
      <button
        v-if="ctaText"
        class="hero-cta"
        :class="{ 'cta-block': status === 'block', 'cta-done': status === 'done' }"
        :disabled="ctaDisabled || ctaLoading"
        @click="emit('cta')"
      >
        <template v-if="ctaLoading">
          <span class="hero-spinner" aria-hidden="true"></span>
          {{ ctaText }}…
        </template>
        <template v-else>{{ ctaText }}</template>
      </button>
      <button
        v-if="secondaryText"
        class="hero-secondary"
        :disabled="secondaryDisabled"
        @click="emit('secondary')"
      >
        {{ secondaryText }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 20px 0 24px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 24px;
}

.hero-left {
  display: flex;
  align-items: center;
  gap: 14px;
  flex: 1;
  min-width: 0;
}

.hero-check {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--green, #639922);
  color: var(--panel);
  font-size: 16px;
  font-weight: 500;
  display: flex;
  align-items: center;
  justify-content: center;
}
.hero-dot {
  flex: none;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin: 0 11px;
}
.hero-dot.run { background: var(--blue); }
.hero-dot.warn { background: var(--amber, #BA7517); }

.hero-text { min-width: 0; }
.hero-title {
  font-size: 22px;
  font-weight: 500;
  margin: 0;
  line-height: 1.3;
  color: var(--fg);
}
.hero.done .hero-title { color: var(--fg); }
.hero-desc {
  font-size: 13px;
  color: var(--fg-2);
  margin: 6px 0 0;
  line-height: 1.6;
}

.hero-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: none;
}

.hero-cta {
  height: 48px;
  padding: 0 24px;
  font-size: 15px;
  font-weight: 600;
  background: var(--blue);
  color: #fff;
  border: 0;
  border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0, 47, 167, 0.32);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  transition: background .15s ease;
}
.hero-cta:hover:not(:disabled) { background: var(--blue-d); }
.hero-cta:disabled { opacity: .5; cursor: not-allowed; }
.hero-cta.cta-done { background: var(--green, #639922); }
.hero-cta.cta-done:hover:not(:disabled) { background: #4d7a16; }
.hero-cta.cta-block { background: var(--amber, #BA7517); }
.hero-cta.cta-block:hover:not(:disabled) { background: #8f5713; }

.hero-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid rgba(255,255,255,.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.hero-secondary {
  height: 44px;
  padding: 0 18px;
  font-size: 13px;
  background: transparent;
  color: var(--fg-2);
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  cursor: pointer;
  white-space: nowrap;
}
.hero-secondary:hover:not(:disabled) { color: var(--fg); border-color: var(--line-2); }
.hero-secondary:disabled { opacity: .5; cursor: not-allowed; }

@media (max-width: 720px) {
  .hero { flex-direction: column; align-items: stretch; gap: 14px; }
  .hero-actions { width: 100%; }
  .hero-cta { flex: 1; justify-content: center; }
}
</style>