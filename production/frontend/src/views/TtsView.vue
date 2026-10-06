<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useStudioStore } from '@/stores/studio'
import { api } from '@/api'
import StatusBadge from '@/components/StatusBadge.vue'
import HeroAction from '@/components/HeroAction.vue'
import TtsControls from '@/components/TtsControls.vue'
import VoiceCloneUpload from '@/components/VoiceCloneUpload.vue'
import { controlsKey, normalizeTtsControls, TTS_EMOTIONS } from '../api/tts-controls'
import type { AudioAssetInfo, VoiceAsset, VoiceCatalogStatus } from '@/api/types'

const store = useStudioStore()
const router = useRouter()
const sampleText = ref('大家好，今天我们用三分钟讲清楚一个容易被忽略的细节。')
const listened = ref<Record<string, boolean>>({})
watch(() => [store.currentWorkId, store.currentWork?.voice_asset_id, store.currentWork?.tts_controls_revision,
  controlsKey(store.ttsControlsDraft ?? store.savedTtsControls)], () => { listened.value = {} }, { flush: 'sync' })

// ---------- 音色库筛选 ----------
// 音色多起来之后，没有筛选就只能靠滚。分组顺序固定，避免"每次进来顺序都不一样"。
const GROUP_ORDER = ['普通话预置', '经典中文', '其他']
const groupRank = (g: string) => (GROUP_ORDER.indexOf(g) < 0 ? 99 : GROUP_ORDER.indexOf(g))

const voiceQuery = ref('')
const voiceGroup = ref('all')

const approvedVoices = computed(() => store.voices.filter(v => v.approval_state === 'approved'))

const voiceGroupOptions = computed(() => {
  const set = new Set(store.voices.map(v => v.catalog_group || '其他'))
  return [...set].sort((a, b) => groupRank(a) - groupRank(b))
})

const filteredVoices = computed(() => {
  const q = voiceQuery.value.trim().toLowerCase()
  return store.voices.filter(v => {
    if (voiceGroup.value !== 'all' && (v.catalog_group || '其他') !== voiceGroup.value) return false
    if (!q) return true
    return [v.name, v.description, v.remote_voice_id]
      .some(s => (s ?? '').toLowerCase().includes(q))
  })
})

/** 已确认的排在各自分组前面：用户最可能回头找的是自己用过的那几条。 */
const groupedVoices = computed(() => {
  const map = new Map<string, VoiceAsset[]>()
  for (const v of filteredVoices.value) {
    const g = v.catalog_group || '其他'
    if (!map.has(g)) map.set(g, [])
    map.get(g)!.push(v)
  }
  return [...map.entries()]
    .sort((a, b) => groupRank(a[0]) - groupRank(b[0]))
    .map(([group, items]) => ({
      group,
      items: [...items].sort((a, b) =>
        Number(b.approval_state === 'approved') - Number(a.approval_state === 'approved')),
    }))
})

/** 候选音色点"选用"= 先由用户显式确认（批准），再选定。两步都走接口，不合并成隐式副作用。 */
async function chooseVoice(voice: VoiceAsset) {
  if (store.ttsControlsDirty) return
  if (voice.clone_metadata && !hasBaseline(voice)) {
    store.setNotice('warn', '请先试听这条克隆音色，并登记试听基准，再确认选用。')
    return
  }
  if (voice.approval_state !== 'approved') {
    const ok = await store.approveVoice(voice.id)
    if (!ok) return
  }
  await store.selectVoice(voice.id)
}

const hasBaseline = (voice: VoiceAsset) => !!voice.baseline_sample?.available || !!voice.baseline_audio_asset_id
const hasRealPreview = (voice: VoiceAsset) => store.audios.some(audio => audio.kind === 'preview'
  && audio.voice_asset_id === voice.id && !audio.is_mock && !!audio.url && store.audioMatchesTtsControls(audio))
const cloneExpiry = (voice: VoiceAsset) => {
  const value = voice.clone_metadata?.expires_at
  return typeof value === 'string' ? new Date(value).toLocaleString('zh-CN') : ''
}

// ---------- 生产就绪补齐 ----------
// "选用"只代表用户确认了这组音色；能否进视频合成另由 voice_ready() 判定。
// 缺什么就逐条列出来，能由用户补齐的给操作入口，不能补齐的如实说明。
const catalogMeta = ref<VoiceCatalogStatus | null>(null)
const attestBasis = ref('')
const attestAck = ref(false)

const blockerNeedsBaseline = (v: VoiceAsset) =>
  (v.production_blockers ?? []).some(b => b.includes('基准'))
const blockerNeedsLicense = (v: VoiceAsset) =>
  (v.production_blockers ?? []).some(b => b.includes('权属'))

/** 确认口径（含版本标记）由后端给：前端不自己写一份措辞。 */
async function ensureCatalogMeta() {
  if (catalogMeta.value) return
  catalogMeta.value = await store.guard('读取确认口径', () => api.getVoiceCatalogStatus())
}

async function submitAttest(voice: VoiceAsset) {
  const meta = catalogMeta.value
  if (!meta || !voice.license_id) return
  const ok = await store.attestVoiceLicense(voice.license_id, attestBasis.value, meta.attest_token)
  if (ok) attestAck.value = false
}

const scriptReady = computed(() => store.scriptReady)
const previews = computed(() => store.audios.filter(a => a.kind === 'preview'))
const fulls = computed(() => store.audios.filter(a => a.kind === 'full'
  && (!a.script_version_id || a.script_version_id === store.currentWork?.script_version_id)
  && (!a.voice_asset_id || a.voice_asset_id === store.currentWork?.voice_asset_id)
  && store.audioMatchesTtsControls(a)))
const historicalFulls = computed(() => store.audios.filter(a => a.kind === 'full' && !fulls.value.some(item => item.id === a.id)))
const dur = (ms: number) => (Math.round(ms / 100) / 10).toFixed(1)
const audioSettings = (audio: AudioAssetInfo) => {
  if (!audio.synthesis_profile) return '参数未记录'
  const p = normalizeTtsControls(audio.synthesis_profile)
  return `${TTS_EMOTIONS.find(option => option.value === p.emotion)?.label ?? p.emotion} · 语速 ${p.speed} 倍 · 音量 ${p.vol} · 音调 ${p.pitch}`
}
type AudioChecks = { is_mock?: boolean; confirmed?: boolean; checks: { code: string; outcome: string; hard_gate: boolean; message: string }[] }
const confirmBlock = (audio: AudioChecks) => {
  if (store.ttsControlsDirty) return '参数尚未应用，请先应用并按当前参数重新试听。'
  if (audio.confirmed) return '已确认为最终音频'
  if (audio.is_mock) return '模拟音频不可采用，请配置真实语音服务后重新合成。'
  const failed = audio.checks.filter(check => check.hard_gate && check.outcome !== 'pass')
  if (failed.length) return failed.map(check => check.message).join('；') || '音频检查未通过，请查看检测详情。'
  return ''
}
const qualitySummary = (audio: AudioChecks) => {
  if (audio.is_mock) return '模拟音频'
  if (audio.checks.some(check => check.hard_gate && check.outcome !== 'pass')) return '检测未通过'
  if (!audio.checks.length) return '未提供检测明细'
  return `${audio.checks.filter(check => check.outcome === 'pass').length}/${audio.checks.length} 项检测通过`
}
const synthesizing = computed(() => ['全文合成', '试听'].includes(store.busy))

/** Hero：当前该做什么。 */
const hero = computed(() => {
  if (store.ttsControlsDirty) {
    return { status: 'block' as const, title: '先应用配音参数',
      desc: '参数尚未应用。请在右侧应用或撤销修改，再试听与确认音频。', ctaText: undefined }
  }
  if (!scriptReady.value) {
    return {
      status: 'block' as const,
      title: '先选稿件',
      desc: '在口播稿页采用一篇通过检查的稿件。',
      ctaText: '去选定稿件',
    }
  }
  if (store.confirmedAudio) {
    return {
      status: 'done' as const,
      title: `音频已确认 · ${dur(store.confirmedAudio.duration_ms)} 秒`,
      desc: `${store.currentVoice?.name ?? '已选音色'} · 视频将使用这条音频。`,
      ctaText: '下一步：合成视频',
    }
  }
  if (store.busy === '全文合成') {
    return {
      status: 'run' as const,
      title: '正在合成全文',
      desc: '生成中，请稍候。',
      ctaText: '合成全文音频',
      ctaLoading: true,
    }
  }
  if (fulls.value.length) {
    return {
      status: 'todo' as const,
      title: '试听并确认',
      desc: '听完右侧音频，勾选确认后点"确认这条全文音频"。',
      ctaText: undefined,
    }
  }
  if (!store.currentVoice) {
    return {
      status: 'todo' as const,
      title: '选一种音色',
      desc: store.voices.length
        ? `音色库有 ${store.voices.length} 种可选：先"试听"用同一句比对，满意再点"选用"。`
        : '音色库为空，请检查后端服务是否已启动。',
      ctaText: undefined,
    }
  }
  return {
    status: 'todo' as const,
    title: '合成全文音频',
    desc: `${store.currentVoice.name} · 稿《${store.selectedScript?.title ?? '已采用'}》`,
    ctaText: '合成全文音频',
    ctaDisabled: !scriptReady.value || !store.currentVoice || store.busy !== '',
  }
})

function onHeroCta() {
  if (store.ttsControlsDirty) return
  if (!scriptReady.value) router.push('/script')
  else if (store.confirmedAudio) router.push('/video')
  else if (fulls.value.length === 0 && store.currentVoice) store.synthesizeFull()
}
</script>

<template>
  <div class="voice-page">
    <HeroAction
      :status="hero.status"
      :title="hero.title"
      :desc="hero.desc"
      :cta-text="hero.ctaText"
      :cta-disabled="hero.ctaDisabled"
      :cta-loading="hero.ctaLoading"
      @cta="onHeroCta"
    />

    <div class="voice-layout section">
      <section class="voice-library" aria-label="选择音色">
        <div class="section-head">
          <h2 class="section-title">选择音色</h2>
          <span class="voice-count">共 {{ store.voices.length }} 种 · 已确认 {{ approvedVoices.length }}</span>
        </div>

        <VoiceCloneUpload />

        <div class="voice-tools">
          <input v-model="voiceQuery" class="voice-search" type="search"
                 placeholder="搜索音色：如「温柔」「男声」「Crisp」" aria-label="搜索音色" />
          <div class="voice-chips">
            <button class="chip" :class="{ on: voiceGroup === 'all' }" @click="voiceGroup = 'all'">全部</button>
            <button v-for="g in voiceGroupOptions" :key="g" class="chip"
                    :class="{ on: voiceGroup === g }" @click="voiceGroup = g">{{ g }}</button>
          </div>
        </div>

        <div class="voice-list">
          <template v-for="bucket in groupedVoices" :key="bucket.group">
            <p class="voice-group-head"><span>{{ bucket.group }}</span><span>{{ bucket.items.length }}</span></p>
            <article v-for="voice in bucket.items" :key="voice.id" class="voice-choice"
                     :class="{ chosen: store.currentWork?.voice_asset_id === voice.id }">
              <div class="voice-name-row">
                <h3>{{ voice.name }}</h3>
                <span v-if="store.currentWork?.voice_asset_id === voice.id" class="chosen-label">当前音色</span>
                <span v-else-if="voice.approval_state === 'approved'" class="state-pill ok">已确认</span>
                <span v-else class="state-pill">候选</span>
              </div>
              <p v-if="voice.description" class="voice-tone">{{ voice.description }}</p>
              <div class="btn-row">
                <button class="sm" :disabled="store.busy !== '' || !store.currentWork || !sampleText.trim() || store.ttsControlsDirty"
                        @click="store.voicePreview(voice.id, sampleText)">{{ store.busy === '试听' ? '生成中…' : '试听' }}</button>
                <button class="sm primary-sm"
                        :disabled="store.busy !== '' || !store.currentWork || store.currentWork?.voice_asset_id === voice.id || store.ttsControlsDirty || (!!voice.clone_metadata && !hasBaseline(voice))"
                        @click="chooseVoice(voice)">{{ store.currentWork?.voice_asset_id === voice.id ? '已选用' : voice.clone_metadata && !hasBaseline(voice) ? '先登记试听基准' : (voice.approval_state === 'approved' ? '选用' : '选用并确认') }}</button>
              </div>
              <div v-if="voice.clone_metadata" class="clone-next">
                <p v-if="voice.clone_metadata.activation_state !== 'active'">临时克隆音色{{ cloneExpiry(voice) ? ` · 使用期限 ${cloneExpiry(voice)}` : '' }}。首次试听会合成语音，并可能收取首次使用费用。</p>
                <template v-if="!hasBaseline(voice)">
                  <p>先点击“试听”，听完后登记基准，再确认选用。</p>
                  <button class="sm" :disabled="!!store.busy || store.ttsControlsDirty || !hasRealPreview(voice)" @click="store.registerVoiceBaseline(voice.id)">登记这条试听为基准</button>
                </template>
                <button class="sm" :disabled="!!store.busy || !store.currentWork || !sampleText.trim() || store.ttsControlsDirty" @click="store.voicePreview(voice.id, sampleText, true)">重新试听（可能再次收费）</button>
              </div>
              <details class="voice-provider">
                <summary>音色信息</summary>
                <p>{{ voice.provider }} / {{ voice.model }} / {{ voice.region }}</p>
                <p class="faint">{{ voice.remote_voice_id }}</p>
              </details>

              <div v-if="store.currentWork?.voice_asset_id === voice.id && voice.production_blockers?.length"
                   class="ready-block">
                <p class="ready-title">还不能用于视频合成，还差：</p>
                <ul class="ready-list"><li v-for="b in voice.production_blockers" :key="b">{{ b }}</li></ul>
                <button v-if="blockerNeedsBaseline(voice)" class="sm" :disabled="!!store.busy"
                        @click="store.registerVoiceBaseline(voice.id)">把这条试听登记为基准</button>
                <details v-if="blockerNeedsLicense(voice)" class="attest" @toggle="ensureCatalogMeta">
                  <summary>确认官方条款允许商用</summary>
                  <p class="attest-note">{{ catalogMeta?.attest_note || '正在读取当前确认口径…' }}</p>
                  <label class="attest-field">
                    <span>商用依据</span>
                    <input v-model="attestBasis" type="text"
                           placeholder="例如：官方服务协议允许本项目商业使用（含抖音发布）" />
                  </label>
                  <label class="attest-ack">
                    <input v-model="attestAck" type="checkbox" />
                    <span>我已确认使用范围，并理解这是我作出的判断</span>
                  </label>
                  <button class="sm primary-sm"
                          :disabled="!attestAck || !attestBasis.trim() || !catalogMeta || !!store.busy"
                          @click="submitAttest(voice)">保存使用范围确认</button>
                </details>
              </div>
            </article>
          </template>
          <p v-if="!store.voices.length" class="inline-empty">还没有可用音色，请检查后端服务是否已启动。</p>
          <p v-else-if="!filteredVoices.length" class="inline-empty">没有匹配的音色，换个关键词或分组试试。</p>
        </div>
        <details class="sample-settings"><summary>修改试听短句</summary><label class="field"><span>所有音色使用同一段文字试听</span><textarea v-model="sampleText" rows="3"></textarea></label></details>
        <p class="cost-note">生成试听和重新合成可能产生语音服务费用。</p>
        <section v-if="previews.length" class="preview-section" aria-label="音色试听">
          <h3>试听记录</h3>
          <div v-for="audio in previews" :key="audio.id" class="preview-record">
            <div class="audio-meta"><strong>{{ store.voices.find(voice => voice.id === audio.voice_asset_id)?.name ?? '音色试听' }}</strong><span>{{ dur(audio.duration_ms) }} 秒</span></div>
            <p class="audio-settings">{{ audioSettings(audio) }}<span v-if="!store.audioMatchesTtsControls(audio)"> · 历史参数试听</span></p>
            <audio v-if="audio.url && !audio.is_mock" :src="audio.url" controls preload="none"></audio>
            <p v-else class="faint">暂无可播放的真实试听音频。</p>
          </div>
        </section>
      </section>

      <section class="full-audio" aria-label="全文合成与确认">
        <div class="section-head"><h2 class="section-title">合成全文</h2></div>
        <div class="synthesis-input">
          <dl class="input-summary">
            <div><dt>采用稿件</dt><dd>{{ store.selectedScript?.title ?? '尚未采用稿件' }}</dd></div>
            <div><dt>当前音色</dt><dd>{{ store.currentVoice?.name ?? '尚未选择音色' }}</dd></div>
          </dl>
          <details v-if="store.selectedScript?.text" class="script-preview"><summary>查看配音正文</summary><p>{{ store.selectedScript.text }}</p></details>
          <TtsControls />
          <div class="synthesis-actions">
            <button class="primary" :disabled="!scriptReady || !store.currentVoice || store.busy !== '' || store.ttsControlsDirty" @click="store.synthesizeFull()">{{ store.busy === '全文合成' ? '正在合成…' : fulls.length ? '读取或复用全文音频' : '合成全文音频' }}</button>
            <button v-if="fulls.length" class="ghost" :disabled="!scriptReady || !store.currentVoice || !!store.busy || store.ttsControlsDirty" @click="store.synthesizeFull(true)">重新合成（可能再次收费）</button>
          </div>
          <p v-if="!store.currentVoice" class="field-help">先选择一种已确认的音色。</p>
          <div v-if="synthesizing" class="synthesis-progress" role="status"><progress :aria-label="store.busy === '全文合成' ? '全文合成进度' : '音色试听生成进度'"></progress><span>{{ store.busy === '全文合成' ? '正在生成全文音频，请稍候。' : '正在生成试听音频，请稍候。' }}</span></div>
        </div>
        <div v-if="!fulls.length" class="audio-empty">合成后先试听，确认内容、读音和声音合适，再进入视频合成。</div>
        <article v-for="(audio, index) in fulls" :key="audio.id" class="full-record">
          <header class="audio-meta"><h3>全文音频{{ fulls.length > 1 ? ` ${index + 1}` : '' }}</h3><span>{{ dur(audio.duration_ms) }} 秒{{ audio.confirmed ? ' · 已确认' : '' }}</span></header>
          <p class="audio-settings">{{ audioSettings(audio) }}</p>
          <audio v-if="audio.url && !audio.is_mock" :src="audio.url" controls preload="none"></audio>
          <p v-else class="field-help">没有可播放的真实音频，请检查服务配置后重新合成。</p>
          <details class="audio-checks">
            <summary>{{ qualitySummary(audio) }}<span>检测详情</span></summary>
            <div v-for="check in audio.checks" :key="check.code" class="audio-check">
              <div><span>{{ check.code }}</span><StatusBadge :text="check.outcome" /></div><p>{{ check.message }}</p>
            </div>
          </details>
          <p v-if="confirmBlock(audio) && !audio.confirmed" class="audio-warning">{{ confirmBlock(audio) }}</p>
          <div class="audio-confirmation">
            <label v-if="!audio.confirmed && !confirmBlock(audio)" class="ack-label"><input v-model="listened[audio.id]" type="checkbox" />我已试听全文，确认内容、发音和音色符合这一期要求</label>
            <button class="primary" :disabled="!!confirmBlock(audio) || !listened[audio.id] || store.busy !== ''" @click="store.confirmAudio(audio.id)">{{ audio.confirmed ? '已确认为最终音频' : '确认这条全文音频' }}</button>
          </div>
        </article>
        <details v-if="historicalFulls.length" class="historical-audio">
          <summary>历史全文音频（{{ historicalFulls.length }} 条）</summary>
          <div v-for="audio in historicalFulls" :key="audio.id" class="preview-record">
            <p class="audio-settings">{{ dur(audio.duration_ms) }} 秒 · {{ audioSettings(audio) }} · 已失效，不能确认为当前音轨</p>
            <audio v-if="audio.url && !audio.is_mock" :src="audio.url" controls preload="none"></audio>
          </div>
        </details>
        <p class="downstream-note">换音色、修改稿件或应用新参数后，需要重新合成音频和视频。历史文件与费用记录保留。</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.clone-next, .audio-settings { margin: 8px 0; color: var(--fg-3); font-size: 11px; line-height: 1.7; }
.clone-next p { margin: 6px 0; }
.historical-audio { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 14px; font-size: 12px; color: var(--fg-2); }
.historical-audio summary { cursor: pointer; }
.voice-layout { display: grid; grid-template-columns: minmax(300px, 1fr) minmax(0, 1.35fr); gap: 32px; align-items: start; }
.voice-library, .full-audio { min-width: 0; }
.voice-library .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.voice-count { font-size: 12px; color: var(--fg-3); white-space: nowrap; }
.voice-tools { display: grid; gap: 10px; margin-bottom: 12px; }
.voice-search { width: 100%; padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2); color: var(--fg); font: inherit; font-size: 13px; }
.voice-search:focus { outline: none; border-color: var(--line-2); background: var(--panel); }
.voice-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.chip { padding: 5px 12px; border: 1px solid var(--line); border-radius: 999px; background: var(--panel); color: var(--fg-2); font: inherit; font-size: 12px; cursor: pointer; transition: background .15s, color .15s, border-color .15s; }
.chip:hover { border-color: var(--line-2); color: var(--fg); }
.chip.on { background: var(--fg); border-color: var(--fg); color: var(--panel); font-weight: 600; }
.voice-list { max-height: 620px; overflow-y: auto; border-top: 1px solid var(--line); padding-right: 4px; }
.voice-group-head { position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; gap: 12px; margin: 0; padding: 10px 0 7px; background: var(--bg); border-bottom: 1px solid var(--line); font-size: 11px; letter-spacing: .08em; color: var(--fg-3); }
.voice-choice { padding: 16px 0; border-bottom: 1px solid var(--line); }
.voice-choice.chosen { padding-left: 14px; border-left: 3px solid var(--fg); }
.voice-name-row { display: flex; gap: 8px; align-items: center; margin-bottom: 7px; }
.voice-name-row h3 { font-size: 15px; font-weight: 600; margin-right: auto; overflow-wrap: anywhere; }
.chosen-label { font-size: 11px; color: var(--fg-2); white-space: nowrap; }
.state-pill { padding: 2px 8px; border: 1px solid var(--line); border-radius: 999px; font-size: 11px; color: var(--fg-3); white-space: nowrap; }
.state-pill.ok { border-color: transparent; background: rgba(66, 104, 82, .12); color: var(--green); }
.voice-tone { margin: 0 0 12px; font-size: 12px; line-height: 1.6; color: var(--fg-3); }
.primary-sm { border-color: var(--blue) !important; background: var(--blue); color: #fff; font-weight: 600; }
.primary-sm:hover:not(:disabled) { background: var(--blue-d); border-color: var(--blue-d) !important; }
.primary-sm:disabled { opacity: .4; }
.voice-provider { font-size: 12px; margin-top: 12px; color: var(--fg-3); }
.voice-provider p { margin: 8px 0 0; overflow-wrap: anywhere; }
.ready-block { margin-top: 12px; padding: 12px 14px; border: 1px solid var(--line); border-left: 3px solid var(--amber); border-radius: 8px; background: var(--panel-2); }
.ready-title { margin: 0 0 6px; font-size: 12.5px; font-weight: 600; color: var(--fg-2); }
.ready-list { margin: 0 0 10px; padding-left: 18px; font-size: 12px; line-height: 1.7; color: var(--fg-3); }
.ready-list li { overflow-wrap: anywhere; }
.attest { margin-top: 10px; font-size: 12px; color: var(--fg-2); }
.attest summary { cursor: pointer; font-size: 12.5px; font-weight: 500; }
.attest-note { margin: 10px 0; color: var(--fg-3); line-height: 1.65; }
.attest-field { display: block; margin-bottom: 10px; }
.attest-field > span { display: block; font-size: 12px; color: var(--fg-3); margin-bottom: 5px; }
.attest-field input { width: 100%; padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--fg); font: inherit; font-size: 12.5px; }
.attest-field input:focus { outline: none; border-color: var(--line-2); }
.attest-ack { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 10px; line-height: 1.6; }
.attest-ack input { margin-top: 3px; }
.voice-provider summary, .sample-settings summary, .script-preview summary { cursor: pointer; }
.sample-settings { font-size: 13px; margin-top: 20px; color: var(--fg-2); }
.sample-settings .field { margin-top: 12px; }
.cost-note, .field-help, .downstream-note { font-size: 12px; color: var(--fg-3); margin: 12px 0 0; }
.synthesis-input { padding: 20px 24px; background: var(--panel); border: 1px solid var(--line); }
.input-summary { margin: 0; display: grid; gap: 14px; }
.input-summary > div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 12px; }
.input-summary dt { font-size: 12px; color: var(--fg-3); padding-top: 2px; }
.input-summary dd { margin: 0; font-weight: 500; overflow-wrap: anywhere; }
.script-preview { margin-top: 18px; font-size: 12px; color: var(--fg-2); }
.script-preview p { white-space: pre-wrap; line-height: 1.85; padding-top: 12px; max-height: 300px; overflow-y: auto; }
.synthesis-actions { margin-top: 22px; display: flex; gap: 10px; flex-wrap: wrap; }
.synthesis-progress { margin-top: 18px; display: grid; gap: 8px; color: var(--fg-2); font-size: 12px; }
.synthesis-progress progress { width: 100%; height: 5px; accent-color: var(--fg); }
.audio-empty { border-bottom: 1px solid var(--line); padding: 22px 24px; font-size: 13px; color: var(--fg-3); margin: 0; }
.preview-section { margin-top: 28px; }
.preview-section > h3 { font-size: 14px; margin-bottom: 6px; }
.preview-record { padding: 14px 0; border-bottom: 1px solid var(--line); }
.audio-meta { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.audio-meta h3 { font-size: 16px; }
.audio-meta strong { font-size: 13px; font-weight: 500; }
.audio-meta > span { font-size: 12px; color: var(--fg-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
.voice-page audio { width: 100%; display: block; }
.full-record { padding: 24px; border: 1px solid var(--line); border-top: 0; background: var(--panel); }
.audio-checks { margin-top: 16px; font-size: 12px; }
.audio-checks summary { cursor: pointer; color: var(--fg-2); }
.audio-checks summary > span { margin-left: 14px; color: var(--fg-3); }
.audio-check { padding: 12px 0; border-bottom: 1px solid var(--line); }
.audio-check > div { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.audio-check p { color: var(--fg-2); margin: 6px 0 0; }
.audio-warning { color: var(--red); font-size: 12px; margin: 14px 0 0; }
.audio-confirmation { margin-top: 18px; }
.audio-confirmation .ack-label { margin-bottom: 14px; line-height: 1.65; }
.inline-empty { padding: 24px 0; color: var(--fg-3); font-size: 13px; }
@media (max-width: 1000px) { .voice-layout { grid-template-columns: 1fr; gap: 28px; } .voice-list { max-height: none; } }
@media (max-width: 600px) { .synthesis-input, .full-record, .audio-empty { padding: 18px 16px; } }
</style>
