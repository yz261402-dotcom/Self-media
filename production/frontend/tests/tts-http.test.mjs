import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const output = await mkdtemp(resolve('node_modules/.tts-http-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({ entryPoints: ['src/api/http.ts'], outfile: join(output, 'http.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { httpApi } = await import(pathToFileURL(join(output, 'http.mjs')).href)
await build({ entryPoints: ['src/api/mock.ts'], outfile: join(output, 'mock.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { mockApi } = await import(pathToFileURL(join(output, 'mock.mjs')).href)

test('controls are saved with PUT and one nested controls payload', async () => {
  const controls = { emotion: 'auto', speed: 1.25, vol: 1, pitch: -2, voice_modify: { pitch: 3, intensity: -4, timbre: 5 } }
  let sent
  globalThis.fetch = async (url, init) => { sent = { url, init }; return new Response(JSON.stringify({ id: 'work', tts_controls: controls })) }
  assert.equal(typeof httpApi.updateTtsControls, 'function')
  const result = await httpApi.updateTtsControls('work', controls)
  assert.equal(sent.url, '/api/v1/works/work/tts-controls')
  assert.equal(sent.init.method, 'PUT')
  assert.deepEqual(JSON.parse(sent.init.body), { controls })
  assert.equal(result.tts_controls.emotion, 'auto')
})

test('普通试听保留缓存，只有显式重新试听提交force_new', async () => {
  const sent = []
  globalThis.fetch = async (url, init) => { sent.push(JSON.parse(init.body)); return new Response(JSON.stringify({ cached: false, audio: {} })) }
  await httpApi.createVoicePreview('work', 'voice', '同一句')
  await httpApi.createVoicePreview('work', 'voice', '同一句', true)
  assert.equal(sent[0].force_new, false)
  assert.equal(sent[1].force_new, true)
})

test('cloning submits exactly the acknowledged request then queries its original id', async () => {
  const input = { request_id: 'request-id', name: '本人声音', filename: '本人.wav', content_base64: 'YXVkaW8=', source_evidence: '本人录制', rights_ack: true, activation_cost_ack: true }
  const sent = []
  globalThis.fetch = async (url, init) => { sent.push({ url, init }); return new Response(JSON.stringify({ voice: null, clone_job: { request_id: input.request_id, state: 'unknown' } })) }
  assert.equal(typeof httpApi.createVoiceClone, 'function')
  assert.equal(typeof httpApi.getVoiceClone, 'function')
  await httpApi.createVoiceClone(input)
  await httpApi.getVoiceClone(input.request_id)
  assert.equal(sent[0].url, '/api/v1/voice-clones')
  assert.deepEqual(JSON.parse(sent[0].init.body), input)
  assert.equal(sent[1].url, '/api/v1/voice-clones/request-id')
  assert.equal(sent.length, 2)
})

test('mock cloning is blocked without a provider request or a production voice', async () => {
  globalThis.fetch = async () => { throw new Error('不允许真实请求') }
  assert.equal(typeof mockApi.createVoiceClone, 'function')
  assert.equal(typeof mockApi.getVoiceClone, 'function')
  const result = await mockApi.createVoiceClone({ request_id: 'mock-request', name: '本人', filename: '本人.wav', content_base64: 'YQ==', source_evidence: '本人录制', rights_ack: true, activation_cost_ack: true })
  assert.equal(result.voice, null)
  assert.equal(result.clone_job.state, 'blocked')
  const job = await mockApi.getVoiceClone('mock-request')
  assert.equal(job.state, 'blocked')
})
