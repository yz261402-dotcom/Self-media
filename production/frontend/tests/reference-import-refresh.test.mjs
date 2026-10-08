import assert from 'node:assert/strict'
import { after, afterEach, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.reference-import-refresh-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'view.mjs')
await build({
  stdin: { contents: `export { default as ScriptView } from './src/views/ScriptView.vue'; export { useContentStore } from './src/stores/content.ts'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
  plugins: [{ name: 'reference-import-test', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    builder.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `
      export const apiMode = 'http';
      export const api = new Proxy({}, { get: (_, key) => (...args) => globalThis.referenceImportApi[key](...args) });
    ` }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: 'reference-import-test', inlineTemplate: true, templateOptions: { ssr: true } }).content,
        loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const { ScriptView, useContentStore, useStudioStore } = await import(pathToFileURL(outfile).href)

const emptyState = (workId, extra = {}) => ({ work_id: workId, content_revision: 1,
  brief: { topic: '主题', content_route: 'short_video_storyboard' }, reference: null, style: null,
  prompt: null, batch: null, selected_script_id: null, active_jobs: [], recent_jobs: [], references: [], limits: null, ...extra })
const pendingImport = (extra = {}) => ({ job_id: 'original-import', kind: 'import', state: 'waiting_external', stage: 'transcript',
  result_refs: {}, actions: ['cancel', 'refresh'], calls: [{ id: 'original-call', step: 'transcript_submit', provider: 'redfox',
    state: 'waiting_external', has_remote_handle: true }], ...extra })
const pausedImport = (code = 'REFERENCE_POLL_TIMEOUT', extra = {}) => pendingImport({ state: 'unknown', actions: ['verify', 'refresh'],
  error: { code, message: code === 'REFERENCE_POLL_TIMEOUT' ? '转写查询已超过 10 分钟，自动查询已暂停；原任务仍保留。' : '查询转写结果失败，自动查询已暂停；原任务仍保留。' }, ...extra })
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const flush = () => new Promise(r => setImmediate(r))
const stores = []
let pinia
beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
})
afterEach(() => { for (const store of stores.splice(0)) store.leave() })

function setup(job = pendingImport(), overrides = {}) {
  const reads = [], queries = []
  globalThis.referenceImportApi = {
    getContentState: async id => { reads.push(id); return emptyState(id, { active_jobs: id === 'W' ? [job] : [] }) },
    getContentJob: async () => job,
    refreshContentJob: async (...args) => { queries.push(args); return { job_id: job.job_id, state: 'queued', status: 'refreshed' } },
    ...overrides,
  }
  const store = useContentStore()
  store.workId = 'W'
  store.state = emptyState('W', { active_jobs: [job] })
  stores.push(store)
  return { store, reads, queries }
}

test('等待中的参考只显式查询原导入任务，不重新提交导入', async () => {
  const { store, queries, reads } = setup()
  assert.equal(store.waitingImportJob?.job_id, 'original-import')
  await store.refreshImportResult('original-import')
  assert.deepEqual(queries, [['W', 'original-import']])
  assert.deepEqual(reads, ['W'])
  assert.equal(store.refreshingImportJobId, '')
  assert.equal(store.polling, true, '查询后继续读取原任务的本地进度')
})

test('重复点击查询不会并发查询同一个远端任务', async () => {
  const slow = deferred(), calls = []
  const { store } = setup(pendingImport(), { refreshContentJob: (...args) => { calls.push(args); return slow.promise } })
  const first = store.refreshImportResult('original-import')
  await flush()
  assert.equal(store.refreshingImportJobId, 'original-import')
  const second = store.refreshImportResult('original-import')
  await flush()
  assert.equal(calls.length, 1)
  slow.resolve({ job_id: 'original-import', state: 'waiting_external', status: 'refreshed' })
  await Promise.all([first, second])
  assert.equal(store.refreshingImportJobId, '')
})

test('刚读取的任务不允许 refresh 时不查询远端', async () => {
  const { store, queries } = setup(pendingImport(), {
    getContentJob: async () => pendingImport({ actions: ['cancel'] }),
  })
  await store.refreshImportResult('original-import')
  assert.equal(queries.length, 0)
  assert.equal(store.refreshingImportJobId, '')
})

test('结果未知、无远端句柄或其他任务类型均不查询或重提', async () => {
  for (const job of [
    pendingImport({ state: 'unknown' }),
    pendingImport({ calls: [{ step: 'transcript_submit', state: 'waiting_external', has_remote_handle: false }] }),
    pendingImport({ kind: 'generation' }),
    pausedImport('REFERENCE_POLL_TIMEOUT', { calls: [{ step: 'transcript_submit', provider: 'redfox', state: 'unknown', has_remote_handle: false }] }),
    pausedImport('REFERENCE_QUERY_FAILED', { calls: [{ step: 'transcript_submit', provider: 'other', state: 'waiting_external', has_remote_handle: true }] }),
  ]) {
    const { store, queries } = setup(job)
    assert.equal(store.waitingImportJob, null)
    await store.refreshImportResult(job.job_id)
    assert.equal(queries.length, 0)
    store.leave()
    setActivePinia(createPinia())
  }
})

test('切作品后原查询的迟到结果不会覆盖当前作品状态或提示', async () => {
  const slow = deferred(), calls = []
  const { store, reads } = setup(pendingImport(), { refreshContentJob: (...args) => { calls.push(args); return slow.promise } })
  const oldQuery = store.refreshImportResult('original-import')
  await flush()
  assert.deepEqual(calls, [['W', 'original-import']])
  await store.enter('B')
  store.notice = '新作品提示'
  assert.equal(store.refreshingImportJobId, '')
  slow.resolve({ job_id: 'original-import', state: 'queued', status: 'refreshed' })
  await oldQuery
  assert.equal(store.state.work_id, 'B')
  assert.equal(store.notice, '新作品提示')
  assert.deepEqual(reads, ['B'], '旧响应不得重新请求旧作品或刷新新作品')
  assert.equal(store.polling, false)
})

test('普通状态轮询只读取本地状态，不自动查询红狐', async () => {
  const { store, queries, reads } = setup()
  store.startPolling()
  await new Promise(r => setTimeout(r, 700))
  assert.ok(reads.length >= 1)
  assert.equal(queries.length, 0)
})

async function renderWaitingImport(refreshing = '', job = pendingImport()) {
  const { store } = setup(job)
  const paused = job.state === 'unknown'
  if (paused) store.state = emptyState('W', { recent_jobs: [job] })
  store.refreshingImportJobId = refreshing
  const studio = useStudioStore()
  studio.currentWorkId = 'W'
  studio.apiReady = true
  studio.works = [{ id: 'W', identity_id: 'person', brief: { topic: '主题', content_route: 'short_video_storyboard' } }]
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/script', component: ScriptView }] })
  await router.push('/script')
  assert.equal(store.busy, !paused)
  return renderToString(createSSRApp(ScriptView).use(pinia).use(router))
}
const queryButton = html => [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
  .map(match => match[0]).find(button => /查询转写进度|继续查询转写|查询中/.test(button))

test('导入等待期间显示可点击的查询入口，内容 busy 不锁住该入口', async () => {
  const html = await renderWaitingImport()
  const button = queryButton(html)
  assert.ok(button, '内容创作页应显示查询转写进度按钮')
  assert.doesNotMatch(button, /\bdisabled\b/)
})

test('显式查询正在请求时只禁用当前查询按钮', async () => {
  const html = await renderWaitingImport('original-import')
  const button = queryButton(html)
  assert.ok(button)
  assert.match(button, /\bdisabled\b/)
})

test('自动转写查询暂停后从历史任务恢复原句柄，其他创作操作解除 busy', async () => {
  for (const code of ['REFERENCE_POLL_TIMEOUT', 'REFERENCE_QUERY_FAILED']) {
    const job = pausedImport(code), { store, queries } = setup(job)
    store.state = emptyState('W', { recent_jobs: [pausedImport(code, { job_id: 'older-import' }), job] })
    assert.equal(store.activeJob, null)
    assert.equal(store.busy, false)
    assert.equal(store.waitingImportJob?.job_id, 'original-import', '优先恢复最新一次暂停的查询')
    await store.refreshImportResult('original-import')
    assert.deepEqual(queries, [['W', 'original-import']])
    store.leave()
    setActivePinia(createPinia())
  }
})

test('暂停的转写查询保留具体错误，页面显示继续查询入口且不占用 busy', async () => {
  for (const code of ['REFERENCE_POLL_TIMEOUT', 'REFERENCE_QUERY_FAILED']) {
    const job = pausedImport(code)
    const html = await renderWaitingImport('', job)
    assert.match(html, /查询已暂停/)
    assert.ok(html.includes(job.error.message), '显示可定位的查询错误，避免误报为重新生成结果未知')
    const button = queryButton(html)
    assert.ok(button)
    assert.doesNotMatch(button, /\bdisabled\b/)
  }
})

test('导入结果查询超时的任务结束后，store显示查询超时原因', async () => {
  const job = pausedImport()
  const { store } = setup(job)
  await store.waitForJob(job.job_id, () => {})
  assert.equal(store.error, job.error.message)
})

test('正常等待期间界面明确转写结果会自动同步', async () => {
  const html = await renderWaitingImport()
  assert.match(html, /自动同步|自动查询/)
})
