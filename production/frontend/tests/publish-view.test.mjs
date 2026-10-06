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
import { createRouter, createMemoryHistory } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.studio-publish-tests-'))
after(() => rm(output, { recursive: true, force: true }))

for (const ssr of [false, true]) {
const outfile = join(output, ssr ? 'PublishView.mjs' : 'PublishState.mjs')
await build({
  stdin: {
    contents: "export { default } from './src/views/PublishView.vue'; export { useStudioStore } from './src/stores/studio.ts'",
    resolveDir: resolve('.'),
  },
  outfile,
  bundle: true,
  format: 'esm',
  platform: 'node',
  external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
  plugins: [{
    name: 'vue-test',
    setup(builder) {
      builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = new Proxy({}, {get: (_, key) => globalThis.publishTestApi?.[key]}); export const apiMode = "http";' }))
      builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
        let source = await readFile(path, 'utf8')
        if (ssr) source = source.replace('</script>', 'if (globalThis.publishViewSeed) publishTask.value = globalThis.publishViewSeed\n</script>')
        const { descriptor } = parse(source, { filename: path })
        const result = compileScript(descriptor, { id: 'publish-test', inlineTemplate: ssr, templateOptions: { ssr } })
        return { contents: result.content, loader: 'ts', resolveDir: dirname(path) }
      })
    },
  }],
})
}

const view = await import(pathToFileURL(join(output, 'PublishView.mjs')).href)
const state = await import(pathToFileURL(join(output, 'PublishState.mjs')).href)

async function render(setup) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = view.useStudioStore()
  store.apiReady = true
  setup(store)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['/publish', '/video'].map(path => ({ path, component: { template: '<div />' } })),
  })
  await router.push('/publish')
  return renderToString(createSSRApp(view.default).use(pinia).use(router))
}

test('无交付包时发布页阻断准备并引导返回视频制作', async () => {
  const html = await render(store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', name: 'RAG 入门', brief: {} }]
  })
  assert.match(html, /尚未有可发布的交付包/)
  assert.match(html, /去制作视频/)
  assert.doesNotMatch(html, /确认并发布/)
})

function setupState() {
  setActivePinia(createPinia())
  const store = state.useStudioStore()
  store.currentWorkId = 'work'
  store.works = [{ id: 'work', name: 'RAG 入门', brief: {} }, { id: 'other', name: '另一作品', brief: {} }]
  store.pkg = { id: 'pkg', work_id: 'work', state: 'ready', mode: 'production', title: 'RAG 标题', description: '简介', tags: ['AI'], video_url: '/video.mp4', download_url: '/pkg.zip' }
  globalThis.publishTestApi = { getPublishStatus: async () => ({ id: null, work_id: 'work', state: 'draft' }) }
  const scope = effectScope()
  const bindings = scope.run(() => state.default.setup({}, { expose() {} }))
  return { store, scope, bindings }
}

test('开始准备只调用后端准备端点且显式禁止自动提交', async () => {
  const { scope, bindings: b } = setupState()
  try {
    let request
    globalThis.publishTestApi.startPublish = async (workId, input) => {
      request = { workId, ...input }
      return { id: 'pub', work_id: 'work', state: 'awaiting_login' }
    }
    globalThis.publishTestApi.confirmPublish = () => assert.fail('准备不能提交发布')
    await b.beginPrepare()
    assert.deepEqual(request, { workId: 'work', platform: 'douyin', auto_submit: false, title: 'RAG 标题', description: '简介', tags: ['AI'] })
    assert.equal(b.publishTask.value.state, 'awaiting_login')
  } finally { scope.stop() }
})

test('只有后端待确认任务加人工勾选才可触发明确发布操作', async () => {
  const { scope, bindings: b } = setupState()
  try {
    const calls = []
    globalThis.publishTestApi.confirmPublish = async (...args) => {
      calls.push(args)
      return { id: 'pub', work_id: 'work', state: 'submitted' }
    }
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'awaiting_login', requires_user_confirmation: true }
    b.publishAcknowledged.value = true
    await b.confirmAndPublish()
    assert.equal(calls.length, 0)
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'ready_to_confirm', requires_user_confirmation: true, package_digest: 'digest-reviewed', confirmation_digest: 'confirmation-reviewed' }
    b.publishAcknowledged.value = false
    await b.confirmAndPublish()
    assert.equal(calls.length, 0)
    b.publishAcknowledged.value = true
    await b.confirmAndPublish()
    assert.deepEqual(calls, [['work', 'pub', 'digest-reviewed', 'confirmation-reviewed']])
    assert.equal(b.publishTask.value.state, 'submitted')
  } finally { scope.stop() }
})

test('待确认任务没有服务端交付包摘要时不能提交发布', async () => {
  const { scope, bindings: b } = setupState()
  try {
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'ready_to_confirm', requires_user_confirmation: true }
    b.publishAcknowledged.value = true
    globalThis.publishTestApi.confirmPublish = () => assert.fail('缺少摘要不能发布')
    assert.equal(b.canConfirm.value, false)
    await b.confirmAndPublish()
  } finally { scope.stop() }
})

test('待确认任务缺少确认快照摘要时不能提交，摘要变化会清空旧人工确认', async () => {
  const { scope, bindings: b } = setupState()
  try {
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'ready_to_confirm', requires_user_confirmation: true, package_digest: 'package' }
    b.publishAcknowledged.value = true
    assert.equal(b.canConfirm.value, false)
    b.publishTask.value = { ...b.publishTask.value, confirmation_digest: 'snapshot-v1' }
    b.publishAcknowledged.value = true
    assert.equal(b.canConfirm.value, true)
    b.publishTask.value = { ...b.publishTask.value, confirmation_digest: 'snapshot-v2' }
    await nextTick()
    assert.equal(b.publishAcknowledged.value, false)
    assert.equal(b.canConfirm.value, false)
  } finally { scope.stop() }
})

test('投稿文案可编辑，准备提交使用用户修改的标题简介和话题', async () => {
  const { scope, bindings: b } = setupState()
  try {
    b.publishTitle.value = '  RAG 为什么能减少胡说  '
    b.publishDescription.value = '用自己的知识库回答问题。'
    b.publishTags.value = '#AI #RAG，知识库'
    let request
    globalThis.publishTestApi.startPublish = async (_workId, input) => {
      request = input
      return { id: 'pub', work_id: 'work', state: 'uploading' }
    }
    await b.beginPrepare()
    assert.equal(request.title, 'RAG 为什么能减少胡说')
    assert.equal(request.description, '用自己的知识库回答问题。')
    assert.deepEqual(request.tags, ['AI', 'RAG', '知识库'])
  } finally { scope.stop() }
})

test('标题限制为1到30个字符，过长不截断且不会开始上传', async () => {
  const { scope, bindings: b } = setupState()
  try {
    globalThis.publishTestApi.startPublish = () => assert.fail('非法标题不能开始上传')
    for (const invalid of ['', '  ', '长'.repeat(31)]) {
      b.publishTitle.value = invalid
      assert.equal(b.canStart.value, false)
      await b.beginPrepare()
      assert.equal(b.publishTitle.value, invalid)
    }
    b.publishTitle.value = '字'.repeat(30)
    assert.equal(b.canStart.value, true)
  } finally { scope.stop() }
})

test('切换作品后迟到发布准备结果不能污染新作品', async () => {
  const { store, scope, bindings: b } = setupState()
  try {
    let finish
    globalThis.publishTestApi.startPublish = () => new Promise(resolve => { finish = resolve })
    const pending = b.beginPrepare()
    store.currentWorkId = 'other'
    await nextTick()
    finish({ id: 'old', work_id: 'work', state: 'ready_to_confirm' })
    await pending
    assert.notEqual(b.publishTask.value?.id, 'old')
  } finally { scope.stop() }
})

test('没有交付包也能打开专用浏览器登录，账号状态只采纳后端检测', async () => {
  const { store, scope, bindings: b } = setupState()
  try {
    store.pkg = null
    let logins = 0
    globalThis.publishTestApi.openPublishLogin = async () => {
      logins++
      return { state: 'awaiting_login', adapter: 'playwright', message: '请扫码登录' }
    }
    await b.openLogin()
    assert.equal(logins, 1)
    assert.equal(b.loginSession.value.state, 'awaiting_login')
    globalThis.publishTestApi.getPublishSession = async () => ({ state: 'logged_in', adapter: 'playwright', account_label: '测试账号', account_id: '123' })
    await b.refreshSession()
    assert.equal(b.loginSession.value.account_label, '测试账号')
  } finally { scope.stop() }
})

test('等待登录的任务使用恢复接口，不能新建另一个发布任务', async () => {
  const { scope, bindings: b } = setupState()
  try {
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'awaiting_login' }
    let resumed
    globalThis.publishTestApi.startPublish = () => assert.fail('登录恢复不得重复创建任务')
    globalThis.publishTestApi.resumePublish = async (...args) => {
      resumed = args
      return { id: 'pub', work_id: 'work', state: 'uploading' }
    }
    await b.beginPrepare()
    assert.deepEqual(resumed, ['work', 'pub'])
    assert.equal(b.publishTask.value.state, 'uploading')
  } finally { scope.stop() }
})

test('正在上传时不开放取消，准备完成后才可取消任务', async () => {
  const { scope, bindings: b } = setupState()
  try {
    globalThis.publishTestApi.cancelPublish = () => assert.fail('在途上传不能调用取消')
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'uploading' }
    assert.equal(b.canCancel.value, false)
    await b.cancelPrepare()
    assert.match(b.statusDetail.value, /准备完成后.*取消/)
    b.publishTask.value = { id: 'pub', work_id: 'work', state: 'ready_to_confirm' }
    assert.equal(b.canCancel.value, true)
  } finally { scope.stop() }
})

test('交付包就绪时展示抖音准备、二次确认和投稿元数据', async () => {
  const html = await render(store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', name: 'RAG 入门', brief: {} }]
    store.pkg = {
      id: 'pkg', work_id: 'work', task_id: 'task', mode: 'production', state: 'ready',
      title: '三分钟讲懂 RAG', description: '从检索到生成，一次讲清。', tags: ['AI', 'RAG'], ai_label: 'AI生成内容',
      files: [], video_url: '/final.mp4', cover_url: '/cover.jpg', download_url: '/package.zip', manifest_digest: 'sha256:test',
    }
  })
  assert.match(html, /抖音自动发布/)
  assert.match(html, /开始准备（不发布）/)
  assert.match(html, /确认并发布/)
  assert.match(html, /打开抖音创作者中心/)
  assert.match(html, /三分钟讲懂 RAG/)
  assert.match(html, /AI、RAG/)
  assert.match(html, /复制标题/)
  assert.match(html, /复制话题/)
  assert.match(html, /视频预览/)
  assert.match(html, /封面预览/)
})

test('非抖音平台保留开发中状态并提示先使用抖音准备链路', async () => {
  const html = await render(store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', name: 'RAG 入门', brief: {} }]
    store.pkg = { work_id: 'work', state: 'ready', mode: 'production', title: '标题', description: '', tags: [], files: [] }
  })
  assert.match(html, /B 站/)
  assert.match(html, /快手/)
  assert.match(html, /小红书/)
  assert.match(html, /微信视频号/)
  assert.match(html, /开发中/)
})
