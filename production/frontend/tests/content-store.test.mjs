import assert from 'node:assert/strict'
import { after, beforeEach, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { createPinia, setActivePinia } from 'pinia'
import { watch } from 'vue'

const output = await mkdtemp(resolve('node_modules/.content-store-tests-'))
after(() => rm(output, { recursive: true, force: true }))
const outfile = join(output, 'store.mjs')
await build({
  stdin: { contents: `export { useContentStore } from './src/stores/content.ts'`, resolveDir: resolve('.') },
  outfile, bundle: true, format: 'esm', platform: 'node', external: ['vue', 'pinia'],
  plugins: [{ name: 'stub-api', setup(b) {
    b.onResolve({ filter: /^@\/api$/ }, () => ({ path: 'api', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `
      export const api = new Proxy({}, { get: (_, key) => (...args) => globalThis.contentApi[key](...args) });
      export class ApiError extends Error { constructor(code, message) { super(message); this.code = code } }` }))
  } }],
})
const { useContentStore } = await import(pathToFileURL(outfile).href)

const memory = new Map()
globalThis.localStorage = { getItem: (k) => memory.get(k) ?? null, setItem: (k, v) => memory.set(k, String(v)), removeItem: (k) => memory.delete(k) }

const emptyState = (workId, extra = {}) => ({ work_id: workId, content_revision: 1, brief: {}, reference: null, style: null, prompt: null, batch: null,
  selected_script_id: null, active_jobs: [], recent_jobs: [], references: [], limits: null, ...extra })
const deferred = () => { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j }); return { promise, resolve, reject } }
const tick = (ms = 5) => new Promise(r => setTimeout(r, ms))

beforeEach(() => { memory.clear(); setActivePinia(createPinia()) })

test('切作品后晚到的旧作品响应不会覆盖新作品', async () => {
  const slow = deferred()
  globalThis.contentApi = { getContentState: (id) => id === 'A' ? slow.promise : Promise.resolve(emptyState('B', { content_revision: 9 })) }
  const store = useContentStore()
  const first = store.enter('A')
  await store.enter('B')
  assert.equal(store.state.work_id, 'B')
  slow.resolve(emptyState('A', { content_revision: 1 }))
  await first
  assert.equal(store.state.work_id, 'B'); assert.equal(store.revision, 9)
})

test('leave 后不再轮询；有任务时轮询，终态后自动停止', async () => {
  let calls = 0
  let active = [{ job_id: 'j', kind: 'generation', state: 'running' }]
  globalThis.contentApi = { getContentState: async (id) => { calls++; return emptyState(id, { active_jobs: active }) } }
  const store = useContentStore()
  await store.enter('W')
  assert.equal(store.polling, true)
  await tick(700); assert.ok(calls >= 2, '有在途任务时应继续轮询')
  active = []
  await tick(1800)
  assert.equal(store.polling, false, '任务终态后停止轮询')
  const settled = calls
  await tick(400); assert.equal(calls, settled, '终态后不再请求')
  active = [{ job_id: 'j2', kind: 'generation', state: 'running' }]
  await store.refresh(); store.startPolling()
  store.leave()
  const atLeave = calls
  await tick(800); assert.equal(calls, atLeave, '卸载后不再轮询')
  assert.equal(store.polling, false); assert.equal(store.state, null)
})

test('刷新后 request_id 保留：同一意图取回原任务，不重复提交', async () => {
  const seen = []
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, { prompt: { id: 'p1', version: 1, prompt_text: 't', angle_plan: [] } }),
    startGeneration: async (id, input) => { seen.push(input.request_id); return { job_id: 'cj', kind: 'generation', state: 'running', replayed: seen.length > 1, result_refs: {} } },
    getContentJob: async () => ({ job_id: 'cj', kind: 'generation', state: 'running', result_refs: {} }),
  }
  let store = useContentStore()
  await store.enter('W')
  void store.generate(false)
  await tick(30)
  store.leave()
  setActivePinia(createPinia())           // 模拟刷新：store 全新，localStorage 还在
  store = useContentStore()
  await store.enter('W')
  void store.generate(false)
  await tick(30)
  store.leave()
  assert.equal(seen.length, 2); assert.equal(seen[0], seen[1], '刷新后必须复用同一个 request_id')
})

test('“重新生成”是新意图：换新的 request_id；任务终态后同样操作也是新一次', async () => {
  const seen = []
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, { prompt: { id: 'p1', version: 1, prompt_text: 't', angle_plan: [] } }),
    startGeneration: async (id, input) => { seen.push(input.request_id); return { job_id: 'cj' + seen.length, kind: 'generation', state: 'succeeded', result_refs: {} } },
    getContentJob: async (id, jobId) => ({ job_id: jobId, kind: 'generation', state: 'succeeded', result_refs: {} }),
  }
  const store = useContentStore()
  await store.enter('W')
  await store.generate(false)
  await store.generate(false)
  await store.generate(true)
  store.leave()
  assert.equal(new Set(seen).size, 3, '终态后再点、以及显式重新生成，都是新的 request_id')
})

test('修订号过期时提示已刷新，不当成成功', async () => {
  let revision = 1
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, { content_revision: revision }),
    preparePrompt: async () => { revision = 5; const e = new Error('stale'); e.code = 'CONTENT_REVISION_STALE'; throw Object.assign(e, { name: 'ApiError' }) },
  }
  const store = useContentStore()
  await store.enter('W')
  store.state.reference = { id: 'r', transcript: { state: 'ready' } }
  await store.preparePrompt()
  assert.equal(store.revision, 5, '应重新读取最新修订号')
  assert.ok(store.error)
  store.leave()
})

test('内容状态尚未加载时只提示正在读取费用信息', () => {
  const store = useContentStore()
  assert.equal(store.costNote, '正在读取费用信息。')
  store.leave()
})

test('内容创作费用说明不再把空额度解释为禁用收费调用', async () => {
  globalThis.contentApi = { getContentState: async (id) => emptyState(id) }
  const store = useContentStore()
  await store.enter('W')
  const expected = '内容创作不设每日、单流程或调用次数额度上限，费用按实际调用记录。'
  assert.equal(store.costNote, expected)
  store.state.limits = { per_flow_fen: null, daily_fen: null, max_text_calls: null, concurrency: 2, max_repair_rounds: 2 }
  assert.equal(store.costNote, expected)
  assert.doesNotMatch(store.costNote, /不会发起收费调用|导入参考|token|协议/i)
  store.leave()
})

test('已通过数以后端 passed 为准，不自行数', async () => {
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { batch: { id: 'b', passed: 2, produced: 3, requested: 3, candidates: [{ pickable: true }, { pickable: true }, { pickable: false }] } }) }
  const store = useContentStore()
  await store.enter('W')
  assert.equal(store.passedCount, 2)
  store.leave()
})

test('写稿路线随生成请求提交，切换路线后不复用旧 request_id', async () => {
  const seen = []
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, {
      prompt: { id: 'p1', version: 1, prompt_text: 't', angle_plan: [] },
      brief: { content_route: 'talking_script' },
    }),
    startGeneration: async (_id, input) => {
      seen.push(input)
      return { job_id: `j${seen.length}`, kind: 'generation', state: 'succeeded', result_refs: {} }
    },
    getContentJob: async (_id, jobId) => ({ job_id: jobId, kind: 'generation', state: 'succeeded', result_refs: {} }),
  }
  const store = useContentStore()
  await store.enter('W')
  await store.generate(false)
  store.state.brief.content_route = 'short_video_storyboard'
  await store.generate(false)
  assert.deepEqual(seen.map(x => x.content_route), ['talking_script', 'short_video_storyboard'])
  assert.notEqual(seen[0].request_id, seen[1].request_id)
  store.leave()
})

test('切换内容路线保存到作品并刷新，旧提示不被继续使用', async () => {
  let route = 'talking_script'
  const saves = []
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, {
      content_revision: saves.length + 1,
      brief: { topic: '主题', content_route: route },
      prompt: route === 'talking_script' ? { id: 'old', prompt_text: '旧提示' } : null,
    }),
    saveContentSettings: async (_id, input) => {
      saves.push(input)
      route = input.brief.content_route
      return { ok: true, content_revision: saves.length + 1 }
    },
  }
  const store = useContentStore()
  await store.enter('W')
  await store.setRoute('short_video_storyboard')
  assert.equal(saves[0].expected_content_revision, 1)
  assert.equal(saves[0].brief.content_route, 'short_video_storyboard')
  assert.equal(store.state.brief.content_route, 'short_video_storyboard')
  assert.equal(store.prompt, null)
  store.leave()
})

test('路线设置返回成功状态，同路线和忙碌状态不提交', async () => {
  const saves = []
  let route = 'talking_script'
  globalThis.contentApi = {
    getContentState: async id => emptyState(id, { brief: { content_route: route } }),
    saveContentSettings: async (_id, input) => { saves.push(input); route = input.brief.content_route; return { ok: true, content_revision: 2 } },
  }
  const store = useContentStore()
  await store.enter('W')
  assert.equal(await store.setRoute(route), false)
  for (const busy of ['loading', 'submitting', 'active']) {
    store.loading = busy === 'loading'
    store.submittingKind = busy === 'submitting' ? 'review' : null
    store.state.active_jobs = busy === 'active' ? [{ job_id: 'job', kind: 'review', state: 'running' }] : []
    assert.equal(await store.setRoute('short_video_storyboard'), false)
  }
  assert.equal(saves.length, 0)
  store.loading = false; store.submittingKind = null; store.state.active_jobs = []
  assert.equal(await store.setRoute('short_video_storyboard'), true)
  assert.equal(saves.length, 1)
  assert.equal(store.submittingKind, null)
  store.leave()
})

test('路线设置锁定在途提交，保存失败或旧作品响应不能报告成功', async () => {
  const pending = deferred(); const calls = []
  let route = 'talking_script'
  globalThis.contentApi = {
    getContentState: async id => emptyState(id, { brief: { content_route: id === 'B' ? 'talking_script' : route } }),
    saveContentSettings: async (_id, input) => { calls.push(input); await pending.promise; route = input.brief.content_route; return { ok: true } },
  }
  const store = useContentStore()
  await store.enter('A')
  const first = store.setRoute('short_video_storyboard')
  assert.equal(store.submittingKind, 'settings')
  assert.equal(await store.setRoute('short_video_storyboard'), false)
  assert.equal(calls.length, 1)
  await store.enter('B')
  pending.resolve()
  assert.equal(await first, false)
  assert.equal(store.state.work_id, 'B')
  assert.equal(store.submittingKind, null)
  globalThis.contentApi.saveContentSettings = async () => { throw new Error('保存失败') }
  assert.equal(await store.setRoute('short_video_storyboard'), false)
  assert.equal(store.error, '保存失败')
  assert.equal(store.submittingKind, null)
  store.leave()
})

test('写稿落库后立即向页面提供初稿，采用资格仍保留后端投影', async () => {
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, {
    batch: { id: 'b', requested: 3, produced: 3, passed: 2, ready_to_present: false,
      candidates: [{ id: 'draft', text: '初稿正文', pickable: false, review_state: 'needs_review' }] },
  }) }
  const store = useContentStore()
  await store.enter('W')
  assert.equal(store.visibleCandidates[0].text, '初稿正文')
  assert.equal(store.visibleCandidates[0].pickable, false)
  store.state.batch = { id: 'b2', requested: 3, produced: 3, passed: 3, ready_to_present: true,
    candidates: [1, 2, 3].map(id => ({ id: `c${id}`, text: `稿${id}`, pickable: true })) }
  assert.equal(store.visibleCandidates.length, 3)
  store.leave()
})

test('任务在后端已终态后，本地不再因过期回执一直显示忙碌', async () => {
  const state = (active) => emptyState('W', { active_jobs: active, recent_jobs: active.length ? [] : [{ job_id: 'sj', kind: 'search', state: 'succeeded' }] })
  let active = [{ job_id: 'sj', kind: 'search', state: 'running' }]
  globalThis.contentApi = {
    getContentState: async () => state(active),
    searchReferences: async () => ({ job_id: 'sj', kind: 'search', state: 'queued', result_refs: {} }),
    getContentJob: async () => { active = []; return { job_id: 'sj', kind: 'search', state: 'succeeded', result_refs: { items: [] } } },
  }
  const store = useContentStore()
  await store.enter('W')
  store.query = 'AI'
  await store.search()
  assert.equal(store.busy, false, '搜索已成功，不应继续占用忙碌状态')
  assert.equal(store.activeJob, null)
  store.leave()
})

test('无口播参考导入后只读取详情，保留原主参考且不自动分析或生成', async () => {
  const calls = []
  const selected = { id: 'old', work_id: 'W', transcript: { state: 'ready', text: '旧口播' } }
  const imported = { id: 'new', work_id: 'W', transcript: { state: 'failed', text: '' } }
  globalThis.contentApi = {
    getContentState: async (id) => { calls.push('state'); return emptyState(id, { reference: selected, brief: { content_route: 'short_video_storyboard' } }) },
    importReference: async () => { calls.push('import'); return { job_id: 'ij', kind: 'import', state: 'succeeded' } },
    getContentJob: async () => { calls.push('job'); return { job_id: 'ij', kind: 'import', state: 'succeeded', result_refs: { reference_id: 'new' } } },
    getReferenceSnapshot: async (id, referenceId) => { calls.push('detail'); assert.equal(id, 'W'); assert.equal(referenceId, 'new'); return imported },
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    await store.importReference({ platform_work_id: 'video' })
    assert.equal(store.inspectedReference?.id, 'new')
    assert.equal(store.selectedReference.id, 'old')
    assert.match(store.notice, /已导入.*尚未生成分镜剧本/)
    assert.deepEqual(calls, ['state', 'import', 'state', 'job', 'detail', 'state'])
  } finally { store.leave() }
})

test('查看参考与主参考分离，同作品晚到的详情不会覆盖后查看的参考', async () => {
  const slow = deferred()
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, { reference: { id: 'main', work_id: id } }),
    getReferenceSnapshot: async (id, refId) => refId === 'first' ? slow.promise : { id: refId, work_id: id },
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    const first = store.inspectReference('first')
    await store.inspectReference('second')
    slow.resolve({ id: 'first', work_id: 'W' })
    await first
    assert.equal(store.inspectedReference.id, 'second')
    assert.equal(store.selectedReference.id, 'main')
  } finally { store.leave() }
})

test('切作品后晚到的参考详情不会写入新作品', async () => {
  const slow = deferred()
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id),
    getReferenceSnapshot: async () => slow.promise,
  }
  const store = useContentStore()
  try {
    await store.enter('A')
    const pending = store.inspectReference('old-ref')
    await store.enter('B')
    slow.resolve({ id: 'old-ref', work_id: 'A' })
    await pending
    assert.equal(store.inspectedReference, null)
    assert.equal(store.inspecting, false)
    assert.equal(store.workId, 'B')
  } finally { store.leave() }
})

const promptState = (id) => emptyState(id, { prompt: { id: 'p1', version: 1, prompt_text: 't', angle_plan: [] } })
const doneJob = (id = 'j') => ({ job_id: id, kind: 'generation', state: 'succeeded', result_refs: {} })

const writerState = (id, extra = {}) => emptyState(id, {
  prompt: { id: 'p1', version: 1, prompt_text: 't', angle_plan: [] },
  batch: { id: 'b1', retry_targets: [{ candidate_id: 'c1', review_state: 'failed' }], candidates: [] },
  writer_runtime: { execution_mode: 'claude_code', timeout_seconds: 600, profile_id: 'airelay-claude-code' },
  ...extra,
})
const writerActions = {
  generation: (store, force = false) => store.generate(force),
  repair: (store) => store.repair({ id: 'c1', review_state: 'failed' }, '增强场景细节'),
  repair_pending: (store) => store.repairPending(),
}

for (const passed of [0, 1, 3]) test(`重写完成后展示新版本与当前批次 ${passed}/3 的真实审核结果`, async () => {
  const job = { ...doneJob(), kind: 'repair', state: passed === 3 ? 'succeeded' : 'partial',
    result_refs: { batch_id: 'b1', produced: 3, passed } }
  globalThis.contentApi = {
    getContentState: async id => writerState(id, { batch: { id: 'b1', produced: 3, passed: 3, candidates: [] } }),
    startRepair: async () => job,
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    await writerActions.repair(store)
    assert.equal(store.error, '')
    assert.match(store.notice, /重写新版本已生成/)
    assert.match(store.notice, new RegExp(`当前批次 ${passed}/3 篇通过独立审核`))
    if (passed < 3) {
      assert.match(store.notice, new RegExp(`${3 - passed} 篇尚未通过`))
      assert.doesNotMatch(store.notice, /全部通过/)
    } else assert.doesNotMatch(store.notice, /尚未通过/)
  } finally { store.leave() }
})

test('重写请求失败时展示原错误，不能误报新版本生成成功', async () => {
  const message = '审核服务拒绝了本次请求。'
  const job = { ...doneJob(), kind: 'repair', state: 'failed', error: { code: 'PROVIDER_REJECTED', message },
    result_refs: { produced: 3, passed: 1 } }
  globalThis.contentApi = {
    getContentState: async id => writerState(id),
    startRepair: async () => job,
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    await writerActions.repair(store)
    assert.equal(store.error, message)
    assert.equal(store.notice, '')
  } finally { store.leave() }
})

test('确定性硬门禁需复核的稿可定向返工，单纯未审稿不会混入', async () => {
  const sent = []
  const gated = { id: 'gated', review_state: 'needs_review', quality_report: {
    checks: [{ code: 'FACT_EVIDENCE', hard_gate: true, outcome: 'review' }] } }
  globalThis.contentApi = {
    getContentState: async id => writerState(id, { batch: { id: 'b', candidates: [gated],
      retry_targets: [{ candidate_id: 'gated', review_state: 'needs_review' },
        { candidate_id: 'unreviewed', review_state: 'needs_review' }] } }),
    startRepair: async (_id, input) => { sent.push(input); return { ...doneJob(), kind: 'repair' } },
  }
  const store = useContentStore()
  try {
    await store.enter('W'); store.waitForJob = async () => {}
    await store.repairPending()
    assert.equal(sent.length, 1)
    assert.deepEqual(sent[0].candidate_ids, ['gated'])
  } finally { store.leave() }
})

test('没有明确返工目标时不会自动补写，未审目标也不能发送返工', async () => {
  const sent = []
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { batch: { id: 'b', candidates: [
      { id: 'draft', review_state: 'needs_review', quality_report: { checks: [] } }], retry_targets: [] } }),
    startGeneration: async () => { sent.push('generation'); return doneJob() },
    startRepair: async () => { sent.push('repair'); return doneJob() },
    getContentJob: async () => doneJob(),
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    await store.repairPending()
    store.state.batch.retry_targets = [{ candidate_id: 'draft', review_state: 'needs_review' }]
    await store.repairPending()
    await store.repair(store.state.batch.candidates[0])
    assert.deepEqual(sent, [])
    assert.match(store.notice, /重新审核/)
  } finally { store.leave() }
})

const reviewJob = (state = 'unknown', id = 'review-job') => ({ job_id: id, kind: 'review', batch_id: 'b1',
  state, content_revision: 1, result_refs: {}, error: state === 'unknown'
    ? { code: 'PROVIDER_RESPONSE_TIMEOUT', message: '审核响应超时，结果与费用未确认。' } : null })
const retryReview = { call_id: 'review-call', job_id: 'review-job', batch_id: 'b1', reserved_fen: 127, has_remote_handle: false }

test('审核未知结果刷新后沿用同一 RID，不默认释放后再新开付费任务', async () => {
  const sent = [], job = reviewJob()
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { recent_jobs: [job], review_retry: null }),
    startReview: async (_id, input) => { sent.push(input); return job },
    getContentJob: async () => job,
  }
  let store = useContentStore()
  try {
    await store.enter('W'); await store.rereview()
    store.leave(); setActivePinia(createPinia()); store = useContentStore()
    await store.enter('W'); await store.rereview()
    assert.equal(sent[0].request_id, sent[1].request_id)
    assert.equal(sent[1].retry_unknown_call_id, undefined)
    assert.match(store.error, /审核响应超时/)
  } finally { store.leave() }
})

test('审核未知恢复必须明确确认可能重复计费，拒绝不提交且接受后绑定原调用', async () => {
  const sent = [], confirmations = [], oldWindow = globalThis.window
  let accept = false
  globalThis.window = { confirm(message) { confirmations.push(message); return accept } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { review_retry: retryReview }),
    startReview: async (_id, input) => { sent.push(input); return reviewJob() },
    getContentJob: async () => reviewJob(),
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    const original = store.rid('review', 'b1@1')
    await store.rereview()
    assert.deepEqual(sent, [])
    assert.match(confirmations[0], /可能.*费用.*预留.*1\.27.*重复/)
    accept = true
    await store.rereview()
    assert.notEqual(sent[0].request_id, original)
    assert.equal(sent[0].retry_unknown_call_id, 'review-call')
    assert.equal(sent[0].retry_paid_ack, true)
    assert.equal(store.rid('review', 'b1@1'), original, '旧的未知请求不能被新意图释放')
    await store.rereview()
    assert.equal(sent[0].request_id, sent[1].request_id, '相同恢复意图必须重放原任务')
  } finally {
    store.leave()
    if (oldWindow === undefined) delete globalThis.window
    else globalThis.window = oldWindow
  }
})

for (const state of ['succeeded', 'failed', 'unknown']) test(`页面重新进入时审核${state}回执结算并按终态保留或释放 RID`, async () => {
  const sent = []
  let active = [], recent = [], job = reviewJob('running')
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { active_jobs: active, recent_jobs: recent }),
    startReview: async (_id, input) => { sent.push(input); active = [job]; return job },
  }
  let store = useContentStore()
  try {
    await store.enter('W'); store.waitForJob = async () => {}; await store.rereview()
    store.leave(); setActivePinia(createPinia()); store = useContentStore()
    job = reviewJob(state); active = []; recent = [job]
    await store.enter('W')
    assert.equal(store.busy, false)
    if (state === 'unknown') assert.match(store.error, /审核响应超时/)
    store.waitForJob = async () => {}; await store.rereview()
    assert.equal(sent[0].request_id === sent[1].request_id, state === 'unknown')
  } finally { store.leave() }
})

test('审核仍在运行时重复点击不会开单或弹出费用确认', async () => {
  const oldWindow = globalThis.window, sent = [], confirmations = []
  globalThis.window = { confirm(message) { confirmations.push(message); return true } }
  globalThis.contentApi = { getContentState: async (id) => writerState(id, {
    active_jobs: [reviewJob('running')], review_retry: retryReview }),
    startReview: async () => { sent.push('review'); return reviewJob() } }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.rereview()
    assert.deepEqual(sent, []); assert.deepEqual(confirmations, [])
  } finally {
    store.leave()
    if (oldWindow === undefined) delete globalThis.window
    else globalThis.window = oldWindow
  }
})

test('写稿与返工依据运行配置等待，并按实际线路说明写稿方式', async () => {
  let runtime = { execution_mode: 'claude_code', timeout_seconds: 900 }
  const waits = []
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { writer_runtime: runtime }),
    startGeneration: async () => ({ ...doneJob(), state: 'running' }),
    startRepair: async () => ({ ...doneJob(), kind: 'repair', state: 'running' }),
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    store.waitForJob = async (_id, _done, maxMs) => { waits.push(maxMs) }
    for (const action of Object.values(writerActions)) {
      await action(store)
      assert.match(store.notice, /Claude Code/)
    }
    runtime = { execution_mode: 'claude_code' }; await store.refresh()
    await store.generate()
    runtime = { execution_mode: 'http_api' }; await store.refresh()
    await store.generate()
    assert.doesNotMatch(store.notice, /Claude Code/)
    assert.deepEqual(waits, [990000, 990000, 990000, 690000, 210000])
  } finally { store.leave() }
})

for (const [kind, action] of Object.entries(writerActions)) test(`${kind}未知结果刷新后沿用原请求，不会隐式创建新付费请求`, async () => {
  const sent = []
  const job = { ...doneJob(), state: 'unknown', error: { code: 'CONTENT_CALL_PENDING', message: '原请求费用未确认' } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { recent_jobs: [job] }),
    startGeneration: async (_id, input) => { sent.push(input.request_id); return job },
    startRepair: async (_id, input) => { sent.push(input.request_id); return { ...job, kind: 'repair' } },
    getContentJob: async () => job,
  }
  let store = useContentStore()
  try {
    await store.enter('W'); await action(store)
    store.leave(); setActivePinia(createPinia()); store = useContentStore()
    await store.enter('W'); await action(store)
    assert.equal(sent.length, 2)
    assert.equal(sent[0], sent[1], '未知调用必须重放同 request_id，不创建另一份付费意图')
    assert.match(store.error, /结果未确认.*不会自动重发/)
  } finally { store.leave() }
})

test('写稿明确失败保留客户端限制说明，人工再次提交可以使用新的请求', async () => {
  const sent = [], message = '当前分组只允许 Claude Code 客户端。'
  const job = { ...doneJob(), state: 'failed', error: { code: 'PROVIDER_CLIENT_RESTRICTED', message } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { recent_jobs: [job] }),
    startGeneration: async (_id, input) => { sent.push(input.request_id); return job },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.generate()
    assert.equal(store.error, message)
    assert.equal(store.notice, '')
    await store.generate(true)
    assert.notEqual(sent[0], sent[1])
  } finally { store.leave() }
})

test('写稿断连展示安全原因，未知费用仍保留原请求', async () => {
  const sent = [], message = '外部服务连接提前断开，原请求的结果和费用尚未确认。系统不会自动重发。'
  const job = { ...doneJob(), state: 'unknown',
    error: { code: 'PROVIDER_CONNECTION_CLOSED', message } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { batch: null, recent_jobs: [job] }),
    startGeneration: async (_id, input) => { sent.push(input.request_id); return job },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.generate()
    assert.equal(store.error, message)
    assert.equal(store.notice, '')
    await store.generate()
    assert.equal(sent[0], sent[1], '断连不能被当成已确认失败而释放原请求')
  } finally { store.leave() }
})

test('写稿未知结果先沿用原请求；明确确认重试才创建新request_id', async () => {
  const sent = [], inputs = []
  const job = { ...doneJob(), state: 'unknown', prompt_id: 'p1', content_revision: 1,
    calls: [{ id: 'write-call-1', step: 'write', state: 'unknown', has_remote_handle: false, reserved_fen: 127 }],
    error: { code: 'CONTENT_CALL_PENDING', message: '原请求费用未确认' } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { recent_jobs: [job] }),
    startGeneration: async (_id, input) => { sent.push(input.request_id); inputs.push(input); return job },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    await store.generate(false)
    const original = sent[0]
    await store.generate(true)
    assert.equal(sent.length, 2)
    assert.notEqual(sent[1], original)
    assert.equal('retry_unknown_call_id' in inputs[0], false)
    assert.equal(inputs[1].retry_unknown_call_id, 'write-call-1')
    assert.equal(inputs[1].retry_paid_ack, true)
  } finally { store.leave() }
})

test('写稿运行方式与profile变更后，不沿用之前线路的回执', async () => {
  const sent = [], job = { ...doneJob(), state: 'unknown' }
  let runtime = { execution_mode: 'http_api', timeout_seconds: 120 }, profile = 'http-profile'
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { writer_runtime: { ...runtime, profile_id: profile } }),
    startGeneration: async (_id, input) => { sent.push(input.request_id); return job },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    store.waitForJob = async () => {}
    await store.generate()
    runtime = { execution_mode: 'claude_code', timeout_seconds: 600 }; await store.refresh(); await store.generate()
    profile = 'airelay-claude-code'; await store.refresh(); await store.generate()
    assert.equal(new Set(sent).size, 3, '实际写稿配置变化后必须成为可辨认的新意图')
  } finally { store.leave() }
})

for (const terminal of ['unknown', 'failed', 'succeeded']) test(`同步等待结束后的写稿${terminal}仍展示正确反馈，按确认状态管理RID`, async (t) => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 })
  let active = [], recent = [], job
  const sent = [], message = terminal === 'unknown' ? '写稿响应超时，费用与结果未确认。' : terminal === 'failed' ? 'Claude Code 写稿进程失败。' : ''
  const ended = { ...doneJob('writer'), state: terminal,
    ...(terminal === 'unknown' ? { prompt_id: 'p1', content_revision: 1,
      calls: [{ id: 'write-call-1', step: 'write', state: 'unknown', has_remote_handle: false, reserved_fen: 127 }] } : {}),
    error: message ? { code: terminal === 'unknown' ? 'PROVIDER_RESPONSE_TIMEOUT' : 'CLAUDE_CODE_FAILED', message } : null }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { active_jobs: active, recent_jobs: recent }),
    startGeneration: async (_id, input) => {
      sent.push(input.request_id)
      job = { ...doneJob('writer'), state: 'running' }; active = [job]
      return job
    },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    const originalWait = store.waitForJob
    store.waitForJob = (id, done, _maxMs, current) => originalWait(id, done, 100, current)
    const generating = store.generate()
    await new Promise(resolve => setImmediate(resolve))
    t.mock.timers.tick(1200)
    await generating
    assert.equal(store.polling, true)
    assert.equal(store.busy, true)
    job = ended; active = []; recent = [ended]
    t.mock.timers.tick(1500)
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(store.error, message, '晚于同步等待期限的失败仍要显示原错误')
    assert.equal(store.notice, '', '终态后清除写稿已开始通知')
    assert.equal(store.busy, false)
    assert.equal(store.polling, false)
    store.waitForJob = async () => {}
    await store.generate(true)
    if (terminal === 'unknown') assert.notEqual(sent[0], sent[1], '显式重新生成已确认重复计费风险后创建新请求')
    else assert.notEqual(sent[0], sent[1], '已确认终态后人工再点可以成为新请求')
  } finally { store.leave(); t.mock.timers.reset() }
})

test('写稿等待期间切换profile，旧进程的晚到失败不覆盖当前线路反馈', async () => {
  const pending = deferred()
  let profile = 'old'
  const job = { ...doneJob('writer'), state: 'running' }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, {
      writer_runtime: { execution_mode: 'claude_code', timeout_seconds: 600, profile_id: profile }, active_jobs: [job],
    }),
    startGeneration: async () => job,
    getContentJob: async () => pending.promise,
  }
  const store = useContentStore()
  let generating
  try {
    await store.enter('W'); generating = store.generate()
    await new Promise(resolve => setImmediate(resolve))
    profile = 'new'; await store.refresh()
    store.error = '当前线路反馈'; store.notice = '当前线路通知'
    pending.resolve({ ...job, state: 'failed', error: { code: 'CLAUDE_CODE_FAILED', message: '旧线路失败' } })
    await generating
    assert.equal(store.error, '当前线路反馈')
    assert.equal(store.notice, '当前线路通知')
  } finally { pending.resolve(job); if (generating) await generating; store.leave() }
})

test('当前写稿任务仍运行时，历史未确认回执不能覆盖当前通知', async () => {
  const pending = deferred(), job = { ...doneJob('current'), state: 'running' }
  const history = { ...doneJob('old'), state: 'unknown', error: { code: 'PROVIDER_RESPONSE_TIMEOUT', message: '历史失败' } }
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { active_jobs: [job], recent_jobs: [history] }),
    startGeneration: async () => job,
    getContentJob: async () => pending.promise,
  }
  const store = useContentStore()
  let generating
  try {
    await store.enter('W'); generating = store.generate()
    await new Promise(resolve => setImmediate(resolve))
    await store.refresh()
    assert.equal(store.error, '')
    assert.match(store.notice, /Claude Code/)
    assert.equal(store.busy, true)
  } finally { pending.resolve(doneJob('current')); if (generating) await generating; store.leave() }
})

for (const terminal of ['succeeded', 'failed', 'unknown']) test(`写稿在刷新后${terminal}，首个再次操作按真实终态选择新请求或重放`, async () => {
  const pending = deferred(), sent = []
  let active = [], recent = [], job
  globalThis.contentApi = {
    getContentState: async (id) => writerState(id, { active_jobs: active, recent_jobs: recent }),
    startGeneration: async (_id, input) => {
      sent.push(input.request_id)
      job = { ...doneJob(`writer-${sent.length}`), state: 'running' }; active = [job]
      return job
    },
    getContentJob: async () => pending.promise,
  }
  let store = useContentStore(), generating
  try {
    await store.enter('W'); generating = store.generate()
    await new Promise(resolve => setImmediate(resolve))
    const ended = { ...job, state: terminal,
      ...(terminal === 'unknown' ? { prompt_id: 'p1', content_revision: 1,
        calls: [{ id: 'write-call-1', step: 'write', state: 'unknown', has_remote_handle: false, reserved_fen: 127 }] } : {}),
      error: terminal === 'succeeded' ? null : { code: 'PROVIDER_RESPONSE_TIMEOUT', message: '写稿结果未确认。' } }
    store.leave(); setActivePinia(createPinia()); store = useContentStore()
    active = []; recent = [ended]
    await store.enter('W')
    store.waitForJob = async () => {}
    await store.generate(true)
    assert.equal(sent.length, 2)
    if (terminal === 'unknown') assert.notEqual(sent[0], sent[1], '显式重新生成创建新RID')
    else assert.notEqual(sent[0], sent[1], '刷新后原任务已确认结束，首点击就应产生新RID')
  } finally { pending.resolve(doneJob()); if (generating) await generating; store.leave() }
})

test('生成回执返回前立即忙碌，重复重新生成不提交也不释放在途 request_id', async () => {
  const pending = deferred(), sent = []
  globalThis.contentApi = {
    getContentState: async (id) => promptState(id),
    startGeneration: async (_id, input) => { sent.push(input.request_id); return pending.promise },
    getContentJob: async () => doneJob(),
  }
  const store = useContentStore()
  await store.enter('W')
  const first = store.generate(true)
  try {
    assert.equal(store.busy, true)
    assert.equal(store.submittingKind, 'generation')
    await store.generate(true)
    assert.equal(sent.length, 1)
    assert.equal([...memory.values()][0], sent[0], '重复点击不得释放在途的 request_id')
  } finally { pending.resolve(doneJob()); await first; store.leave() }
})

test('提交失败保留错误并解除提交状态，不继续显示旧通知', async () => {
  const pending = deferred()
  globalThis.contentApi = { getContentState: async (id) => promptState(id), startGeneration: async () => pending.promise }
  const store = useContentStore()
  await store.enter('W'); store.notice = '旧操作已成功'
  const generating = store.generate()
  try {
    assert.equal(store.submittingKind, 'generation')
    assert.equal(store.notice, '')
  } finally { pending.reject(new Error('模拟提交失败')); await generating }
  try {
    assert.equal(store.error, '模拟提交失败')
    assert.equal(store.submittingKind, null)
    assert.equal(store.busy, false)
  } finally { store.leave() }
})

test('旧作品提交完成的 finally 不解除新作品的提交锁', async () => {
  const a = deferred(), b = deferred()
  globalThis.contentApi = {
    getContentState: async (id) => promptState(id),
    startGeneration: async (id) => id === 'A' ? a.promise : b.promise,
    getContentJob: async () => doneJob('jb'),
  }
  const store = useContentStore()
  await store.enter('A'); const old = store.generate()
  await store.enter('B'); const current = store.generate()
  try {
    a.resolve(doneJob('ja')); await old
    assert.equal(store.workId, 'B')
    assert.equal(store.submittingKind, 'generation')
    assert.equal(store.busy, true)
  } finally { a.resolve(doneJob('ja')); b.resolve(doneJob('jb')); await Promise.all([old, current]); store.leave() }
})

test('回执后刷新挂起时切作品，不在新作品轮询旧任务', async () => {
  const refresh = deferred(), polled = []; let aReads = 0
  globalThis.contentApi = {
    getContentState: async (id) => id === 'A' && ++aReads === 2 ? refresh.promise : promptState(id),
    startGeneration: async () => doneJob('ja'),
    getContentJob: async (id, jobId) => { polled.push([id, jobId]); return doneJob(jobId) },
  }
  const store = useContentStore()
  await store.enter('A'); const old = store.generate()
  try {
    await tick(); await store.enter('B')
    refresh.resolve(promptState('A')); await old
    assert.deepEqual(polled, [])
    assert.equal(store.polling, false)
    assert.equal(store.workId, 'B')
  } finally { refresh.resolve(promptState('A')); await old; store.leave() }
})

test('重叠刷新期间保持加载状态，晚到旧响应不能覆盖最新修订', async () => {
  const first = deferred(), second = deferred(); let reads = 0
  globalThis.contentApi = { getContentState: async (id) => ++reads === 1 ? promptState(id) : reads === 2 ? first.promise : second.promise }
  const store = useContentStore()
  await store.enter('W')
  const old = store.refresh(), current = store.refresh()
  try {
    assert.equal(store.loading, true)
    assert.equal(store.busy, true)
    second.resolve(emptyState('W', { content_revision: 9 })); await current
    assert.equal(store.loading, false)
    first.resolve(emptyState('W', { content_revision: 3 })); await old
    assert.equal(store.revision, 9)
  } finally { first.resolve(promptState('W')); second.resolve(promptState('W')); await Promise.all([old, current]); store.leave() }
})

test('旧作品轮询晚失败不会覆盖新作品错误', async () => {
  const pending = deferred()
  globalThis.contentApi = { getContentState: async (id) => promptState(id), getContentJob: async () => pending.promise }
  const store = useContentStore()
  await store.enter('A'); const old = store.waitForJob('ja', () => {})
  await store.enter('B'); store.error = '新作品的错误'
  pending.reject(new Error('旧作品的错误')); await old
  try { assert.equal(store.error, '新作品的错误') } finally { store.leave() }
})

const visualTimeoutMessage = '画面分析响应超时，原请求可能已计费。供应商没有提供查询句柄，重新发送需要重新授权费用。'
const visualJob = (extra = {}) => ({
  job_id: 'vj', kind: 'visual_analysis', state: 'unknown', reference_snapshot_id: 'ref', media_asset_id: 'media',
  result_refs: {}, error: { code: 'PROVIDER_RESPONSE_TIMEOUT', message: visualTimeoutMessage }, ...extra,
})

test('视觉超时终态保留后端给出的计费与重新授权说明', async () => {
  const job = visualJob()
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id, { recent_jobs: [job] }),
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    let terminal
    await store.waitForJob('vj', job => { terminal = job })
    assert.equal(store.error, visualTimeoutMessage)
    assert.equal(terminal.state, 'unknown')
  } finally { store.leave() }
})

test('其他结果未确认的任务仍使用保守说明，不把普通错误当作已确认超时', async () => {
  const job = visualJob({ error: { code: 'CONTENT_CALL_PENDING', message: '不确定的供应商返回' } })
  globalThis.contentApi = { getContentState: async (id) => emptyState(id), getContentJob: async () => job }
  const store = useContentStore()
  try {
    await store.enter('W')
    await store.waitForJob('vj', () => {})
    assert.match(store.error, /结果未确认.*不会自动重发/)
    assert.doesNotMatch(store.error, /不确定的供应商返回/)
  } finally { store.leave() }
})

test('刷新恢复视觉错误时严格匹配当前参考与媒体，不混入其他步骤', async () => {
  const jobs = [visualJob({ job_id: 'prompt', kind: 'prompt' }),
    visualJob({ job_id: 'other-ref', reference_snapshot_id: 'other' }),
    visualJob({ job_id: 'old-media', media_asset_id: 'old' }), visualJob()]
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { recent_jobs: jobs }) }
  const store = useContentStore()
  try {
    await store.enter('W')
    store.error = '其他步骤的全局错误'
    assert.equal(store.visualErrorForReference('ref', 'media'), visualTimeoutMessage)
    assert.equal(store.visualJobForReference('ref', 'media').job_id, 'vj')
    assert.equal(store.visualErrorForReference('missing', 'media'), '')
    assert.equal(store.visualErrorForReference('ref', 'replacement'), '')
    assert.equal(store.visualErrorForReference('ref', ''), '')
  } finally { store.leave() }
})

test('同一参考与媒体的新任务进行中或成功后，不再展示旧超时', async () => {
  const timeout = visualJob()
  const current = visualJob({ job_id: 'new', state: 'running', error: null })
  let active = [current], recent = [timeout]
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { active_jobs: active, recent_jobs: recent }) }
  const store = useContentStore()
  try {
    await store.enter('W')
    assert.equal(store.visualErrorForReference('ref', 'media'), '')
    assert.equal(store.visualJobForReference('ref', 'media').state, 'running')
    active = []; recent = [timeout, { ...current, state: 'succeeded' }]
    await store.refresh()
    assert.equal(store.visualErrorForReference('ref', 'media'), '')
  } finally { store.leave() }
})

test('画面分析按报价等待期限加90秒，报价缺失时兼容600秒默认值', async () => {
  const seen = [], submitted = []
  globalThis.contentApi = {
    getContentState: async (id) => emptyState(id),
    startVisualAnalysis: async (_id, input) => { submitted.push(input); return visualJob({ state: 'running', error: null }) },
  }
  const store = useContentStore()
  try {
    await store.enter('W')
    store.waitForJob = async (_job, _done, maxMs) => { seen.push(maxMs) }
    store.visualQuote = { timeout_seconds: 900 }
    await store.analyzeReferenceVisual('ref', 'media', 80)
    store.visualQuote = null
    await store.analyzeReferenceVisual('ref', 'media', 80)
    assert.deepEqual(seen, [990000, 690000])
    assert.equal(submitted.length, 2, '仅用户的两次明确操作提交请求')
  } finally { store.leave() }
})

test('同步等待期限结束后仍运行的视觉任务继续轮询，终态后停止且不重发', async (t) => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: 1000 })
  let active = [visualJob({ state: 'running', error: null })], reads = 0
  globalThis.contentApi = {
    getContentState: async (id) => { reads++; return emptyState(id, { active_jobs: active, recent_jobs: active.length ? [] : [visualJob()] }) },
    getContentJob: async () => active[0],
  }
  const store = useContentStore()
  try {
    await store.enter('W'); store.stopPolling()
    const waited = store.waitForJob('vj', () => {}, 100)
    await Promise.resolve()
    t.mock.timers.tick(1200)
    await waited
    assert.equal(store.polling, true, '同步等待退出后应继续后台查询')
    assert.equal(store.visualJobForReference('ref', 'media').state, 'running')
    const before = reads
    active = []
    t.mock.timers.tick(600)
    await new Promise(resolve => setImmediate(resolve))
    assert.ok(reads > before)
    assert.equal(store.polling, false)
    assert.equal(store.visualErrorForReference('ref', 'media'), visualTimeoutMessage)
    assert.equal(store.notice, '', '后台终态后不应残留仍在进行的误导提示')
  } finally { store.leave(); t.mock.timers.reset() }
})

const retryQuote = (callId = 'old-call') => ({ request_digest: 'visual-digest', reserve_fen: 80,
  call_list: { model: 'qwen3-vl-plus' }, retry_unknown_call: { call_id: callId, reserved_fen: 80 } })

test('视觉报价刷新时立即撤销旧报价，晚到的其他参考报价不会带入当前输入', async () => {
  const old = deferred(), current = deferred()
  globalThis.contentApi = { getContentState: async (id) => emptyState(id),
    quoteVisualAnalysis: async (_id, input) => input.reference_snapshot_id === 'old' ? old.promise : current.promise }
  const store = useContentStore()
  try {
    await store.enter('W')
    store.visualQuote = retryQuote('stale')
    const first = store.loadVisualQuote('old', 'same-media')
    assert.equal(store.visualQuote, null, '费用重新读取期间旧确认依据必须立即失效')
    const second = store.loadVisualQuote('new', 'same-media')
    current.resolve(retryQuote('new-call')); await second
    old.resolve(retryQuote('old-call')); await first
    assert.equal(store.visualQuote.retry_unknown_call.call_id, 'new-call')
    assert.deepEqual(store.visualQuoteBinding, { referenceId: 'new', mediaId: 'same-media' })
  } finally { old.resolve(retryQuote()); current.resolve(retryQuote()); store.leave() }
})

test('已有未知视觉调用只有显式追加确认且匹配报价原调用时才允许提交', async () => {
  const sent = []
  globalThis.contentApi = { getContentState: async (id) => emptyState(id),
    quoteVisualAnalysis: async () => retryQuote(),
    startVisualAnalysis: async (_id, input) => { sent.push(input); return visualJob({ state: 'running', error: null }) } }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.loadVisualQuote('ref', 'media')
    store.waitForJob = async () => {}
    await store.analyzeReferenceVisual('ref', 'media', 80)
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: 'old-call', retry_paid_ack: false })
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: 'wrong-call', retry_paid_ack: true })
    await store.analyzeReferenceVisual('other-ref', 'media', 80, { retry_unknown_call_id: 'old-call', retry_paid_ack: true })
    assert.equal(sent.length, 0)
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: 'old-call', retry_paid_ack: true })
    assert.equal(sent.length, 1)
    assert.equal(sent[0].paid_ack, true)
    assert.equal(sent[0].retry_unknown_call_id, 'old-call'); assert.equal(sent[0].retry_paid_ack, true)
  } finally { store.leave() }
})

test('未知视觉结果刷新报价并保留request_id重放，原调用变化后才成为新意图', async () => {
  let callId = 'original', quoteReads = 0
  const sent = [], oldMessage = '此前请求结果未知；本次没有向外部服务发送新请求。'
  const job = visualJob({ error: { code: 'VISUAL_PREVIOUS_CALL_UNKNOWN', message: oldMessage } })
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { recent_jobs: [job] }),
    quoteVisualAnalysis: async () => { quoteReads++; return retryQuote(callId) },
    startVisualAnalysis: async (_id, input) => { sent.push(input); return job },
    getContentJob: async () => job }
  let store = useContentStore()
  try {
    await store.enter('W'); await store.loadVisualQuote('ref', 'media')
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: callId, retry_paid_ack: true })
    assert.equal(store.error, oldMessage, '此前请求未发送错误必须保留原说明')
    assert.equal(quoteReads, 2, '未知终态后只刷新费用依据，不自动重新提交')
    store.leave(); setActivePinia(createPinia()); store = useContentStore()
    await store.enter('W'); await store.loadVisualQuote('ref', 'media')
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: callId, retry_paid_ack: true })
    assert.equal(sent[0].request_id, sent[1].request_id, '未知操作刷新后必须沿用原request_id')
    callId = 'new-original'; await store.loadVisualQuote('ref', 'media')
    await store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: callId, retry_paid_ack: true })
    assert.notEqual(sent[1].request_id, sent[2].request_id)
  } finally { store.leave() }
})

test('查看其他参考期间旧视觉提交失败不能刷新旧参考报价', async () => {
  const pending = deferred(), quoted = []
  globalThis.contentApi = { getContentState: async (id) => emptyState(id),
    quoteVisualAnalysis: async (_id, input) => { quoted.push(input.reference_snapshot_id); return retryQuote() },
    startVisualAnalysis: async () => pending.promise }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.loadVisualQuote('ref', 'media')
    const old = store.analyzeReferenceVisual('ref', 'media', 80, { retry_unknown_call_id: 'old-call', retry_paid_ack: true })
    await store.loadVisualQuote('new-ref', 'media')
    pending.reject(new Error('旧提交失败')); await old
    assert.deepEqual(quoted, ['ref', 'new-ref'])
    assert.deepEqual(store.visualQuoteBinding, { referenceId: 'new-ref', mediaId: 'media' })
  } finally { pending.resolve(null); store.leave() }
})

test('本地残留的旧视觉回执不优先于同参考最新终态', async () => {
  const old = visualJob(), current = visualJob({ job_id: 'new', state: 'succeeded', error: null })
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { recent_jobs: [old, current] }) }
  const store = useContentStore()
  try {
    await store.enter('W'); store.jobs.legacy = old
    assert.equal(store.visualJobForReference('ref', 'media').job_id, 'new')
    assert.equal(store.visualErrorForReference('ref', 'media'), '')
  } finally { store.leave() }
})

for (const operation of ['upload', 'fetch']) test(`${operation}换媒体触发同步报价读取时，不在其后撤销新报价`, async () => {
  globalThis.contentApi = { getContentState: async (id) => emptyState(id), quoteVisualAnalysis: async () => retryQuote(),
    uploadReferenceMedia: async () => ({ asset: { id: 'new-media' } }),
    startMediaFetch: async () => ({ job_id: 'fetch', kind: 'media_fetch', state: 'succeeded' }) }
  const store = useContentStore()
  let stop
  try {
    await store.enter('W'); await store.loadVisualQuote('ref', 'old-media')
    stop = watch(() => store.mediaByRef.ref?.id, (id) => { if (id) void store.loadVisualQuote('ref', id) }, { flush: 'sync' })
    if (operation === 'upload') await store.uploadReferenceMedia('ref', new File([], 'clip.mp4'))
    else {
      store.waitForJob = async (_id, done) => done({ state: 'succeeded', result_refs: { media_asset_id: 'new-media' } })
      await store.fetchReferenceMedia('ref', 100)
    }
    await tick()
    assert.deepEqual(store.visualQuoteBinding, { referenceId: 'ref', mediaId: 'new-media' })
    assert.equal(store.visualQuote?.retry_unknown_call.call_id, 'old-call')
  } finally { stop?.(); store.leave() }
})

test('直接生成响应丢失后修订号变化，刷新仍重放原完整请求', async () => {
  let revision = 4
  let first = true
  const sent = []
  const job = { job_id: 'direct-job', kind: 'generation', state: 'succeeded', replayed: true, error: null, result_refs: {} }
  globalThis.contentApi = {
    getContentState: async id => emptyState(id, { content_revision: revision,
      brief: { content_route: 'short_video_storyboard', topic: '主题' }, recent_jobs: first ? [] : [job] }),
    startDirectGeneration: async (_id, input) => {
      sent.push(structuredClone(input))
      if (first) { first = false; revision++; throw new Error('响应丢失') }
      return job
    },
    getContentJob: async () => job,
  }
  const store = useContentStore()
  try {
    await store.enter('direct-work')
    await store.generateDirect('主题', ['依据'])
    assert.equal(store.directRequestPending, true)
    store.leave()
    await store.enter('direct-work')
    assert.equal(store.revision, 5)
    assert.equal(store.directRequestPending, true)
    await store.generateDirect('另一个主题', ['依据'])
    assert.equal(sent.length, 1, '未核对旧提交时不能悄悄换输入')
    await store.generateDirect('主题', ['依据'])
    assert.deepEqual(sent[1], sent[0])
    assert.equal(sent[1].expected_content_revision, 4)
    assert.equal(store.directRequestPending, false)
  } finally { store.leave() }
})

test('未知视觉请求同报价摘要刷新沿用RID，视觉profile改变后新摘要使用新RID', async () => {
  let digest = 'profile-a'
  const sent = [], job = visualJob()
  globalThis.contentApi = { getContentState: async (id) => emptyState(id, { recent_jobs: [job] }),
    quoteVisualAnalysis: async () => ({ request_digest: digest, reserve_fen: 80, retry_unknown_call: null }),
    startVisualAnalysis: async (_id, input) => { sent.push(input.request_id); return job }, getContentJob: async () => job }
  const store = useContentStore()
  try {
    await store.enter('W'); await store.loadVisualQuote('ref', 'media')
    await store.analyzeReferenceVisual('ref', 'media', 80)
    await store.loadVisualQuote('ref', 'media'); await store.analyzeReferenceVisual('ref', 'media', 80)
    assert.equal(sent[0], sent[1], '同报价摘要的未知请求必须重放原RID')
    digest = 'profile-b'; await store.loadVisualQuote('ref', 'media')
    await store.analyzeReferenceVisual('ref', 'media', 80)
    assert.notEqual(sent[1], sent[2], '模型或解析版本变化的新报价不得重放旧未知任务')
  } finally { store.leave() }
})
