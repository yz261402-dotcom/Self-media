import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const output = await mkdtemp(resolve('node_modules/.video-poll-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'lifecycle.mjs')
await build({
  stdin: { contents: `export { default } from './src/views/VideoView.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'pinia', 'vue-router'],
  plugins: [{ name: 'vue-lifecycle-test', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
    builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const apiMode = "http"; export const api = new Proxy({}, { get: (_, key) => globalThis.videoPollingApi[key] });' }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: path }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const module = await import(pathToFileURL(outfile).href)
const flush = () => new Promise(resolve => setImmediate(resolve))

test('真实挂载生命周期在后期blocked时仍按定时器查询口播并回收原片', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = module.useStudioStore()
  store.currentWorkId = 'work'
  store.works = [{ id: 'work', brief: {} }]
  let polls = 0
  let complete = false
  const run = () => ({ id: 'run', work_id: 'work', is_mock: false, provider: 'm2_worker', route: 'M2', remote_job_id: 'remote', state: complete ? 'succeeded' : 'waiting_provider' })
  globalThis.videoPollingApi = {
    getSubmissions: async () => ({ work_id: 'work', runs: [run()] }),
    getVideos: async () => complete ? [{ id: 'original', work_id: 'work', kind: 'original', url: '/original.mp4' }] : [],
    getVideoGate: async () => ({ video_id: 'original', gate: { outcome: 'review' } }),
    getPostprocess: async () => ({ task: { state: 'blocked' } }),
    pollRun: async () => { polls++; complete = polls >= 2; return { state: complete ? 'succeeded' : 'running' } },
  }
  for (const key of ['getTalkingHeadRoutes', 'getTalkingHeadPreflight', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.videoPollingApi[key] = async () => ({ work_id: 'work' })
  }
  let timer
  let cleared = false
  const set = globalThis.setInterval, clear = globalThis.clearInterval
  globalThis.setInterval = callback => { timer = callback; return 42 }
  globalThis.clearInterval = id => { cleared = id === 42 }
  const renderer = createRenderer({
    insert() {}, remove() {}, createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {},
  })
  const app = renderer.createApp({ ...module.default, render: () => null }).use(pinia)
  try {
    app.mount({})
    await flush()
    assert.equal(polls, 1)
    assert.equal(store.postprocess.task.state, 'blocked')
    assert.equal(typeof timer, 'function')
    await timer()
    await flush()
    assert.equal(polls, 2)
    assert.equal(store.videos[0].url, '/original.mp4')
    assert.equal(store.videoGates.original.gate.outcome, 'review')
    await timer()
    assert.equal(polls, 2, '终态后停止重复回收')
  } finally {
    app.unmount()
    globalThis.setInterval = set
    globalThis.clearInterval = clear
  }
  assert.equal(cleared, true)
})

test('冷启动同作品loadWork递增版本后丢弃旧响应并自动重读原片', async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = module.useStudioStore()
  const work = { id: 'work', brief: {}, final_audio_id: 'full', current_task_id: null }
  let firstRoutes
  let routeReads = 0
  globalThis.videoPollingApi = {
    getCapabilities: async () => ({}), getFeatures: async () => [], listIdentities: async () => [],
    identitiesReadiness: async () => ({}), listVoices: async () => [], listWorks: async () => [work],
    getScriptBatch: async () => null, listVariants: async () => [], getPackage: async () => null,
    listAudios: async () => [{ id: 'full', work_id: 'work', kind: 'full', confirmed: true, duration_ms: 46428, is_mock: false }],
    getTalkingHeadRoutes: async () => {
      routeReads++
      if (routeReads === 1) return new Promise(resolve => { firstRoutes = resolve })
      return { work_id: 'work', route_confirmed: true }
    },
    getSubmissions: async () => ({ work_id: 'work', runs: [] }),
    getVideos: async () => [{ id: 'original', work_id: 'work', kind: 'original', url: '/saved-original.mp4' }],
    getVideoGate: async () => ({ video_id: 'original', gate: { outcome: 'review' } }),
    getPostprocess: async () => ({ task: { state: 'blocked' } }),
  }
  for (const key of ['getTalkingHeadPreflight', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.videoPollingApi[key] = async () => ({ work_id: 'work' })
  }
  const set = globalThis.setInterval, clear = globalThis.clearInterval
  globalThis.setInterval = () => 42
  globalThis.clearInterval = () => {}
  const renderer = createRenderer({
    insert() {}, remove() {}, createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
    setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {},
  })
  const app = renderer.createApp({ ...module.default, render: () => null }).use(pinia)
  try {
    app.mount({})
    await store.bootstrap()
    await flush()
    assert.equal(routeReads, 1)
    assert.equal(store.workLoadVersion, 1)
    assert.equal(store.confirmedAudio.duration_ms, 46428)
    firstRoutes({ work_id: 'work', route_confirmed: false })
    await flush()
    assert.equal(routeReads, 2)
    assert.equal(store.thRoutes.route_confirmed, true)
    assert.equal(store.videos[0].url, '/saved-original.mp4')
    await flush()
    assert.equal(routeReads, 2, '上下文稳定后不无限刷新')
    await store.loadWork('work')
    await flush()
    assert.equal(routeReads, 3, '同ID再次加载后也恢复视频状态')
    assert.equal(store.videos[0].id, 'original')
  } finally {
    app.unmount()
    globalThis.setInterval = set
    globalThis.clearInterval = clear
  }
})
