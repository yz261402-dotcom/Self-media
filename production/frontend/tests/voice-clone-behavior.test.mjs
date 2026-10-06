import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { effectScope, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const output = await mkdtemp(resolve('node_modules/.voice-clone-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({
  stdin: { contents: `export { default } from './src/components/VoiceCloneUpload.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
  outfile: join(output, 'state.mjs'), bundle: true, format: 'esm', platform: 'node', external: ['vue', 'pinia'],
  plugins: [{ name: 'clone-test', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
    builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = new Proxy({}, { get: (_, key) => globalThis.cloneTestApi[key] });' }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: path }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const module = await import(pathToFileURL(join(output, 'state.mjs')).href)
const voice = { id: 'cloned-voice', name: '本人', is_preset: false, approval_state: 'pending', clone_metadata: { state: 'temporary' } }
beforeEach(() => {
  const entries = new Map()
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: key => entries.delete(key),
  } })
})

function setup() {
  setActivePinia(createPinia())
  const store = module.useStudioStore()
  store.works = [{ id: 'one', brief: {} }, { id: 'two', brief: {} }]
  store.currentWorkId = 'one'
  globalThis.cloneTestApi = {}
  const scope = effectScope()
  const bindings = scope.run(() => module.default.setup({}, { expose() {} }))
  return { store, scope, b: bindings }
}
function ready(b) {
  b.name.value = '本人声音'
  b.sourceEvidence.value = '本人录制，允许用于本项目配音'
  b.file.value = new File([new Uint8Array(32)], '本人.wav', { type: 'audio/wav' })
  b.duration.value = 20
  b.rightsAck.value = true
  b.activationCostAck.value = true
}

test('克隆需真实音频元数据、来源和两项确认，首次费用不能默认同意', () => {
  const { b, scope } = setup()
  try {
    ready(b)
    assert.equal(b.canSubmit.value, true)
    b.activationCostAck.value = false
    assert.equal(b.canSubmit.value, false)
    b.activationCostAck.value = true
    b.rightsAck.value = false
    assert.equal(b.canSubmit.value, false)
    b.rightsAck.value = true
    b.duration.value = null
    assert.equal(b.canSubmit.value, false)
  } finally { scope.stop() }
})

test('前端阻止超限文件和超时长音频，但不替后端判断合格', () => {
  const { b, scope } = setup()
  try {
    ready(b)
    for (const duration of [9.99, 300.01, Number.NaN, Number.POSITIVE_INFINITY]) {
      b.duration.value = duration
      assert.equal(b.canSubmit.value, false)
    }
    b.duration.value = 10
    assert.equal(b.canSubmit.value, true)
    b.file.value = { name: '过大.wav', size: 20_000_001 }
    assert.equal(b.canSubmit.value, false)
    b.file.value = { name: '不支持.mp4', size: 30 }
    assert.equal(b.canSubmit.value, false)
  } finally { scope.stop() }
})

test('网络失败保留原请求编号并锁住重提，查询成功只登记候选音色', async () => {
  const { b, store, scope } = setup()
  try {
    ready(b)
    let submissions = 0
    let original
    globalThis.cloneTestApi.createVoiceClone = async input => {
      submissions++
      original = input
      throw new Error('连接中断，结果未知')
    }
    assert.equal(typeof b.submitClone, 'function')
    await b.submitClone()
    assert.equal(submissions, 1)
    assert.equal(b.requestId.value, original.request_id)
    assert.equal(b.canSubmit.value, false)
    await b.submitClone()
    assert.equal(submissions, 1)
    globalThis.cloneTestApi.getVoiceClone = async id => {
      assert.equal(id, original.request_id)
      return { request_id: id, state: 'succeeded', voice_asset_id: voice.id }
    }
    globalThis.cloneTestApi.listVoices = async () => [voice]
    await b.queryOriginal()
    assert.equal(store.voices.length, 1)
    assert.equal(store.voices[0].id, voice.id)
    assert.equal(store.currentWork.voice_asset_id, undefined)
    assert.equal(submissions, 1)
    assert.equal(b.requestId.value, original.request_id)
  } finally { scope.stop() }
})

test('克隆回包前切换作品，不把旧作业结果放入新作品的页面状态', async () => {
  const { b, store, scope } = setup()
  try {
    ready(b)
    let finish
    globalThis.cloneTestApi.createVoiceClone = input => new Promise(resolve => { finish = () => resolve({ voice, clone_job: { request_id: input.request_id, state: 'succeeded' } }) })
    assert.equal(typeof b.submitClone, 'function')
    const pending = b.submitClone()
    while (!finish) await new Promise(resolve => setImmediate(resolve))
    store.currentWorkId = 'two'
    await nextTick()
    finish()
    await pending
    assert.equal(store.voices.length, 0)
    assert.equal(b.requestId.value, '')
    assert.equal(b.name.value, '')
    store.currentWorkId = 'one'
    await nextTick()
    assert.ok(b.requestId.value, '原作品保留可查询编号')
    assert.equal(b.canSubmit.value, false)
  } finally { scope.stop() }
})

test('查询失败仍保留未知作业，不自动开始第二次克隆', async () => {
  const { b, scope } = setup()
  try {
    ready(b)
    globalThis.cloneTestApi.createVoiceClone = async input => ({ voice: null, clone_job: { request_id: input.request_id, state: 'unknown' } })
    assert.equal(typeof b.submitClone, 'function')
    await b.submitClone()
    const id = b.requestId.value
    globalThis.cloneTestApi.getVoiceClone = async () => { throw new Error('仍无法查询') }
    await b.queryOriginal()
    assert.equal(b.requestId.value, id)
    assert.equal(b.canSubmit.value, false)
    assert.match(b.error.value, /仍无法查询/)
  } finally { scope.stop() }
})

test('本地校验被拒后查询确认没有作业，允许显式修正，未知网络结果的404仍锁定', async () => {
  for (const status of [422, undefined]) {
    const { b, scope } = setup()
    try {
      ready(b)
      globalThis.cloneTestApi.createVoiceClone = async () => { throw Object.assign(new Error('未完成'), { status }) }
      await b.submitClone()
      const id = b.requestId.value
      globalThis.cloneTestApi.getVoiceClone = async () => { throw Object.assign(new Error('没有此作业'), { status: 404 }) }
      await b.queryOriginal()
      assert.equal(b.requestId.value, id)
      assert.equal(b.unresolved.value, status !== 422)
      if (status === 422) {
        b.startNew()
        assert.equal(b.requestId.value, '')
      }
    } finally { scope.stop() }
  }
})

test('mock blocked 结果不登记音色也不锁成无法恢复的未知计费', async () => {
  const { b, store, scope } = setup()
  try {
    ready(b)
    globalThis.cloneTestApi.createVoiceClone = async input => ({ voice: null, clone_job: { request_id: input.request_id, state: 'blocked', error: { message: '模拟模式不会真实克隆' } } })
    await b.submitClone()
    assert.equal(store.voices.length, 0)
    assert.equal(b.unresolved.value, false)
    assert.match(b.error.value, /模拟模式/)
  } finally { scope.stop() }
})
