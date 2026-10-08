import assert from 'node:assert/strict'
import { beforeEach, after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { createPinia, setActivePinia } from 'pinia'

// Only the HTTP boundary is replaced. Tests exercise the real Pinia actions/getters.
const output = await mkdtemp(resolve('node_modules/.studio-tests-'))
await build({
  entryPoints: ['src/stores/studio.ts'], outfile: join(output, 'studio.mjs'),
  bundle: true, platform: 'node', format: 'esm', external: ['vue', 'pinia'],
  plugins: [{ name: 'test-api', setup(builder) {
    builder.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'test' }))
    builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
      contents: 'export const api = new Proxy({}, { get: (_, key) => globalThis.studioTestApi[key] });',
    }))
  } }],
})
const { useStudioStore } = await import(pathToFileURL(join(output, 'studio.mjs')).href)
after(() => rm(output, { recursive: true, force: true }))

const work = (id, extra = {}) => ({
  id, name: id, brief: { topic: id, materials: [] }, identity_id: null,
  voice_asset_id: null, script_version_id: null, final_audio_id: null,
  current_task_id: null, ...extra,
})
const audio = (id, workId, extra = {}) => ({
  id, work_id: workId, kind: 'full', confirmed: true, is_mock: false,
  voice_asset_id: 'voice', duration_ms: 60000, checks: [], ...extra,
})
let serverWorks
let store
beforeEach(() => {
  setActivePinia(createPinia())
  serverWorks = [work('one'), work('two')]
  globalThis.studioTestApi = {
    listWorks: async () => structuredClone(serverWorks),
    getScriptBatch: async () => null, listVariants: async () => [],
    getPackage: async () => null, listAudios: async () => [],
    listMotions: async () => null, listMasks: async () => null,
    listIdentityAssets: async () => [],
  }
  store = useStudioStore()
  store.works = structuredClone(serverWorks)
  for (const name of ['getTalkingHeadRoutes', 'getTalkingHeadPreflight', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getTalkingHeadGate', 'getAudioGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.studioTestApi[name] = async () => ({ work_id: 'one' })
  }
  globalThis.studioTestApi.getVideoGate = async id => ({ video_id: id, gate: { ok: false, outcome: 'review' } })
  globalThis.studioTestApi.getTalkingHeadRoutes = async () => ({ work_id: 'one',
    route_confirmed: store.thRoutes?.route_confirmed ?? false,
    selected_route: store.currentWork?.route ?? null, routes: [], price_snapshots: [] })
  // 假 api 要覆盖真 api 的表面，否则"端点不存在"会被误当成被测行为。
  // 默认给"没有进度快照"，需要续发场景的用例自行覆盖。
  globalThis.studioTestApi.getTalkingHeadDriver = async () => null
  globalThis.studioTestApi.resumeTalkingHeadDriver = async () => ({ state: 'IDLE', resumable: false, started: false })
})

test('返工后仍不可采用时接收回执并提示待重新审核', async () => {
  store.currentWorkId = 'one'
  store.scriptBatch = { id: 'old', candidates: [{ id: 'old-script', text: '旧稿' }], ready_to_present: true }
  globalThis.studioTestApi.repairScript = async () => ({ id: 'new-script', parent_id: 'old-script', ready_to_present: false })
  globalThis.studioTestApi.getScriptBatch = async () => ({ id: 'new', candidates: [], ready_to_present: false, retry_targets: [{ candidate_id: 'new-script', slot: 1, reason: '审核未通过' }] })
  const result = await store.repairScript('old-script')
  assert.equal(result.id, 'new-script')
  assert.deepEqual(store.scriptBatch.candidates, [])
  assert.equal(store.notice.kind, 'warn')
  assert.match(store.notice.text, /重新审核/)
  assert.doesNotMatch(store.notice.text, /三篇全部通过/)
})

test('返工目标稿可采用时即使整批部分通过也给出选稿提示', async () => {
  store.currentWorkId = 'one'
  const fixed = { id: 'new-script', parent_id: 'old-script', pickable: true }
  globalThis.studioTestApi.repairScript = async () => fixed
  globalThis.studioTestApi.getScriptBatch = async () => ({ id: 'new', ready_to_present: false,
    candidates: [fixed, { id: 'failed-2', pickable: false }, { id: 'failed-3', pickable: false }] })
  const result = await store.repairScript('old-script')
  assert.equal(result.id, fixed.id)
  assert.equal(store.notice.kind, 'ok')
  assert.match(store.notice.text, /返工稿已通过审核，可以采用/)
  assert.doesNotMatch(store.notice.text, /三篇均已通过|三篇全部通过/)
})

test('其他候选可采用也不会把未通过返工目标标为已通过', async () => {
  store.currentWorkId = 'one'
  const fixed = { id: 'new-script', parent_id: 'old-script', pickable: false }
  globalThis.studioTestApi.repairScript = async () => fixed
  globalThis.studioTestApi.getScriptBatch = async () => ({ id: 'new', ready_to_present: false,
    candidates: [fixed, { id: 'passed', pickable: true }] })
  await store.repairScript('old-script')
  assert.equal(store.notice.kind, 'warn')
  assert.match(store.notice.text, /重新审核/)
})

test('部分通过批次已采用稿件可进入配音或分镜制作下一步', () => {
  store.currentWorkId = 'one'
  Object.assign(store.works[0], { identity_id: 'person', script_version_id: 'passed' })
  store.readinessMap = { person: { ready: true, reasons: [] } }
  store.scriptBatch = { id: 'partial', ready_to_present: false, passed: 1,
    candidates: [{ id: 'passed', pickable: true }, { id: 'failed-2', pickable: false }, { id: 'failed-3', pickable: false }] }
  assert.equal(store.scriptReady, true)
  assert.equal(store.nextStep.key, 'tts')
  assert.equal(store.nextStep.to, '/tts')
  store.works[0].brief.content_route = 'short_video_storyboard'
  assert.equal(store.nextStep.key, 'video')
  assert.equal(store.nextStep.to, '/video')
})

test('稿件明确不可采用和历史批次明确未就绪时仍挡制作下一步', () => {
  store.currentWorkId = 'one'
  Object.assign(store.works[0], { script_version_id: 'selected', brief: { content_route: 'short_video_storyboard' } })
  for (const [candidate, ready] of [[{ id: 'selected', pickable: false }, true], [{ id: 'selected', pickable: false }, false], [{ id: 'selected' }, false]]) {
    store.scriptBatch = { candidates: [candidate], ready_to_present: ready }
    assert.equal(store.scriptReady, false)
    assert.equal(store.nextStep.key, 'script')
    assert.equal(store.nextStep.to, '/script')
    assert.doesNotMatch(store.nextStep.detail, /三篇.*均通过审核后/)
  }
})

test('历史已采用稿件省略新审核字段时保持原有兼容', () => {
  store.currentWorkId = 'one'
  store.works[0].script_version_id = 'historical'
  store.scriptBatch = { candidates: [{ id: 'historical' }] }
  assert.equal(store.scriptReady, true)
  store.scriptBatch.ready_to_present = true
  assert.equal(store.scriptReady, true)
  store.works[0].script_version_id = null
  assert.equal(store.scriptReady, false)
})

test('opening a work restores its persisted final audio and current bindings', async () => {
  serverWorks[0].final_audio_id = 'saved'
  globalThis.studioTestApi.listAudios = async id => [audio('saved', id)]
  await store.selectWork('one')
  assert.equal(store.currentWork.final_audio_id, 'saved')
  assert.equal(store.confirmedAudio?.id, 'saved')
})

test('switching works removes the previous audio, video, price and package before loading', async () => {
  store.currentWorkId = 'one'
  store.audios = [audio('old', 'one')]
  store.videos = [{ id: 'video-old', work_id: 'one', kind: 'original', confirmed: true }]
  store.thCost = { confirmation: { id: 'old-price' } }
  store.pkg = { id: 'old-package' }
  let release
  globalThis.studioTestApi.getScriptBatch = () => new Promise(r => { release = r })
  const loading = store.selectWork('two')
  assert.equal(store.confirmedAudio, null)
  assert.equal(store.finalVideo, null)
  assert.equal(store.thConfirmedCost, null)
  assert.equal(store.pkg, null)
  release(null)
  await loading
})

test('a slower previous work request cannot overwrite the newly selected work', async () => {
  const pending = {}
  globalThis.studioTestApi.getScriptBatch = id => new Promise(r => { pending[id] = r })
  const first = store.selectWork('one')
  const second = store.selectWork('two')
  pending.two({ id: 'batch-two', candidates: [] })
  await second
  pending.one({ id: 'batch-one', candidates: [] })
  await first
  assert.equal(store.currentWorkId, 'two')
  assert.equal(store.scriptBatch.id, 'batch-two')
})

test('choosing a person uses all server bindings including its default voice', async () => {
  store.currentWorkId = 'one'
  globalThis.studioTestApi.selectIdentity = async () => {
    serverWorks[0] = work('one', { identity_id: 'person', voice_asset_id: 'default-voice' })
    return structuredClone(serverWorks[0])
  }
  await store.selectIdentity('person')
  assert.equal(store.currentWork.voice_asset_id, 'default-voice')
})

test('changing voice removes invalidated final audio and old delivery state', async () => {
  store.currentWorkId = 'one'
  store.audios = [audio('old', 'one')]
  store.pkg = { id: 'old-package' }
  globalThis.studioTestApi.selectVoice = async () => {
    serverWorks[0].voice_asset_id = 'new-voice'
    return structuredClone(serverWorks[0])
  }
  await store.selectVoice('new-voice')
  assert.equal(store.confirmedAudio, null)
  assert.equal(store.pkg, null)
  assert.equal(store.currentWork.voice_asset_id, 'new-voice')
})

test('voice defaults initialise controls and unsaved changes block the final audio', () => {
  store.currentWorkId = 'one'
  store.works[0].voice_asset_id = 'voice'
  store.voices = [{ id: 'voice', synthesis_profile: { emotion: 'calm', speed: 1.1, vol: 1, pitch: 0 } }]
  store.audios = [audio('old', 'one')]
  assert.equal(store.savedTtsControls?.emotion, 'calm')
  assert.equal(store.savedTtsControls?.speed, 1.1)
  store.ttsControlsDraft = { ...store.savedTtsControls, speed: 1.25 }
  assert.equal(store.ttsControlsDirty, true)
  assert.equal(store.confirmedAudio, null)
})

test('未显式设置情绪的新克隆音色继承后端平静默认，候选试听按候选音色核参数', () => {
  store.currentWorkId = 'one'
  store.works[0].voice_asset_id = 'selected'
  store.voices = [
    { id: 'selected', synthesis_profile: { emotion: 'happy', speed: 1.2 } },
    { id: 'clone', synthesis_profile: { speed: 1, vol: 1, pitch: 0 } },
  ]
  const preview = audio('preview', 'one', { kind: 'preview', voice_asset_id: 'clone', synthesis_profile: { emotion: 'calm', speed: 1, vol: 1, pitch: 0 } })
  assert.equal(store.audioMatchesTtsControls(preview), true)
  store.works[0].voice_asset_id = 'clone'
  assert.equal(store.savedTtsControls.emotion, 'calm')
  store.works[0].tts_controls = { emotion: 'auto' }
  assert.equal(store.savedTtsControls.emotion, 'auto')
})

test('saving controls restores authoritative invalidation and clears a draft', async () => {
  store.currentWorkId = 'one'
  store.audios = [audio('old', 'one')]
  store.pkg = { id: 'old-package' }
  const controls = { emotion: 'auto', speed: 1.25, vol: 1, pitch: 0, voice_modify: { pitch: 0, intensity: 0, timbre: 0 } }
  store.ttsControlsDraft = controls
  globalThis.studioTestApi.updateTtsControls = async (id, value) => {
    assert.equal(id, 'one')
    assert.deepEqual(value, controls)
    serverWorks[0].tts_controls = value
    serverWorks[0].final_audio_id = null
    return structuredClone(serverWorks[0])
  }
  assert.equal(typeof store.saveTtsControls, 'function')
  assert.equal(await store.saveTtsControls(), true)
  assert.equal(store.confirmedAudio, null)
  assert.equal(store.pkg, null)
  assert.equal(store.ttsControlsDirty, false)
  assert.equal(store.savedTtsControls.speed, 1.25)
})

test('unsaved controls cannot submit previews, full audio, or old audio confirmation', async () => {
  store.currentWorkId = 'one'
  store.ttsControlsDraft = { emotion: 'happy', speed: 1.5, vol: 1, pitch: 0, voice_modify: { pitch: 0, intensity: 0, timbre: 0 } }
  let requests = 0
  for (const key of ['createVoicePreview', 'synthesizeFullAudio', 'confirmAudio', 'submitTalkingHead', 'submitTask']) {
    globalThis.studioTestApi[key] = async () => { requests++; return null }
  }
  await store.voicePreview('voice', '试听文本')
  await store.synthesizeFull()
  await store.confirmAudio('old')
  await store.submitTalkingHead(true)
  await store.submitVideo()
  assert.equal(requests, 0)
  assert.match(store.error ?? '', /应用.*参数/)
})

test('invalidated audio and audio using different controls cannot be the final audio', () => {
  store.currentWorkId = 'one'
  store.works[0].tts_controls = { emotion: 'happy', speed: 1.2, vol: 1, pitch: 0 }
  store.audios = [audio('old', 'one', { invalidated: true })]
  assert.equal(store.confirmedAudio, null)
  store.audios = [audio('different', 'one', { synthesis_profile: { emotion: 'calm', speed: 1, vol: 1, pitch: 0 } })]
  assert.equal(store.confirmedAudio, null)
})

test('改回相同参数也不能重新确认旧参数版本的音轨', () => {
  store.currentWorkId = 'one'
  store.works[0].tts_controls_revision = 2
  store.audios = [audio('old', 'one', { tts_controls_revision: 0 })]
  assert.equal(store.confirmedAudio, null)
  store.audios = [audio('current', 'one', { tts_controls_revision: 2 })]
  assert.equal(store.confirmedAudio?.id, 'current')
})

test('a late controls save cannot restore the previous work draft or audio', async () => {
  store.currentWorkId = 'one'
  store.ttsControlsDraft = { emotion: 'happy', speed: 1.2, vol: 1, pitch: 0, voice_modify: { pitch: 0, intensity: 0, timbre: 0 } }
  let finish
  globalThis.studioTestApi.updateTtsControls = () => new Promise(resolve => { finish = resolve })
  assert.equal(typeof store.saveTtsControls, 'function')
  const saving = store.saveTtsControls()
  await store.selectWork('two')
  finish({ ...serverWorks[0], tts_controls: { emotion: 'happy', speed: 1.2 } })
  assert.equal(await saving, false)
  assert.equal(store.currentWorkId, 'two')
  assert.equal(store.ttsControlsDraft, null)
  assert.equal(store.confirmedAudio, null)
})

test('克隆音色首次试听后同步后端激活状态，保留已返回音频', async () => {
  store.currentWorkId = 'one'
  store.voices = [{ id: 'clone', name: '本人', clone_metadata: { activation_state: 'pending' } }]
  globalThis.studioTestApi.createVoicePreview = async () => ({ cached: false, audio: audio('preview', 'one', { kind: 'preview', confirmed: false }) })
  globalThis.studioTestApi.listVoices = async () => [{ id: 'clone', name: '本人', clone_metadata: { activation_state: 'active' } }]
  await store.voicePreview('clone', '试听文本')
  assert.equal(store.voices[0].clone_metadata.activation_state, 'active')
  assert.equal(store.audios[0].id, 'preview')
})

test('只有用户明确重新试听才请求新的供应商调用', async () => {
  store.currentWorkId = 'one'
  const requested = []
  globalThis.studioTestApi.createVoicePreview = async (_work, _voice, _text, forceNew) => {
    requested.push(forceNew)
    return { cached: false, audio: audio('preview', 'one', { kind: 'preview', confirmed: false }) }
  }
  await store.voicePreview('voice', '试听文本')
  await store.voicePreview('voice', '试听文本', true)
  assert.deepEqual(requested, [false, true])
})

test('a confirmed audio from another work or an obsolete script cannot advance the guide', () => {
  store.currentWorkId = 'one'
  store.works[0].script_version_id = 'script-new'
  store.audios = [audio('other', 'two'), audio('old', 'one', { script_version_id: 'script-old' })]
  assert.equal(store.confirmedAudio, null)
})

test('completed MVP directs the user to download instead of the unavailable publish page', () => {
  store.currentWorkId = 'one'
  Object.assign(store.works[0], { identity_id: 'person', script_version_id: 'script' })
  store.readinessMap = { person: { ready: true, reasons: [] } }
  store.scriptBatch = { candidates: [{ id: 'script' }] }
  store.audios = [audio('final', 'one', { script_version_id: 'script' })]
  store.pkg = { id: 'package', work_id: 'one', state: 'ready', mode: 'production', download_url: '/download' }
  assert.equal(store.nextStep.to, '/video')
  assert.equal(store.nextStep.key, 'download')
})

test('an already selected person with incomplete readiness keeps the guide on the person step', () => {
  store.currentWorkId = 'one'
  store.works[0].identity_id = 'person'
  store.readinessMap = { person: { ready: false, reasons: ['缺少参考图审核'] } }
  assert.equal(store.nextStep.to, '/avatar')
  assert.match(store.nextStep.detail, /缺少参考图审核/)
})

test('late video status from a previous work cannot overwrite current video state', async () => {
  store.currentWorkId = 'one'
  let release
  globalThis.studioTestApi.getTalkingHeadRoutes = () => new Promise(r => { release = r })
  for (const name of ['getTalkingHeadPreflight', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getSubmissions', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.studioTestApi[name] = async () => ({ work_id: 'one' })
  }
  globalThis.studioTestApi.getVideos = async () => []
  const pending = store.loadTalkingHead()
  store.currentWorkId = 'two'
  release({ work_id: 'one' })
  await pending
  assert.equal(store.thRoutes, null)
  assert.equal(store.thPreflight, null)
})

test('failed work creation returns null without selecting a different work', async () => {
  globalThis.studioTestApi.createWork = async () => { throw new Error('temporarily unavailable') }
  const created = await store.createWork({ name: 'new', topic: 'new' })
  assert.equal(created, null)
  assert.equal(store.currentWorkId, null)
  assert.equal(store.error, 'temporarily unavailable')
})

test('删除当前作品选择下一件并清除旧成片状态', async () => {
  store.currentWorkId = 'one'
  store.audios = [audio('old', 'one')]
  store.pkg = { id: 'old-package', work_id: 'one' }
  globalThis.studioTestApi.deleteWork = async id => {
    serverWorks = serverWorks.filter(item => item.id !== id)
    return { id, deleted_at: '2026-10-05T00:00:00Z' }
  }
  assert.equal(typeof store.deleteWork, 'function')
  assert.equal(await store.deleteWork('one'), true)
  assert.equal(store.currentWorkId, 'two')
  assert.deepEqual(store.works.map(item => item.id), ['two'])
  assert.equal(store.pkg, null)
  assert.equal(store.confirmedAudio, null)
  assert.equal(store.notice.kind, 'ok')
})

test('删除最后作品清空记忆选择并使旧异步请求失效', async () => {
  const entries = new Map([['avatar-studio:last-work', 'one']])
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: key => entries.get(key), setItem: (key, value) => entries.set(key, value), removeItem: key => entries.delete(key) } })
  store.works = serverWorks = [work('one')]
  store.currentWorkId = 'one'
  let release
  globalThis.studioTestApi.getScriptBatch = () => new Promise(resolve => { release = resolve })
  const loading = store.loadWork('one')
  globalThis.studioTestApi.deleteWork = async id => {
    serverWorks = []
    return { id, deleted_at: '2026-10-05T00:00:00Z' }
  }
  assert.equal(typeof store.deleteWork, 'function')
  assert.equal(await store.deleteWork('one'), true)
  release({ id: 'late-batch', candidates: [] })
  await loading
  assert.equal(store.currentWorkId, null)
  assert.equal(store.currentWork, null)
  assert.deepEqual(store.works, [])
  assert.equal(store.scriptBatch, null)
  assert.equal(entries.has('avatar-studio:last-work'), false)
  assert.equal(store.busy, '')
})

test('删除失败保留作品和当前状态并返回服务端原因', async () => {
  store.currentWorkId = 'one'
  store.pkg = { id: 'keep' }
  globalThis.studioTestApi.deleteWork = async () => { throw new Error('外部任务结果未知，请先查询') }
  assert.equal(typeof store.deleteWork, 'function')
  assert.equal(await store.deleteWork('one'), false)
  assert.equal(store.currentWorkId, 'one')
  assert.equal(store.pkg.id, 'keep')
  assert.equal(store.works.length, 2)
  assert.equal(store.error, '外部任务结果未知，请先查询')
})

test('删除非当前作品保持当前作品的衍生状态', async () => {
  store.currentWorkId = 'two'
  store.pkg = { id: 'keep' }
  const version = store.workLoadVersion
  globalThis.studioTestApi.deleteWork = async id => ({ id, deleted_at: '2026-10-05T00:00:00Z' })
  assert.equal(typeof store.deleteWork, 'function')
  assert.equal(await store.deleteWork('one'), true)
  assert.equal(store.currentWorkId, 'two')
  assert.equal(store.pkg.id, 'keep')
  assert.equal(store.workLoadVersion, version)
})

test('删除期间的旧作品列表响应不能恢复已删除作品', async () => {
  store.currentWorkId = 'two'
  const stale = structuredClone(serverWorks)
  let release
  globalThis.studioTestApi.listWorks = () => new Promise(resolve => { release = resolve })
  const loading = store.loadWork('two')
  globalThis.studioTestApi.deleteWork = async id => ({ id, deleted_at: '2026-10-05T00:00:00Z' })
  await store.deleteWork('one')
  release(stale)
  await loading
  assert.deepEqual(store.works.map(item => item.id), ['two'])
})

test('人物删除成功后旧素材列表响应不能恢复已删除人物', async () => {
  store.identities = [{ id: 'person' }]
  let release
  let reads = 0
  globalThis.studioTestApi.listIdentities = async () => {
    if (++reads === 1) return new Promise(resolve => { release = resolve })
    return []
  }
  globalThis.studioTestApi.identitiesReadiness = async () => ({ person: { ready: true } })
  const loading = store.reloadIdentities()
  globalThis.studioTestApi.deleteIdentity = async id => ({ id, deleted_at: '2026-10-05T00:00:00Z' })
  await store.deleteIdentity('person')
  release([{ id: 'person' }])
  await loading
  assert.deepEqual(store.identities, [])
  assert.equal(store.readinessMap.person, undefined)
})

test('人物删除失败保留素材库，成功刷新人物与就绪列表', async () => {
  store.identities = [{ id: 'person' }, { id: 'keep' }]
  store.discardedIdentities = [{ id: 'person' }]
  store.readinessMap = { person: { ready: true } }
  globalThis.studioTestApi.deleteIdentity = async () => { throw new Error('仍被作品「测试一期」引用') }
  assert.equal(typeof store.deleteIdentity, 'function')
  assert.equal(await store.deleteIdentity('person'), false)
  assert.equal(store.identities.length, 2)
  assert.equal(store.error, '仍被作品「测试一期」引用')
  globalThis.studioTestApi.deleteIdentity = async id => ({ id, deleted_at: '2026-10-05T00:00:00Z' })
  globalThis.studioTestApi.listIdentities = async () => [{ id: 'keep' }]
  globalThis.studioTestApi.identitiesReadiness = async () => ({ keep: { ready: true } })
  assert.equal(await store.deleteIdentity('person'), true)
  assert.deepEqual(store.identities.map(item => item.id), ['keep'])
  assert.deepEqual(store.discardedIdentities, [])
  assert.equal(store.readinessMap.person, undefined)
})

test('starting postprocess binds the returned task to the selected work', async () => {
  store.currentWorkId = 'one'
  globalThis.studioTestApi.startPostprocess = async () => ({ id: 'post-task', work_id: 'one', state: 'queued' })
  globalThis.studioTestApi.getPostprocess = async () => ({ task: { id: 'post-task', work_id: 'one', state: 'queued' }, ready_for_review: false })
  await store.startPostprocess()
  assert.equal(store.task.id, 'post-task')
  assert.equal(store.currentWork.current_task_id, 'post-task')
})

test('a late postprocess response cannot attach a preview to another work', async () => {
  store.currentWorkId = 'one'
  let release
  globalThis.studioTestApi.getPostprocess = () => new Promise(r => { release = r })
  const pending = store.refreshPostprocess()
  store.currentWorkId = 'two'
  release({ preview_url: '/one.mp4', ready_for_review: true })
  await pending
  assert.equal(store.postprocess, null)
})

test('full audio defaults to reuse and only explicitly requests a new paid synthesis', async () => {
  store.currentWorkId = 'one'
  const requested = []
  globalThis.studioTestApi.synthesizeFullAudio = async (_id, forceNew) => {
    requested.push(forceNew)
    return { audio: audio('synthesized', 'one', { confirmed: false }), is_mock: true, note: 'test' }
  }
  await store.synthesizeFull()
  await store.synthesizeFull(true)
  assert.deepEqual(requested, [false, true])
})

test('a package belonging to another work never completes the current guide', () => {
  store.currentWorkId = 'two'
  store.pkg = { work_id: 'one', state: 'ready', mode: 'production', download_url: '/one.zip' }
  assert.equal(store.deliveryReady, false)
})

test('late task polling cannot attach a previous work task or package', async () => {
  store.currentWorkId = 'one'
  store.works[0].current_task_id = 'task-one'
  let release
  globalThis.studioTestApi.getTask = () => new Promise(r => { release = r })
  globalThis.studioTestApi.getTaskEvents = async () => [{ id: 'old-event' }]
  globalThis.studioTestApi.getPackage = async id => id === 'one' ? { work_id: 'one', state: 'ready' } : null
  const pending = store.refreshTask()
  await store.selectWork('two')
  release({ id: 'task-one', work_id: 'one', state: 'succeeded' })
  await pending
  assert.equal(store.task, null)
  assert.deepEqual(store.events, [])
  assert.equal(store.pkg, null)
})

test('late task events cannot attach after the same work starts a replacement task', async () => {
  store.currentWorkId = 'one'
  store.works[0].current_task_id = 'task-one'
  globalThis.studioTestApi.getTask = async () => ({ id: 'task-one', work_id: 'one', state: 'running' })
  let release
  globalThis.studioTestApi.getTaskEvents = () => new Promise(r => { release = r })
  const pending = store.refreshTask()
  await new Promise(r => setImmediate(r))
  store.works[0].current_task_id = 'task-new'
  store.task = { id: 'task-new', work_id: 'one', state: 'running' }
  release([{ id: 'old-event' }])
  await pending
  assert.deepEqual(store.events, [])
  assert.equal(store.task.id, 'task-new')
})

test('postprocess response from a previous visit cannot overwrite a newer visit to the same work', async () => {
  store.currentWorkId = 'one'
  let release
  globalThis.studioTestApi.getPostprocess = () => new Promise(r => { release = r })
  const pending = store.refreshPostprocess()
  await store.selectWork('two')
  await store.selectWork('one')
  release({ preview_url: '/obsolete.mp4', ready_for_review: true })
  await pending
  assert.equal(store.postprocess, null)
})

test('late audio synthesis cannot populate the newly selected work', async () => {
  store.currentWorkId = 'one'
  let release
  globalThis.studioTestApi.synthesizeFullAudio = () => new Promise(r => { release = r })
  const pending = store.synthesizeFull()
  await store.selectWork('two')
  release({ audio: audio('old', 'one'), is_mock: true, note: 'old audio' })
  await pending
  assert.deepEqual(store.audios, [])
  assert.notEqual(store.notice?.text, 'old audio')
})

test('adopting a motion immediately displays the exact persisted reference', async () => {
  store.currentWorkId = 'one'
  store.works[0].identity_id = 'person'
  globalThis.studioTestApi.setMotionIntent = async (id, input) => ({ work_id: id, motion_intent: { ...input, params: {} } })
  globalThis.studioTestApi.getTalkingHeadPreflight = async () => null
  await store.adoptMotion('smile', 'ref-second')
  assert.equal(store.currentWork.motion_intent?.motion_ref_id, 'ref-second')
})

test('a late motion adoption cannot affect a new person selected in the same work', async () => {
  store.currentWorkId = 'one'
  store.works[0].identity_id = 'person-old'
  let release
  globalThis.studioTestApi.setMotionIntent = () => new Promise(r => { release = r })
  let preflights = 0
  globalThis.studioTestApi.getTalkingHeadPreflight = async () => { preflights++; return {} }
  const pending = store.adoptMotion('smile', 'old-ref')
  store.works[0].identity_id = 'person-new'
  store.works[0].motion_intent = { motion_key: 'smile', motion_ref_id: 'new-ref' }
  store.notice = { kind: 'ok', text: 'new-person' }
  release({ work_id: 'one', motion_intent: { motion_key: 'smile', motion_ref_id: 'old-ref', params: {} } })
  await pending
  assert.equal(store.currentWork.motion_intent.motion_ref_id, 'new-ref')
  assert.equal(store.notice.text, 'new-person')
  assert.equal(preflights, 0)
})

test('生成底片成功后重新读取口播门禁，避免仍显示旧阻断', async () => {
  store.currentWorkId = 'one'
  store.thPreflight = { work_id: 'one', ok: false, blockers: [{ code: 'OLD_BLOCKER' }] }
  globalThis.studioTestApi.createPlate = async () => ({ registered: true, note: '底片已生成' })
  for (const name of ['getTalkingHeadRoutes', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getSubmissions', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.studioTestApi[name] = async () => ({ work_id: 'one' })
  }
  globalThis.studioTestApi.getTalkingHeadPreflight = async () => ({ work_id: 'one', ok: false, blockers: [{ code: 'PLATE_NEEDS_REVIEW' }] })
  globalThis.studioTestApi.getVideos = async () => []
  await store.createPlate(46.428)
  assert.deepEqual(store.thPreflight.blockers, [{ code: 'PLATE_NEEDS_REVIEW' }])
})

test('显式选择底片变体后同步后端作品绑定和版本', async () => {
  store.currentWorkId = 'one'
  globalThis.studioTestApi.createPlate = async () => {
    Object.assign(serverWorks[0], { variant_id: 'selected-variant', version: 4 })
    return { registered: true, note: '底片已生成' }
  }
  for (const name of ['getTalkingHeadRoutes', 'getTalkingHeadPreflight', 'getTalkingHeadCost', 'getSegmentPlan', 'getPlateStatus', 'getSubmissions', 'getTalkingHeadGate', 'getTalkingHeadQueue', 'getTalkingHeadAdapters']) {
    globalThis.studioTestApi[name] = async () => ({ work_id: 'one' })
  }
  globalThis.studioTestApi.getVideos = async () => []
  await store.createPlate(46.428, 'selected-variant')
  assert.equal(store.currentWork.variant_id, 'selected-variant')
  assert.equal(store.currentWork.version, 4)
})

test('云端口播任务成功后自动回收原片，不停留在等待提交', async () => {
  store.currentWorkId = 'one'
  const run = { id: 'run-1', work_id: 'one', route: 'M2', model: 'MuseTalk-1.5', state: 'waiting_provider', remote_job_id: 'remote-1', result_asset_ids: [], is_mock: false }
  store.thSubmissions = { work_id: 'one', runs: [run], auto_fallback: false, note: '等待查询' }
  let polled = 0
  globalThis.studioTestApi.pollRun = async id => {
    polled++
    assert.equal(id, 'run-1')
    return { state: 'succeeded', run: { ...run, state: 'succeeded', result_asset_ids: ['original-1'] }, video: { id: 'original-1', work_id: 'one', kind: 'original' } }
  }
  globalThis.studioTestApi.getSubmissions = async () => ({ work_id: 'one', runs: [{ ...run, state: 'succeeded', result_asset_ids: ['original-1'] }], auto_fallback: false, note: '已完成' })
  globalThis.studioTestApi.getVideos = async () => [{ id: 'original-1', work_id: 'one', kind: 'original', approval_state: 'pending' }]
  await store.pollWaitingRuns()
  assert.equal(polled, 1)
  assert.equal(store.thSubmissions.runs[0].state, 'succeeded')
  assert.equal(store.videos[0].id, 'original-1')
  assert.equal(store.videoGates['original-1'].gate.outcome, 'review')
})

const waitingRun = (id, extra = {}) => ({ id, work_id: 'one', route: 'M2', provider: 'm2_worker', state: 'waiting_provider', remote_job_id: `remote-${id}`, is_mock: false, ...extra })

function installCost(route = 'OMNIHUMAN_1_5', prompt = '自然讲述', resolution = '1080p') {
  store.currentWorkId = 'one'
  store.works[0].route = route
  store.thRoutes = { route_confirmed: true, selected_route: route, routes: [], price_snapshots: [] }
  store.thPrompt = prompt
  store.thResolution = resolution
  const listing = { route, audio_id: 'audio', audio_hash: 'hash', variant_id: null,
    motion_ref_id: null, plate_id: null, prompt: route === 'M2' ? '' : prompt,
    resolution: route === 'M2' ? 'native' : resolution, segment_count: 1, digest: 'confirmed-digest' }
  store.thCost = { work_id: 'one', route, confirmation_valid: true, changed_fields: [],
    current_call_list: { ...listing }, confirmation: { id: 'cost', work_id: 'one', route,
      request_digest: listing.digest, call_list: { ...listing }, estimated_fen: 1200, ceiling_fen: 1440 } }
}

test('修改动作描述立即使费用确认失效，并阻止真实提交', async () => {
  installCost()
  assert.equal(store.thValidConfirmedCost?.id, 'cost')
  store.thPrompt = '挥手后自然讲述'
  assert.equal(store.thValidConfirmedCost, null)
  assert.ok(store.thCostChangedFields.includes('动作描述'))
  globalThis.studioTestApi.submitTalkingHead = async () => assert.fail('过期确认不能提交收费请求')
  await store.submitTalkingHead(false)
  assert.match(store.notice?.text ?? '', /重新确认/)
  assert.equal(store.thConfirmedCost.id, 'cost', '历史确认仍可查看')
})

test('修改分辨率立即失效，改回已确认输入后恢复', () => {
  installCost()
  store.thResolution = '720p'
  assert.equal(store.thValidConfirmedCost, null)
  assert.ok(store.thCostChangedFields.includes('分辨率'))
  store.thResolution = '1080p'
  assert.equal(store.thValidConfirmedCost?.id, 'cost')
})

test('重新确认使用当前参数，新的确认恢复可提交状态', async () => {
  installCost()
  store.thPrompt = '新的动作'
  const requested = []
  globalThis.studioTestApi.confirmTalkingHeadCost = async (id, input) => {
    requested.push(input)
    const listing = { ...store.thCost.current_call_list, ...input, digest: 'new-digest' }
    return { ...store.thConfirmedCost, id: 'new-cost', request_digest: listing.digest, call_list: listing }
  }
  globalThis.studioTestApi.getTalkingHeadCost = async (id, route, prompt, resolution) => {
    assert.deepEqual([id, route, prompt, resolution], ['one', 'OMNIHUMAN_1_5', '新的动作', '1080p'])
    const listing = { ...store.thCost.current_call_list, prompt, resolution, digest: 'new-digest' }
    return { ...store.thCost, current_call_list: listing, confirmation_valid: true,
      confirmation: { ...store.thConfirmedCost, id: 'new-cost', request_digest: listing.digest, call_list: listing } }
  }
  await store.confirmTalkingHeadCost('OMNIHUMAN_1_5')
  assert.equal(requested[0].prompt, '新的动作')
  assert.equal(store.thValidConfirmedCost?.id, 'new-cost')
})

test('切换作品不能沿用另一作品的费用确认', () => {
  installCost()
  store.currentWorkId = 'two'
  store.works[1].route = 'OMNIHUMAN_1_5'
  assert.equal(store.thValidConfirmedCost, null)
})

test('M2 忽略不会提交给模型的动作描述与输出分辨率', () => {
  installCost('M2')
  store.thPrompt = '不属于 M2 的文本'
  store.thResolution = '720p'
  assert.equal(store.thValidConfirmedCost?.id, 'cost')
})

test('旧记录没有清单快照时使用服务端有效性和本次查询参数', () => {
  installCost()
  delete store.thCost.confirmation.call_list
  assert.equal(store.thValidConfirmedCost?.id, 'cost')
  store.thPrompt = '变化'
  assert.equal(store.thValidConfirmedCost, null)
})

test('首次加载恢复已确认的动作参数，后续加载不覆盖用户编辑', async () => {
  installCost()
  const response = JSON.parse(JSON.stringify(store.thCost))
  store.thCost = null
  store.thPrompt = ''
  store.thResolution = '1080p'
  globalThis.studioTestApi.getTalkingHeadCost = async () => response
  globalThis.studioTestApi.getSubmissions = async () => ({ runs: [] })
  globalThis.studioTestApi.getVideos = async () => []
  await store.loadTalkingHead()
  assert.equal(store.thPrompt, '自然讲述')
  store.thPrompt = '用户正在编辑'
  await store.loadTalkingHead()
  assert.equal(store.thPrompt, '用户正在编辑')
  assert.equal(store.thValidConfirmedCost, null)
})

test('费用确认后的迟到旧查询不能覆盖新确认', async () => {
  installCost()
  const old = JSON.parse(JSON.stringify(store.thCost))
  let release
  let calls = 0
  globalThis.studioTestApi.getTalkingHeadCost = () => {
    calls++
    if (calls === 1) return new Promise(resolve => { release = resolve })
    return Promise.resolve({ ...old, confirmation: { ...old.confirmation, id: 'new-cost' } })
  }
  globalThis.studioTestApi.confirmTalkingHeadCost = async () => ({ ...old.confirmation, id: 'new-cost' })
  globalThis.studioTestApi.getSubmissions = async () => ({ runs: [] })
  globalThis.studioTestApi.getVideos = async () => []
  const loading = store.loadTalkingHead()
  await store.confirmTalkingHeadCost('OMNIHUMAN_1_5')
  release(old)
  await loading
  assert.equal(store.thValidConfirmedCost?.id, 'new-cost')
})

test('后端发现绑定变化时刷新费用状态并保留重新确认指引', async () => {
  installCost()
  globalThis.studioTestApi.submitTalkingHead = async () => {
    throw Object.assign(new Error('人物画面已变化，请重新确认本次调用清单与费用'), { code: 'COST_LIST_CHANGED' })
  }
  globalThis.studioTestApi.getTalkingHeadCost = async () => ({ ...store.thCost, confirmation_valid: false,
    current_call_list: { ...store.thCost.current_call_list, variant_id: 'new-variant' }, changed_fields: ['人物画面'] })
  await store.submitTalkingHead(false)
  assert.equal(store.thValidConfirmedCost, null)
  assert.match(store.error ?? '', /重新确认/)
  assert.equal(store.thConfirmedCost.id, 'cost')
})

test('只查询有远端编号的真实任务，相同远端编号只查询一次', async () => {
  store.currentWorkId = 'one'
  store.thSubmissions = { runs: [waitingRun('one'), waitingRun('duplicate', { remote_job_id: 'remote-one' }), waitingRun('mock', { is_mock: true }), waitingRun('no-id', { remote_job_id: null })] }
  const queried = []
  globalThis.studioTestApi.pollRun = async id => { queried.push(id); return { state: 'running' } }
  globalThis.studioTestApi.getSubmissions = async () => store.thSubmissions
  await store.pollWaitingRuns()
  assert.deepEqual(queried, ['one'])
})

test('进度查询防并发且切换作品后的迟到响应不继续查询旧任务', async () => {
  store.currentWorkId = 'one'
  store.thSubmissions = { runs: [waitingRun('one'), waitingRun('two')] }
  const queried = []
  let finish
  globalThis.studioTestApi.pollRun = id => { queried.push(id); return new Promise(resolve => { finish = resolve }) }
  const first = store.pollWaitingRuns()
  const second = store.pollWaitingRuns()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(queried, ['one'])
  store.currentWorkId = 'two'
  store.videos = [{ id: 'new-work-video' }]
  finish({ state: 'succeeded' })
  await Promise.all([first, second])
  assert.deepEqual(queried, ['one'])
  assert.equal(store.videos[0].id, 'new-work-video')
})

test('未知结果只能按已有远端编号查询，不能由按钮伪造对账结论', async () => {
  store.currentWorkId = 'one'
  store.thSubmissions = { runs: [waitingRun('unknown', { state: 'unknown' })] }
  const queried = []
  globalThis.studioTestApi.pollRun = async id => { queried.push(id); return { state: 'unknown' } }
  globalThis.studioTestApi.getSubmissions = async () => store.thSubmissions
  globalThis.studioTestApi.getVideos = async () => []
  globalThis.studioTestApi.settleRun = async () => assert.fail('查询不能写入人工对账')
  await store.pollWaitingRuns()
  assert.deepEqual(queried, [])
  await store.pollWaitingRuns('unknown')
  assert.deepEqual(queried, ['unknown'])
})

test('提交成功立即查询原任务，待结果期间禁止重复真实提交', async () => {
  installCost('M2')
  const run = waitingRun('one')
  let submits = 0
  let polls = 0
  globalThis.studioTestApi.submitTalkingHead = async () => { submits++; return { submitted: true, is_mock: false, runs: [run] } }
  globalThis.studioTestApi.getSubmissions = async () => ({ runs: [run] })
  globalThis.studioTestApi.getVideos = async () => []
  globalThis.studioTestApi.pollRun = async () => { polls++; return { state: 'running' } }
  await store.submitTalkingHead(false)
  assert.equal(polls, 1)
  await store.submitTalkingHead(false)
  assert.equal(submits, 1)
})

test('受理后读取列表失败仍保留回执，不能因此开放第二次真实提交', async () => {
  installCost('M2')
  const run = waitingRun('one')
  let submits = 0
  globalThis.studioTestApi.submitTalkingHead = async () => { submits++; return { submitted: true, is_mock: false, runs: [run] } }
  globalThis.studioTestApi.getSubmissions = async () => { throw new Error('temporary list failure') }
  globalThis.studioTestApi.getVideos = async () => []
  globalThis.studioTestApi.pollRun = async () => ({ state: 'running' })
  await store.submitTalkingHead(false)
  assert.equal(store.thSubmissions.runs[0].id, 'one')
  await store.submitTalkingHead(false)
  assert.equal(submits, 1)
})

test('进度快照取不到时提交仍然算数，只是少一个续发入口', async () => {
  installCost('M2')
  const run = waitingRun('one')
  globalThis.studioTestApi.submitTalkingHead = async () => ({ submitted: true, is_mock: false, runs: [run] })
  globalThis.studioTestApi.getSubmissions = async () => ({ runs: [run] })
  globalThis.studioTestApi.getVideos = async () => []
  globalThis.studioTestApi.pollRun = async () => ({ state: 'running' })
  // 端点整个不存在（后端未重启 / 版本不匹配）：驱动快照是锦上添花，
  // 不能因为它把"已经提交出去了"这个事实一起丢掉。
  delete globalThis.studioTestApi.getTalkingHeadDriver
  await store.submitTalkingHead(false)
  assert.equal(store.thSubmissions.runs[0].id, 'one')
  assert.equal(store.thDriver, null)
})

test('有旧失败/未知记录时，未授权不得重提，授权后才带 replacement_authorized', async () => {
  installCost('M2')
  const run = waitingRun('one', { state: 'unknown' })
  const sent = []
  // 旧记录必须已经摆在 thSubmissions 里，重提门禁才有东西可拦。
  store.thSubmissions = { work_id: 'one', runs: [run], auto_fallback: false, note: '' }
  globalThis.studioTestApi.getSubmissions = async () => ({ runs: [run] })
  globalThis.studioTestApi.getVideos = async () => []
  globalThis.studioTestApi.pollRun = async () => ({ state: 'unknown' })
  globalThis.studioTestApi.submitTalkingHead = async (id, input) => {
    sent.push(input)
    return { submitted: true, is_mock: false, runs: [run] }
  }
  await store.submitTalkingHead(false)
  assert.deepEqual(sent, [], '没有显式授权就不能发出第二次真实调用')
  await store.submitTalkingHead(false, { replacementAuthorized: true })
  assert.equal(sent.length, 1)
  assert.equal(sent[0].replacement_authorized, true)
})
