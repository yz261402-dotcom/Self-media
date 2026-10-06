import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer, reactive, nextTick } from 'vue'

const output = await mkdtemp(resolve('node_modules/.reference-retry-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'component.mjs')
await build({ entryPoints: ['src/components/ReferenceDetail.vue'], outfile, bundle: true, format: 'esm', platform: 'node',
  external: ['vue'], plugins: [{ name: 'vue-test', setup(b) {
    b.onResolve({ filter: /^@\/stores\/content$/ }, () => ({ path: 'store', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useContentStore = () => globalThis.visualRetryStore' }))
    b.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = (await readFile(path, 'utf8')).replace('</script>', `
        globalThis.visualRetryActions = {
          analyze, setConfirmed: (value) => { confirmed.value = value },
          setRetryConfirmed: (value) => { if (typeof retryConfirmed !== 'undefined') retryConfirmed.value = value },
        }
      </script>`)
      const { descriptor } = parse(source, { filename: path })
      return { contents: compileScript(descriptor, { id: 'retry-test', inlineTemplate: true }).content,
        loader: 'ts', resolveDir: dirname(path) }
    })
  } }] })
const { default: ReferenceDetail } = await import(pathToFileURL(outfile).href)

globalThis.document = { activeElement: null }
const node = (type, text = '') => ({ type, tagName: type.toUpperCase(), text, children: [], props: {}, parent: null,
  addEventListener() {}, removeEventListener() {}, getAttribute(key) { return this.props[key] } })
const renderer = createRenderer({
  createElement: type => node(type), createText: text => node('text', text), createComment: text => node('comment', text),
  setText: (n, text) => { n.text = text }, setComment: (n, text) => { n.text = text },
  setElementText: (n, text) => { n.text = text; n.children = [] },
  patchProp: (n, key, _prev, value) => { n.props[key] = value },
  parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] ?? null,
  insert(n, p, anchor = null) { if (n.parent) this.remove(n); n.parent = p; const i = p.children.indexOf(anchor); p.children.splice(i < 0 ? p.children.length : i, 0, n) },
  remove(n) { if (n.parent) n.parent.children.splice(n.parent.children.indexOf(n), 1); n.parent = null },
})
const quote = (callId = 'old') => ({ reserve_fen: 80, request_digest: 'digest', call_list: { model: 'qwen3-vl-plus' },
  retry_unknown_call: { call_id: callId, reserved_fen: 80, sent_at: '2026-09-30T15:03:02+00:00' } })
const reference = id => ({ id, caption: '参考', transcript: { state: 'failed', text: '', source: 'redfox' }, media_asset_id: 'media' })
function mount(overrides = {}) {
  const sent = [], reads = []
  const store = reactive({ inspecting: false, inspectedReference: null, selectedReference: reference('ref'), busy: false,
    uploading: false, state: { brief: { content_route: 'short_video_storyboard' }, references: [] }, mediaByRef: {},
    visualQuote: null, visualQuoteBinding: null, fetchQuote: null,
    loadVisualQuote: async (referenceId, mediaId) => {
      reads.push([referenceId, mediaId]); store.visualQuoteBinding = { referenceId, mediaId }; store.visualQuote = quote()
    },
    loadMediaFetchQuote: async () => {}, visualJobForReference: () => null, visualErrorForReference: () => '',
    analyzeReferenceVisual: async (...args) => { sent.push(args) }, ...overrides })
  globalThis.visualRetryStore = store
  const root = node('root'), app = renderer.createApp(ReferenceDetail)
  app.mount(root)
  return { app, root, store, sent, reads, actions: globalThis.visualRetryActions }
}
const all = n => [n, ...n.children.flatMap(all)]
const button = root => all(root).find(n => n.type === 'button' && /分析画面|重新分析/.test(n.text))
const text = root => all(root).map(n => n.text).join(' ')

test('未知费用显示单独追加确认；只勾普通确认不能重新分析', async () => {
  const x = mount()
  try {
    await nextTick()
    assert.match(text(x.root), /上次结果和费用未知，重新分析会产生一笔新费用/)
    assert.match(text(x.root), /我接受上次可能已计费，并确认重新分析产生的新费用/)
    assert.equal(button(x.root).text, '确认费用后重新分析')
    x.actions.setConfirmed(true); await nextTick(); await x.actions.analyze()
    assert.equal(x.sent.length, 0)
    assert.equal(button(x.root).props.disabled, true)
  } finally { x.app.unmount() }
})

test('双确认后显式点击只提交一次并传原call_id与追加费用ack', async () => {
  const x = mount()
  try {
    await nextTick(); x.actions.setConfirmed(true); x.actions.setRetryConfirmed(true); await nextTick()
    assert.equal(button(x.root).props.disabled, false)
    await x.actions.analyze()
    assert.deepEqual(x.sent, [['ref', 'media', 80, { retry_unknown_call_id: 'old', retry_paid_ack: true }]])
    await nextTick(); assert.equal(button(x.root).props.disabled, true, '提交完成后清空确认')
  } finally { x.app.unmount() }
})

test('同参考报价的原call_id变更立即撤销双确认，busy时也不会发请求', async () => {
  const x = mount()
  try {
    await nextTick(); x.actions.setConfirmed(true); x.actions.setRetryConfirmed(true)
    x.store.busy = true; await x.actions.analyze(); assert.equal(x.sent.length, 0)
    x.store.busy = false; x.store.visualQuote = quote('new-call')
    await x.actions.analyze(); await nextTick()
    assert.equal(x.sent.length, 0)
    assert.equal(button(x.root).props.disabled, true)
  } finally { x.app.unmount() }
})

test('切换到共用媒体的另一个参考也重新读取报价，不继承费用确认', async () => {
  const x = mount()
  try {
    await nextTick(); x.actions.setConfirmed(true); x.actions.setRetryConfirmed(true)
    x.store.selectedReference = reference('other')
    await x.actions.analyze(); await nextTick()
    assert.equal(x.sent.length, 0)
    assert.deepEqual(x.reads, [['ref', 'media'], ['other', 'media']])
    assert.equal(button(x.root).props.disabled, true)
  } finally { x.app.unmount() }
})

test('报价读取期间不使用之前参考残留的未知调用信息，也不能提交', async () => {
  const x = mount({ visualQuote: quote('other-call'), visualQuoteBinding: { referenceId: 'other', mediaId: 'media' },
    loadVisualQuote: async () => {} })
  try {
    await nextTick(); x.actions.setConfirmed(true); x.actions.setRetryConfirmed(true)
    await x.actions.analyze(); await nextTick()
    assert.equal(x.sent.length, 0)
    assert.doesNotMatch(text(x.root), /上次结果和费用未知/)
    assert.equal(button(x.root).props.disabled, true)
  } finally { x.app.unmount() }
})

test('后台刷新返回相同参考和相同未知任务时不重复刷新报价或撤销新勾选', async () => {
  const oldJob = { job_id: 'old-job', state: 'unknown' }
  const x = mount({ state: { brief: {}, references: [], recent_jobs: [oldJob] },
    visualJobForReference: () => globalThis.visualRetryStore.state.recent_jobs.at(-1) })
  try {
    await nextTick(); x.actions.setConfirmed(true); x.actions.setRetryConfirmed(true)
    x.store.selectedReference = reference('ref')
    x.store.state = { brief: {}, references: [], recent_jobs: [{ ...oldJob }] }
    await nextTick()
    assert.deepEqual(x.reads, [['ref', 'media']], '同一输入与终态刷新不能再次读取报价')
    assert.equal(button(x.root).props.disabled, false)
  } finally { x.app.unmount() }
})
