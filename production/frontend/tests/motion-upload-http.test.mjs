import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const output = await mkdtemp(resolve('node_modules/.motion-http-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({ entryPoints: ['src/api/http.ts'], outfile: join(output, 'http.mjs'), bundle: true, platform: 'node', format: 'esm' })
const { httpApi } = await import(pathToFileURL(join(output, 'http.mjs')).href)
const file = { name: '点头 01.mov', size: 42 }
let sent
class Xhr {
  constructor() { this.headers = {}; this.upload = {}; this.responseText = ''; sent = this }
  open(method, path) { this.method = method; this.path = path }
  setRequestHeader(name, value) { this.headers[name] = value }
  send(body) { this.body = body }
  abort() { this.onabort?.() }
  finish(status, data) { this.status = status; this.responseText = JSON.stringify(data); this.onload?.() }
}
globalThis.XMLHttpRequest = Xhr

test('XHR sends raw File and encoded original filename, and reports actual transferred bytes', async () => {
  const progress = []
  const result = { id: 'up', state: 'pending' }
  const promise = httpApi.uploadMotion('人物/甲', file, (loaded, total) => progress.push([loaded, total]))
  assert.equal(sent.method, 'POST')
  assert.equal(sent.path, '/api/v1/identities/%E4%BA%BA%E7%89%A9%2F%E7%94%B2/motion-uploads')
  assert.equal(sent.headers['Content-Type'], 'application/octet-stream')
  assert.equal(sent.headers['X-File-Name'], encodeURIComponent(file.name))
  assert.equal(sent.body, file)
  sent.upload.onprogress({ loaded: 21, total: 42, lengthComputable: true })
  assert.deepEqual(progress, [[21, 42]])
  sent.finish(200, result)
  assert.deepEqual(await promise, result)
})

test('an already cancelled upload is rejected without sending a request', async () => {
  sent = null
  const controller = new AbortController()
  controller.abort()
  const promise = httpApi.uploadMotion('person', file, undefined, controller.signal)
  if (sent) sent.finish(200, { id: 'should-not-send' })
  await assert.rejects(promise, error => error.code === 'UPLOAD_ABORTED')
  assert.equal(sent, null)
})

test('completed upload clears its abort listener and a timeout reports a recoverable error', async () => {
  let callback
  let removed = 0
  const signal = { aborted: false, addEventListener(_, fn) { callback = fn }, removeEventListener(_, fn) { assert.equal(fn, callback); removed++ } }
  const promise = httpApi.uploadMotion('person', file, undefined, signal)
  assert.ok(sent.timeout >= 900_000)
  sent.finish(200, { id: 'up' })
  await promise
  assert.equal(removed, 1)
  const timed = httpApi.uploadMotion('person', file)
  sent.ontimeout()
  await assert.rejects(timed, error => error.code === 'UPLOAD_TIMEOUT')
})
