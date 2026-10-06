<script setup lang="ts">
import { computed } from 'vue'
import { useStudioStore } from '@/stores/studio'
import { controlsError, normalizeTtsControls, TTS_EMOTIONS } from '../api/tts-controls'
import type { TtsControls, TtsEmotion } from '../api/types'

const store = useStudioStore()
const controls = computed(() => store.ttsControlsDraft ?? store.savedTtsControls)
const invalid = computed(() => controlsError(controls.value))

function setControl(key: 'emotion' | 'speed' | 'vol' | 'pitch', event: Event) {
  const value = (event.target as HTMLInputElement).value
  const next = normalizeTtsControls(controls.value)
  if (key === 'emotion') next.emotion = value as TtsEmotion
  else next[key] = value.trim() ? Number(value) : Number.NaN
  store.ttsControlsDraft = next
}

function setEffect(key: keyof TtsControls['voice_modify'], event: Event) {
  const value = (event.target as HTMLInputElement).value
  const next = normalizeTtsControls(controls.value)
  next.voice_modify[key] = value.trim() ? Number(value) : Number.NaN
  store.ttsControlsDraft = next
}
</script>

<template>
  <section class="tts-controls" aria-label="配音参数">
    <div class="controls-title"><h3>配音参数</h3><span>{{ store.ttsControlsDirty ? '尚未应用' : '当前已应用' }}</span></div>
    <p class="controls-help">本期试听与全文使用同一组参数。修改后先应用，再试听确认效果。</p>
    <fieldset :disabled="!store.currentWork || !!store.busy">
      <div class="controls-grid">
        <label><span>情绪</span><select :value="controls.emotion" @change="setControl('emotion', $event)"><option v-for="option in TTS_EMOTIONS" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
        <label><span>语速（倍）</span><input :value="controls.speed" type="number" min="0.5" max="2" step="0.05" @input="setControl('speed', $event)" /><small>0.5–2，默认 1</small></label>
        <label><span>音量</span><input :value="controls.vol" type="number" min="0.01" max="10" step="0.1" @input="setControl('vol', $event)" /><small>大于 0 且不超过 10，默认 1</small></label>
        <label><span>音调（半音）</span><input :value="controls.pitch" type="number" min="-12" max="12" step="1" @input="setControl('pitch', $event)" /><small>-12–12，默认 0</small></label>
      </div>
      <details class="voice-effects">
        <summary>高级：声音效果器</summary>
        <p class="controls-help">改变声音特征；各项为 -100–100 的整数，0 表示不调整。</p>
        <div class="controls-grid effects-grid">
          <label><span>音高效果</span><input :value="controls.voice_modify.pitch" type="number" min="-100" max="100" step="1" @input="setEffect('pitch', $event)" /><small>负值低沉，正值明亮</small></label>
          <label><span>发声力度</span><input :value="controls.voice_modify.intensity" type="number" min="-100" max="100" step="1" @input="setEffect('intensity', $event)" /><small>负值刚劲，正值轻柔</small></label>
          <label><span>音色效果</span><input :value="controls.voice_modify.timbre" type="number" min="-100" max="100" step="1" @input="setEffect('timbre', $event)" /><small>负值浑厚，正值清脆</small></label>
        </div>
      </details>
      <p v-if="invalid" class="control-error" role="alert">{{ invalid }}</p>
      <p v-else-if="store.ttsControlsDirty" class="controls-pending" role="status">参数尚未应用。应用后旧音频确认与下游视频失效，需要重新合成。</p>
      <div class="btn-row controls-actions">
        <button class="sm primary-sm" :disabled="!store.ttsControlsDirty || !!invalid" @click="store.saveTtsControls()">应用参数</button>
        <button class="sm" :disabled="!store.ttsControlsDirty" @click="store.ttsControlsDraft = null">撤销修改</button>
        <button class="sm" @click="store.ttsControlsDraft = normalizeTtsControls()">恢复默认</button>
      </div>
    </fieldset>
  </section>
</template>

<style scoped>
.tts-controls { margin: 18px 0; padding: 16px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.controls-title { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
.controls-title h3 { margin: 0; font-size: 14px; font-weight: 600; }
.controls-title span, small { color: var(--fg-3); font-size: 11px; }
.controls-help { color: var(--fg-2); font-size: 12px; line-height: 1.7; margin: 7px 0 12px; }
fieldset { margin: 0; padding: 0; border: 0; min-width: 0; }
.controls-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
label { display: grid; align-content: start; gap: 6px; font-size: 12px; color: var(--fg-2); }
input, select { width: 100%; min-width: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--fg); font: inherit; }
input:focus, select:focus { outline: 2px solid var(--line-2); outline-offset: 1px; }
.voice-effects { margin-top: 14px; font-size: 12px; }
.voice-effects summary { cursor: pointer; color: var(--fg-2); }
.effects-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.controls-pending { color: var(--amber, #9b6418); font-size: 12px; line-height: 1.7; margin-top: 12px; }
.control-error { color: var(--red, #b63535); font-size: 12px; }
.controls-actions { margin-top: 14px; }
@media (max-width: 480px) { .effects-grid { grid-template-columns: 1fr; } }
</style>
