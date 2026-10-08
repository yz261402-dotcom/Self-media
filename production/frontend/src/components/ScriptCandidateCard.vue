<script setup lang="ts">
import { computed, ref } from 'vue'
import StatusBadge from '@/components/StatusBadge.vue'
import type { ScriptVersion } from '@/api/types'

const props = defineProps<{ candidate: ScriptVersion; index: number; adopted: boolean; busy: boolean; editBusy?: boolean; humanReview?: boolean }>()
const emit = defineEmits<{
  (e: 'adopt', c: ScriptVersion): void
  (e: 'repair', c: ScriptVersion): void
  (e: 'edit', c: ScriptVersion): void
  (e: 'review'): void
}>()

const open = ref(false)
const c = computed(() => props.candidate)
const manualReview = computed(() => props.humanReview ?? c.value.content_route === 'short_video_storyboard')
const REVIEW_LABEL: Record<string, string> = {
  passed: '已通过独立审核', failed: '审核未通过', needs_review: '尚未审核', reviewing: '审核中', stale: '审核已失效，需要重新审核',
}
const hardFails = computed(() => c.value.quality_report.checks.filter((x) => x.hard_gate && x.outcome !== 'pass'))
// 采用与否只看后端投影的 pickable：历史批次（没有该字段）退回到硬门禁判断，但不放宽
const pickable = computed(() => (c.value.pickable ?? null) === null ? hardFails.value.length === 0 : !!c.value.pickable)
const reviewLabel = computed(() => c.value.review_state ? REVIEW_LABEL[c.value.review_state] : '历史稿件（未做独立审核）')
const reasons = computed(() => c.value.unpickable_reasons ?? [])
const manualSource = computed(() => c.value.creation_source === 'manual' && c.value.ai_review_status === 'not_performed')
const canHumanReview = computed(() => pickable.value || (manualSource.value && !hardFails.value.length))
const canRepair = computed(() => !manualSource.value && !humanApproved.value && !pickable.value && c.value.review_state !== 'reviewing'
  && (c.value.review_state === 'failed' || hardFails.value.length > 0))
const editDisabled = computed(() => props.editBusy ?? props.busy)
const humanApproved = computed(() => c.value.human_review_state === 'human_edit'
  || c.value.human_review_state === 'manual_takeover'
  || (c.value.human_review_state === 'scored_pass' && pickable.value))
const canReview = computed(() => !manualSource.value && !humanApproved.value && !pickable.value && ['needs_review', 'stale'].includes(c.value.review_state ?? ''))
const qualityLabel = computed(() => manualReview.value
  ? c.value.human_review_state === 'human_edit' ? '人工修订确认采用'
    : c.value.human_review_state === 'manual_takeover' ? '人工接管评分合格'
    : c.value.human_review_state === 'scored_pass' && pickable.value ? '人工评分合格'
      : canHumanReview.value ? '待人工审核' : '暂不可人工审核'
  : pickable.value ? '可采用' : '暂不可采用')
</script>

<template>
  <article class="script-option" :class="{ adopted }">
    <header class="option-heading">
      <div class="option-meta"><span>稿件 {{ String(index + 1).padStart(2, '0') }}</span><span v-if="adopted" class="adopted-label">已采用</span></div>
      <h3>{{ c.title }}</h3>
      <p v-if="c.angle" class="option-angle">{{ c.angle }}</p>
    </header>
    <div class="option-body">
      <div v-if="c.content_route === 'short_video_storyboard'" class="storyboard-shots">
        <section v-for="shot in c.storyboard ?? []" :key="shot.shot_index" class="storyboard-shot">
          <header><strong>镜头 {{ shot.shot_index }}</strong><span>{{ shot.duration_seconds }} 秒</span></header>
          <p>{{ shot.visual }}</p><dl><dt>台词 / 旁白</dt><dd>{{ shot.dialogue }}</dd><dt>声音</dt><dd>{{ shot.sound_effects }}</dd></dl>
        </section>
      </div>
      <div v-else class="script-copy">{{ c.text }}</div>
    </div>
    <div class="quality-summary" :class="{ warning: !canHumanReview && !humanApproved }">{{ qualityLabel }}<template v-if="manualSource"> · 人工导入 · 未执行模型审核</template><template v-else-if="c.human_review_state !== 'human_edit'"> · {{ reviewLabel }}</template></div>
    <div v-if="!canHumanReview && !humanApproved && reasons.length" class="quality-failures">
      <p v-for="(r, i) in reasons" :key="i">{{ r }}</p>
    </div>
    <div class="option-actions">
      <button class="primary sm" type="button" :disabled="(!canHumanReview && !humanApproved) || busy" @click="emit('adopt', c)">{{ manualReview ? '人工审核' : adopted ? '重新采用' : '采用这篇' }}</button>
      <button v-if="canRepair" class="sm" type="button" :disabled="busy" @click="emit('repair', c)">重写这一篇</button>
      <button v-if="canReview" class="sm" type="button" :disabled="busy" @click="emit('review')">重新审核</button>
      <button v-if="!manualSource" class="ghost sm" type="button" :disabled="editDisabled" @click="emit('edit', c)">编辑</button>
      <button class="ghost sm detail-toggle" type="button" :aria-expanded="open" @click="open = !open">{{ open ? '收起检查' : '检查详情' }}</button>
    </div>
    <div v-if="open" class="quality-details">
      <div v-for="check in c.quality_report.checks" :key="check.code" class="quality-check">
        <div><strong>{{ check.code }}</strong><StatusBadge :text="check.outcome" /></div>
        <p>{{ check.message }}</p>
      </div>
      <h4>{{ manualSource ? '人工内容核对' : '事实核对' }}</h4>
      <ul v-if="c.claims.length" class="claim-list"><li v-for="claim in c.claims" :key="claim.id">{{ claim.text }} <StatusBadge :text="claim.verdict === 'supported' ? 'pass' : claim.verdict === 'refuted' ? 'fail' : 'review'" /></li></ul>
      <p v-if="manualSource" class="faint">稿件由用户确认事实、版权与发布规范；未执行模型事实审核。</p>
      <p v-else-if="!c.claims.length" class="faint">本篇没有需要核对的事实断言。</p>
    </div>
  </article>
</template>

<style scoped>
.script-option { min-width: 0; display: flex; flex-direction: column; background: var(--panel); }
.script-option.adopted { box-shadow: inset 0 3px 0 var(--fg); }
.option-heading { padding: 22px 20px 14px; border-bottom: 1px solid var(--line); min-height: 120px; }
.option-meta { display: flex; justify-content: space-between; gap: 10px; color: var(--fg-3); font-size: 12px; margin-bottom: 12px; }
.adopted-label { color: var(--fg); font-weight: 600; }
.option-heading h3 { font-size: 18px; line-height: 1.6; }
.option-angle { color: var(--fg-3); font-size: 12px; margin: 8px 0 0; }
.option-body { padding: 20px; flex: 1; max-height: 420px; overflow-y: auto; }
.script-copy { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.95; font-size: 14px; }
.storyboard-shot { padding-bottom: 16px; margin-bottom: 16px; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.storyboard-shot header { display: flex; justify-content: space-between; font-size: 13px; }
.storyboard-shot p, .storyboard-shot dd { font-size: 13px; line-height: 1.75; margin: 8px 0; }
.storyboard-shot dt { font-size: 12px; color: var(--fg-3); margin-top: 10px; }
.quality-summary { padding: 12px 20px 0; font-size: 12px; color: var(--fg-2); }
.quality-summary.warning { color: var(--red); }
.quality-failures { padding: 10px 20px 0; font-size: 12px; color: var(--fg-2); }
.quality-failures p { margin: 0 0 6px; }
.option-actions { display: flex; flex-wrap: wrap; gap: 7px; padding: 16px 20px 20px; align-items: center; }
.detail-toggle { margin-left: auto; }
.quality-details { padding: 4px 20px 20px; border-top: 1px solid var(--line); }
.quality-check { padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
.quality-check > div { display: flex; justify-content: space-between; gap: 8px; }
.quality-check p { color: var(--fg-2); margin: 5px 0 0; }
.quality-details h4 { margin: 14px 0 8px; font-size: 13px; }
.claim-list { padding-left: 16px; font-size: 12px; color: var(--fg-2); }
</style>
