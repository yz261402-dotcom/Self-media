import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, effectScope, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'

const output = await mkdtemp(resolve('node_modules/.motion-upload-tests-'))
after(() => rm(output, { recursive: true, force: true }))
for (const ssr of [false, true]) {
  await build({
    stdin: { contents: `export { default } from './src/components/MotionUpload.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
    outfile: join(output, ssr ? 'view.mjs' : 'state.mjs'), bundle: true, format: 'esm', platform: 'node',
    external: ['vue', 'pinia'],
    plugins: [{ name: 'motion-state', setup(builder) {
      builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = new Proxy({}, { get: (_, key) => globalThis.motionTestApi[key] });' }))
      builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
        let text = await readFile(path, 'utf8')
        if (ssr) text = text.replace('</script>', 'if (globalThis.motionViewSeed) { open.value = true; chooseUpload(globalThis.motionViewSeed) }\n</script>')
        const { descriptor } = parse(text, { filename: path })
        const compiled = compileScript(descriptor, { id: path, inlineTemplate: ssr, templateOptions: { ssr } })
        return { contents: compiled.content, loader: 'ts', resolveDir: dirname(path) }
      })
    } }],
  })
}
const state = await import(pathToFileURL(join(output, 'state.mjs')).href)
const view = await import(pathToFileURL(join(output, 'view.mjs')).href)
const upload = { id: 'up', identity_id: 'person', state: 'pending', original_filename: '自定义点头.mp4', duration_seconds: 5, width: 720, height: 1280, size_bytes: 4096, preview_url: '/preview.mp4', reference_id: null }
const reference = { id: 'ref', identity_id: 'person', motion_key: 'nod', label: '点头', approval_state: 'approved' }
function prepare(module = state) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = module.useStudioStore()
  store.works = [{ id: 'work', identity_id: 'person', brief: {} }, { id: 'other', identity_id: 'other_person', brief: {} }]
  store.currentWorkId = 'work'
  globalThis.motionTestApi = {
    listMotionUploads: async () => ({ uploads: [upload] }),
    listMotions: async () => ({ approved: [reference] }),
    getTalkingHeadPreflight: async () => null,
  }
  return { pinia, store }
}
function setupState() {
  const { store } = prepare()
  const scope = effectScope()
  const bindings = scope.run(() => state.default.setup({}, { expose() {} }))
  return { store, scope, bindings }
}

test('已校验的 pending 上传展示待确认表单，不误认为后台处理中', async () => {
  const { pinia } = prepare(view)
  globalThis.motionViewSeed = upload
  const html = await renderToString(createSSRApp(view.default).use(pinia))
  delete globalThis.motionViewSeed
  assert.match(html, /模板名称/)
  assert.match(html, /视频来源依据/)
  assert.match(html, /保存并用于本期/)
  assert.doesNotMatch(html, /仍在后端检查|后端正在检查/)
})

test('上传视频必须播放并勾选，不能仅填文字确认', () => {
  const { scope, bindings: b } = setupState()
  try {
    b.chooseUpload(upload)
    b.label.value = '点头'
    b.sourceBasis.value = '本人拍摄'
    b.acknowledged.value = true
    assert.equal(b.canConfirm.value, false)
    b.previewPlayed.value = true
    assert.equal(b.canConfirm.value, true)
    b.previewFailed.value = true
    assert.equal(b.canConfirm.value, false)
  } finally { scope.stop() }
})

test('确认接口回包前切换人物不会绑定新作品', async () => {
  const { store, scope, bindings: b } = setupState()
  try {
    let finish
    let adopted = 0
    globalThis.motionTestApi.confirmMotionUpload = () => new Promise(resolve => { finish = resolve })
    globalThis.motionTestApi.setMotionIntent = async () => { adopted++; return {} }
    b.chooseUpload(upload)
    b.label.value = '点头'
    b.sourceBasis.value = '本人拍摄'
    b.acknowledged.value = true
    if (b.previewPlayed) b.previewPlayed.value = true
    const pending = b.confirmAndAdopt()
    assert.equal(typeof finish, 'function')
    store.currentWorkId = 'other'
    finish(reference)
    await pending
    await nextTick()
    assert.equal(adopted, 0)
    assert.equal(b.savedReference.value, null)
  } finally { scope.stop() }
})

test('刷新后已确认上传可以用已有引用绑定，不重复确认模板', async () => {
  const { scope, bindings: b } = setupState()
  try {
    let confirmations = 0
    let adopted
    globalThis.motionTestApi.confirmMotionUpload = async () => { confirmations++; return reference }
    globalThis.motionTestApi.setMotionIntent = async (workId, input) => { adopted = input; return { work_id: workId, motion_intent: input } }
    b.chooseUpload({ ...upload, state: 'confirmed', reference_id: 'ref' })
    await b.useConfirmed()
    assert.equal(confirmations, 0)
    assert.equal(adopted.motion_ref_id, 'ref')
  } finally { scope.stop() }
})
