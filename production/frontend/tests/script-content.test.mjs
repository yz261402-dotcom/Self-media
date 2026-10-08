import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.script-content-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'view.mjs')
await build({
  stdin: { contents: `export { default } from './src/views/ScriptView.vue'; export { default as WritingPromptEditor } from './src/components/WritingPromptEditor.vue'; export { useStudioStore } from './src/stores/studio.ts'; export { useContentStore } from './src/stores/content.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
  plugins: [{ name: 'vue', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    builder.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const api = { quoteMediaFetch: async () => ({ reserve_fen: 50 }), quoteVisualAnalysis: async () => ({ reserve_fen: 80 }) }; export const apiMode = "http";' }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      let source = await readFile(path, 'utf8')
      if (path.replaceAll('\\', '/').endsWith('/views/ScriptView.vue')) {
        source = source.replace('</script>', "if (globalThis.storyboardEditSeed) editing.value = globalThis.storyboardEditSeed\n</script>")
        source = source.replace('</script>', 'if (globalThis.routeChoiceCapture) globalThis.routeChoiceCapture({ chooseRoute, content, store })\n</script>')
      }
      if (path.replaceAll('\\', '/').endsWith('/components/WritingPromptEditor.vue')) {
        source = source.replace('</script>', 'if (globalThis.promptDraftSeed !== undefined) draft.value = globalThis.promptDraftSeed\nif (globalThis.promptGenerateCapture) globalThis.promptGenerateCapture(generate)\n</script>')
      }
      const { descriptor } = parse(source, { filename: path })
      return { contents: compileScript(descriptor, { id: 'test', inlineTemplate: true, templateOptions: { ssr: true } }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const view = await import(pathToFileURL(outfile).href)
const candidate = (id) => ({ id, angle: id, title: `标题${id}`, text: `正文${id}`, tags: [], claims: [], pickable: true,
  quality_report: { checks: [], overall: 'pass', review_required: false }, duration_estimate_ms: 5000 })
async function render(batch, route = 'talking_script', contentExtra = {}) {
  const pinia = createPinia(); setActivePinia(pinia)
  const store = view.useStudioStore(); const content = view.useContentStore()
  store.currentWorkId = 'W'; store.apiReady = true
  store.works = [{ id: 'W', identity_id: contentExtra.identityId === undefined ? 'person' : contentExtra.identityId, brief: { topic: '主题', content_route: route } }]
  content.workId = 'W'; content.state = { work_id: 'W', content_revision: 1, brief: { topic: '主题', content_route: route },
    batch, active_jobs: [], recent_jobs: [], references: [], prompt: null, reference: null, style: null, ...contentExtra }
  content.inspectedReference = contentExtra.inspectedReference ?? null
  content.submittingKind = contentExtra.submittingKind ?? null
  content.loading = contentExtra.loading ?? false
  content.error = contentExtra.error ?? ''
  content.notice = contentExtra.notice ?? ''
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/script', component: view.default }] })
  await router.push('/script')
  return renderToString(createSSRApp(view.default).use(pinia).use(router))
}
test('审核未完成时展示初稿和编辑入口，未审稿不能采用或按意见返工', async () => {
  const html = await render({ id: 'b', requested: 3, produced: 3, passed: 2, ready_to_present: false,
    candidates: [{ ...candidate('draft'), review_state: 'needs_review', pickable: false }], retry_targets: [],
    review_targets: [{ candidate_id: 'draft', slot: 3, review_state: 'needs_review', reason: '等待独立审核' }] })
  assert.match(html, /正文draft|标题draft/)
  assert.match(html, /尚未审核/)
  assert.doesNotMatch(button(html, '编辑'), /disabled/)
  assert.match(button(html, '采用这篇'), /disabled/)
  assert.match(html, /重新审核/)
  assert.doesNotMatch(html, /重写未通过稿件|重写这一篇|三篇全部通过审核后展示/)
  assert.match(html, /已通过 2 \/ 已产出 3/)
})

test('部分通过批次可直接采用已通过稿件，其余候选保留编辑和重审入口', async () => {
  const batch = { id: 'partial', status: 'partial', requested: 3, produced: 3, passed: 1, ready_to_present: false,
    candidates: [
      { ...candidate('1'), slot: 1, review_state: 'passed' },
      { ...candidate('2'), slot: 2, review_state: 'failed', pickable: false },
      { ...candidate('3'), slot: 3, review_state: 'failed', pickable: false },
    ], retry_targets: [{ candidate_id: '2', slot: 2, reason: '审核未通过' }, { candidate_id: '3', slot: 3, reason: '审核未通过' }] }
  for (const route of ['talking_script', 'short_video_storyboard']) {
    const html = await render(batch, route)
    const adoptionLabel = route === 'short_video_storyboard' ? '人工审核' : '采用这篇'
    const cards = [...html.matchAll(/<article\b[^>]*class="script-option[^"]*"[^>]*>[\s\S]*?<\/article>/g)].map(match => match[0])
    assert.equal(cards.length, 3)
    assert.ok(button(cards[0], adoptionLabel))
    assert.doesNotMatch(button(cards[0], adoptionLabel), /disabled/)
    for (const card of cards.slice(1)) {
      assert.match(button(card, adoptionLabel), /disabled/)
      assert.ok(button(card, '编辑'))
      assert.doesNotMatch(button(card, '编辑'), /disabled/)
    }
    assert.ok(button(html, '重新审核'))
    assert.match(html, /已通过 1 \/ 已产出 3/)
    const guidance = route === 'short_video_storyboard'
      ? /已通过稿件可进入人工审核。2 篇审核未通过，请查看修改项。/g
      : /已通过稿件可以直接采用。2 篇审核未通过，请查看修改项。/g
    assert.equal((html.match(guidance) ?? []).length, 2,
      '顶部说明和审核提示都应明确部分通过可采用')
    assert.doesNotMatch(html, /三篇均已通过|三篇全部通过审核后/)
  }
})

test('重新审核入口在已有候选时仍可达，活动审核时禁用但保留正文', async () => {
  const batch = { id: 'b', requested: 3, produced: 1, passed: 0, ready_to_present: false,
    candidates: [{ ...candidate('draft'), review_state: 'reviewing', pickable: false }], retry_targets: [] }
  const html = await render(batch, 'short_video_storyboard', {
    active_jobs: [{ job_id: 'review', kind: 'review', state: 'running', stage: 'review' }] })
  assert.match(html, /正文draft/)
  assert.ok(button(html, '重新审核'))
  assert.match(button(html, '重新审核'), /disabled/)
  assert.doesNotMatch(html, /重写这一篇|重写未通过稿件/)
})

test('活动审核允许编辑和保存初稿，写稿或返工期间仍禁用编辑', async () => {
  const batch = { id: 'b', ready_to_present: false, candidates: [
    { ...candidate('draft'), review_state: 'needs_review', pickable: false }] }
  for (const kind of ['review', 'generation']) {
    const extra = { active_jobs: [{ job_id: 'j', kind, state: 'running', stage: 'review' }] }
    const html = await render(batch, 'talking_script', extra)
    assert.doesNotMatch(button(html, '编辑'), /disabled/)
    assert.match(button(html, '采用这篇'), /disabled/)
    globalThis.storyboardEditSeed = { id: 'draft', text: '改后的初稿' }
    try {
      const editing = await render(batch, 'talking_script', extra)
      assert.doesNotMatch(button(editing, '保存为新版本'), /disabled/)
    } finally { delete globalThis.storyboardEditSeed }
  }
  for (const stage of ['write', 'repair']) {
    const html = await render(batch, 'talking_script', {
      active_jobs: [{ job_id: 'j', kind: 'generation', state: 'running', stage }] })
    assert.match(button(html, '编辑'), /disabled/)
  }
})

test('确实缺少事实依据的硬门禁复核项可以定向修改', async () => {
  const draft = { ...candidate('fact'), review_state: 'needs_review', pickable: false,
    quality_report: { checks: [{ code: 'FACT_EVIDENCE', hard_gate: true, outcome: 'review', message: '缺少依据' }] } }
  const html = await render({ id: 'b', ready_to_present: false, candidates: [draft],
    retry_targets: [{ candidate_id: 'fact', review_state: 'needs_review', reason: '缺少依据' }] })
  assert.ok(button(html, '重写这一篇'))
})

test('明确审核失败稿显示返工入口，审核失效稿仅提供编辑和重审', async () => {
  const failed = await render({ id: 'b', ready_to_present: false, candidates: [
    { ...candidate('failed'), review_state: 'failed', pickable: false }],
    retry_targets: [{ candidate_id: 'failed', review_state: 'failed', reason: '需要修正事实' }] })
  assert.ok(button(failed, '重写这一篇'))
  assert.ok(button(failed, '重写未通过稿件'))
  assert.match(failed, /1 篇审核未通过，请查看修改项。/)
  assert.doesNotMatch(failed, /待审稿请重新审核|待重新审核/)
  const stale = await render({ id: 'b', ready_to_present: false, candidates: [
    { ...candidate('stale'), review_state: 'stale', pickable: false }], retry_targets: [] })
  assert.match(stale, /正文stale/)
  assert.match(stale, /审核已失效/)
  assert.ok(button(stale, '重新审核'))
  assert.doesNotMatch(stale, /重写这一篇|重写未通过稿件/)
})

test('批次说明区分审核失败、待审核和正在审核，失败稿不冒充待审稿', async () => {
  const html = await render({ id: 'mixed', requested: 3, produced: 3, passed: 0, ready_to_present: false,
    candidates: [
      { ...candidate('failed'), review_state: 'failed', pickable: false },
      { ...candidate('pending'), review_state: 'needs_review', pickable: false },
      { ...candidate('reviewing'), review_state: 'reviewing', pickable: false },
    ], retry_targets: [{ candidate_id: 'failed', review_state: 'failed', reason: '需要修改' }] }, 'short_video_storyboard')
  assert.match(html, /1 篇审核未通过，请查看修改项。1 篇待重新审核。1 篇独立审核中。/)
  const cards = [...html.matchAll(/<article\b[^>]*class="script-option[^"]*"[^>]*>[\s\S]*?<\/article>/g)].map(match => match[0])
  assert.match(cards[0], /审核未通过/)
  assert.ok(button(cards[0], '重写这一篇'))
  assert.match(cards[1], /尚未审核/)
  assert.ok(button(cards[1], '重新审核'))
  assert.equal(button(cards[1], '重写这一篇'), '')
  assert.match(cards[2], /审核中/)
  assert.equal(button(cards[2], '重写这一篇'), '')
})

test('整批已通过后仍保留明确的重新审核入口', async () => {
  const html = await render({ id: 'b', ready_to_present: true, candidates: [candidate('passed')] })
  assert.ok(button(html, '重新审核'))
  assert.doesNotMatch(button(html, '重新审核'), /disabled/)
})
test('3/3 时显示三个候选并提供路线选择', async () => {
  const html = await render({ id: 'b', requested: 3, produced: 3, passed: 3, ready_to_present: true, candidates: [1, 2, 3].map(candidate) })
  assert.match(html, /正文1/); assert.match(html, /正文2/); assert.match(html, /正文3/)
  assert.match(html, /aria-label="内容路线"/); assert.match(html, /短视频分镜/)
  assert.match(html, /三篇均已通过审核，请选择一篇。/)
})

async function routeChoice(batch = null, contentExtra = {}) {
  let context
  globalThis.routeChoiceCapture = value => { context = value }
  try { await render(batch, 'short_video_storyboard', contentExtra) }
  finally { delete globalThis.routeChoiceCapture }
  return context
}

test('已有候选切换路线取消时不保存、不刷新，当前路线不弹确认', async () => {
  const batch = { id: 'b', ready_to_present: true, candidates: [candidate('selected')] }
  const context = await routeChoice(batch, { selected_script_id: 'selected' })
  const calls = []; const confirmations = []
  context.content.setRoute = async route => { calls.push(['save', route]); return true }
  context.store.syncWork = async id => { calls.push(['sync', id]) }
  const original = globalThis.window
  globalThis.window = { confirm: message => { confirmations.push(message); return false } }
  try {
    await context.chooseRoute('short_video_storyboard')
    assert.deepEqual(confirmations, [])
    await context.chooseRoute('talking_script')
    assert.equal(confirmations.length, 1)
    assert.match(confirmations[0], /撤销.*采用|采用.*失效/)
    assert.match(confirmations[0], /重新.*审核|审核.*失效/)
    assert.match(confirmations[0], /制作|任务图/)
    assert.deepEqual(calls, [])
    assert.equal(context.content.revision, 1)
    assert.equal(context.content.state.selected_script_id, 'selected')
  } finally { globalThis.window = original; context.content.leave() }
})

test('确认路线变更成功才同步原作品，失败与切作品不误刷新', async () => {
  const original = globalThis.window
  globalThis.window = { confirm: () => true }
  try {
    for (const outcome of ['success', 'failed', 'switched']) {
      const context = await routeChoice({ id: 'b', candidates: [candidate('s')] })
      const calls = []
      context.content.setRoute = async route => {
        calls.push(['save', route])
        if (outcome === 'switched') { context.content.generation++; context.store.currentWorkId = 'other' }
        return outcome !== 'failed'
      }
      context.store.syncWork = async id => { calls.push(['sync', id]) }
      await context.chooseRoute('talking_script')
      assert.deepEqual(calls, outcome === 'success' ? [['save', 'talking_script'], ['sync', 'W']] : [['save', 'talking_script']])
      context.content.leave()
    }
  } finally { globalThis.window = original }
})

test('空作品切换路线直接保存，内容正忙时不会弹确认或保存', async () => {
  const original = globalThis.window
  const confirmations = []
  globalThis.window = { confirm: message => { confirmations.push(message); return true } }
  try {
    const context = await routeChoice()
    const calls = []
    context.content.setRoute = async route => { calls.push(['save', route]); return true }
    context.store.syncWork = async id => { calls.push(['sync', id]) }
    await context.chooseRoute('talking_script')
    assert.deepEqual(calls, [['save', 'talking_script'], ['sync', 'W']])
    assert.deepEqual(confirmations, [])
    context.content.loading = true
    await context.chooseRoute('talking_script')
    assert.equal(calls.length, 2)
    context.content.leave()
  } finally { globalThis.window = original }
})
test('分镜候选展示画面、台词、声音和镜头时长', async () => {
  const c = { ...candidate('s'), content_route: 'short_video_storyboard', storyboard: [
    { shot_index: 1, visual: '清晰场景', dialogue: '镜头对白', sound_effects: '环境声音', duration_seconds: 6, prompt: '提示' }] }
  const html = await render({ id: 'b', ready_to_present: true, requested: 3, produced: 3, passed: 3, candidates: [c] }, 'short_video_storyboard')
  assert.match(html, /清晰场景/); assert.match(html, /镜头对白/); assert.match(html, /环境声音/); assert.match(html, /6 秒/)
  assert.doesNotMatch(html, /正文s/)
  assert.ok(button(html, '人工审核'))
  assert.match(html, /待人工审核/)
})

test('人工修订采用保持独立状态，不要求智能体再次审核', async () => {
  const c = { ...candidate('human'), content_route: 'short_video_storyboard', review_state: 'needs_review',
    pickable: false, human_review_state: 'human_edit', unpickable_reasons: ['需要独立审核'] }
  const html = await render({ id: 'human-batch', ready_to_present: false, candidates: [c] }, 'short_video_storyboard')
  assert.match(html, /人工修订确认采用/)
  assert.doesNotMatch(button(html, '人工审核'), /disabled/)
  assert.doesNotMatch(html, /暂不可人工审核|需要独立审核/)
})

test('分镜候选提供编辑入口，编辑状态展示结构化镜头字段', async () => {
  const c = { ...candidate('edit'), content_route: 'short_video_storyboard', storyboard: [
    { shot_index: 1, visual: '原画面', dialogue: '原台词', sound_effects: '原声音', duration_seconds: 6, prompt: '原提示' }] }
  const idle = await render({ id: 'b', ready_to_present: true, requested: 3, produced: 3, passed: 3, candidates: [c] }, 'short_video_storyboard')
  assert.match(idle, />编辑<\/button>/)
  globalThis.storyboardEditSeed = { id: c.id, title: c.title, description: c.description, text: c.text,
    storyboard: c.storyboard.map((shot) => ({ ...shot })) }
  try {
    const editing = await render({ id: 'b', ready_to_present: true, requested: 3, produced: 3, passed: 3, candidates: [c] }, 'short_video_storyboard')
    assert.match(editing, /修改分镜剧本/)
    assert.match(editing, /画面描述/)
    assert.match(editing, /台词 \/ 旁白/)
    assert.match(editing, /声音 \/ 音效/)
    assert.match(editing, /时长（秒）/)
    assert.match(editing, /图片生成提示词/)
  } finally { delete globalThis.storyboardEditSeed }
})

const reference = (id, transcriptState = 'failed', visualState = 'none') => ({
  id, work_id: 'W', platform_work_id: id, caption: `参考${id}`, author: '',
  transcript: { state: transcriptState, text: transcriptState === 'ready' ? '有效口播' : '', source: 'redfox' },
  visual: visualState === 'none' ? {} : { state: visualState, summary: '画面摘要', scenes: [], warnings: [] },
})
const promptFor = (refId) => ({ id: 'p', version: 1, reference_snapshot_id: refId, prompt_text: '写作要求', reference_analysis: {}, angle_plan: [] })
const button = (html, label) => html.match(new RegExp(`<button[^>]*>\\s*${label}\\s*</button>`))?.[0] ?? ''

test('已导入无口播参考可以查看，详情可上传或获取视频且不需要先设为主参考', async () => {
  const inspected = reference('silent')
  const html = await render(null, 'short_video_storyboard', {
    inspectedReference: inspected,
    references: [{ id: 'silent', caption: '静音参考', transcript_state: 'failed', visual_state: 'none' }],
  })
  assert.ok(button(html, '查看参考'))
  assert.doesNotMatch(button(html, '查看参考'), /disabled/)
  assert.match(button(html, '需要口播或画面分析'), /disabled/)
  assert.match(html, /上传参考视频|自动获取视频/)
  assert.match(html, /参考详情/)
})

test('有转写的参考仍可以分析画面', async () => {
  const html = await render(null, 'short_video_storyboard', { reference: reference('speech', 'ready') })
  assert.match(html, /画面分析/)
  assert.match(html, /上传参考视频/)
  assert.match(html, /自动获取视频/)
})

test('visual-only 主参考在分镜路线可准备提示，在口播路线仍不可用', async () => {
  const ref = reference('visual', 'failed', 'ready')
  const storyboard = await render(null, 'short_video_storyboard', { reference: ref })
  assert.doesNotMatch(button(storyboard, '拆解参考并准备提示'), /disabled/)
  const talking = await render(null, 'talking_script', { reference: ref })
  assert.match(button(talking, '拆解参考并准备提示'), /disabled/)
})

test('提示旁提供明确的分镜生成入口，旧提示或忙碌时禁用', async () => {
  const ref = reference('ready', 'ready')
  const extra = { reference: ref, prompt: promptFor(ref.id) }
  const ready = await render(null, 'short_video_storyboard', extra)
  assert.ok(button(ready, '生成三篇分镜剧本'))
  assert.doesNotMatch(button(ready, '生成三篇分镜剧本'), /disabled/)
  const stale = await render(null, 'short_video_storyboard', { ...extra, prompt: promptFor('previous') })
  assert.match(button(stale, '生成三篇分镜剧本'), /disabled/)
  const busy = await render(null, 'short_video_storyboard', { ...extra, active_jobs: [{ job_id: 'j', kind: 'generation', state: 'running' }] })
  assert.match(button(busy, '生成三篇分镜剧本'), /disabled/)
  const talking = await render(null, 'talking_script', extra)
  assert.ok(button(talking, '生成三篇口播稿'))
})

test('未保存的提示修改禁用提示生成入口', async () => {
  globalThis.promptDraftSeed = '修改后尚未保存的要求'
  try {
    const ref = reference('ready', 'ready')
    const html = await render(null, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id) })
    assert.match(button(html, '生成三篇分镜剧本'), /disabled/)
    assert.doesNotMatch(html, /按提示生成三稿|按当前提示继续/)
  } finally { delete globalThis.promptDraftSeed }
})

test('旧提示的主参考失效时禁用生成，visual-only 分镜提示仍可生成', async () => {
  const ref = reference('legacy-silent')
  const blocked = await render(null, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id) })
  assert.match(button(blocked, '生成三篇分镜剧本'), /disabled/)
  assert.match(blocked, /参考待解析/)
  const visual = reference('visual-ready', 'failed', 'ready')
  const ready = await render(null, 'short_video_storyboard', { reference: visual, prompt: promptFor(visual.id) })
  assert.doesNotMatch(button(ready, '生成三篇分镜剧本'), /disabled/)
})

test('审核未通过批次不提供绕过未保存提示的重复生成入口', async () => {
  globalThis.promptDraftSeed = '尚未保存的要求'
  try {
    const ref = reference('ready', 'ready')
    const batch = { id: 'pending', requested: 3, produced: 3, passed: 2, ready_to_present: false, candidates: [], retry_targets: [] }
    const html = await render(batch, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id) })
    assert.match(button(html, '生成三篇分镜剧本'), /disabled/)
    assert.match(button(html, '重新生成三篇'), /disabled/)
    assert.equal((html.match(/>重新生成三篇<\/button>/g) ?? []).length, 1)
    assert.match(html, /重写未通过稿件|重新审核/)
  } finally { delete globalThis.promptDraftSeed }
})

test('0/3 批次仍能在提示旁显式重新生成，参考待解析时两个入口均禁用', async () => {
  const batch = { id: 'pending', requested: 3, produced: 0, passed: 0, ready_to_present: false, candidates: [], retry_targets: [] }
  const ref = reference('ready', 'ready')
  const html = await render(batch, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id) })
  assert.ok(button(html, '重新生成三篇'))
  assert.doesNotMatch(button(html, '重新生成三篇'), /disabled/)
  assert.equal((html.match(/>重新生成三篇<\/button>/g) ?? []).length, 1)
  const silent = reference('silent')
  const blocked = await render(batch, 'short_video_storyboard', { reference: silent, prompt: promptFor(silent.id) })
  assert.match(button(blocked, '生成三篇分镜剧本'), /disabled/)
  assert.match(button(blocked, '重新生成三篇'), /disabled/)
})

const unknownWriteJob = (extra = {}) => ({ job_id: 'unknown-write', kind: 'generation', state: 'unknown',
  prompt_id: 'p', content_revision: 1,
  calls: [{ id: 'write-call', step: 'write', state: 'unknown', has_remote_handle: false, reserved_fen: 127 }],
  ...extra })

test('首次写稿结果未知且无批次时也提供显式重新生成入口', async () => {
  const ref = reference('ready', 'ready')
  const html = await render(null, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id),
    recent_jobs: [unknownWriteJob()] })
  assert.ok(button(html, '重新生成三篇'), '没有批次的未知写稿也需要费用确认重试入口')
  assert.doesNotMatch(button(html, '重新生成三篇'), /disabled/)
})

test('审核、返工、可查询远端和过期写稿不能提供无批次写稿重试入口', async () => {
  const ref = reference('ready', 'ready')
  for (const job of [
    unknownWriteJob({ calls: [{ id: 'review-call', step: 'review', state: 'unknown', has_remote_handle: false }] }),
    unknownWriteJob({ kind: 'repair' }),
    unknownWriteJob({ calls: [{ id: 'remote-write', step: 'write', state: 'unknown', has_remote_handle: true }] }),
    unknownWriteJob({ prompt_id: 'old-prompt' }),
    unknownWriteJob({ content_revision: 0 }),
  ]) {
    const html = await render(null, 'short_video_storyboard', { reference: ref, prompt: promptFor(ref.id), recent_jobs: [job] })
    assert.equal(button(html, '重新生成三篇'), '', JSON.stringify(job))
  }
})

test('首次未知写稿重新生成必须先确认上次费用，拒绝时不提交', async () => {
  const pinia = createPinia(); setActivePinia(pinia)
  const content = view.useContentStore(), ref = reference('ready', 'ready')
  content.state = { work_id: 'W', content_revision: 1, brief: { content_route: 'short_video_storyboard' },
    reference: ref, prompt: promptFor(ref.id), batch: null, active_jobs: [], recent_jobs: [unknownWriteJob()], references: [] }
  const emitted = [], confirmations = [], previousWindow = globalThis.window
  let generate, accepted = false
  globalThis.promptGenerateCapture = (handler) => { generate = handler }
  globalThis.window = { confirm(message) { confirmations.push(message); return accepted } }
  try {
    await renderToString(createSSRApp(view.WritingPromptEditor, { onGenerate: (force) => emitted.push(force) }).use(pinia))
    generate(true)
    assert.deepEqual(emitted, [])
    assert.match(confirmations[0], /可能已经产生费用.*上次预留 ¥1\.27.*可能重复收费/)
    accepted = true
    generate(true)
    assert.deepEqual(emitted, [true])
  } finally {
    delete globalThis.promptGenerateCapture
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  }
})

const promptSection = (html) => html.match(/<section[^>]*aria-label="写作提示"[^>]*>[\s\S]*?<\/section>/)?.[0] ?? ''
const readyPrompt = () => ({ reference: reference('ready', 'ready'), prompt: promptFor('ready') })

test('提示已自动保存的说明就在编辑器内，空白修改不能另存', async () => {
  const saved = promptSection(await render(null, 'short_video_storyboard', readyPrompt()))
  assert.match(saved, /提示已自动保存/)
  assert.match(saved, /修改后可保存为新版本/)
  assert.match(button(saved, '保存为新版本'), /disabled/)
  globalThis.promptDraftSeed = '新的创作要求'
  try {
    const edited = promptSection(await render(null, 'short_video_storyboard', readyPrompt()))
    assert.doesNotMatch(button(edited, '保存为新版本'), /disabled/)
    assert.match(edited, /有未保存的修改/)
    globalThis.promptDraftSeed = '   '
    const blank = promptSection(await render(null, 'short_video_storyboard', readyPrompt()))
    assert.match(button(blank, '保存为新版本'), /disabled/)
    assert.match(blank, /创作要求不能为空/)
  } finally { delete globalThis.promptDraftSeed }
})

test('提交错误和操作通知显示在提示按钮旁', async () => {
  const failed = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), error: '生成请求被拒绝，请重新准备提示' }))
  assert.match(failed, /role="alert"[^>]*>生成请求被拒绝，请重新准备提示/)
  const success = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), notice: '已保存为新的提示版本。' }))
  assert.match(success, /已保存为新的提示版本/)
})

test('收到回执前与刷新期间提示区显示进度并禁用生成', async () => {
  const submitting = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), submittingKind: 'generation' }))
  assert.match(submitting, /正在提交.*生成.*请求/)
  assert.match(button(submitting, '生成三篇分镜剧本'), /disabled/)
  const refreshing = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), loading: true }))
  assert.match(refreshing, /正在刷新写作状态/)
  assert.match(button(refreshing, '生成三篇分镜剧本'), /disabled/)
  const active = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), active_jobs: [{ job_id: 'j', kind: 'generation', state: 'running', stage: 'review' }] }))
  assert.match(active, /正在处理任务.*独立审核/)
})

test('未保存、旧参考、待解析和缺主播的禁用原因显示在提示区', async () => {
  const stale = promptSection(await render(null, 'short_video_storyboard', { ...readyPrompt(), prompt: promptFor('old') }))
  assert.match(stale, /另一个参考.*重新准备/)
  const unavailable = promptSection(await render(null, 'short_video_storyboard', { reference: reference('silent'), prompt: promptFor('silent') }))
  assert.match(unavailable, /参考待解析.*转写或画面分析/)
  const missingIdentity = promptSection(await render(null, 'talking_script', { ...readyPrompt(), identityId: null }))
  assert.match(missingIdentity, /先选择主播/)
  assert.match(button(missingIdentity, '生成三篇口播稿'), /disabled/)
})

test('尚无提示但参考有效时，准备按钮也解释缺主播的禁用原因', async () => {
  const html = promptSection(await render(null, 'talking_script', { reference: reference('ready', 'ready'), identityId: null }))
  assert.match(button(html, '拆解参考并准备提示'), /disabled/)
  assert.match(html, /先选择主播/)
})
