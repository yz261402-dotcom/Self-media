import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const output = await mkdtemp(resolve('node_modules/.content-http-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({ entryPoints: ['src/api/http.ts'], outfile: join(output, 'http.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { httpApi, ApiError } = await import(pathToFileURL(join(output, 'http.mjs')).href)
await build({ entryPoints: ['src/api/mock.ts'], outfile: join(output, 'mock.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { mockApi } = await import(pathToFileURL(join(output, 'mock.mjs')).href)

function capture(response = {}, status = 200) {
  const sent = []
  globalThis.fetch = async (url, init) => {
    sent.push({ url, init, body: init?.body ? JSON.parse(init.body) : null })
    return new Response(JSON.stringify(response), { status })
  }
  return sent
}

test('写操作带 request_id 与 expected_content_revision，路径与方法正确', async () => {
  const sent = capture({ job_id: 'j', kind: 'x', state: 'queued' })
  await httpApi.preparePrompt('w1', { request_id: 'r1', expected_content_revision: 3 })
  await httpApi.startGeneration('w1', { request_id: 'r2', prompt_id: 'p', expected_content_revision: 3 })
  await httpApi.startRepair('w1', { request_id: 'r3', batch_id: 'b', candidate_ids: ['s1'], expected_content_revision: 3 })
  await httpApi.importReference('w1', { request_id: 'r4', platform_work_id: '77', expected_content_revision: 3 })
  assert.deepEqual(sent.map(s => [s.init.method, s.url]), [
    ['POST', '/api/v1/works/w1/content/prompts'], ['POST', '/api/v1/works/w1/content/generations'],
    ['POST', '/api/v1/works/w1/content/repairs'], ['POST', '/api/v1/works/w1/content/imports'],
  ])
  for (const s of sent) { assert.ok(s.body.request_id); assert.equal(s.body.expected_content_revision, 3) }
})

test('Wan 分镜制作 API 按作品和任务图绑定确认摘要', async () => {
  const sent = capture({ id: 'g', shots: [] })
  await httpApi.createWanStoryboard('w1', { resolution: '720p', image_ceiling_fen_per_shot: 200 })
  await httpApi.approveWanStoryboardImages('w1', 'g', { plan_digest: 'digest', total_ceiling_fen: 600, paid_ack: true, actor_id: 'user_local' })
  await httpApi.submitWanStoryboardVideos('w1', 'g')
  assert.deepEqual(sent.map(s => s.url), [
    '/api/v1/works/w1/wan/storyboards',
    '/api/v1/works/w1/wan/storyboards/g/images/approve',
    '/api/v1/works/w1/wan/storyboards/g/videos/submit',
  ])
  assert.equal(sent[1].body.plan_digest, 'digest')
})

test('镜头图片回收使用原任务图与镜头编号，不提交生成', async () => {
  const sent = capture({ id: 'g', shots: [] })
  await httpApi.recoverWanStoryboardImage('work a', 'graph b', 2)
  assert.equal(sent[0].url, '/api/v1/works/work%20a/wan/storyboards/graph%20b/shots/2/image/recover')
  assert.equal(sent[0].init.method, 'POST')
  assert.deepEqual(sent[0].body, {})
})

test('单镜头重新生成透传原作业和追加费用确认，不自行补授权', async () => {
  const result = { id: 'graph b', shots: [{ shot_index: 2, image_attempt: 2 }] }
  const sent = capture(result)
  assert.equal(typeof httpApi.regenerateWanStoryboardImage, 'function')
  const input = { actor_id: 'user_local', paid_ack: true, duplicate_charge_ack: true,
    plan_digest: 'plan', expected_image_job_id: 'original-image', request_id: 'retry-unique', ceiling_fen: 200 }
  assert.deepEqual(await httpApi.regenerateWanStoryboardImage('work a', 'graph b', 2, input), result)
  assert.equal(sent[0].url, '/api/v1/works/work%20a/wan/storyboards/graph%20b/shots/2/image/regenerate')
  assert.equal(sent[0].init.method, 'POST')
  assert.deepEqual(sent[0].body, input)
  await httpApi.regenerateWanStoryboardImage('work a', 'graph b', 2, { ...input, paid_ack: false, duplicate_charge_ack: false })
  assert.equal(sent[1].body.paid_ack, false)
  assert.equal(sent[1].body.duplicate_charge_ack, false)
})

test('单镜头视频重生成透传原视频作业、任务摘要和追加费用确认', async () => {
  const result = { id: 'graph b', shots: [{ shot_index: 2, video_attempt: 2 }] }
  const sent = capture(result)
  assert.equal(typeof httpApi.regenerateWanStoryboardVideo, 'function')
  const input = { actor_id: 'user_local', paid_ack: true, duplicate_charge_ack: true,
    task_graph_digest: 'task', expected_video_job_id: 'original-video', request_id: 'video-retry-unique', ceiling_fen: 480 }
  assert.deepEqual(await httpApi.regenerateWanStoryboardVideo('work a', 'graph b', 2, input), result)
  assert.equal(sent[0].url, '/api/v1/works/work%20a/wan/storyboards/graph%20b/shots/2/video/regenerate')
  assert.equal(sent[0].init.method, 'POST')
  assert.deepEqual(sent[0].body, input)
  await httpApi.regenerateWanStoryboardVideo('work a', 'graph b', 2, { ...input, paid_ack: false, duplicate_charge_ack: false })
  assert.equal(sent[1].body.paid_ack, false)
  assert.equal(sent[1].body.duplicate_charge_ack, false)
})

test('成片字幕生成绑定原视频与输出摘要，透传 GPU 确认且不提交 Wan 镜头', async () => {
  const result = { id: 'graph b', state: 'succeeded', subtitles: { state: 'queued', job_id: 'subtitle-job' } }
  const sent = capture(result)
  assert.equal(typeof httpApi.generateWanStoryboardSubtitles, 'function')
  const input = { actor_id: 'user_local', compute_ack: true, expected_video_id: 'original-video',
    output_digest: 'original-hash', request_id: 'subtitle-request' }
  assert.deepEqual(await httpApi.generateWanStoryboardSubtitles('work a', 'graph b', input), result)
  assert.equal(sent[0].url, '/api/v1/works/work%20a/wan/storyboards/graph%20b/subtitles')
  assert.equal(sent[0].init.method, 'POST')
  assert.deepEqual(sent[0].body, input)
  await httpApi.generateWanStoryboardSubtitles('work a', 'graph b', { ...input, compute_ack: false })
  assert.equal(sent[1].body.compute_ack, false)
})

test('参考绑定与设置使用 PUT，读状态使用 GET 且不带请求体', async () => {
  const sent = capture({ ok: true })
  await httpApi.selectReference('w1', 'ref_1', 4)
  await httpApi.saveContentSettings('w1', { expected_content_revision: 4, style_snapshot: { persona: 'x' } })
  await httpApi.getContentState('w1')
  await httpApi.getContentJob('w1', 'j 1')
  assert.deepEqual(sent.map(s => s.init?.method ?? 'GET'), ['PUT', 'PUT', 'GET', 'GET'])
  assert.deepEqual(sent[0].body, { reference_snapshot_id: 'ref_1', expected_content_revision: 4 })
  assert.equal(sent[3].url, '/api/v1/works/w1/content/jobs/j%201')
  assert.equal(sent[2].init?.body, undefined)
})

test('选稿带修订号；旧调用不带时不发送该字段', async () => {
  const sent = capture({ ok: true, message: 'ok' })
  await httpApi.selectScript('w1', 's1', 7)
  await httpApi.selectScript('w1', 's1')
  assert.deepEqual(sent[0].body, { script_id: 's1', expected_content_revision: 7 })
  assert.deepEqual(sent[1].body, { script_id: 's1' })
})

test('ApiError 保留超时任务的 job_id 与 poll_url，其他错误没有', async () => {
  capture({ error: { code: 'CONTENT_JOB_PENDING', message: '仍在后台', retryable: false, job_id: 'cjob_1', poll_url: '/api/v1/works/w/content/jobs/cjob_1' } }, 504)
  await assert.rejects(httpApi.generateScripts('w', 't'), (e) => {
    assert.ok(e instanceof ApiError)
    assert.equal(e.code, 'CONTENT_JOB_PENDING'); assert.equal(e.jobId, 'cjob_1'); assert.match(e.pollUrl, /cjob_1$/); assert.equal(e.retryable, false)
    return true
  })
  capture({ error: { code: 'X', message: 'y' } }, 409)
  await assert.rejects(httpApi.selectScript('w', 's'), (e) => e.jobId === null && e.pollUrl === null && e.status === 409)
})

test('审核相关的 409 错误码原样透传，不被改写成成功', async () => {
  for (const code of ['SCRIPT_REVIEW_REQUIRED', 'SCRIPT_QUALITY_FAILED', 'STALE_SELECTION', 'CONTENT_REVISION_STALE', 'IDEMPOTENCY_CONFLICT']) {
    capture({ error: { code, message: code } }, 409)
    await assert.rejects(httpApi.selectScript('w', 's'), (e) => e.code === code && e.status === 409)
  }
})

test('Mock 内容接口：数据带演示标记，缺失互动数保持 null，重放同 request_id 返回原任务', async () => {
  const search = await mockApi.searchReferences('mw', { request_id: 'a', query: 'AI' })
  assert.match(search.result_refs.contract, /mock/)
  assert.ok(search.result_refs.items.every(i => i.caption.includes('演示数据')))
  assert.ok(search.result_refs.items.some(i => i.metrics.likes === null))
  const again = await mockApi.searchReferences('mw', { request_id: 'a', query: 'AI' })
  assert.equal(again.job_id, search.job_id); assert.equal(again.replayed, true)
  const imp = await mockApi.importReference('mw', { request_id: 'i', platform_work_id: 'mock_1' })
  const state = await mockApi.getContentState('mw')
  assert.equal(state.references[0].transcript_state, 'ready')
  assert.equal(imp.result_refs.transcript_state, 'ready')
})

test('Mock 内容接口：修订号过期同样返回 409 CONTENT_REVISION_STALE', async () => {
  await assert.rejects(mockApi.selectReference('mw2', 'ref_x', 99), (e) => e.code === 'CONTENT_REVISION_STALE')
})

test('参考视频上传走 multipart（不带 JSON 头），视觉分析带 paid_ack 与费用上限', async () => {
  const sent = []
  globalThis.fetch = async (url, init) => {
    sent.push({ url, init })
    const body = String(url).endsWith('/content/media') ? { asset: { id: 'media_1' } } : { job_id: 'j', kind: 'visual_analysis', state: 'queued' }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  const file = new File([new Uint8Array([1, 2, 3])], 'dance.mp4', { type: 'video/mp4' })
  await httpApi.uploadReferenceMedia('w1', 'ref_1', file)
  assert.equal(sent[0].url, '/api/v1/works/w1/content/media')
  assert.ok(sent[0].init.body instanceof FormData)
  assert.equal(sent[0].init.body.get('reference_snapshot_id'), 'ref_1')
  assert.equal(sent[0].init.headers?.['Content-Type'], undefined, 'multipart 边界必须由浏览器生成')
  await httpApi.startVisualAnalysis('w1', { request_id: 'r', reference_snapshot_id: 'ref_1', media_asset_id: 'media_1',
    expected_content_revision: 2, paid_ack: true, total_ceiling_fen: 80 })
  const body = JSON.parse(sent[1].init.body)
  assert.equal(sent[1].url, '/api/v1/works/w1/content/visual-analyses')
  assert.equal(body.paid_ack, true); assert.equal(body.total_ceiling_fen, 80)
})

test('视觉重新分析只透传用户明确提供的原调用与追加费用确认，不自行补默认授权', async () => {
  const sent = capture({ job_id: 'j', state: 'queued' })
  const input = { request_id: 'r', reference_snapshot_id: 'ref', media_asset_id: 'media',
    expected_content_revision: 2, paid_ack: true, total_ceiling_fen: 80 }
  await httpApi.startVisualAnalysis('w', input)
  await httpApi.startVisualAnalysis('w', { ...input, request_id: 'retry-r', retry_unknown_call_id: 'original-call', retry_paid_ack: true })
  assert.equal('retry_unknown_call_id' in sent[0].body, false)
  assert.equal('retry_paid_ack' in sent[0].body, false)
  assert.equal(sent[1].body.retry_unknown_call_id, 'original-call')
  assert.equal(sent[1].body.retry_paid_ack, true)
})

test('演示 API：分镜路线可选画面分析完成的参考，口播路线仍拒绝无口播参考；错误码与后端一致', async () => {
  const w = 'w_visual_mock'
  await mockApi.saveContentSettings(w, { expected_content_revision: 0, brief: { content_route: 'short_video_storyboard' } }).catch(() => {})
  const imp = await mockApi.importReference(w, { request_id: 'i1', platform_work_id: 'mock_2' })
  const rid = imp.result_refs.reference_id
  assert.equal(imp.result_refs.transcript_state, 'failed')
  await assert.rejects(() => mockApi.startVisualAnalysis(w, { request_id: 'v0', reference_snapshot_id: rid, media_asset_id: 'x', expected_content_revision: 0, paid_ack: false, total_ceiling_fen: 80 }),
    (e) => e.code === 'VISUAL_COST_CONFIRMATION_REQUIRED')
  await assert.rejects(() => mockApi.quoteVisualAnalysis(w, { reference_snapshot_id: rid, media_asset_id: '' }),
    (e) => e.code === 'REFERENCE_MEDIA_UNAVAILABLE')
  await assert.rejects(() => mockApi.uploadReferenceMedia(w, rid, new File([new Uint8Array([1])], 'x.exe')),
    (e) => e.code === 'MEDIA_TYPE_UNSUPPORTED')
  const { asset } = await mockApi.uploadReferenceMedia(w, rid, new File([new Uint8Array([1])], 'dance.mp4'))
  const state0 = await mockApi.getContentState(w)
  const rev = state0.content_revision
  const job = await mockApi.startVisualAnalysis(w, { request_id: 'v1', reference_snapshot_id: rid, media_asset_id: asset.id,
    expected_content_revision: rev, paid_ack: true, total_ceiling_fen: 80 })
  assert.equal(job.state, 'succeeded')
  const state = await mockApi.getContentState(w)
  const child = state.references.find(r => r.id === job.result_refs.reference_id)
  assert.equal(child.visual_state, 'ready')
  const orig = state.references.find(r => r.id === rid)
  assert.equal(orig.visual_state, 'none', '原参考保持不变')
})

test('读取任意参考详情使用独立 GET，返回完整快照且不带请求体', async () => {
  const sent = capture({ id: 'ref 1', work_id: 'w 1', transcript: { state: 'failed', text: '' } })
  const ref = await httpApi.getReferenceSnapshot('w 1', 'ref 1')
  assert.equal(ref.id, 'ref 1')
  assert.equal(ref.transcript.state, 'failed')
  assert.equal(sent[0].url, '/api/v1/works/w%201/content/references/ref%201')
  assert.equal(sent[0].init?.method ?? 'GET', 'GET')
  assert.equal(sent[0].init?.body, undefined)
})

test('mock 可读取未设为主参考的无口播详情，找不到或跨作品返回 404', async () => {
  const workId = 'mock_preview'
  const imported = await mockApi.importReference(workId, { request_id: 'preview', platform_work_id: 'mock_2' })
  const refId = imported.result_refs.reference_id
  const ref = await mockApi.getReferenceSnapshot(workId, refId)
  assert.equal(ref.id, refId)
  assert.equal(ref.transcript.state, 'failed')
  assert.equal((await mockApi.getContentState(workId)).reference, null)
  await assert.rejects(() => mockApi.getReferenceSnapshot('different-work', refId), (e) => e.code === 'REFERENCE_NOT_FOUND' && e.status === 404)
})

test('mock 提示准备遵守主参考路线门槛，visual-only 分镜可用而切到口播被阻断', async () => {
  const work = await mockApi.createWork({ name: '视觉参考提示测试', topic: '舞蹈', content_type: '', audience: '', keywords: [], target_duration_ms: 0 })
  const id = work.id
  await mockApi.saveContentSettings(id, { expected_content_revision: 0, brief: { content_route: 'short_video_storyboard' } })
  const imported = await mockApi.importReference(id, { request_id: 'i', platform_work_id: 'mock_2' })
  const refId = imported.result_refs.reference_id
  const { asset } = await mockApi.uploadReferenceMedia(id, refId, new File([new Uint8Array([1])], 'dance.mp4'))
  const before = await mockApi.getContentState(id)
  const analyzed = await mockApi.startVisualAnalysis(id, { request_id: 'v', reference_snapshot_id: refId, media_asset_id: asset.id,
    expected_content_revision: before.content_revision, paid_ack: true, total_ceiling_fen: 80 })
  await mockApi.selectReference(id, analyzed.result_refs.reference_id, (await mockApi.getContentState(id)).content_revision)
  const prepared = await mockApi.preparePrompt(id, { request_id: 'p' })
  assert.equal(prepared.state, 'succeeded')
  await mockApi.saveContentSettings(id, { expected_content_revision: (await mockApi.getContentState(id)).content_revision, brief: { content_route: 'talking_script' } })
  await assert.rejects(() => mockApi.preparePrompt(id, { request_id: 'blocked' }), (e) => e.code === 'REFERENCE_TRANSCRIPT_REQUIRED')
})
