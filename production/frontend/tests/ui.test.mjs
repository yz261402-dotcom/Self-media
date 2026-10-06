import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { after, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.studio-ui-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const views = {}
for (const name of ['App', 'views/AvatarView', 'views/TtsView', 'views/VideoView']) {
  const outfile = join(output, `${name.replaceAll('/', '-')}.mjs`)
  await build({
    stdin: { contents: `export { default } from './src/${name}.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
    outfile, bundle: true, format: 'esm', platform: 'node',
    external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
    plugins: [{ name: 'vue-test', setup(builder) {
      builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = {}; export const apiMode = "http";' }))
      builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
        let source = await readFile(path, 'utf8')
        if (path.replaceAll('\\', '/').endsWith('/src/App.vue')) {
          source = source.replace('</script>', 'if (globalThis.durationFormSeed !== undefined) { showCreate.value = true; form.value.topic = "自由时长"; form.value.duration = globalThis.durationFormSeed }\n</script>')
        }
        const { descriptor } = parse(source, { filename: path })
        const id = createHash('sha256').update(path.replaceAll('\\', '/')).digest('hex').slice(0, 12)
        const result = compileScript(descriptor, { id, inlineTemplate: true, templateOptions: { ssr: true } })
        return { contents: result.content, loader: 'ts', resolveDir: dirname(path) }
      })
    } }],
  })
  views[name] = await import(pathToFileURL(outfile).href)
}

async function render(name, setup, path = '/avatar') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const view = views[name]
  const store = view.useStudioStore()
  store.apiReady = true
  setup(store)
  const router = createRouter({ history: createMemoryHistory(), routes: ['avatar', 'script', 'tts', 'video', 'publish', 'metrics', 'models'].map(name => ({ path: `/${name}`, component: { template: '<div />' } })) })
  await router.push(path)
  return renderToString(createSSRApp(view.default).use(pinia).use(router))
}


test('empty workspace shows the creation guide and a direct create-work action', async () => {
  const html = await render('App', () => {})
  assert.match(html, /aria-label="创作向导"/)
  assert.match(html, /创建第一个作品/)
  assert.match(html, /填写主题，依次选择人物、文案和声音/)
})

for (const duration of ['', 0.5, 15, 60, 180, 3600]) {
  test(`新建作品允许目标时长 ${duration === '' ? '留空' : duration + ' 秒'}`, async () => {
    globalThis.durationFormSeed = duration
    try {
      const html = await render('App', () => {})
      assert.match(html, /目标时长（秒，选填）/)
      assert.doesNotMatch(html, /min="45"|max="90"/)
      const submit = html.match(/<button[^>]*>创建并选择人物<\/button>/)?.[0]
      assert.ok(submit)
      assert.doesNotMatch(submit, /disabled/)
    } finally { delete globalThis.durationFormSeed }
  })
}

for (const duration of [-1, 0, Number.NaN, Number.POSITIVE_INFINITY]) {
  test(`新建作品拒绝非法目标时长 ${duration}`, async () => {
    globalThis.durationFormSeed = duration
    try {
      const html = await render('App', () => {})
      assert.match(html.match(/<button[^>]*>创建并选择人物<\/button>/)?.[0] ?? '', /disabled/)
    } finally { delete globalThis.durationFormSeed }
  })
}

test('已有待审核原片时侧栏和向导不再提示尚未提交或重新生成', async () => {
  const html = await render('App', store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', brief: {}, identity_id: 'person', script_version_id: 'script' }]
    store.readinessMap = { person: { ready: true, reasons: [] } }
    store.scriptBatch = { candidates: [{ id: 'script' }] }
    store.audios = [{ id: 'audio', work_id: 'work', kind: 'full', confirmed: true, is_mock: false, duration_ms: 46000 }]
    store.videos = [{ id: 'original', work_id: 'work', kind: 'original', is_mock: false, approval_state: 'pending', confirmed: false }]
    store.task = { stage: 'talking_head', state: 'waiting_review' }
  }, '/video')
  assert.match(html, /原片待审核/)
  assert.match(html, /口播原片已生成，请观看并审核/)
  assert.doesNotMatch(html, /尚未提交|音频已确认，接下来生成口播视频/)
})

test('voice previews contain an actual audio player and require an explicit listening acknowledgement', async () => {
  const html = await render('views/TtsView', store => {
    store.works = [{ id: 'work', brief: {}, script_version_id: 'script' }]
    store.currentWorkId = 'work'
    store.scriptBatch = { candidates: [{ id: 'script' }] }
    store.audios = [
      { id: 'preview', kind: 'preview', duration_ms: 3000, url: '/preview.wav', checks: [] },
      { id: 'full', kind: 'full', duration_ms: 60000, url: '/full.wav', checks: [], confirmed: false },
    ]
  })
  assert.match(html, /<audio[^>]+src="\/preview.wav"/)
  assert.match(html, /我已试听/)
})

test('语音页展示统一参数、声音效果器和真人音色克隆费用说明', async () => {
  const html = await render('views/TtsView', store => {
    store.works = [{ id: 'work', brief: {}, script_version_id: 'script' }]
    store.currentWorkId = 'work'
    store.scriptBatch = { candidates: [{ id: 'script' }] }
  })
  for (const label of ['配音参数', '情绪', '语速', '音量', '音调', '声音效果器', '刚劲', '轻柔', '浑厚', '清脆', '克隆我的音色', '9.9 元', '7 天']) {
    assert.ok(html.includes(label), `缺少 ${label}`)
  }
  assert.doesNotMatch(html, /whisper|fluent|情绪强度/)
})

test('未应用参数时语音页阻止旧音轨确认和进入视频', async () => {
  const html = await render('views/TtsView', store => {
    store.works = [{ id: 'work', brief: {}, script_version_id: 'script', voice_asset_id: 'voice', final_audio_id: 'old' }]
    store.currentWorkId = 'work'
    store.voices = [{ id: 'voice', name: '测试声音', approval_state: 'approved' }]
    store.scriptBatch = { candidates: [{ id: 'script' }] }
    store.audios = [{ id: 'old', work_id: 'work', kind: 'full', confirmed: true, is_mock: false, duration_ms: 12000, checks: [] }]
    store.ttsControlsDraft = { emotion: 'happy', speed: 1.2, vol: 1, pitch: 0, voice_modify: { pitch: 0, intensity: 0, timbre: 0 } }
  })
  assert.match(html, /参数尚未应用/)
  assert.doesNotMatch(html, /下一步：合成视频/)
})

test('新克隆音色先试听登记基准，未登记前不能直接批准选用', async () => {
  const html = await render('views/TtsView', store => {
    store.works = [{ id: 'work', brief: {} }]
    store.currentWorkId = 'work'
    store.voices = [{ id: 'clone', name: '本人', approval_state: 'pending', clone_metadata: { activation_state: 'active' } }]
    store.audios = [{ id: 'preview', work_id: 'work', kind: 'preview', voice_asset_id: 'clone', is_mock: false, url: '/preview.wav', duration_ms: 6000, checks: [] }]
  })
  const select = html.match(/<button[^>]*>先登记试听基准<\/button>/)?.[0] ?? ''
  assert.match(select, /disabled/)
  const baseline = html.match(/<button[^>]*>登记这条试听为基准<\/button>/)?.[0] ?? ''
  assert.ok(baseline)
  assert.doesNotMatch(baseline, /disabled/)
  assert.doesNotMatch(html, /临时克隆音色/)
})

test('completed video page renders the real delivery preview and download', async () => {
  const html = await render('views/VideoView', store => {
    store.currentWorkId = 'work'
    store.pkg = { work_id: 'work', state: 'ready', mode: 'production', title: '交付作品', files: [], video_url: '/final.mp4', download_url: '/package.zip' }
  })
  assert.match(html, /<video[^>]+src="\/final.mp4"/)
  assert.match(html, /href="\/package.zip"/)
  assert.match(html, /下载完整作品包/)
  assert.match(html, /成片与下载/)
})

test('动作或分辨率改变时页面明确要求重新确认费用并禁用真实生成', async () => {
  const html = await render('views/VideoView', store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', route: 'OMNIHUMAN_1_5', brief: {} }]
    store.thRoutes = { route_confirmed: true, selected_route: 'OMNIHUMAN_1_5', routes: [], price_snapshots: [] }
    store.thPrompt = '改为挥手'
    store.thPreflight = { ok: true, blockers: [] }
    store.audios = [{ id: 'audio', work_id: 'work', kind: 'full', confirmed: true, is_mock: false, duration_ms: 12000 }]
    const listing = { route: 'OMNIHUMAN_1_5', audio_id: 'audio', audio_hash: 'hash', variant_id: null,
      motion_ref_id: null, plate_id: null, prompt: '自然讲述', resolution: '1080p', segment_count: 1, digest: 'digest' }
    store.thCost = { work_id: 'work', route: 'OMNIHUMAN_1_5', confirmation_valid: true, changed_fields: [],
      current_call_list: listing, billable_allowed: true,
      confirmation: { id: 'cost', work_id: 'work', route: 'OMNIHUMAN_1_5', request_digest: 'digest', call_list: listing,
        segment_count: 1, resolution: '1080p', estimated_fen: 1200, ceiling_fen: 1440 } }
  }, '/video')
  assert.match(html, /动作描述已变化，请重新确认/)
  assert.match(html.match(/<button[^>]*>\s*生成口播（真实调用[\s\S]*?<\/button>/)?.[0] ?? '', /disabled/)
  assert.match(html, /历史确认/)
})

test('长音频不再显示分段入口，并明确阻断 OmniHuman', async () => {
  const audio = { id: 'audio', work_id: 'work', kind: 'full', confirmed: true, is_mock: false, duration_ms: 61723, checks: [] }
  const withWork = store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', brief: {} }]
    store.audios = [audio]
  }

  const html = await render('views/VideoView', store => {
    withWork(store)
    store.thRoutes = { route_confirmed: false, routes: [{ route: 'OMNIHUMAN_1_5', label: 'OmniHuman' }], price_snapshots: [] }
  })
  assert.match(html, /OmniHuman 仅支持小于 60 秒/)
  assert.doesNotMatch(html, /音频分段/)
  assert.doesNotMatch(html, /运行对齐/)
  assert.doesNotMatch(html, /建立\/刷新分段计划/)
})

test('person selection starts with clear cards and variant options use real background assets', async () => {
  const html = await render('views/AvatarView', store => {
    store.identities = [{ id: 'person', name: '现有主播', approval_state: 'approved', persona: '自然讲述' }]
    store.backgroundAssets = [{ id: 'background-real', note: '已登记的演播室', kind: 'background', approval_state: 'approved' }]
  })
  assert.match(html, /class="avatar-cards"/)
  assert.match(html, /选择已有主播/)
  assert.match(html, /value="background-real"/)
  assert.doesNotMatch(html, /value="studio"/)
})

test('frozen incomplete person links to details without claiming automatic approval', async () => {
  const html = await render('views/AvatarView', store => {
    store.identities = [{ id: 'legacy', name: '旧主播', approval_state: 'approved', frozen: true, persona: '自然讲述' }]
    store.readinessMap = { legacy: { ready: false, reasons: ['旧审核不完整'] } }
  })
  assert.match(html, /查看待完善资料/)
  assert.doesNotMatch(html, /已确认，可以制作/)
})

test('a running script or avatar task does not show video synthesis in progress', async () => {
  const html = await render('App', store => {
    store.task = { id: 'task', state: 'running', stage: 'avatar_variant' }
  })
  assert.doesNotMatch(html, /合成进行中/)
})

test('future features remain reachable without showing unrelated creation guidance', async () => {
  const html = await render('App', () => {}, '/models')
  for (const href of ['/avatar', '/script', '/tts', '/video', '/publish', '/metrics', '/models']) {
    assert.ok(html.includes(`href="${href}"`))
  }
  assert.doesNotMatch(html, /aria-label="创作向导"/)
})

test('active work keeps step navigation without a duplicate introductory heading', async () => {
  const html = await render('App', store => {
    store.works = [{id: 'work', name: 'RAG', brief: {topic: '介绍RAG'}}]
    store.currentWorkId = 'work'
  })
  assert.match(html, /aria-label="创作向导"/)
  assert.match(html, /class="workflow-summary"/)
  assert.doesNotMatch(html, /这一期，做到哪一步了|从一个主题，做出一条口播视频/)
})

test('storyboard route goes to writing before Wan production without requiring a presenter', async () => {
  const html = await render('App', store => {
    store.works = [{ id: 'work', name: '短片', route: 'WAN_3_0', brief: { content_route: 'short_video_storyboard' } }]
    store.currentWorkId = 'work'
  })
  assert.match(html, /前往「去写分镜剧本」/)
  assert.doesNotMatch(html, /先为这期作品选择人物/)
})

test('selected storyboard goes straight to shot production without TTS', async () => {
  const html = await render('App', store => {
    store.works = [{ id: 'work', name: '短片', route: 'WAN_3_0', script_version_id: 'shot-script', brief: { content_route: 'short_video_storyboard' } }]
    store.currentWorkId = 'work'
    store.scriptBatch = { candidates: [{ id: 'shot-script' }] }
  })
  assert.match(html, /前往「制作分镜短片」/)
  assert.doesNotMatch(html, /前往「去合成音轨」/)
})

test('a person without motion references has an expanded, actionable template empty state', async () => {
  const html = await render('views/AvatarView', store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', identity_id: 'person', brief: {} }]
    store.identities = [{ id: 'person', name: '人物草稿', approval_state: 'approved' }]
    store.motionCatalog = { catalog: {}, approved: [] }
  })
  assert.match(html, /<details[^>]+open[^>]*>[\s\S]*?动作参考/)
  assert.match(html, /还没有绑定动作/)
  assert.match(html, /从已有动作添加/)
  assert.doesNotMatch(html, /需要先登记并审核动作参考/)
})

test('motion options use reference identifiers even when motion keys are the same', async () => {
  const html = await render('views/AvatarView', store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', identity_id: 'person', brief: {}, motion_intent: { motion_key: 'smile', motion_ref_id: 'ref-second' } }]
    store.identities = [{ id: 'person', name: '人物草稿', approval_state: 'approved' }]
    store.motionCatalog = { catalog: {}, approved: [
      { id: 'ref-first', motion_key: 'smile', label: '第一种微笑' },
      { id: 'ref-second', motion_key: 'smile', label: '第二种微笑' },
    ] }
  })
  assert.match(html, /value="ref-first"/)
  assert.match(html, /value="ref-second"/)
  assert.match(html, /本期动作：[\s\S]*?第二种微笑/)
})
