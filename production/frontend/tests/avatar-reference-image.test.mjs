import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, effectScope } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.avatar-reference-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const views = {}
for (const ssr of [false, true]) {
  const outfile = join(output, ssr ? 'ssr.mjs' : 'state.mjs')
  await build({
    stdin: { contents: "export { default } from './src/views/AvatarView.vue'; export { useStudioStore } from './src/stores/studio.ts'", resolveDir: resolve('.') },
    outfile, bundle: true, platform: 'node', format: 'esm', external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
    plugins: [{ name: 'avatar-reference-test', setup(builder) {
      builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = new Proxy({}, { get: (_, key) => globalThis.avatarReferenceApi[key] });' }))
      builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
        let source = await readFile(path, 'utf8')
        if (ssr && path.replaceAll('\\', '/').endsWith('/views/AvatarView.vue')) {
          source = source.replace('</script>', 'if (globalThis.avatarReferenceSeed) { showCreation.value = true; creationMode.value = "reference"; referenceImage.value = globalThis.avatarReferenceSeed }\n</script>')
        }
        const { descriptor } = parse(source, { filename: path })
        const id = createHash('sha256').update(path).digest('hex').slice(0, 12)
        return { contents: compileScript(descriptor, { id, inlineTemplate: ssr, templateOptions: { ssr } }).content, loader: 'ts', resolveDir: dirname(path) }
      })
    } }],
  })
  views[ssr ? 'ssr' : 'state'] = await import(pathToFileURL(outfile).href)
}
await build({ entryPoints: ['src/api/http.ts', 'src/api/mock.ts'], outdir: output, bundle: true, platform: 'node', format: 'esm', outExtension: { '.js': '.mjs' } })
const { httpApi } = await import(pathToFileURL(join(output, 'http.mjs')).href)
const { mockApi } = await import(pathToFileURL(join(output, 'mock.mjs')).href)
const sourceImage = { id: 'reference-one', storage_uri: 'local://reference.png', preview_url: '/reference.png', file_hash: 'sha256:one', width: 400, height: 600 }
const candidate = { identity: { id: 'new-person', name: '参考主播', approval_state: 'pending' }, image_url: '/new.png', warnings: [] }

beforeEach(() => {
  const entries = new Map()
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value), removeItem: key => entries.delete(key) } })
  globalThis.avatarReferenceApi = {}
  delete globalThis.avatarReferenceSeed
})
function setup() {
  setActivePinia(createPinia())
  const store = views.state.useStudioStore()
  store.reloadIdentities = async () => {}
  const scope = effectScope()
  const b = scope.run(() => views.state.default.setup({}, { expose() {} }))
  return { store, scope, b }
}
function ready(b) {
  b.creationMode.value = 'reference'
  b.referenceImage.value = sourceImage
  b.referencePaidAck.value = true
}
test('HTTP uploads raw image, sends explicit cost acknowledgement, and queries original request', async () => {
  const calls = []
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return new Response(JSON.stringify(url.includes('reference-images') ? sourceImage : url.includes('image-generations') ? { request_id: 'original/id', state: 'unknown', result: null, error: null } : candidate)) }
  const file = new File(['image'], '人像.webp', { type: 'image/webp' })
  assert.equal(typeof httpApi.uploadAvatarReferenceImage, 'function')
  assert.deepEqual(await httpApi.uploadAvatarReferenceImage(file), sourceImage)
  assert.equal(calls[0].url, '/api/v1/avatar-identities/reference-images')
  assert.equal(calls[0].init.body, file)
  assert.equal(calls[0].init.headers['Content-Type'], 'image/webp')
  const input = { reference_image_id: sourceImage.id, appearance: '', free_text: '', paid_ack: true, request_id: 'original/id' }
  assert.deepEqual(await httpApi.createIdentityCandidateFromImage(input), candidate)
  assert.deepEqual(JSON.parse(calls[1].init.body), input)
  await httpApi.getAvatarImageGeneration('original/id')
  assert.equal(calls[2].url, '/api/v1/avatar-identities/image-generations/original%2Fid')
})
test('offline reference-image endpoints report unavailable rather than fake generated media', async () => {
  for (const [method, value] of [['uploadAvatarReferenceImage', new File(['a'], 'a.png', { type: 'image/png' })], ['createIdentityCandidateFromImage', {}], ['getAvatarImageGeneration', 'request']]) {
    assert.equal(typeof mockApi[method], 'function')
    await assert.rejects(mockApi[method](value), error => error.code === 'AVATAR_REFERENCE_UNAVAILABLE')
  }
})
test('reference mode renders photo preview and paid gate without default gender or age fields', async () => {
  globalThis.avatarReferenceSeed = sourceImage
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/avatar', component: { template: '<div />' } }] })
  await router.push('/avatar')
  const html = await renderToString(createSSRApp(views.ssr.default).use(pinia).use(router))
  assert.match(html, /文字生成/)
  assert.match(html, /参考图片/)
  assert.match(html, /<img[^>]+src="\/reference.png"/)
  assert.match(html, /image\/png,image\/jpeg,image\/webp/)
  assert.match(html, /替换/)
  assert.match(html, /移除/)
  assert.match(html, /我确认本次图片生成费用/)
  assert.match(html.match(/<button[^>]*>根据图片生成主播<\/button>/)?.[0] ?? '', /disabled/)
  assert.doesNotMatch(html, /<span>性别<\/span>|<span>年龄<\/span>/)
})
test('reference upload rejects unsupported and oversized files before making a request', async () => {
  const { b, scope } = setup()
  try {
    assert.equal(typeof b.chooseReferenceImage, 'function')
    let uploads = 0
    globalThis.avatarReferenceApi.uploadAvatarReferenceImage = async () => { uploads++; return sourceImage }
    for (const file of [{ type: 'image/gif', size: 1 }, { type: 'image/png', size: 10 * 1024 * 1024 + 1 }, { type: 'image/png', size: 0 }]) await b.chooseReferenceImage({ target: { files: [file], value: 'selected' } })
    assert.equal(uploads, 0)
    assert.equal(b.referenceImage.value, null)
    const file = new File(['png'], 'person.png', { type: 'image/png' })
    await b.chooseReferenceImage({ target: { files: [file], value: 'selected' } })
    assert.equal(uploads, 1)
    assert.deepEqual(b.referenceImage.value, sourceImage)
    b.removeReferenceImage()
    assert.equal(b.referenceImage.value, null)
  } finally { scope.stop() }
})
test('reference generation requires fee confirmation and never sends text-mode gender defaults', async () => {
  const { b, scope } = setup()
  try {
    ready(b)
    b.referencePaidAck.value = false
    let calls = 0
    globalThis.avatarReferenceApi.createIdentityCandidateFromImage = async input => { calls++; assert.equal(input.reference_image_id, sourceImage.id); assert.equal(input.paid_ack, true); assert.equal('gender' in input, false); assert.equal('age' in input, false); return candidate }
    await b.genCandidates()
    assert.equal(calls, 0)
    b.referencePaidAck.value = true
    await b.genCandidates()
    assert.equal(calls, 1)
    assert.equal(b.candidates.value[0].identity.id, candidate.identity.id)
    assert.equal(b.referenceState.value, 'succeeded')
  } finally { scope.stop() }
})
test('lost response preserves original request and prevents duplicate paid submissions', async () => {
  const { b, scope } = setup()
  try {
    ready(b)
    let calls = 0
    let input
    globalThis.avatarReferenceApi.createIdentityCandidateFromImage = async value => { calls++; input = value; throw new Error('connection lost') }
    await b.genCandidates()
    assert.equal(b.referenceRequestId.value, input.request_id)
    assert.equal(b.referenceState.value, 'unknown')
    await b.genCandidates()
    assert.equal(calls, 1)
    globalThis.avatarReferenceApi.getAvatarImageGeneration = async id => { assert.equal(id, input.request_id); return { request_id: id, state: 'succeeded', result: candidate, error: null } }
    await b.queryReferenceGeneration()
    await b.queryReferenceGeneration()
    assert.equal(b.candidates.value.length, 1)
    assert.equal(calls, 1)
  } finally { scope.stop() }
})
test('confirmed failure allows a new user submission with a new request id', async () => {
  const { b, scope } = setup()
  try {
    ready(b)
    const ids = []
    globalThis.avatarReferenceApi.createIdentityCandidateFromImage = async input => { ids.push(input.request_id); throw new Error('lost') }
    await b.genCandidates()
    globalThis.avatarReferenceApi.getAvatarImageGeneration = async id => ({ request_id: id, state: 'failed', result: null, error: { code: 'FAILED', message: '生成失败' } })
    await b.queryReferenceGeneration()
    b.referencePaidAck.value = true
    await b.genCandidates()
    assert.equal(ids.length, 2)
    assert.notEqual(ids[0], ids[1])
  } finally { scope.stop() }
})
test('refresh restores unresolved generation and only permits querying the original request', async () => {
  sessionStorage.setItem('avatar-studio:avatar-reference-generation', JSON.stringify({ request_id: 'saved-request', state: 'unknown', source: sourceImage, appearance: '红色外套', free_text: '正面半身' }))
  const { b, scope } = setup()
  try {
    assert.equal(b.creationMode.value, 'reference')
    assert.equal(b.referenceRequestId.value, 'saved-request')
    assert.equal(b.referenceImage.value.id, sourceImage.id)
    assert.equal(b.newIdentity.value.appearance, '红色外套')
    b.referencePaidAck.value = true
    let submissions = 0
    globalThis.avatarReferenceApi.createIdentityCandidateFromImage = async () => { submissions++ }
    await b.genCandidates()
    assert.equal(submissions, 0)
    globalThis.avatarReferenceApi.getAvatarImageGeneration = async id => ({ request_id: id, state: 'running', result: null, error: null })
    await b.queryReferenceGeneration()
    assert.equal(b.referenceState.value, 'running')
  } finally { scope.stop() }
})
