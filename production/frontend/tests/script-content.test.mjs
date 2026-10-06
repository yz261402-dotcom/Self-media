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
  stdin: { contents: `export { default } from './src/views/ScriptView.vue'; export { useStudioStore } from './src/stores/studio.ts'; export { useContentStore } from './src/stores/content.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
  plugins: [{ name: 'vue', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    builder.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const api = { quoteMediaFetch: async () => ({ reserve_fen: 50 }), quoteVisualAnalysis: async () => ({ reserve_fen: 80 }) }; export const apiMode = "http";' }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      let source = await readFile(path, 'utf8')
      if (path.replaceAll('\\', '/').endsWith('/components/WritingPromptEditor.vue')) {
        source = source.replace('</script>', 'if (globalThis.promptDraftSeed !== undefined) draft.value = globalThis.promptDraftSeed\n</script>')
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
test('2/3 时隐藏所有候选正文并显示安全返工入口', async () => {
  const html = await render({ id: 'b', requested: 3, produced: 3, passed: 2, ready_to_present: false,
    candidates: [candidate('hidden')], retry_targets: [{ candidate_id: 'hidden', slot: 3, reason: '审核尚未通过' }] })
  assert.doesNotMatch(html, /正文hidden|标题hidden/)
  assert.match(html, /继续返工/); assert.match(html, /已通过 2 \/ 已产出 3/)
})
test('3/3 时显示三个候选并提供路线选择', async () => {
  const html = await render({ id: 'b', requested: 3, produced: 3, passed: 3, ready_to_present: true, candidates: [1, 2, 3].map(candidate) })
  assert.match(html, /正文1/); assert.match(html, /正文2/); assert.match(html, /正文3/)
  assert.match(html, /aria-label="内容路线"/); assert.match(html, /短视频分镜/)
})
test('分镜候选展示画面、台词、声音和镜头时长', async () => {
  const c = { ...candidate('s'), content_route: 'short_video_storyboard', storyboard: [
    { shot_index: 1, visual: '清晰场景', dialogue: '镜头对白', sound_effects: '环境声音', duration_seconds: 6, prompt: '提示' }] }
  const html = await render({ id: 'b', ready_to_present: true, requested: 3, produced: 3, passed: 3, candidates: [c] }, 'short_video_storyboard')
  assert.match(html, /清晰场景/); assert.match(html, /镜头对白/); assert.match(html, /环境声音/); assert.match(html, /6 秒/)
  assert.doesNotMatch(html, /正文s/)
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
    assert.match(html, /继续返工|重新审核/)
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
