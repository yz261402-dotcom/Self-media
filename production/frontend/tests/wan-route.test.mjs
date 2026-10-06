import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const output = await mkdtemp(resolve('node_modules/.wan-route-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'wan.mjs')
await build({
  stdin: { contents: `export { default } from './src/views/VideoView.vue'; export { useStudioStore } from './src/stores/studio.ts'; export { mockApi } from './src/api/mock.ts'; export { default as WorkflowGuide } from './src/components/WorkflowGuide.vue'; export { httpApi } from './src/api/http.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'pinia', 'vue-router'],
  plugins: [{ name: 'wan-view', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
    builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const apiMode = "http"; export const api = new Proxy({}, { get: (_, key) => globalThis.wanTestApi[key] });' }))
    builder.onLoad({ filter: /\.vue$/ }, async ({ path }) => {
      const { descriptor } = parse(await readFile(path, 'utf8'), { filename: path })
      return { contents: compileScript(descriptor, { id: path.replace(/[^a-zA-Z0-9]/g, ''), inlineTemplate: true, templateOptions: { ssr: true } }).content, loader: 'ts', resolveDir: dirname(path) }
    })
  } }],
})
const module = await import(pathToFileURL(outfile).href)
let store, pinia
const capability = route => ({
  route, label: route, provider: route === 'WAN_3_0' ? 'dashscope_wan' : route === 'M2' ? 'm2_worker' : 'volcengine',
  model: route === 'WAN_3_0' ? 'wan3.0-video' : route, requires_motion_plate: route === 'M2',
  declared: route === 'WAN_3_0' ? { duration_min_seconds: 2, duration_max_seconds: 30, prompt_max_chars: 20000, resolutions: ['480p', '720p', '1080p'] } : {},
  cost_summary: route === 'WAN_3_0' ? '官方北京 720P 0.60 元/生成秒' : '费用由服务端提供',
  cost_notice_required: route !== 'M2', cost_notice: { route, unit_text: '服务端报价：720P 每秒 0.60 元', risks: [], excluded: [] },
  body_motion: { scope: '待预览验收', verified: false }, verified_state: 'unverified', open_questions: ['口型待验收'],
})
function seed(route = 'WAN_3_0', duration = 12000) {
  store.currentWorkId = 'work'
  store.works = [{ id: 'work', name: '测试作品', brief: {}, route, identity_id: 'person', final_audio_id: 'audio' }]
  store.identities = [{ id: 'person', name: '当前人物' }]
  store.audios = [{ id: 'audio', kind: 'full', work_id: 'work', duration_ms: duration, confirmed: true, is_mock: false }]
  store.thRoutes = { routes: ['M2', 'OMNIHUMAN_1_5', 'WAN_3_0'].map(capability), selected_route: route, route_confirmed: true, price_snapshots: [] }
  store.thPreflight = { work_id: 'work', ok: true, blockers: [] }
  store.thAdapters = { wan: { provider: 'dashscope_wan', model: 'wan3.0-video', configured: true,
    state: 'unverified', detail: '已配置；真实生成与口型效果尚未实测。' } }
  store.thPrompt = '人物平视镜头自然讲述，背景光线柔和。'
  store.thResolution = '720p'
  store.thDurationSeconds = 5
  store.wanImage = { asset_id: 'image', asset_hash: 'image-hash', storage_uri: 'file:///image.png', preview_url: '/image.png', label: '场景参考图', width: 720, height: 1280 }
}
function installCost() {
  const route = store.currentWork.route
  const listing = { route, audio_id: 'audio', audio_hash: 'hash', prompt: store.thPrompt, resolution: store.thResolution, segment_count: 1, digest: 'server-digest', ...(route === 'WAN_3_0' ? { generated_seconds: store.thDurationSeconds, generation_mode: 'image_prompt_native_audio', sound_mode: 'native_audio' } : {}) }
  store.thCost = { work_id: 'work', route, confirmation_valid: true, changed_fields: [], current_call_list: { ...listing },
    confirmation: { id: 'confirmed', work_id: 'work', route, call_list: { ...listing }, segment_count: 1, resolution: store.thResolution, estimated_fen: 720, ceiling_fen: 720 },
    estimate: { route, estimated_fen: 720, ceiling_fen: 720, price_basis: '服务端计算', note: '估价不等于账单' }, billable_allowed: true }
}
beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  store = module.useStudioStore()
  globalThis.wanTestApi = { getTalkingHeadCost: async () => null }
})
async function render(component = module.default) {
  const router = createRouter({ history: createMemoryHistory(), routes: ['video', 'avatar', 'tts'].map(name => ({ path: `/${name}`, component: { template: '<div />' } })) })
  await router.push('/video')
  return renderToString(createSSRApp(component).use(pinia).use(router))
}

test('三路线均显式呈现，未选择时不默认M2且不显示动作底片或零元报价', async () => {
  seed('M2')
  store.thRoutes.route_confirmed = false
  store.thRoutes.selected_route = null
  assert.equal(store.currentRoute, null)
  const html = await render()
  assert.match(html, /M2 · 低成本口播/)
  assert.match(html, /OmniHuman · 数字人口播/)
  assert.match(html, /Wan 3\.0 · 创意短视频/)
  assert.equal((html.match(/aria-pressed="false"/g) ?? []).length, 3)
  assert.doesNotMatch(html, /准备动作底片/)
  assert.doesNotMatch(html, /¥0\.00/)
})

test('Wan使用上传图片和提示词生成原生声音，无人物稿件或TTS仍可进入生成流程', async () => {
  seed(); installCost()
  store.audios = []; store.identities = []; store.works[0].identity_id = null
  store.ttsControlsDraft = { speed: 1.2 }
  assert.equal(store.thInputError, '')
  assert.equal(store.thReady, true)
  assert.equal(store.nextStep.to, '/video')
  const html = await render()
  assert.match(html, /上传参考图片/)
  assert.match(html, /src="\/image.png"/)
  assert.match(html, /场景、动作、台词和环境音/)
  assert.match(html, /2–30 秒/)
  assert.match(html, /9:16/)
  assert.doesNotMatch(html, /先确认音频|完整原音频时长|1–15 秒|准备动作底片|最终音轨保留已确认/)
  const button = html.match(/<button[^>]*>\s*生成创意短视频（真实调用）\s*<\/button>/)?.[0]
  assert.ok(button)
  assert.doesNotMatch(button, /disabled/)
})

for (const duration of [1, 31, 2.5, NaN]) test(`Wan时长${duration}秒阻断报价确认和提交`, async () => {
  seed(); store.thDurationSeconds = duration; installCost()
  let calls = 0
  globalThis.wanTestApi.submitTalkingHead = async () => { calls++; return {} }
  globalThis.wanTestApi.confirmTalkingHeadCost = async () => { calls++; return {} }
  await store.confirmTalkingHeadCost('WAN_3_0')
  await store.submitTalkingHead(false)
  assert.equal(calls, 0)
  assert.match(store.thInputError, /2–30 秒.*整数/)
})

test('Wan提示词不能为空，超长提示词不能提交', async () => {
  seed()
  installCost()
  let calls = 0
  globalThis.wanTestApi.confirmTalkingHeadCost = async () => { calls++; return {} }
  for (const text of ['  ', '文'.repeat(20001)]) {
    store.thPrompt = text
    await store.confirmTalkingHeadCost('WAN_3_0')
  }
  assert.equal(calls, 0)
  assert.match(store.error ?? store.notice?.text ?? '', /20000|提示词/)
})

test('Wan首次选择默认720p且必须走服务端费用确认，切换路线清理旧费用', async () => {
  seed('M2')
  installCost()
  store.thResolution = '1080p'
  globalThis.wanTestApi.confirmCost = async (id, route, ack) => {
    assert.deepEqual([id, route, ack], ['work', 'WAN_3_0', true])
    return { ok: true, message: '已选择' }
  }
  globalThis.wanTestApi.getTalkingHeadRoutes = async () => ({ ...store.thRoutes, selected_route: 'WAN_3_0', route_confirmed: true })
  globalThis.wanTestApi.getTalkingHeadPreflight = async () => ({ work_id: 'work', ok: true, blockers: [] })
  globalThis.wanTestApi.getTalkingHeadCost = async () => null
  await store.confirmRoute('WAN_3_0', true)
  assert.equal(store.currentRoute, 'WAN_3_0')
  assert.equal(store.thResolution, '720p')
  assert.equal(store.thValidConfirmedCost, null)
})

test('Wan参数修改立即撤销费用有效性并按新画质取回服务端报价', async () => {
  seed()
  installCost()
  let release
  globalThis.wanTestApi.getTalkingHeadCost = (id, route, prompt, resolution) => {
    assert.deepEqual([id, route, prompt, resolution], ['work', 'WAN_3_0', store.thPrompt, '1080p'])
    return new Promise(resolve => { release = resolve })
  }
  const refreshing = store.updateTalkingHeadOptions({ resolution: '1080p' })
  assert.equal(store.thValidConfirmedCost, null)
  assert.equal(store.thCost.estimate, null, '旧画质报价不可继续展示')
  await Promise.resolve()
  release({ ...store.thCost, estimate: { route: 'WAN_3_0', estimated_fen: 1440, ceiling_fen: 1440 }, confirmation_valid: false })
  await refreshing
  assert.equal(store.thCost.estimate.estimated_fen, 1440)
})

test('参数快速改变后迟到的旧报价不能覆盖新报价', async () => {
  seed(); installCost()
  const responses = []
  globalThis.wanTestApi.getTalkingHeadCost = () => new Promise(resolve => responses.push(resolve))
  const old = store.updateTalkingHeadOptions({ resolution: '480p' })
  const latest = store.updateTalkingHeadOptions({ resolution: '1080p' })
  await Promise.resolve()
  responses[1]({ ...store.thCost, estimate: { estimated_fen: 1440 } })
  await latest
  responses[0]({ ...store.thCost, estimate: { estimated_fen: 360 } })
  await old
  assert.equal(store.thCost.estimate.estimated_fen, 1440)
})

test('Wan未知任务只能核对原任务，不能通过旧重新生成入口绕过', async () => {
  seed(); installCost()
  store.thSubmissions = { work_id: 'work', runs: [{ id: 'wan-unknown', work_id: 'work', route: 'WAN_3_0', provider: 'dashscope_wan', model: 'wan3.0-video', state: 'unknown', remote_job_id: 'remote', is_mock: false }] }
  globalThis.wanTestApi.submitTalkingHead = async () => assert.fail('未知Wan任务不得再次生成')
  await store.submitTalkingHead(false, { replacementAuthorized: true })
  const html = await render()
  assert.match(html, /查询原任务/)
  assert.doesNotMatch(html, /<button[^>]*>\s*重新生成（真实调用，会再次计费）\s*<\/button>/)
})

test('相同远端task_id的Wan与Omni分别查询，轮询不重复提交', async () => {
  seed()
  const runs = ['dashscope_wan', 'volcengine'].map((provider, i) => ({ id: `run${i}`, work_id: 'work', provider, remote_job_id: 'same-task-id', state: 'running', is_mock: false }))
  store.thSubmissions = { work_id: 'work', runs }
  const queried = []
  globalThis.wanTestApi.pollRun = async id => { queried.push(id); return { state: 'running' } }
  globalThis.wanTestApi.getSubmissions = async () => ({ work_id: 'work', runs })
  await store.pollWaitingRuns()
  assert.deepEqual(queried, ['run0', 'run1'])
})

test('Omni60秒阻断不会意外阻止已选择M2的长音频', async () => {
  seed('M2', 60000); installCost()
  store.thCost.confirmation.call_list.prompt = ''
  store.thCost.confirmation.call_list.resolution = 'native'
  store.thCost.current_call_list = { ...store.thCost.confirmation.call_list }
  const html = await render()
  assert.match(html, /OmniHuman 仅支持小于 60 秒/)
  const button = html.match(/<button[^>]*>\s*生成口播（真实调用）\s*<\/button>/)?.[0]
  assert.ok(button)
  assert.doesNotMatch(button, /disabled/)
})

for (const duration of [2, 30]) test(`Wan ${duration} 秒合法边界无TTS提交单请求`, async () => {
  seed(); store.thDurationSeconds = duration; store.audios = []; installCost()
  let received
  globalThis.wanTestApi.submitTalkingHead = async (id, input) => { received = { id, input }; return { is_mock: true, submitted: false, note: '离线契约检查' } }
  await store.submitTalkingHead(false)
  assert.equal(received.id, 'work')
  assert.deepEqual(received.input, { route: 'WAN_3_0', prompt: store.thPrompt, resolution: '720p', duration_seconds: duration, dry_run: false })
})

test('报价读取暂时失败不阻止已有Wan作业和原片恢复', async () => {
  seed()
  const routes = { ...store.thRoutes }
  globalThis.wanTestApi.getTalkingHeadRoutes = async () => routes
  globalThis.wanTestApi.getTalkingHeadCost = async () => { throw new Error('cost temporarily unavailable') }
  for (const key of ['getTalkingHeadPreflight', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) globalThis.wanTestApi[key] = async () => ({ work_id: 'work' })
  globalThis.wanTestApi.getSubmissions = async () => ({ work_id: 'work', runs: [{ id: 'wan-run', work_id: 'work', provider: 'dashscope_wan', state: 'running', remote_job_id: 'remote' }] })
  globalThis.wanTestApi.getVideos = async () => [{ id: 'saved-video' }]
  globalThis.wanTestApi.getVideoGate = async () => ({})
  await store.loadTalkingHead()
  assert.equal(store.thSubmissions.runs[0].id, 'wan-run')
  assert.equal(store.videos[0].id, 'saved-video')
  assert.equal(store.thCost, null)
})

test('首次打开Wan恢复已确认的提示词和画质，不覆盖之后编辑', async () => {
  seed(); installCost()
  const cost = JSON.parse(JSON.stringify(store.thCost))
  cost.confirmation.call_list.resolution = '480p'
  cost.confirmation.call_list.generated_seconds = 30
  store.thPrompt = ''; store.thResolution = '1080p'; store.thCost = null
  const routes = { ...store.thRoutes }
  globalThis.wanTestApi.getTalkingHeadRoutes = async () => routes
  globalThis.wanTestApi.getTalkingHeadCost = async () => cost
  for (const key of ['getTalkingHeadPreflight', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) globalThis.wanTestApi[key] = async () => ({ work_id: 'work' })
  globalThis.wanTestApi.getSubmissions = async () => ({ work_id: 'work', runs: [] })
  globalThis.wanTestApi.getVideos = async () => []
  await store.loadTalkingHead()
  assert.equal(store.thResolution, '480p')
  assert.equal(store.thDurationSeconds, 30)
  assert.equal(store.thPrompt, cost.confirmation.call_list.prompt)
  store.thPrompt = '用户新输入'
  await store.loadTalkingHead()
  assert.equal(store.thPrompt, '用户新输入')
})

test('离线模式展示Wan声明但不能伪造音频、报价或已配置适配器', async () => {
  const routes = await module.mockApi.getTalkingHeadRoutes()
  const wan = routes.routes.find(route => route.route === 'WAN_3_0')
  assert.ok(wan)
  assert.equal(wan.provider, 'dashscope_wan')
  assert.equal(wan.verified_state, 'unverified')
  const work = await module.mockApi.createWork({ name: 'Wan test', topic: '', content_type: '', audience: '', keywords: [], target_duration_ms: 12000 })
  await assert.rejects(module.mockApi.confirmCost(work.id, 'WAN_3_0', false), /COST_NOTICE_REQUIRED/)
  await module.mockApi.confirmCost(work.id, 'WAN_3_0', true)
  const cost = await module.mockApi.getTalkingHeadCost(work.id, 'WAN_3_0', '自然讲述', '720p')
  assert.equal(cost.estimate, null)
  assert.equal(cost.confirmation_valid, false)
  assert.equal((await module.mockApi.getTalkingHeadAdapters()).dashscope_wan.state, 'not_configured')
  await assert.rejects(module.mockApi.confirmTalkingHeadCost(work.id, { route: 'WAN_3_0', prompt: '自然讲述', resolution: '720p' }), /离线模式/)
})

test('后端改变声音模式或摘要版本时旧费用确认必须失效', () => {
  seed(); installCost()
  store.thCost.confirmation_valid = false
  store.thCost.current_call_list.digest = 'new-server-contract'
  store.thCost.changed_fields = ['声音模式']
  assert.equal(store.thValidConfirmedCost, null)
  assert.ok(store.thCostChangedFields.includes('声音模式'))
})

test('恢复保存的Wan参数后重新读取匹配报价，空提示词旧响应不污染确认', async () => {
  seed(); installCost()
  const saved = JSON.parse(JSON.stringify(store.thCost)), routes = { ...store.thRoutes }
  store.thPrompt = ''; store.thResolution = '1080p'; store.thCost = null
  const queried = []
  globalThis.wanTestApi.getTalkingHeadRoutes = async () => routes
  globalThis.wanTestApi.getTalkingHeadCost = async (id, route, prompt, resolution) => {
    queried.push({ prompt, resolution })
    return prompt ? saved : { ...saved, current_call_list: null, confirmation_valid: false, blocker: { code: 'WAN_PROMPT_REQUIRED', message: '提示词为空' } }
  }
  for (const key of ['getTalkingHeadPreflight', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) globalThis.wanTestApi[key] = async () => ({ work_id: 'work' })
  globalThis.wanTestApi.getSubmissions = async () => ({ work_id: 'work', runs: [] })
  globalThis.wanTestApi.getVideos = async () => []
  await store.loadTalkingHead()
  assert.equal(queried.length, 2)
  assert.equal(store.thValidConfirmedCost.id, 'confirmed')
  assert.equal(store.thCost.blocker, undefined)
})

test('Wan优先使用扣除固定指令后的用户提示词额度，缺字段时保留官方上限', async () => {
  seed()
  const wan = store.thRoutes.routes.find(route => route.route === 'WAN_3_0')
  wan.declared.user_prompt_max_chars = 19940
  store.thPrompt = '文'.repeat(19941)
  assert.match(store.thInputError, /19940/)
  let confirmed = 0
  globalThis.wanTestApi.confirmTalkingHeadCost = async () => { confirmed++; return {} }
  await store.confirmTalkingHeadCost('WAN_3_0')
  assert.equal(confirmed, 0)
  const html = await render()
  assert.match(html, /19941 \/ 19940/)
  delete wan.declared.user_prompt_max_chars
  assert.equal(store.thInputError, '')
})

test('Wan缺工作空间时展示后端原因并禁止真实提交，仍可查报价和历史任务', async () => {
  seed(); installCost()
  store.thAdapters.wan = { provider: 'dashscope_wan', model: 'wan3.0-video', configured: false,
    state: 'not_configured', detail: '请配置 Wan 北京工作空间 ID；当前密钥不包含工作空间信息。' }
  let submitted = 0, confirmations = 0
  globalThis.wanTestApi.submitTalkingHead = async () => { submitted++; return { is_mock: false, submitted: false } }
  globalThis.wanTestApi.confirmTalkingHeadCost = async () => { confirmations++; return store.thCost.confirmation }
  globalThis.wanTestApi.getTalkingHeadCost = async () => store.thCost
  await store.confirmTalkingHeadCost('WAN_3_0')
  await store.submitTalkingHead(false)
  assert.equal(confirmations, 1)
  assert.equal(submitted, 0)
  const html = await render()
  assert.match(html, /请配置 Wan 北京工作空间 ID/)
  assert.match(html.match(/<button[^>]*>\s*生成创意短视频（真实调用）\s*<\/button>/)?.[0] ?? '', /disabled/)
  const run = { id: 'history-wan', work_id: 'work', provider: 'dashscope_wan', remote_job_id: 'original-id', state: 'running', is_mock: false }
  store.thSubmissions = { work_id: 'work', runs: [run] }
  const polled = []
  globalThis.wanTestApi.pollRun = async id => { polled.push(id); return { state: 'running' } }
  globalThis.wanTestApi.getSubmissions = async () => ({ work_id: 'work', runs: [run] })
  await store.pollWaitingRuns(run.id)
  assert.deepEqual(polled, ['history-wan'])
  assert.equal(submitted, 0)
})


test('Wan时长编辑立即撤销报价且迟到响应不能污染最新时长', async () => {
  seed(); installCost()
  const responses = []
  globalThis.wanTestApi.getTalkingHeadCost = (id, route, prompt, resolution, duration) => new Promise(resolve => responses.push({ duration, resolve }))
  const old = store.updateTalkingHeadOptions({ duration_seconds: 10 })
  assert.equal(store.thValidConfirmedCost, null)
  assert.equal(store.thCost.estimate, null)
  const latest = store.updateTalkingHeadOptions({ duration_seconds: 30 })
  await Promise.resolve()
  assert.deepEqual(responses.map(r => r.duration), [10, 30])
  responses[1].resolve({ ...store.thCost, estimate: { estimated_fen: 1800 } }); await latest
  responses[0].resolve({ ...store.thCost, estimate: { estimated_fen: 600 } }); await old
  assert.equal(store.thCost.estimate.estimated_fen, 1800)
  assert.equal(store.thDurationSeconds, 30)
})

test('Wan费用确认携带原生时长，Omni与M2不携带该参数', async () => {
  for (const route of ['WAN_3_0', 'OMNIHUMAN_1_5', 'M2']) {
    seed(route); installCost(); store.thDurationSeconds = 30
    let input
    globalThis.wanTestApi.confirmTalkingHeadCost = async (id, body) => { input = body; return store.thCost.confirmation }
    globalThis.wanTestApi.getTalkingHeadCost = async () => store.thCost
    await store.confirmTalkingHeadCost(route)
    assert.equal(input.duration_seconds, route === 'WAN_3_0' ? 30 : undefined)
  }
})

test('上传Wan图片后立即撤销旧费用并刷新素材状态，切换作品不污染新作品', async () => {
  seed(); installCost()
  const image = { ...store.wanImage, asset_id: 'new-image', asset_hash: 'new-hash' }
  globalThis.wanTestApi.uploadWanImage = async (id, file) => { assert.equal(id, 'work'); assert.equal(file.name, 'new.png'); return { source: image } }
  globalThis.wanTestApi.getTalkingHeadPreflight = async () => ({ work_id: 'work', ok: true, blockers: [] })
  globalThis.wanTestApi.getTalkingHeadCost = async () => null
  await store.uploadWanImage({ name: 'new.png' })
  assert.equal(store.wanImage.asset_id, 'new-image')
  assert.equal(store.thValidConfirmedCost, null)
  let release
  globalThis.wanTestApi.uploadWanImage = () => new Promise(resolve => { release = resolve })
  const uploading = store.uploadWanImage({ name: 'late.png' })
  store.currentWorkId = 'other'; store.workLoadVersion++
  release({ source: { ...image, asset_id: 'late' } }); await uploading
  assert.notEqual(store.wanImage?.asset_id, 'late')
})

test('原生视频使用观看确认与原声下载，隐藏数字人口型和TTS音轨表单', async () => {
  seed(); store.audios = []
  store.videos = [{ id: 'native', kind: 'original', generation_mode: 'image_prompt_native_audio', work_id: 'work', route: 'WAN_3_0', url: '/native.mp4', duration_ms: 30000, hard_failures: [], approval_state: 'pending', is_mock: false }]
  const html = await render()
  assert.match(html, /我已观看这条视频，确认画面、台词和声音符合预期/)
  assert.match(html, /保留原声并生成下载包/)
  assert.match(html, /不含自动字幕/)
  assert.doesNotMatch(html, /核对音轨保持|口型、身份、表情与穿帮/)
})

test('原生视频确认要求显式观看勾选并刷新交付状态', async () => {
  seed()
  const calls = []
  globalThis.wanTestApi.confirmWanNativeVideo = async (id, video) => { calls.push([id, video]); return { ok: true, video: { id: video }, package: { work_id: id, state: 'ready' } } }
  store.refreshPostprocess = async () => { calls.push('refresh') }
  store.loadWork = async id => { calls.push(['load', id]) }
  await store.confirmWanNativeVideo('native', false)
  assert.deepEqual(calls, [])
  await store.confirmWanNativeVideo('native', true)
  assert.deepEqual(calls, [['work', 'native'], ['load', 'work'], 'refresh'])
})


test('已选择Wan的作品从其他页面载入后仍引导至视频，不回退人物或TTS', async () => {
  seed(); store.audios = []; store.thRoutes = null
  assert.equal(store.nextStep.to, '/video')
  const html = await render(module.WorkflowGuide)
  assert.match(html, /上传参考图片/)
  assert.doesNotMatch(html, /先为这期作品选择人物|合成并试听全文/)
})

test('Wan状态加载不查询TTS音频门禁，并恢复当前源图', async () => {
  seed()
  const source = { ...store.wanImage, label: '保存的场景图' }
  const routes = store.thRoutes
  globalThis.wanTestApi.getTalkingHeadRoutes = async () => routes
  for (const name of ['getTalkingHeadPreflight', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) globalThis.wanTestApi[name] = async () => ({})
  globalThis.wanTestApi.getSubmissions = async () => ({ runs: [] })
  globalThis.wanTestApi.getVideos = async () => []
  globalThis.wanTestApi.getWanImage = async () => ({ source })
  let audioCalls = 0
  globalThis.wanTestApi.getAudioGate = async () => { audioCalls++; return {} }
  await store.loadTalkingHead()
  assert.equal(audioCalls, 0)
  assert.equal(store.thAudioGate, null)
  assert.equal(store.wanImage.label, source.label)
})


test('Wan HTTP图片上传保持原始二进制、图片类型与编码文件名', async () => {
  const source = { asset_id: 'image', preview_url: '/image.png' }
  const file = new File([new Uint8Array([1, 2, 3])], '场景 图片.png', { type: 'image/png' })
  let sent
  globalThis.fetch = async (url, init) => { sent = { url, init }; return new Response(JSON.stringify({ source })) }
  const result = await module.httpApi.uploadWanImage('work', file)
  assert.equal(sent.url, '/api/v1/works/work/wan/image')
  assert.equal(sent.init.method, 'POST')
  assert.equal(sent.init.body, file)
  assert.equal(sent.init.headers['Content-Type'], 'image/png')
  assert.equal(sent.init.headers['X-Filename'], encodeURIComponent(file.name))
  assert.deepEqual(result.source, source)
})

test('Wan HTTP报价发送生成秒数且不把该参数带入其他路线', async () => {
  const requests = []
  globalThis.fetch = async url => { requests.push(new URL(url, 'http://test')); return new Response('{}') }
  await module.httpApi.getTalkingHeadCost('work', 'WAN_3_0', '鸟鸣', '720p', 30)
  await module.httpApi.getTalkingHeadCost('work', 'OMNIHUMAN_1_5', '', '1080p', 30)
  assert.equal(requests[0].searchParams.get('duration_seconds'), '30')
  assert.equal(requests[1].searchParams.has('duration_seconds'), false)
})

test('Wan HTTP人工确认使用独立端点与明确观看确认', async () => {
  let sent
  globalThis.fetch = async (url, init) => { sent = { url, init }; return new Response('{"ok":true}') }
  await module.httpApi.confirmWanNativeVideo('work', 'native')
  assert.equal(sent.url, '/api/v1/works/work/wan/videos/native/confirm')
  assert.deepEqual(JSON.parse(sent.init.body), { actor_id: 'user_ui', review_ack: true })
})

test('历史Wan参考音频视频仍保留数字人口型与音轨审核入口', async () => {
  seed()
  store.videos = [{ id: 'legacy', kind: 'original', work_id: 'work', route: 'WAN_3_0', url: '/legacy.mp4', duration_ms: 5000, hard_failures: [], approval_state: 'pending', is_mock: false }]
  const html = await render()
  assert.match(html, /观看后审核这条原片/)
  assert.match(html, /核对音轨保持/)
  assert.doesNotMatch(html, /保留原声并生成下载包/)
})


test('原生视频确认后展示原声交付，不再要求制作字幕', async () => {
  seed(); store.audios = []
  store.thGate = { original_confirmed: true, original_count: 1, note: '原片已确认' }
  store.videos = [{ id: 'native', kind: 'original', generation_mode: 'image_prompt_native_audio', work_id: 'work', route: 'WAN_3_0', url: '/native.mp4', duration_ms: 5000, hard_failures: [], approval_state: 'approved', confirmed: true, is_mock: false }]
  store.pkg = { work_id: 'work', generation_mode: 'image_prompt_native_audio', state: 'ready', mode: 'real', download_url: '/package.zip', video_url: '/native.mp4' }
  const html = await render()
  assert.match(html, /download[^>]*>下载完整作品包/)
  assert.match(html, /原声视频已整理好/)
  assert.doesNotMatch(html, /可继续制作字幕与成片|生成带字幕的成片/)
})
