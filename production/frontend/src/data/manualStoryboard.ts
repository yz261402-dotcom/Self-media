import type { StoryboardShot } from '@/api/types'

export function validateManualStoryboard(title: string, storyboard: StoryboardShot[]): string[] {
  const errors: string[] = []
  if (!title.trim()) errors.push('标题不能为空。')
  if (title.trim().length > 500) errors.push('标题不能超过 500 字。')
  if (!storyboard.length) errors.push('至少需要一个镜头。')
  if (storyboard.length > 30) errors.push('分镜数量不能超过 30 个。')
  storyboard.forEach((shot, index) => {
    if (shot.shot_index !== index + 1) errors.push('镜头编号须从 1 连续递增。')
    if (!Number.isInteger(shot.duration_seconds) || shot.duration_seconds < 2 || shot.duration_seconds > 30) errors.push(`镜头 ${index + 1} 时长须为 2 到 30 秒的整数。`)
    if (!shot.visual.trim() || !shot.dialogue.trim() || !shot.sound_effects.trim()) errors.push(`镜头 ${index + 1} 的画面、台词和声音不能为空；无台词或音效请填写“无”。`)
  })
  return [...new Set(errors)]
}

export function parseManualStoryboardJson(text: string, fallbackTitle = '', fallbackDescription = '') {
  const value: unknown = JSON.parse(text)
  const container = !Array.isArray(value) && value && typeof value === 'object' ? value as Record<string, unknown> : null
  const shots: unknown = Array.isArray(value) ? value : container?.storyboard
  if (!Array.isArray(shots)) throw new Error('JSON 须为镜头数组，或包含 title、description、storyboard 的对象。')
  if (container && container.title !== undefined && typeof container.title !== 'string') throw new Error('title 须为字符串。')
  if (container && container.description !== undefined && typeof container.description !== 'string') throw new Error('description 须为字符串。')
  const storyboard: StoryboardShot[] = shots.map((shot, index) => {
    if (!shot || typeof shot !== 'object' || Array.isArray(shot)) throw new Error(`镜头 ${index + 1} 格式不正确。`)
    const item = shot as Record<string, unknown>
    if (!Number.isInteger(item.shot_index) || typeof item.duration_seconds !== 'number'
      || ['visual', 'dialogue', 'sound_effects'].some(key => typeof item[key] !== 'string')
      || (item.prompt !== undefined && typeof item.prompt !== 'string')) throw new Error(`镜头 ${index + 1} 的编号、时长或文本字段格式不正确。`)
    const visual = (item.visual as string).trim()
    const dialogue = (item.dialogue as string).trim()
    const sound = (item.sound_effects as string).trim()
    return { shot_index: item.shot_index as number, duration_seconds: item.duration_seconds,
      visual, dialogue, sound_effects: sound, prompt: (item.prompt as string | undefined)?.trim() || `${visual}；${dialogue}；音效：${sound}` }
  })
  return { title: typeof container?.title === 'string' ? container.title : fallbackTitle,
    description: typeof container?.description === 'string' ? container.description : fallbackDescription, storyboard }
}
