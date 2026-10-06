import type { TtsControls, TtsEmotion } from './types'

export const TTS_EMOTIONS: { value: TtsEmotion; label: string }[] = [
  { value: 'auto', label: '自动（按文本判断）' },
  { value: 'happy', label: '开心' }, { value: 'sad', label: '悲伤' },
  { value: 'angry', label: '愤怒' }, { value: 'fearful', label: '害怕' },
  { value: 'disgusted', label: '厌恶' }, { value: 'surprised', label: '惊讶' },
  { value: 'calm', label: '平静' },
]

/** Only UI-editable controls are copied; provider metadata is never resubmitted. */
export function normalizeTtsControls(profile: Partial<TtsControls> = {}): TtsControls {
  return {
    emotion: profile.emotion ?? 'auto', speed: profile.speed ?? 1,
    vol: profile.vol ?? 1, pitch: profile.pitch ?? 0,
    voice_modify: {
      pitch: profile.voice_modify?.pitch ?? 0,
      intensity: profile.voice_modify?.intensity ?? 0,
      timbre: profile.voice_modify?.timbre ?? 0,
    },
  }
}

export function controlsKey(controls: Partial<TtsControls>): string {
  return JSON.stringify(normalizeTtsControls(controls))
}

/** Existing voices inherit calm unless the work explicitly requests auto. */
export function effectiveTtsControls(base: Partial<TtsControls> = {}, overrides: Partial<TtsControls> = {}): TtsControls {
  return normalizeTtsControls({ emotion: 'calm', ...base, ...overrides,
    voice_modify: { ...base.voice_modify, ...overrides.voice_modify } as TtsControls['voice_modify'] })
}

export function controlsError(controls: TtsControls): string {
  if (!TTS_EMOTIONS.some(item => item.value === controls.emotion)) return '请选择支持的情绪。'
  if (!Number.isFinite(controls.speed) || controls.speed < 0.5 || controls.speed > 2) return '语速范围为 0.5–2 倍。'
  if (!Number.isFinite(controls.vol) || controls.vol <= 0 || controls.vol > 10) return '音量必须大于 0，且不超过 10。'
  if (!Number.isInteger(controls.pitch) || controls.pitch < -12 || controls.pitch > 12) return '音调须为 -12–12 的整数。'
  if (Object.values(controls.voice_modify).some(value => !Number.isInteger(value) || value < -100 || value > 100)) return '声音效果器各项须为 -100–100 的整数。'
  return ''
}
