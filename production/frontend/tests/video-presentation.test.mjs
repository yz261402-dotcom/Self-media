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
for (const name of ['views/VideoView', 'components/VideoDelivery']) {
  const outfile = join(output, `${name.replaceAll('/', '-')}.mjs`)
  await build({
    stdin: { contents: `export { default } from './src/${name}.vue'; export { useStudioStore } from './src/stores/studio.ts'`, resolveDir: resolve('.') },
    outfile, bundle: true, format: 'esm', platform: 'node',
    external: ['vue', 'vue/server-renderer', 'pinia', 'vue-router'],
    plugins: [{ name: 'vue-test', setup(builder) {
      builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const api = {}; export const apiMode = "http";' }))
      builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
        const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
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


test('video duration uses only the current confirmed audio, not stale preflight data', async () => {
  const html = await render('views/VideoView', store => {
    store.currentWorkId = 'work'
    store.works = [{ id: 'work', brief: {}, script_version_id: null }]
    store.thPreflight = { audio_duration_ms: 23200, blockers: [], ok: true }
  })
  assert.doesNotMatch(html, /23\.2/)
  assert.match(html, /待确认音频/)
  assert.doesNotMatch(html, /生成底片（9\.0s）/)
  assert.doesNotMatch(html, /素材已就绪/)
})

function seedPlate(store, preparation = { ok: true, blockers: [], source: { kind: 'hero', asset_id: 'hero', identity_id: 'person', label: '当前人物定妆原图', storage_uri: 'assets/hero.png' } }) {
  store.currentWorkId = 'work'
  store.works = [{ id: 'work', brief: {}, identity_id: 'person', final_audio_id: 'audio', route: 'M2' }]
  store.identities = [{ id: 'person', name: '本期主播' }]
  store.audios = [{ id: 'audio', work_id: 'work', kind: 'full', confirmed: true, is_mock: false, duration_ms: 46428 }]
  store.thRoutes = { route_confirmed: true, routes: [], price_snapshots: [] }
  store.thPlate = { work_id: 'work', approved: false, plates: [], preparation }
  store.thPreflight = { ok: false, blockers: [{ code: 'PLATE_NOT_APPROVED', message: '等待底片' }] }
}

test('底片沿用已审定妆原图时可直接生成，不被缺少下游底片阻断', async () => {
  const html = await render('views/VideoView', store => seedPlate(store))
  assert.match(html, /当前人物定妆原图/)
  assert.match(html, /<img[^>]+alt="底片源图预览"/)
  const button = html.match(/<button[^>]*>\s*生成底片（46\.4s）\s*<\/button>/)?.[0]
  assert.ok(button)
  assert.doesNotMatch(button, /disabled/)
  assert.doesNotMatch(html, /沿用作品当前绑定（未绑定）/)
})

test('底片素材不合格时明确展示原因并禁用生成，旧人物变体不进入选项', async () => {
  const html = await render('views/VideoView', store => {
    seedPlate(store, { ok: false, blockers: [{ code: 'MOTION_REQUIRED', message: '请先选择本期动作参考' }], source: null })
    store.variants = [
      { id: 'other', identity_id: 'other-person', approval_state: 'approved', preview_label: '别人的画面' },
      { id: 'mine', identity_id: 'person', approval_state: 'approved', preview_label: '本期人物换景' },
    ]
  })
  assert.match(html, /请先选择本期动作参考/)
  assert.match(html, /<button[^>]+disabled[^>]*>\s*生成底片（46\.4s）/)
  assert.doesNotMatch(html, /别人的画面/)
  assert.match(html, /本期人物换景/)
})

test('历史已审底片失效后不得宣称当前已有可用底片', async () => {
  const html = await render('views/VideoView', store => {
    seedPlate(store)
    store.videos = [{ id: 'old-plate', kind: 'plate', approval_state: 'approved', is_mock: false, duration_ms: 50000 }]
  })
  assert.doesNotMatch(html, /已有审核通过的底片/)
})

test('底片生成中显示等待说明并阻止重复点击', async () => {
  const html = await render('views/VideoView', store => { seedPlate(store); store.busy = '生成底片' })
  assert.match(html, /正在生成动作底片/)
  assert.match(html, /请勿重复提交/)
})

/**
 * 造一个"素材就绪 + 费用已确认"的现场，让主按钮的可用性只取决于运行状态。
 * 否则断言"按钮被禁用"会因为费用没确认而恒真，测不出任何东西。
 */
function seedReadyToSubmit(store, runs = []) {
  seedPlate(store)
  store.thPreflight = { ok: true, blockers: [] }
  const listing = { route: 'M2', resolution: 'native', audio_id: 'audio', prompt: '' }
  store.thCost = {
    work_id: 'work', route: 'M2', confirmation_valid: true, changed_fields: [],
    current_call_list: { ...listing }, billable_allowed: true,
    confirmation: { id: 'cost', work_id: 'work', route: 'M2', call_list: { ...listing },
      segment_count: 1, resolution: 'native', estimated_fen: 0, ceiling_fen: 0 },
  }
  store.thSubmissions = { work_id: 'work', runs, auto_fallback: false, note: '' }
}

const driverView = (extra = {}) => ({
  work_id: 'work', state: 'PENDING', total: 7, succeeded: 1, in_flight: 0, pending: 6,
  failed: 0, unknown: 0, resumable: true, stop_reason: null, blocked_runs: [],
  attempt: 'a1', note: '', ...extra,
})

test('在途口播禁止重复真实提交，且不把"还没结果"引导成人工改判', async () => {
  const html = await render('views/VideoView', store => {
    seedReadyToSubmit(store, [{
      id: 'live-run', work_id: 'work', state: 'waiting_provider',
      model: 'MuseTalk', remote_job_id: 'remote', is_mock: false,
    }])
  })
  assert.match(html, /<button[^>]+disabled[^>]*>\s*生成口播（真实调用/)
  assert.match(html, /查询原任务/)
  // 在途 ≠ 未知：还没出结果时不该出现对账入口，否则会把"再等等"引导成"人工改判"。
  // 只断言按钮本身——模板注释也会进 SSR 产物，按词断言会误伤。
  assert.doesNotMatch(html, /<button[^>]*>\s*对账\s*<\/button>/)
})

test('unknown 运行同时给出"查询原任务"与"对账"两条出路', async () => {
  const html = await render('views/VideoView', store => {
    seedReadyToSubmit(store, [{
      id: 'unknown-run', work_id: 'work', state: 'unknown',
      model: 'MuseTalk', remote_job_id: 'remote', is_mock: false,
    }])
  })
  assert.match(html, /查询原任务/)
  assert.match(html, /<button[^>]*>\s*对账\s*<\/button>/)
})

test('unknown 不再把真实提交永久锁死，改为可点的"重新生成"并说明会再次计费', async () => {
  const html = await render('views/VideoView', store => {
    seedReadyToSubmit(store, [{
      id: 'unknown-run', work_id: 'work', state: 'unknown',
      model: 'MuseTalk', remote_job_id: 'remote', is_mock: false,
    }])
  })
  const button = html.match(/<button[^>]*>\s*重新生成（真实调用，会再次计费）\s*<\/button>/)?.[0]
  assert.ok(button, '应渲染可点的"重新生成"按钮')
  assert.doesNotMatch(button, /disabled/)
})

test('旧分段推进状态不再进入视频合成界面', async () => {
  const html = await render('views/VideoView', store => {
    seedReadyToSubmit(store, [{
      id: 'run-1', work_id: 'work', state: 'succeeded',
      model: 'OmniHuman', remote_job_id: 'r1', is_mock: false,
    }])
    store.thDriver = driverView({ note: '还有 6 段待发' })
  })
  assert.doesNotMatch(html, /继续生成剩余分段|还有 6 段待发|已完成 \d+ \/ \d+ 段/)
})

test('旧分段停止状态不再显示为视频合成状态', async () => {
  const html = await render('views/VideoView', store => {
    seedReadyToSubmit(store, [{
      id: 'unknown-run', work_id: 'work', state: 'unknown',
      model: 'OmniHuman', remote_job_id: 'r1', is_mock: false,
    }])
    store.thDriver = driverView({
      state: 'STOPPED', succeeded: 1, pending: 5, unknown: 1, resumable: false,
      stop_reason: 'UNKNOWN_REQUIRES_RECONCILIATION', blocked_runs: ['unknown-run'],
      note: '有 1 段结果未知，先对账',
    })
  })
  assert.doesNotMatch(html, /继续生成剩余分段/)
  assert.doesNotMatch(html, /STOPPED|有 1 段结果未知，先对账|停住的运行/)
})

test('an unrelated task is not presented as postprocessing', async () => {
  const html = await render('components/VideoDelivery', store => {
    store.postprocess = { task: { stage: 'script', state: 'running', stage_rows: [{ stage: 'script', label: '写稿任务', state: 'active' }] } }
  })
  assert.doesNotMatch(html, /正在制作成片/)
  assert.doesNotMatch(html, /写稿任务/)
  assert.doesNotMatch(html, /后期阶段明细/)
})

test('postprocessing details show only relevant stages', async () => {
  const html = await render('components/VideoDelivery', store => {
    store.postprocess = { task: { stage: 'postprocess', state: 'running', stage_rows: [
      { stage: 'script', label: '写稿任务', state: 'done' },
      { stage: 'postprocess', label: '字幕与成片任务', state: 'active' },
    ] } }
  })
  assert.match(html, /后期阶段明细/)
  assert.equal((html.match(/class="active stage-row"/g) ?? []).length, 1)
  assert.doesNotMatch(html, /class="done stage-row"/)
  assert.doesNotMatch(html, /写稿任务/)
})

function seedPostprocessFailure(store) {
  store.currentWorkId = 'work'
  store.works = [{ id: 'work', brief: {}, current_task_id: 'postprocess-task' }]
  store.postprocess = {
    task: {
      id: 'postprocess-task', work_id: 'work', stage: 'postprocess', state: 'blocked',
      last_error: { code: 'ASR_STAGE_FAILED', message: '字幕识别失败，请检查识别服务' },
      stage_rows: [{ stage: 'postprocess', label: '字幕与成片任务', state: 'blocked' }],
    },
    execution: { id: 'execution', task_id: 'postprocess-task', work_id: 'work', state: 'blocked' },
  }
}

test('后期阻断原因直接显示在生成按钮旁，不需要展开阶段明细', async () => {
  const html = await render('components/VideoDelivery', seedPostprocessFailure)
  const controls = html.split('class="delivery-controls"')[1].split('<details')[0]
  assert.match(controls, /<p[^>]+role="alert"[^>]*>字幕识别失败，请检查识别服务<\/p>/)
})

test('当前任务没有last_error时显示匹配执行的失败原因', async () => {
  const html = await render('components/VideoDelivery', store => {
    seedPostprocessFailure(store)
    store.postprocess.task.last_error = null
    store.postprocess.execution.error = { message: '成片编码失败，请检查视频输入' }
  })
  assert.match(html, /<p[^>]+role="alert"[^>]*>成片编码失败，请检查视频输入<\/p>/)
})

test('顶层错误已展示时不重复展示任务和执行错误', async () => {
  const html = await render('components/VideoDelivery', store => {
    seedPostprocessFailure(store)
    store.postprocess.error = { code: 'ASR_STAGE_FAILED', message: '字幕识别失败，请检查识别服务' }
    store.postprocess.execution.error = { message: '字幕识别失败，请检查识别服务' }
  })
  assert.equal((html.match(/字幕识别失败，请检查识别服务/g) ?? []).length, 1)
  assert.equal((html.match(/role="alert"/g) ?? []).length, 1)
})

test('其他作品、过期任务和不匹配执行的错误不进入当前成片操作区', async () => {
  const staleCases = [
    store => { store.postprocess.task.work_id = 'another-work' },
    store => { store.works[0].current_task_id = 'new-task' },
    store => { store.postprocess.task.stage = 'script' },
    store => { store.postprocess.execution.task_id = 'old-task' },
    store => { store.postprocess.task.last_error = null; store.postprocess.execution.work_id = 'another-work' },
    store => { store.postprocess.task.last_error = null; delete store.postprocess.execution.task_id },
  ]
  for (const stale of staleCases) {
    const html = await render('components/VideoDelivery', store => {
      seedPostprocessFailure(store)
      store.postprocess.execution.error = { message: '旧执行错误' }
      stale(store)
    })
    assert.doesNotMatch(html, /字幕识别失败，请检查识别服务|旧执行错误/)
  }
})

test('任务已恢复运行或成功时不继续显示残留失败信息', async () => {
  for (const state of ['queued', 'running', 'waiting_provider', 'waiting_review', 'succeeded']) {
    const html = await render('components/VideoDelivery', store => {
      seedPostprocessFailure(store)
      store.postprocess.task.state = state
      store.postprocess.execution.error = { message: '旧执行错误' }
    })
    assert.doesNotMatch(html, /字幕识别失败，请检查识别服务|旧执行错误/)
  }
})
