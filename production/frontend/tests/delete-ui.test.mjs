import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, createRenderer } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.delete-ui-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const views = {}
for (const name of ['App', 'views/AvatarView']) {
  views[name] = {}
  for (const ssr of [false, true]) {
    const outfile = join(output, `${name.replaceAll('/', '-')}-${ssr}.mjs`)
    await build({
      stdin: { contents: `export { default } from './src/${name}.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
      outfile, bundle: true, platform: 'node', format: 'esm', external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
      plugins: [{ name: 'delete-ui-test', setup(builder) {
        builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
        builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = new Proxy({}, { get: (_, key) => globalThis.deleteUiApi[key] }); export const apiMode = "http";' }))
        builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
          let source = await readFile(path, 'utf8')
          if (ssr && path.replaceAll('\\', '/').endsWith(`/src/${name}.vue`)) {
            source = source.replace('</script>', `if (globalThis.deleteUiSeed) {
              if (typeof startWorkDelete === 'function') startWorkDelete();
              if (typeof startIdentityDelete === 'function') startIdentityDelete(store.identities[0]);
              if (typeof deleteWorkError !== 'undefined') deleteWorkError.value = globalThis.deleteUiSeed.error;
              if (typeof deleteIdentityError !== 'undefined') deleteIdentityError.value = globalThis.deleteUiSeed.error;
              if (typeof deletingWork !== 'undefined') deletingWork.value = globalThis.deleteUiSeed.pending;
              if (typeof deletingIdentity !== 'undefined') deletingIdentity.value = globalThis.deleteUiSeed.pending;
            }\n</script>`)
          }
          const { descriptor } = parse(source, { filename: path })
          const id = createHash('sha256').update(path).digest('hex').slice(0, 12)
          return { contents: compileScript(descriptor, { id, inlineTemplate: ssr, templateOptions: { ssr } }).content, loader: 'ts', resolveDir: dirname(path) }
        })
      } }],
    })
    views[name][ssr ? 'ssr' : 'state'] = await import(pathToFileURL(outfile).href)
  }
}

const work = { id: 'work', name: '测试一期', brief: {}, identity_id: null }
const person = { id: 'person', name: '测试主播', approval_state: 'approved', frozen: true }
beforeEach(() => { delete globalThis.deleteUiSeed; globalThis.deleteUiApi = { listIdentityAssets: async () => [] } })
function setup(name) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const view = views[name].state
  const store = view.useStudioStore()
  store.works = [structuredClone(work)]
  store.currentWorkId = work.id
  store.identities = [structuredClone(person)]
  store.bootstrap = async () => {}
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  const renderer = createRenderer({ insert() {}, remove() {}, createElement: () => ({}), createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {}, parentNode: () => null, nextSibling: () => null, patchProp() {} })
  let b
  const app = renderer.createApp({ setup() { b = view.default.setup({}, { expose() {} }); return () => null } }).use(pinia).use(router)
  app.mount({})
  return { store, scope: { stop: () => app.unmount() }, b }
}
async function render(name) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const view = views[name].ssr
  const store = view.useStudioStore()
  store.apiReady = true
  store.works = [structuredClone(work)]
  store.currentWorkId = work.id
  if (globalThis.deleteUiSeed?.empty) { store.works = []; store.currentWorkId = null }
  store.identities = [structuredClone(person)]
  store.discardedIdentities = [{ id: 'old', name: '已丢弃主播', discarded_at: 'today' }]
  const router = createRouter({ history: createMemoryHistory(), routes: ['avatar', 'script', 'tts', 'video', 'publish', 'metrics', 'models'].map(name => ({ path: `/${name}`, component: { template: '<div data-test="router-content"></div>' } })) })
  await router.push('/avatar')
  return renderToString(createSSRApp(view.default).use(pinia).use(router))
}

for (const [name, entity, open, cancel, confirm, target, pending, error, action] of [
  ['App', '作品', 'startWorkDelete', 'cancelWorkDelete', 'confirmWorkDelete', 'workToDelete', 'deletingWork', 'deleteWorkError', 'deleteWork'],
  ['views/AvatarView', '人物', 'startIdentityDelete', 'cancelIdentityDelete', 'confirmIdentityDelete', 'identityToDelete', 'deletingIdentity', 'deleteIdentityError', 'deleteIdentity'],
]) {
  test(`${entity}取消不删除，失败保留弹框和具体原因，等待禁止重复提交`, async () => {
    const { store, scope, b } = setup(name)
    try {
      assert.equal(typeof b[open], 'function')
      let calls = 0
      store[action] = async () => { calls++; store.error = '仍有任务运行'; return false }
      b[open](person)
      b[cancel]()
      assert.equal(b[target].value, null)
      assert.equal(calls, 0)
      b[open](person)
      await b[confirm]()
      assert.ok(b[target].value)
      assert.equal(b[error].value, '仍有任务运行')
      let release
      store[action] = () => { calls++; return new Promise(resolve => { release = resolve }) }
      const first = b[confirm]()
      assert.equal(b[pending].value, true)
      b[cancel]()
      await b[confirm]()
      assert.equal(calls, 2)
      assert.ok(b[target].value)
      release(true)
      await first
      assert.equal(b[target].value, null)
      assert.equal(b[pending].value, false)
    } finally { scope.stop() }
  })
  test(`${entity}确认弹框显示名称、内联错误并禁用等待按钮`, async () => {
    globalThis.deleteUiSeed = { error: '服务端具体原因', pending: true }
    const html = await render(name)
    assert.match(html, new RegExp(`aria-label="删除${entity}"`))
    assert.match(html, /服务端具体原因/)
    assert.match(html, /role="alert"/)
    assert.match(html, entity === '作品' ? /测试一期/ : /测试主播/)
    assert.match(html, /<button[^>]*disabled[^>]*>[^<]*删除中/)
  })
}

test('已定版人物及已丢弃候选均有删除入口', async () => {
  const html = await render('views/AvatarView')
  assert.equal((html.match(/>删除人物<\/button>/g) ?? []).length, 2)
})

test('删除最后作品后人物库仍然可访问', async () => {
  globalThis.deleteUiSeed = { empty: true, error: '', pending: false }
  const html = await render('App')
  assert.match(html, /data-test="router-content"/)
})

test('作品确认固定最初对象，切换选择不会误删另一作品', async () => {
  const { store, scope, b } = setup('App')
  try {
    assert.equal(typeof b.startWorkDelete, 'function')
    b.startWorkDelete()
    store.works.push({ id: 'other', name: '另一件', brief: {} })
    store.currentWorkId = 'other'
    let id
    store.deleteWork = async value => { id = value; return true }
    await b.confirmWorkDelete()
    assert.equal(id, 'work')
  } finally { scope.stop() }
})

test('人物删除成功清除生成候选、图片与已打开资料', async () => {
  const { store, scope, b } = setup('views/AvatarView')
  try {
    assert.equal(typeof b.startIdentityDelete, 'function')
    store.deleteIdentity = async () => true
    b.candidates.value = [{ identity: person }, { identity: { id: 'keep' } }]
    b.avatarImages.value = { person: '/old.png', keep: '/keep.png' }
    b.inspectId.value = person.id
    b.heroAssets.value = [{ id: 'old-hero' }]
    b.startIdentityDelete(person)
    await b.confirmIdentityDelete()
    assert.equal(b.inspectId.value, '')
    assert.deepEqual(b.heroAssets.value, [])
    assert.deepEqual(b.candidates.value.map(item => item.identity.id), ['keep'])
    assert.equal(b.avatarImages.value.person, undefined)
    assert.equal(b.avatarImages.value.keep, '/keep.png')
  } finally { scope.stop() }
})
