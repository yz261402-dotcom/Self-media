import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createRenderer, createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'

const dir = await mkdtemp(resolve('node_modules/.wan-storyboard-ui-'))
after(() => rm(dir, { recursive: true, force: true }))
const output = join(dir, 'view.mjs')
await build({
  stdin: { contents: `export { default } from './src/components/WanStoryboardStudio.vue'; export { useStudioStore } from './src/stores/studio.ts'; export { setTestApiMode } from '@/api'`, resolveDir: resolve('.') },
  outfile: output, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'vue/server-renderer', 'pinia'],
  plugins: [{ name: 'vue', setup(b) {
    b.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `
      export const api = new Proxy({ listWanStoryboards: async () => ({ graphs: globalThis.wanGraphSeed ? [globalThis.wanGraphSeed] : [] }) },
        { get: (target, key) => globalThis.wanStoryboardTestApi?.[key] ?? target[key] });
      export let apiMode = "http";
      export function setTestApiMode(mode) { apiMode = mode }
    ` }))
    b.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const source = (await readFile(path, 'utf8')).replace('</script>', `
        graph.value = globalThis.wanGraphSeed;
        if (globalThis.wanStoryboardActions) Object.assign(globalThis.wanStoryboardActions, { graph, create, generateImages, submitVideos, approveImages, approveVideos });
        </script>`)
      const { descriptor } = parse(source, { filename: path })
      return { contents: compileScript(descriptor, { id: 'test', inlineTemplate: true }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const module = await import(pathToFileURL(output).href)
const base = { id: 'g', work_id: 'W', script_version_id: 's', schema_version: 3, identity_reference: null, state: 'images_ready', resolution: '720p', plan_digest: 'p', task_graph_digest: 't',
  image_ceiling_fen_per_shot: 200,
  estimate: { image_calls: 1, video_calls: 1, image_ceiling_total_fen: 200, video_total_fen: 480 },
  shots: [{ shot_index: 1, visual: '市场街景', dialogue: '镜头对白', sound_effects: '环境声音', duration_seconds: 8,
    image_state: 'succeeded', video_state: 'pending', image: { preview_url: '/image.png' } }], image_approval: { plan_digest: 'p' } }
const identityReference = { kind: 'hero', asset_id: 'hero', asset_hash: 'hero-hash', identity_id: 'person',
  variant_id: null, storage_uri: 'assets/hero.png', label: '小禾定妆图', identity_version: 1,
  identity_digest: 'person-digest', preview_url: '/hero.png' }
const button = (html, label) => html.match(new RegExp(`<button[^>]*>\\s*${label}\\s*</button>`))?.[0] ?? ''
async function render(graph, work = {}, mode = 'http') {
  module.setTestApiMode(mode)
  const pinia = createPinia(); setActivePinia(pinia)
  const studio = module.useStudioStore()
  studio.currentWorkId = 'W'; studio.works = [{ id: 'W', script_version_id: 's', identity_id: null, variant_id: null,
    brief: { content_route: 'short_video_storyboard' }, ...work }]
  globalThis.wanGraphSeed = graph
  try {
    const app = createSSRApp(module.default).use(pinia)
    app.component('router-link', { props: ['to'], render() { return h('a', { href: this.to }, this.$slots.default?.()) } })
    return await renderToString(app)
  }
  finally { delete globalThis.wanGraphSeed; module.setTestApiMode('http') }
}
test('Wan 任务图展示镜头图和总费用，未勾选时不能批准视频调用', async () => {
  const html = await render(base)
  assert.match(html, /市场街景|镜头对白/); assert.match(html, /¥4\.80/)
  assert.match(html, /<img[^>]+src="\/image.png"/)
  assert.match(html, /<button[^>]+disabled[^>]*>确认任务图与视频费用/)
})
test('Wan 未知镜头有查询原作业入口，禁止再次提交', async () => {
  const html = await render({ ...base, state: 'unknown', video_approval: { task_graph_digest: 't' },
    shots: [{ ...base.shots[0], video_state: 'unknown', remote_task_id: 'original-task' }] })
  assert.match(html, /查询原作业/); assert.match(html, /original-task/)
  assert.match(html, /<button[^>]+disabled[^>]*>开始逐镜头生成/)
})

test('图片下载中断后提供回收入口并保留原作业号，不能再次生成', async () => {
  const html = await render({ ...base, state: 'unknown', shots: [{ ...base.shots[0], image: null,
    image_state: 'unknown', image_job_id: 'original-image-job', image_recovery_available: true,
    error: { code: 'IMAGE_PROVIDER_UNREACHABLE', message: '下载生成结果失败：ReadTimeout' } }] })
  assert.match(html, /回收已有图片/)
  assert.match(html, /original-image-job/)
  assert.match(html, /ReadTimeout/)
  assert.match(html, /<button[^>]+disabled[^>]*>生成镜头图片/)
})

test('没有图片回执时显示供应商核对指引', async () => {
  const html = await render({ ...base, state: 'unknown', shots: [{ ...base.shots[0], image: null,
    image_state: 'unknown', image_job_id: 'original-image-job', image_recovery_available: false }] })
  assert.doesNotMatch(html, /回收已有图片/)
  assert.match(html, /供应商后台/)
  assert.match(html, /核对原请求或确认追加费用后重新生成/)
  assert.doesNotMatch(html, /核对前不能重新生成/)
})

test('只有服务端允许的镜头显示重新生成入口，已有回收入口保留', async () => {
  const html = await render({ ...base, shots: [
    { ...base.shots[0], image_state: 'unknown', image_job_id: 'old-image', image: null,
      image_recovery_available: true, image_regeneration_available: true },
    { ...base.shots[0], shot_index: 2, image_regeneration_available: false },
  ] })
  assert.equal((html.match(/>重新生成此镜头</g) ?? []).length, 1)
  assert.match(html, /回收已有图片/)
  assert.doesNotMatch(html, /原请求可能已计费/)
})

test('旧人物任务或视频已开始时禁用单镜头重新生成', async () => {
  const eligible = { ...base, shots: [{ ...base.shots[0], image_job_id: 'old-image', image_regeneration_available: true }] }
  const legacy = await render({ ...eligible, schema_version: 2 }, { identity_id: 'person' })
  assert.match(button(legacy, '重新生成此镜头'), /disabled/)
  for (const extra of [{ video_state: 'waiting_provider' }, { remote_task_id: 'video-task' },
    { video_job_id: 'video-job' }, { clip: { preview_url: '/clip.mp4' } }]) {
    const html = await render({ ...eligible, shots: [{ ...eligible.shots[0], ...extra }] })
    assert.match(button(html, '重新生成此镜头'), /disabled/)
  }
  for (const extra of [{ state: 'stitch_queued' }, { state: 'stitching' }, { state: 'stitch_failed' },
    { video_id: 'video' }, { output: { preview_url: '/final.mp4', file_hash: 'hash' } }]) {
    const html = await render({ ...eligible, ...extra })
    assert.match(button(html, '重新生成此镜头'), /disabled/)
  }
})
test('拼接成片需要观看确认后才生成发布包', async () => {
  const html = await render({ ...base, state: 'succeeded', video_id: 'v', output: { preview_url: '/final.mp4', file_hash: 'hash' } })
  assert.match(html, /<video[^>]+src="\/final.mp4"/)
  assert.match(html, /<button[^>]+disabled[^>]*>确认成片并生成发布包/)
})

test('本地拼接失败后提供重试入口，不触发镜头重新生成', async () => {
  const html = await render({ ...base, state: 'stitch_failed',
    video_approval: { task_graph_digest: 't' }, clip_approval: { clip_digest: 'c' },
    shots: [{ ...base.shots[0], video_state: 'succeeded', clip: { preview_url: '/clip.mp4' } }] })
  assert.match(html, /重试本地拼接/)
  assert.doesNotMatch(html, /开始逐镜头生成/)
})

test('本地拼接长时间排队时提示检查 Worker', async () => {
  const html = await render({ ...base, state: 'stitch_queued',
    updated_at: new Date(Date.now() - 120_000).toISOString(),
    video_approval: { task_graph_digest: 't' }, clip_approval: { clip_digest: 'c' },
    shots: [{ ...base.shots[0], video_state: 'succeeded', clip: { preview_url: '/clip.mp4' } }] })
  assert.match(html, /排队超过 60 秒/)
  assert.match(html, /storyboard-worker\.err\.log/)
})

test('失效选稿的拼接任务显示原因并隐藏无效重试', async () => {
  const html = await render({ ...base, state: 'stitch_failed',
    error: { code: 'STORYBOARD_INPUT_CHANGED', message: '选稿或内容审核已变化，请重新采用当前审核通过的分镜' },
    video_approval: { task_graph_digest: 't' }, clip_approval: { clip_digest: 'c' },
    shots: [{ ...base.shots[0], video_state: 'succeeded', clip: { preview_url: '/clip.mp4' } }] })
  assert.match(html, /选稿或内容审核已变化，请重新采用当前审核通过的分镜/)
  assert.doesNotMatch(html, /重试本地拼接/)
})

test('分镜制作显示任务实际绑定的人物图和标签', async () => {
  const html = await render({ ...base, identity_reference: identityReference }, { identity_id: 'person' })
  assert.match(html, /人物参考图/)
  assert.match(html, /<img[^>]+src="\/hero.png"/)
  assert.match(html, /小禾定妆图/)
  assert.doesNotMatch(html, /重新准备任务图|未使用人物图/)
})

test('未绑定人物的分镜说明按画面描述生成', async () => {
  const html = await render(base)
  assert.match(html, /未选择人物.*按画面描述生成/)
  assert.doesNotMatch(html, /重新准备任务图|未使用人物图/)
})

test('旧分镜已有图片授权时仍提供人物重准备入口并保留结果', async () => {
  const html = await render({ ...base, schema_version: 2, identity_reference: undefined }, { identity_id: 'person' })
  assert.match(html, /旧任务未使用人物图/)
  assert.doesNotMatch(html, /每张图片预算预留/)
  assert.ok(button(html, '按当前人物重新准备任务图'))
  assert.doesNotMatch(button(html, '按当前人物重新准备任务图'), /disabled/)
  assert.match(html, /<img[^>]+src="\/image.png"/)
  assert.match(button(html, '确认任务图与视频费用'), /disabled/)
})

test('旧分镜不能继续收费生图或提交 Wan', async () => {
  const legacy = { ...base, schema_version: 2, identity_reference: undefined }
  const imageHtml = await render({ ...legacy, shots: [{ ...base.shots[0], image_state: 'pending' }] }, { identity_id: 'person' })
  assert.match(button(imageHtml, '生成镜头图片'), /disabled/)
  const videoHtml = await render({ ...legacy, video_approval: { task_graph_digest: 't' } }, { identity_id: 'person' })
  assert.match(button(videoHtml, '开始逐镜头生成'), /disabled/)
})

test('新任务参考与作品人物或变体不一致时要求重新准备', async () => {
  for (const work of [{ identity_id: 'other' }, { identity_id: 'person', variant_id: 'variant-new' }, { identity_id: null }]) {
    const html = await render({ ...base, identity_reference: identityReference, video_approval: { task_graph_digest: 't' } }, work)
    assert.match(html, /人物参考已变化.*重新准备任务图/)
    assert.ok(button(html, '按当前人物重新准备任务图'))
    assert.match(button(html, '开始逐镜头生成'), /disabled/)
  }
})

test('有已选人物但新任务没有人物图时也要求重新准备', async () => {
  const html = await render(base, { identity_id: 'person' })
  assert.match(html, /人物参考已变化.*重新准备任务图/)
  assert.ok(button(html, '按当前人物重新准备任务图'))
})

// Use Vue's renderer without a browser or network so the real watchers and polling run.
const node = (type, text = '') => ({ type, text, children: [], props: {}, parent: null, addEventListener() {},
  get options() { return this.children.filter(child => child.type === 'option') } })
const renderer = createRenderer({
  createElement: type => node(type), createText: text => node('text', text), createComment: text => node('comment', text),
  setText: (target, text) => { target.text = text }, setElementText: (target, text) => { target.text = text; target.children = [] },
  parentNode: target => target.parent, nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
  patchProp: (target, key, previous, value) => { target.props[key] = value },
  insert: (target, parent, anchor = null) => {
    if (target.parent) target.parent.children.splice(target.parent.children.indexOf(target), 1)
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, target)
  },
  remove: target => {
    if (target.parent) target.parent.children.splice(target.parent.children.indexOf(target), 1)
    target.parent = null
  },
})
async function mount(graph, work = {}, api = {}, live = false, mode = 'http') {
  module.setTestApiMode(mode)
  const pinia = createPinia(); setActivePinia(pinia)
  const studio = module.useStudioStore()
  studio.currentWorkId = 'W'; studio.works = [{ id: 'W', script_version_id: 's', identity_id: 'person', variant_id: null,
    brief: { content_route: 'short_video_storyboard' }, ...work }]
  globalThis.wanGraphSeed = graph; globalThis.wanStoryboardTestApi = api; globalThis.wanStoryboardActions = {}
  const app = renderer.createApp(live ? module.default : { ...module.default, setup(props, context) {
    module.default.setup(props, context)
    return () => null
  } }).use(pinia)
  app.component('router-link', { props: ['to'], render() { return h('a', { href: this.to }, this.$slots.default?.()) } })
  const root = node('root')
  app.mount(root)
  await nextTick()
  return { studio, root, actions: globalThis.wanStoryboardActions, cleanup() {
    app.unmount(); delete globalThis.wanGraphSeed; delete globalThis.wanStoryboardTestApi; delete globalThis.wanStoryboardActions
    module.setTestApiMode('http')
  } }
}

const descendants = root => root.children.flatMap(child => [child, ...descendants(child)])
const content = target => target.text + target.children.map(content).join('')
const liveButton = (context, label) => descendants(context.root).find(target => target.type === 'button' && content(target).trim() === label)
const duplicateAck = context => descendants(context.root).find(target => target.type === 'input'
  && target.props.type === 'checkbox' && /再次计费/.test(content(target.parent)))
const eligibleGraph = () => ({ ...base, identity_reference: identityReference, shots: [
  { ...base.shots[0], image_job_id: 'original-image', image_regeneration_available: true, image_attempt: 1 },
  { ...base.shots[0], shot_index: 2, image_job_id: 'other-image', image_regeneration_available: false },
] })
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); await nextTick() }
const click = async (context, label) => {
  const target = liveButton(context, label)
  assert.ok(target, `找不到按钮：${label}`)
  target.props.onClick()
  await flush()
}

const eligibleVideoGraph = () => ({ ...base, state: 'failed', identity_reference: identityReference,
  estimate: { ...base.estimate, video_total_fen: 960, shot_prices: [
    { shot_index: 1, ceiling_fen: 480, price_snapshot_id: 'price-1' },
    { shot_index: 2, ceiling_fen: 360, price_snapshot_id: 'price-2' },
  ] }, video_approval: { task_graph_digest: 't' }, shots: [
    { ...base.shots[0], video_state: 'failed', video_job_id: 'original-video',
      video_regeneration_available: true, video_attempt: 1, error: { code: 'WAN_BALANCE_INSUFFICIENT', message: '余额不足' } },
    { ...base.shots[0], shot_index: 2, video_state: 'succeeded', video_job_id: 'other-video', video_regeneration_available: false },
  ] })

const subtitleGraph = (extra = {}) => ({ ...base, identity_reference: identityReference, state: 'succeeded',
  video_id: 'original-final', subtitle_generation_available: true,
  output: { storage_uri: 'videos/original.mp4', preview_url: '/final.mp4', file_hash: 'original-hash' },
  ...extra })
const subtitleAck = context => descendants(context.root).find(target => target.type === 'input'
  && target.props.type === 'checkbox' && /确认使用云端 GPU/.test(content(target.parent)))
const finalAck = context => descendants(context.root).find(target => target.type === 'input'
  && target.props.type === 'checkbox' && /已观看成片/.test(content(target.parent)))

test('已生成发布包的无字幕成片仍提供 GPU 字幕入口，必须显式确认', async () => {
  const html = await render(subtitleGraph({ identity_reference: null, package_id: 'existing-package' }))
  assert.match(button(html, '生成字幕与成片'), /disabled/)
  assert.match(html, /根据视频实际对白生成字幕，使用已连接的云端 GPU；原视频保留/)
  assert.match(html, /确认使用云端 GPU 生成字幕/)
  assert.match(html, /下载成片/)
})

test('字幕入口在失效选稿、不匹配人物和演示模式禁用', async () => {
  for (const [g, work, mode] of [
    [subtitleGraph({ error: { code: 'STORYBOARD_INPUT_CHANGED', message: '选稿已变化' } }), {}, 'http'],
    [subtitleGraph({ error: { code: 'STORYBOARD_SELECTION_REQUIRED', message: '采用稿件失效' } }), {}, 'http'],
    [subtitleGraph(), { identity_id: 'other' }, 'http'],
    [subtitleGraph({ identity_reference: null }), {}, 'mock'],
  ]) {
    const html = await render(g, work, mode)
    assert.match(button(html, '生成字幕与成片'), /disabled/)
  }
})

test('字幕生成透传绑定确认，只调用字幕接口并阻止双击', async () => {
  const calls = []; let finish
  const context = await mount(subtitleGraph(), {}, {
    generateWanStoryboardSubtitles: async (...args) => { calls.push(args); return new Promise(resolve => { finish = resolve }) },
    submitWanStoryboardVideos: async () => { throw new Error('不得调用收费镜头') },
  }, true)
  try {
    await click(context, '生成字幕与成片')
    assert.deepEqual(calls, [])
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '生成字幕与成片')
    assert.equal(liveButton(context, '生成字幕与成片').props.disabled, true)
    await click(context, '生成字幕与成片')
    assert.equal(calls.length, 1)
    assert.deepEqual(calls[0].slice(0, 2), ['W', 'g'])
    assert.deepEqual({ ...calls[0][2], request_id: '<random>' }, { actor_id: 'user_local', compute_ack: true,
      expected_video_id: 'original-final', output_digest: 'original-hash', request_id: '<random>' })
    assert.match(calls[0][2].request_id, /^[a-f0-9-]{36}$/)
    finish(subtitleGraph({ subtitle_generation_available: false, subtitles: { state: 'queued', job_id: 'subtitles' } }))
    await flush()
    assert.match(content(context.root), /字幕生成排队中/)
    assert.equal(liveButton(context, '生成字幕与成片').props.disabled, true)
  } finally { context.cleanup() }
})

test('字幕提交暂未返回时保留错误和同一请求号供重放', async () => {
  const calls = []
  const context = await mount(subtitleGraph(), {}, { generateWanStoryboardSubtitles: async (...args) => {
    calls.push(args); throw new Error('字幕请求暂未返回，请刷新核对')
  } }, true)
  try {
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '生成字幕与成片')
    assert.match(content(context.root), /字幕请求暂未返回，请刷新核对/)
    await click(context, '生成字幕与成片')
    assert.equal(calls.length, 2)
    assert.equal(calls[0][2].request_id, calls[1][2].request_id)
  } finally { context.cleanup() }
})

test('已有字幕任务明确失败后需重新勾选，并用新请求号重试', async () => {
  const calls = []
  const context = await mount(subtitleGraph(), {}, { generateWanStoryboardSubtitles: async (...args) => {
    calls.push(args)
    return subtitleGraph({ subtitle_generation_available: false, subtitles: { state: 'queued', job_id: `subtitle-${calls.length}` } })
  } }, true)
  try {
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '生成字幕与成片')
    context.actions.graph.value = subtitleGraph({ subtitles: { state: 'failed', job_id: 'subtitle-1',
      error: { code: 'GPU_UNAVAILABLE', message: '云端服务未连接' } } })
    await nextTick()
    assert.match(content(context.root), /云端服务未连接/)
    await click(context, '重试生成字幕')
    assert.equal(calls.length, 1)
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '重试生成字幕')
    assert.equal(calls.length, 2)
    assert.notEqual(calls[0][2].request_id, calls[1][2].request_id)
    assert.equal(calls[1][2].expected_video_id, 'original-final')
  } finally { context.cleanup() }
})

test('失效字幕入口即使绕过禁用复选框也不会提交生成', async () => {
  for (const [g, work, mode] of [
    [subtitleGraph({ error: { code: 'STORYBOARD_INPUT_CHANGED', message: '选稿已失效' } }), {}, 'http'],
    [subtitleGraph(), { identity_id: 'other' }, 'http'],
    [subtitleGraph(), {}, 'mock'],
  ]) {
    const calls = []
    const context = await mount(g, work, { generateWanStoryboardSubtitles: async (...args) => { calls.push(args); return g } }, true, mode)
    try {
      subtitleAck(context).props['onUpdate:modelValue'](true)
      await nextTick()
      await click(context, '生成字幕与成片')
      assert.deepEqual(calls, [])
    } finally { context.cleanup() }
  }
})

test('字幕处理中即使成片状态已成功也轮询，且只 GET 原任务状态', async () => {
  const originalTimeout = globalThis.setTimeout; const originalClear = globalThis.clearTimeout
  try {
    for (const state of ['queued', 'running']) {
      const timers = new Map(); let timerId = 0; const calls = []
      globalThis.setTimeout = callback => { const id = ++timerId; timers.set(id, callback); return id }
      globalThis.clearTimeout = id => { timers.delete(id) }
      const g = subtitleGraph({ subtitle_generation_available: false, subtitles: { state, job_id: 'subtitles' } })
      const context = await mount(g, {}, {
        getWanStoryboard: async (...args) => { calls.push(['read', ...args]); return g },
        submitWanStoryboardVideos: async () => { calls.push(['paid']); return g },
        generateWanStoryboardSubtitles: async () => { calls.push(['subtitle']); return g },
      }, true)
      try {
        assert.equal(timers.size, 1)
        assert.match(content(context.root), state === 'queued' ? /字幕生成排队中/ : /正在识别对白并生成字幕/)
        await timers.values().next().value()
        assert.deepEqual(calls, [['read', 'W', 'g']])
        finalAck(context).props['onUpdate:modelValue'](true)
        await nextTick()
        assert.equal(liveButton(context, '确认成片并生成发布包').props.disabled, true)
      } finally { context.cleanup() }
    }
  } finally { globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear }
})

test('字幕失败显示原因与重试；未知任务只能刷新已有状态', async () => {
  const failed = await render(subtitleGraph({ identity_reference: null, subtitles: { state: 'failed', job_id: 'failed',
    error: { code: 'GPU_UNAVAILABLE', message: '云端 GPU 服务未连接，请重新连接' } } }))
  assert.match(failed, /云端 GPU 服务未连接，请重新连接/)
  assert.ok(button(failed, '重试生成字幕'))
  const calls = []; const g = subtitleGraph({ subtitles: { state: 'unknown', job_id: 'unknown' } })
  const context = await mount(g, {}, {
    getWanStoryboard: async (...args) => { calls.push(['read', ...args]); return g },
    generateWanStoryboardSubtitles: async () => { calls.push(['submit']); return g },
  }, true)
  try {
    assert.equal(liveButton(context, '生成字幕与成片'), undefined)
    assert.equal(liveButton(context, '重试生成字幕'), undefined)
    await click(context, '刷新字幕状态')
    assert.deepEqual(calls, [['read', 'W', 'g']])
  } finally { context.cleanup() }
})

test('字幕结果未知时禁止成片确认，绕过按钮禁用也不能生成发布包', async () => {
  const calls = []
  const g = subtitleGraph({ subtitles: { state: 'unknown', job_id: 'unknown-subtitles' } })
  const context = await mount(g, {}, { confirmWanStoryboard: async (...args) => {
    calls.push(args); return { graph: g, package: { id: 'unexpected-package' } }
  } }, true)
  try {
    assert.equal(finalAck(context).props.disabled, true)
    finalAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    assert.equal(liveButton(context, '确认成片并生成发布包').props.disabled, true)
    await click(context, '确认成片并生成发布包')
    assert.deepEqual(calls, [])
    assert.ok(liveButton(context, '刷新字幕状态'))
  } finally { context.cleanup() }
})

test('字幕成功或成片版本变化清空观看授权，并展示字幕下载与审核说明', async () => {
  const context = await mount(subtitleGraph(), {}, {}, true)
  try {
    finalAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    assert.equal(liveButton(context, '确认成片并生成发布包').props.disabled, false)
    context.actions.graph.value = subtitleGraph({ video_id: 'captioned-final', subtitles: { state: 'succeeded', job_id: 'done' },
      output: { storage_uri: 'captioned.mp4', preview_url: '/captioned.mp4', file_hash: 'captioned-hash', subtitles_burned: true,
        subtitle_files: [{ kind: 'subtitles_ass', storage_uri: 'captions.ass', file_hash: 'ass-hash', bytes: 10, preview_url: '/captions.ass' },
          { kind: 'subtitles_srt', storage_uri: 'captions.srt', file_hash: 'srt-hash', bytes: 10, preview_url: '/captions.srt' }] } })
    await nextTick()
    assert.equal(liveButton(context, '确认成片并生成发布包').props.disabled, true)
    assert.match(content(context.root), /已观看成片，确认字幕、画面和声音/)
    const links = descendants(context.root).filter(target => target.type === 'a')
    assert.ok(links.some(link => link.props.href === '/captions.ass' && content(link).includes('ASS')))
    assert.ok(links.some(link => link.props.href === '/captions.srt' && content(link).includes('SRT')))
    finalAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    context.actions.graph.value.output.file_hash = 'another-hash'
    await nextTick()
    assert.equal(liveButton(context, '确认成片并生成发布包').props.disabled, true)
  } finally { context.cleanup() }
})

test('字幕确认绑定原成片，版本切换清空 GPU 授权和请求号', async () => {
  const calls = []
  const context = await mount(subtitleGraph(), {}, { generateWanStoryboardSubtitles: async (...args) => {
    calls.push(args); throw new Error('暂未返回')
  } }, true)
  try {
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '生成字幕与成片')
    context.actions.graph.value.video_id = 'other-final'
    await nextTick()
    await click(context, '生成字幕与成片')
    assert.equal(calls.length, 1)
    subtitleAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '生成字幕与成片')
    assert.equal(calls.length, 2)
    assert.notEqual(calls[0][2].request_id, calls[1][2].request_id)
    assert.equal(calls[1][2].expected_video_id, 'other-final')
  } finally { context.cleanup() }
})

test('切换作品人物变体脚本清空字幕授权，晚到字幕结果不覆盖当前图', async () => {
  for (const selection of ['work', 'identity_id', 'variant_id', 'script_version_id']) {
    let finish
    const context = await mount(subtitleGraph(), {}, { generateWanStoryboardSubtitles: async () => new Promise(resolve => { finish = resolve }) }, true)
    try {
      subtitleAck(context).props['onUpdate:modelValue'](true)
      await nextTick()
      await click(context, '生成字幕与成片')
      globalThis.wanGraphSeed = subtitleGraph({ id: 'selected-subtitle-graph', script_version_id: selection === 'script_version_id' ? 'new-script' : 's' })
      if (selection === 'work') {
        context.studio.works.push({ ...context.studio.works[0], id: 'new-work' })
        context.studio.currentWorkId = 'new-work'
      } else context.studio.works[0][selection] = selection === 'script_version_id' ? 'new-script' : 'new-selection'
      await nextTick()
      finish(subtitleGraph({ id: 'late-subtitle-graph', subtitles: { state: 'succeeded', job_id: 'done' } }))
      await flush()
      assert.equal(context.actions.graph.value.id, 'selected-subtitle-graph')
      assert.equal(liveButton(context, '生成字幕与成片').props.disabled, true)
    } finally { context.cleanup() }
  }
})

test('服务端允许的视频镜头显示重生成入口，正常执行中的镜头保持原作业查询', async () => {
  const g = eligibleVideoGraph()
  const html = await render({ ...g, identity_reference: null, shots: [...g.shots,
    { ...base.shots[0], shot_index: 3, video_state: 'waiting_provider', remote_task_id: 'running-video', video_regeneration_available: false }] })
  assert.equal((html.match(/>重新生成视频</g) ?? []).length, 1)
  assert.match(html, /original-video|余额不足/)
  assert.match(html, /查询原作业/)
  assert.doesNotMatch(html, /原视频请求可能已计费/)
})

test('视频重生成在旧人物任务、失效选稿、演示模式或成片拼接冻结时禁用', async () => {
  const g = eligibleVideoGraph()
  const legacy = await render({ ...g, schema_version: 2, identity_reference: null }, { identity_id: 'person' })
  assert.match(button(legacy, '重新生成视频'), /disabled/)
  const mock = await render({ ...g, identity_reference: null }, {}, 'mock')
  assert.match(button(mock, '重新生成视频'), /disabled/)
  for (const extra of [{ state: 'stitch_queued' }, { state: 'stitching' }, { state: 'stitch_failed' }, { state: 'succeeded' },
    { video_id: 'final-video' }, { output: { preview_url: '/final.mp4', file_hash: 'hash' } }, { package_id: 'published' },
    { error: { code: 'STORYBOARD_INPUT_CHANGED', message: '选稿已变化' } },
    { error: { code: 'STORYBOARD_SELECTION_REQUIRED', message: '请先采用脚本' } }]) {
    const html = await render({ ...g, identity_reference: null, ...extra })
    assert.match(button(html, '重新生成视频'), /disabled/)
  }
})

test('视频重新生成先确认本镜头估算和重复计费，取消后清空授权', async () => {
  const calls = []
  const context = await mount(eligibleVideoGraph(), {}, { regenerateWanStoryboardVideo: async (...args) => { calls.push(args); return eligibleVideoGraph() } }, true)
  try {
    await click(context, '重新生成视频')
    assert.match(content(context.root), /原视频请求可能已计费，重新生成可能再次计费/)
    assert.match(content(context.root), /本镜头预计.*¥4\.80/)
    assert.equal(liveButton(context, '确认重新生成视频').props.disabled, true)
    await click(context, '确认重新生成视频')
    assert.deepEqual(calls, [])
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    assert.equal(liveButton(context, '确认重新生成视频').props.disabled, false)
    await click(context, '取消')
    assert.equal(liveButton(context, '确认重新生成视频'), undefined)
    await click(context, '重新生成视频')
    assert.equal(liveButton(context, '确认重新生成视频').props.disabled, true)
    assert.deepEqual(calls, [])
  } finally { context.cleanup() }
})

test('视频重生成只提交选中镜头并防重，成功后再次确认生成新请求号', async () => {
  const calls = []; let finish
  const context = await mount(eligibleVideoGraph(), {}, { regenerateWanStoryboardVideo: async (...args) => {
    calls.push(args); return new Promise(resolve => { finish = resolve })
  } }, true)
  try {
    await click(context, '重新生成视频')
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成视频')
    assert.equal(liveButton(context, '重新生成视频').props.disabled, true)
    await click(context, '确认重新生成视频')
    assert.equal(calls.length, 1)
    const [work, graph, shot, input] = calls[0]
    assert.deepEqual([work, graph, shot], ['W', 'g', 1])
    assert.deepEqual({ ...input, request_id: '<random>' }, { actor_id: 'user_local', paid_ack: true,
      duplicate_charge_ack: true, task_graph_digest: 't', expected_video_job_id: 'original-video', request_id: '<random>', ceiling_fen: 480 })
    assert.ok(input.request_id.length >= 16)
    finish({ ...eligibleVideoGraph(), shots: eligibleVideoGraph().shots.map(s => s.shot_index === 1
      ? { ...s, video_job_id: 'new-video', video_attempt: 2 } : s) })
    await flush()
    assert.equal(liveButton(context, '确认重新生成视频'), undefined)
    await click(context, '重新生成视频')
    assert.equal(liveButton(context, '确认重新生成视频').props.disabled, true)
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成视频')
    assert.equal(calls.length, 2)
    assert.notEqual(calls[1][3].request_id, input.request_id)
    assert.equal(calls[1][3].expected_video_job_id, 'new-video')
    finish(eligibleVideoGraph())
    await flush()
  } finally { context.cleanup() }
})

test('视频重生成请求暂未返回时再次确认重放同一请求号', async () => {
  const calls = []
  const context = await mount(eligibleVideoGraph(), {}, { regenerateWanStoryboardVideo: async (...args) => {
    calls.push(args); throw new Error('视频请求暂未返回')
  } }, true)
  try {
    await click(context, '重新生成视频')
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成视频')
    assert.match(content(context.root), /视频请求暂未返回/)
    await click(context, '确认重新生成视频')
    assert.equal(calls.length, 2)
    assert.equal(calls[0][3].request_id, calls[1][3].request_id)
  } finally { context.cleanup() }
})

test('镜头视频作业、任务摘要、报价或可用状态变化会阻止旧确认提交', async () => {
  for (const change of [g => { g.shots[0].video_job_id = 'changed-video' }, g => { g.task_graph_digest = 'new-task' },
    g => { g.estimate.shot_prices[0].ceiling_fen = 500 }, g => { g.shots[0].video_regeneration_available = false },
    g => { g.shots[0].video_state = 'waiting_provider' }, g => { g.shots[0].image_state = 'unknown' }]) {
    const calls = []
    const context = await mount(eligibleVideoGraph(), {}, { regenerateWanStoryboardVideo: async (...args) => { calls.push(args); return eligibleVideoGraph() } }, true)
    try {
      await click(context, '重新生成视频')
      duplicateAck(context).props['onUpdate:modelValue'](true)
      change(context.actions.graph.value)
      await nextTick()
      await click(context, '确认重新生成视频')
      assert.deepEqual(calls, [])
    } finally { context.cleanup() }
  }
})

test('缺少原视频作业或本镜头有效报价时不打开收费重生成确认', async () => {
  for (const change of [g => { g.shots[0].video_job_id = null }, g => { delete g.estimate.shot_prices },
    g => { g.estimate.shot_prices[0].ceiling_fen = NaN }, g => { g.estimate.shot_prices[0].ceiling_fen = -1 }]) {
    const g = eligibleVideoGraph(); change(g)
    const context = await mount(g, {}, {}, true)
    try {
      await click(context, '重新生成视频')
      assert.equal(liveButton(context, '确认重新生成视频'), undefined)
    } finally { context.cleanup() }
  }
})

test('切换作品人物变体脚本清空视频重生成确认，晚到结果不覆盖当前图', async () => {
  for (const selection of ['work', 'identity_id', 'variant_id', 'script_version_id']) {
    let finish
    const context = await mount(eligibleVideoGraph(), {}, { regenerateWanStoryboardVideo: async () => new Promise(resolve => { finish = resolve }) }, true)
    try {
      await click(context, '重新生成视频')
      duplicateAck(context).props['onUpdate:modelValue'](true)
      await nextTick()
      await click(context, '确认重新生成视频')
      globalThis.wanGraphSeed = { ...eligibleVideoGraph(), id: 'selected-video-graph', script_version_id: selection === 'script_version_id' ? 'new-script' : 's' }
      if (selection === 'work') {
        context.studio.works.push({ ...context.studio.works[0], id: 'new-work' })
        context.studio.currentWorkId = 'new-work'
      } else {
        context.studio.works[0][selection] = selection === 'script_version_id' ? 'new-script' : 'new-selection'
      }
      await nextTick()
      finish({ ...eligibleVideoGraph(), id: 'late-video-graph' })
      await flush()
      assert.equal(context.actions.graph.value.id, 'selected-video-graph')
      assert.equal(liveButton(context, '确认重新生成视频'), undefined)
    } finally { context.cleanup() }
  }
})

test('重新生成先内联确认追加费用，取消后重新打开清空确认', async () => {
  const calls = []
  const context = await mount(eligibleGraph(), {}, { regenerateWanStoryboardImage: async (...args) => { calls.push(args); return eligibleGraph() } }, true)
  try {
    await click(context, '重新生成此镜头')
    assert.match(content(context.root), /原请求可能已计费，重新生成会发起新的图片请求，可能再次计费/)
    assert.equal(liveButton(context, '确认重新生成').props.disabled, true)
    await click(context, '确认重新生成')
    assert.deepEqual(calls, [])
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    assert.equal(liveButton(context, '确认重新生成').props.disabled, false)
    await click(context, '取消')
    assert.equal(liveButton(context, '确认重新生成'), undefined)
    await click(context, '重新生成此镜头')
    assert.equal(liveButton(context, '确认重新生成').props.disabled, true)
    assert.deepEqual(calls, [])
  } finally { context.cleanup() }
})

test('只重新生成选中镜头，提交中阻止重复，成功后再次确认生成新请求号', async () => {
  const calls = []; let finish
  const context = await mount(eligibleGraph(), {}, { regenerateWanStoryboardImage: async (...args) => {
    calls.push(args); return new Promise(resolve => { finish = resolve })
  } }, true)
  try {
    await click(context, '重新生成此镜头')
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成')
    assert.equal(liveButton(context, '重新生成此镜头').props.disabled, true)
    await click(context, '确认重新生成')
    assert.equal(calls.length, 1)
    const [work, graph, shot, input] = calls[0]
    assert.deepEqual([work, graph, shot], ['W', 'g', 1])
    assert.deepEqual({ ...input, request_id: '<random>' }, { actor_id: 'user_local', paid_ack: true,
      duplicate_charge_ack: true, plan_digest: 'p', expected_image_job_id: 'original-image', request_id: '<random>', ceiling_fen: 200 })
    assert.ok(input.request_id.length >= 16)
    finish({ ...eligibleGraph(), shots: eligibleGraph().shots.map(s => s.shot_index === 1
      ? { ...s, image_job_id: 'new-image', image_attempt: 2 } : s) })
    await flush()
    assert.equal(liveButton(context, '确认重新生成'), undefined)
    await click(context, '重新生成此镜头')
    assert.equal(liveButton(context, '确认重新生成').props.disabled, true)
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成')
    assert.equal(calls.length, 2)
    assert.notEqual(calls[1][3].request_id, input.request_id)
    assert.equal(calls[1][3].expected_image_job_id, 'new-image')
    finish(eligibleGraph())
    await flush()
  } finally { context.cleanup() }
})

test('重新生成失败后重复确认沿用同一请求号，取消后重置', async () => {
  const calls = []
  const context = await mount(eligibleGraph(), {}, { regenerateWanStoryboardImage: async (...args) => {
    calls.push(args); throw new Error('请求暂未返回')
  } }, true)
  try {
    await click(context, '重新生成此镜头')
    duplicateAck(context).props['onUpdate:modelValue'](true)
    await nextTick()
    await click(context, '确认重新生成')
    assert.match(content(context.root), /请求暂未返回/)
    await click(context, '确认重新生成')
    assert.equal(calls.length, 2)
    assert.equal(calls[0][3].request_id, calls[1][3].request_id)
    await click(context, '取消')
    await click(context, '重新生成此镜头')
    assert.equal(liveButton(context, '确认重新生成').props.disabled, true)
  } finally { context.cleanup() }
})

test('重生成确认打开后任务图作业变化会阻止用旧确认提交', async () => {
  const calls = []
  const context = await mount(eligibleGraph(), {}, { regenerateWanStoryboardImage: async (...args) => { calls.push(args); return eligibleGraph() } }, true)
  try {
    await click(context, '重新生成此镜头')
    duplicateAck(context).props['onUpdate:modelValue'](true)
    context.actions.graph.value.shots[0].image_job_id = 'changed-image'
    await nextTick()
    await click(context, '确认重新生成')
    assert.deepEqual(calls, [])
  } finally { context.cleanup() }
})

test('作品人物变体或脚本切换清空重生成确认并忽略晚到响应', async () => {
  for (const selection of ['work', 'identity_id', 'variant_id', 'script_version_id']) {
    let finish
    const context = await mount(eligibleGraph(), {}, { regenerateWanStoryboardImage: async () => new Promise(resolve => { finish = resolve }) }, true)
    try {
      await click(context, '重新生成此镜头')
      duplicateAck(context).props['onUpdate:modelValue'](true)
      await nextTick()
      await click(context, '确认重新生成')
      globalThis.wanGraphSeed = { ...eligibleGraph(), id: 'selected-graph', script_version_id: selection === 'script_version_id' ? 'new-script' : 's' }
      if (selection === 'work') {
        context.studio.works.push({ ...context.studio.works[0], id: 'new-work' })
        context.studio.currentWorkId = 'new-work'
      } else {
        context.studio.works[0][selection] = selection === 'script_version_id' ? 'new-script' : 'new-selection'
      }
      await nextTick()
      finish({ ...eligibleGraph(), id: 'late-graph' })
      await flush()
      assert.equal(context.actions.graph.value.id, 'selected-graph')
      assert.equal(liveButton(context, '确认重新生成'), undefined)
    } finally { context.cleanup() }
  }
})

test('旧任务或不匹配人物的操作处理函数不会调用收费 API', async () => {
  for (const graph of [{ ...base, schema_version: 2 }, { ...base, identity_reference: { ...identityReference, identity_id: 'other' } }]) {
    const paid = []
    const context = await mount(graph, {}, Object.fromEntries(['generateWanStoryboardImages', 'submitWanStoryboardVideos',
      'approveWanStoryboardImages', 'approveWanStoryboardVideos'].map(key => [key, async () => { paid.push(key); return graph }])))
    try {
      context.actions.generateImages(); context.actions.submitVideos(); context.actions.approveImages(); context.actions.approveVideos()
      await nextTick()
      assert.deepEqual(paid, [])
    } finally { context.cleanup() }
  }
})

test('同一作品切换人物或变体会重新加载，并忽略切换前晚到的任务图', async () => {
  const responses = []
  const context = await mount({ ...base, identity_reference: identityReference }, {}, {
    listWanStoryboards: async () => new Promise(resolve => responses.push(resolve)),
  })
  try {
    context.studio.works[0].identity_id = 'other'
    await nextTick()
    assert.equal(responses.length, 2)
    responses[1]({ graphs: [{ ...base, id: 'other-graph', identity_reference: { ...identityReference, identity_id: 'other' } }] })
    await nextTick()
    responses[0]({ graphs: [{ ...base, id: 'old-graph', identity_reference: identityReference }] })
    await nextTick()
    assert.equal(context.actions.graph.value.id, 'other-graph')
    context.studio.works[0].variant_id = 'variant-new'
    await nextTick()
    assert.equal(responses.length, 3)
    assert.equal(context.actions.graph.value, null)
  } finally { context.cleanup() }
})

test('旧图轮询只读查询，人物匹配且已授权的新图继续既有逐镜头推进', async () => {
  const originalTimeout = globalThis.setTimeout; const originalClear = globalThis.clearTimeout
  try {
    for (const legacy of [true, false]) {
      const timers = new Map(); let timerId = 0; const calls = []
      globalThis.setTimeout = callback => { const id = ++timerId; timers.set(id, callback); return id }
      globalThis.clearTimeout = id => { timers.delete(id) }
      const graph = { ...base, schema_version: legacy ? 2 : 3, identity_reference: legacy ? null : identityReference,
        state: 'waiting_provider', video_approval: { task_graph_digest: 't' } }
      const context = await mount(graph, {}, {
        getWanStoryboard: async () => { calls.push('read'); return graph },
        submitWanStoryboardVideos: async () => { calls.push('submit'); return graph },
      })
      try {
        assert.equal(timers.size, 1)
        await timers.values().next().value()
        assert.deepEqual(calls, [legacy ? 'read' : 'submit'])
      } finally { context.cleanup() }
    }
  } finally { globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear }
})
