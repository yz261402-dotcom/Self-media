import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'

const output = await mkdtemp(resolve('node_modules/.manual-storyboard-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({
  stdin: { contents: `export { default as Creator } from './src/components/ManualStoryboardCreator.vue';
    export { default as Candidate } from './src/components/ScriptCandidateCard.vue';
    export * from './src/data/manualStoryboard.ts';`, resolveDir: resolve('.') },
  outfile: join(output, 'components.mjs'), bundle: true, format: 'esm', platform: 'node', external: ['vue'],
  plugins: [{ name: 'vue', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    builder.onResolve({ filter: /^@\/api\/http$/ }, () => ({ path: 'http', namespace: 'stub' }))
    builder.onLoad({ filter: /.*/, namespace: 'stub' }, ({ path }) => ({ contents: path === 'api'
      ? 'export const apiMode = "http";'
      : `export class ApiError extends Error { constructor(message, status) { super(message); this.status = status } }
        export const httpApi = { createManualStoryboard: (...args) => globalThis.manualSubmit(...args) };` }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      let source = await readFile(path, 'utf8')
      if (path.replaceAll('\\', '/').endsWith('/ManualStoryboardCreator.vue')) {
        source = source.replace('</script>', `if (globalThis.captureCreator) globalThis.captureCreator({
          title, description, storyboard, jsonText, reason, manualAck, factsAck, pending, mode, error,
          canSubmit, locked, submit, importJson, moveShot, addShot, restoreDraft, saveDraft })\n</script>`)
      }
      const { descriptor } = parse(source, { filename: path })
      return { contents: compileScript(descriptor, { id: 'manual-test', inlineTemplate: true,
        templateOptions: { ssr: true } }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const ui = await import(pathToFileURL(join(output, 'components.mjs')).href)
await build({ entryPoints: ['src/api/http.ts'], outfile: join(output, 'http.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { httpApi } = await import(pathToFileURL(join(output, 'http.mjs')).href)
const shot = (index = 1) => ({ shot_index: index, visual: '画面内容', dialogue: '台词内容', sound_effects: '无', duration_seconds: 5, prompt: '' })
function localStorageStub() {
  const values = new Map()
  globalThis.localStorage = { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) }
  return values
}
async function creator(props = { workId: 'work', revision: 3 }) {
  let captured
  globalThis.captureCreator = value => { captured = value }
  try { await renderToString(createSSRApp(ui.Creator, props)) }
  finally { delete globalThis.captureCreator }
  return captured
}
function populate(form) {
  form.title.value = '人工标题'; form.storyboard.value = [shot()]; form.reason.value = '模型不可用'
  form.manualAck.value = true; form.factsAck.value = true
}

test('JSON 导入接受对象和镜头数组，未提供 prompt 时根据已写字段生成', () => {
  const object = ui.parseManualStoryboardJson(JSON.stringify({ title: '标题', description: '简介', storyboard: [shot()] }))
  assert.equal(object.title, '标题')
  assert.match(object.storyboard[0].prompt, /画面内容/)
  const array = ui.parseManualStoryboardJson(JSON.stringify([shot()]), '外部标题', '外部简介')
  assert.equal(array.title, '外部标题'); assert.equal(array.description, '外部简介')
})

test('导入拒绝类型混淆，结构校验拒绝空稿、断号和非整数时长', () => {
  assert.throws(() => ui.parseManualStoryboardJson(JSON.stringify([{ ...shot(), duration_seconds: '5' }])), /格式不正确/)
  assert.throws(() => ui.parseManualStoryboardJson(JSON.stringify({ title: {}, storyboard: [shot()] })), /title/)
  assert.throws(() => ui.parseManualStoryboardJson('{}'), /JSON/)
  assert.ok(ui.validateManualStoryboard('', []).length)
  for (const duration of [1, 31, 2.5, NaN]) assert.ok(ui.validateManualStoryboard('标题', [{ ...shot(), duration_seconds: duration }]).length)
  assert.ok(ui.validateManualStoryboard('标题', [shot(2)]).length)
  assert.ok(ui.validateManualStoryboard('标题', [{ ...shot(), dialogue: ' ' }]).length)
  assert.deepEqual(ui.validateManualStoryboard('标题', [shot(), shot(2)]), [])
})

test('手写提交要求接管原因及两次内容确认，按顺序移动后重新编号', async () => {
  localStorageStub()
  const form = await creator()
  populate(form)
  assert.equal(form.canSubmit.value, true)
  form.manualAck.value = false; assert.equal(form.canSubmit.value, false)
  form.manualAck.value = true; form.factsAck.value = false; assert.equal(form.canSubmit.value, false)
  form.factsAck.value = true; form.reason.value = ' '; assert.equal(form.canSubmit.value, false)
  form.storyboard.value = [{ ...shot(), visual: '第一镜头' }, { ...shot(2), visual: '第二镜头' }]
  form.moveShot(1, -1)
  assert.equal(form.storyboard.value[0].visual, '第二镜头')
  assert.deepEqual(form.storyboard.value.map(s => s.shot_index), [1, 2])
})

test('JSON 解析失败保留手写稿，解析后还需明确保存操作', async () => {
  localStorageStub()
  const form = await creator(); populate(form)
  let calls = 0
  globalThis.manualSubmit = async () => { ++calls }
  form.jsonText.value = '{broken'; form.importJson()
  assert.equal(form.title.value, '人工标题'); assert.match(form.error.value, /格式不正确/)
  form.jsonText.value = JSON.stringify({ title: '导入标题', storyboard: [shot()] }); form.importJson()
  assert.equal(form.title.value, '导入标题'); assert.equal(form.mode.value, 'structured'); assert.equal(calls, 0)
})

test('未知提交结果保留原意图、刷新锁定草稿，重确认沿用请求编号', async () => {
  const values = localStorageStub()
  const form = await creator(); populate(form)
  const requests = []
  let finish
  globalThis.manualSubmit = async (work, input) => {
    requests.push({ work, input: JSON.parse(JSON.stringify(input)) })
    await new Promise(resolve => { finish = resolve })
    throw new Error('响应丢失')
  }
  const first = form.submit()
  await form.submit()
  assert.equal(requests.length, 1, `重复点击不能重复请求：${form.error.value}`)
  finish(); await first; await nextTick()
  const requestId = form.pending.value.request_id
  assert.equal(form.locked.value, true)
  assert.equal(JSON.parse(values.get('avatar-studio:manual-storyboard:work:3')).pending.request_id, requestId)
  const restored = await creator()
  assert.equal(restored.pending.value.request_id, requestId)
  globalThis.manualSubmit = async (work, input) => { requests.push({ work, input: JSON.parse(JSON.stringify(input)) }); return { ok: true, script_id: 'manual', batch_id: 'batch', content_revision: 3, candidate: {} } }
  await restored.submit()
  assert.deepEqual(requests[0], requests[1], '重确认原始请求必须保持相同内容与 request_id')
  assert.equal(values.has('avatar-studio:manual-storyboard:work:3'), false)
  const otherRevision = await creator({ workId: 'work', revision: 4 })
  assert.equal(otherRevision.title.value, '')
})

test('人工候选无 AI 通过标签、可进入人工评分，普通未审稿仍阻断', async () => {
  const candidate = { id: 'manual', title: '人工稿', angle: '', description: '', tags: [], text: '', storyboard: [shot()],
    content_route: 'short_video_storyboard', claims: [], pickable: false, review_state: 'needs_review',
    creation_source: 'manual', ai_review_status: 'not_performed', human_review_state: 'pending',
    quality_report: { checks: [], overall: 'review', review_required: true } }
  const html = await renderToString(createSSRApp(ui.Candidate, { candidate, index: 0, adopted: false, busy: false, humanReview: true }))
  assert.match(html, /人工导入 · 未执行模型审核/)
  assert.doesNotMatch(html, /已通过独立审核|重新审核|按意见重写/)
  const reviewButton = html.replace(/<!--[\s\S]*?-->/g, '').match(/<button[^>]*>\s*人工审核\s*<\/button>/)?.[0]
  assert.ok(reviewButton); assert.doesNotMatch(reviewButton, /disabled/)
  const normal = await renderToString(createSSRApp(ui.Candidate, { candidate: { ...candidate, creation_source: 'llm', ai_review_status: 'required' }, index: 0, adopted: false, busy: false, humanReview: true }))
  assert.match(normal.replace(/<!--[\s\S]*?-->/g, '').match(/<button[^>]*>\s*人工审核\s*<\/button>/)?.[0] ?? '', /disabled/)
  const adopted = await renderToString(createSSRApp(ui.Candidate, { candidate: { ...candidate, human_review_state: 'manual_takeover' }, index: 0, adopted: true, busy: false, humanReview: true }))
  assert.match(adopted, /人工接管评分合格/)
})

test('HTTP 两个降级入口使用作品隔离路径，传递修订号和原请求编号', async () => {
  const calls = []
  globalThis.fetch = async (url, init) => { calls.push({ url, body: JSON.parse(init.body) }); return new Response('{"ok":true}', { status: 200 }) }
  const manual = { request_id: 'manual-id', expected_content_revision: 3, title: '标题', storyboard: [shot()], takeover_reason: '故障', manual_ack: true, facts_ack: true }
  await httpApi.createManualStoryboard('work a', manual)
  await httpApi.startDirectGeneration('work a', { request_id: 'direct-id', expected_content_revision: 3, topic: '主题', materials: ['资料'], content_route: 'short_video_storyboard' })
  assert.equal(calls[0].url, '/api/v1/works/work%20a/content/manual-storyboards')
  assert.deepEqual(calls[0].body, manual)
  assert.equal(calls[1].url, '/api/v1/works/work%20a/content/direct-generations')
  assert.equal(calls[1].body.request_id, 'direct-id'); assert.equal(calls[1].body.expected_content_revision, 3)
})
