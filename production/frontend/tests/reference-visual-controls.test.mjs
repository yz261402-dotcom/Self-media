import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'

const output = await mkdtemp(resolve('node_modules/.reference-controls-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'component.mjs')
await build({
  entryPoints: ['src/components/ReferenceDetail.vue'], outfile, bundle: true, format: 'esm', platform: 'node',
  external: ['vue'], plugins: [{ name: 'vue-test', setup(b) {
    b.onResolve({ filter: /^@\/stores\/content$/ }, () => ({ path: 'store', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useContentStore = () => globalThis.referenceControlsStore' }))
    b.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = (await readFile(path, 'utf8')).replace('</script>', `
        globalThis.referenceControlsActions = { analyze, fetchMedia }
        if (globalThis.referenceControlsConfirmSeed) { confirmed.value = true; fetchConfirmed.value = true }
      </script>`)
      const { descriptor } = parse(source, { filename: path })
      return { contents: compileScript(descriptor, { id: 'test', inlineTemplate: true,
        templateOptions: { ssr: true } }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const { default: ReferenceDetail } = await import(pathToFileURL(outfile).href)

async function render(mediaId = 'original-video', uploading = false, overrides = {}) {
  globalThis.referenceControlsStore = {
    inspecting: false, inspectedReference: null, busy: false, uploading,
    state: { brief: { content_route: 'short_video_storyboard' }, references: [] },
    selectedReference: { id: 'r', caption: '舞蹈', transcript: { state: 'failed', text: '', source: 'redfox' },
      media_asset_id: 'original-video', visual: { state: 'ready', analysis_version: 'visual-v1',
        media_asset_id: 'original-video', model: 'qwen3-vl-flash', duration_ms: 24000,
        summary: '旧的画面观察', scenes: [], warnings: [] } },
    mediaByRef: { r: { id: mediaId, duration_ms: 24000, width: 1080, height: 1920 } },
    visualQuote: { reserve_fen: 80, call_list: { model: 'qwen3-vl-plus' } },
    visualQuoteBinding: { referenceId: 'r', mediaId }, fetchQuote: null,
    loadVisualQuote: async () => {}, loadMediaFetchQuote: async () => {},
    visualJobForReference: () => null, visualErrorForReference: () => '',
    analyzeReferenceVisual: async () => { globalThis.referenceControlsCalls++ },
    fetchReferenceMedia: async () => { globalThis.referenceControlsCalls++ },
    ...overrides,
  }
  return renderToString(createSSRApp(ReferenceDetail))
}

test('已完成旧版解析仍可以更换完整视频，并经费用确认重新解析', async () => {
  const html = await render()
  assert.match(html, /换一个视频/)
  assert.match(html, /重新分析画面/)
  assert.match(html, /qwen3-vl-plus/)
  assert.match(html, /<button[^>]*disabled[^>]*>重新分析画面<\/button>/)
  assert.match(html, /重新获取视频/)
})

test('更换媒体后明确说明展示的是旧视频解析', async () => {
  const html = await render('replacement-video')
  assert.match(html, /当前展示的是旧视频的解析/)
  assert.match(html, /重新分析画面/)
})

test('正在替换视频时即使原费用已确认，也不提交旧视频的解析或获取请求', async () => {
  globalThis.referenceControlsConfirmSeed = true
  globalThis.referenceControlsCalls = 0
  try {
    const html = await render('original-video', true)
    assert.match(html, /<button[^>]*disabled[^>]*>重新分析画面<\/button>/)
    await globalThis.referenceControlsActions.analyze()
    await globalThis.referenceControlsActions.fetchMedia()
    assert.equal(globalThis.referenceControlsCalls, 0)
  } finally { delete globalThis.referenceControlsConfirmSeed }
})

test('画面分析按钮附近明确展示当前参考的供应商超时说明', async () => {
  const message = '画面分析响应超时，原请求可能已计费，重新发送需要重新授权费用。'
  const html = await render('original-video', false, {
    error: '无关的写稿错误',
    visualJobForReference: () => ({ kind: 'visual_analysis', state: 'unknown' }),
    visualErrorForReference: (referenceId, mediaId) => {
      assert.equal(referenceId, 'r'); assert.equal(mediaId, 'original-video')
      return message
    },
  })
  assert.match(html, new RegExp(`<[^>]*role="alert"[^>]*>${message}</`))
  assert.ok(html.indexOf(message) > html.indexOf('确认费用'), '错误应在画面分析操作处')
  assert.doesNotMatch(html, /无关的写稿错误/)
})

test('后台仍运行时，分析按钮旁展示持续查询状态', async () => {
  const html = await render('original-video', false, {
    busy: true,
    visualJobForReference: () => ({ kind: 'visual_analysis', state: 'running' }),
  })
  assert.match(html, /role="status"[^>]*>正在分析画面，系统会继续查询进度。/)
  assert.match(html, /<button[^>]*disabled[^>]*>分析画面中…<\/button>/)
})
