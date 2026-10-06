import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const output = await mkdtemp(resolve('node_modules/.delete-http-tests-'))
after(() => rm(output, { recursive: true, force: true }))
await build({ entryPoints: ['src/api/http.ts', 'src/api/mock.ts'], outdir: output, bundle: true, platform: 'node', format: 'esm', outExtension: { '.js': '.mjs' } })
const { httpApi } = await import(pathToFileURL(join(output, 'http.mjs')).href)
const { mockApi } = await import(pathToFileURL(join(output, 'mock.mjs')).href)

for (const [method, route] of [['deleteWork', 'works'], ['deleteIdentity', 'avatar-identities']]) {
  test(`${method} uses DELETE with explicit confirmation and an encoded id`, async () => {
    const receipt = { id: 'one/two', deleted_at: '2026-10-05T00:00:00Z' }
    let sent
    globalThis.fetch = async (url, init) => { sent = { url, init }; return new Response(JSON.stringify(receipt)) }
    assert.equal(typeof httpApi[method], 'function')
    assert.deepEqual(await httpApi[method](receipt.id), receipt)
    assert.equal(sent.url, `/api/v1/${route}/one%2Ftwo`)
    assert.equal(sent.init.method, 'DELETE')
    assert.deepEqual(JSON.parse(sent.init.body), { confirmed: true })
  })
  test(`${method} preserves the server conflict reason`, async () => {
    globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: 'DELETE_BLOCKED', message: '仍被作品「测试一期」引用' } }), { status: 409 })
    assert.equal(typeof httpApi[method], 'function')
    await assert.rejects(httpApi[method]('one'), error => error.status === 409 && error.message === '仍被作品「测试一期」引用')
  })
}

test('mock deletes are idempotent and keep deleted people out of future works', async () => {
  assert.equal(typeof mockApi.deleteWork, 'function')
  assert.equal(typeof mockApi.deleteIdentity, 'function')
  const identities = await mockApi.listIdentities()
  const person = identities.find(identity => identity.id === 'av_base_b_v1')
  assert.ok(person)
  const work = await mockApi.createWork({ name: '离线删除测试', topic: '测试', content_type: '测试', audience: '测试', keywords: [], target_duration_ms: 0 })
  await assert.rejects(mockApi.deleteIdentity(person.id), /引用/)
  const receipt = await mockApi.deleteWork(work.id)
  assert.deepEqual(await mockApi.deleteWork(work.id), receipt)
  assert.ok(!(await mockApi.listWorks()).some(item => item.id === work.id))
  const deletedPerson = await mockApi.deleteIdentity(person.id)
  assert.deepEqual(await mockApi.deleteIdentity(person.id), deletedPerson)
  assert.ok(!(await mockApi.listIdentities(true)).some(item => item.id === person.id))
  assert.equal((await mockApi.createWork({ name: '删除后创建', topic: '', content_type: '', audience: '', keywords: [], target_duration_ms: 0 })).identity_id, null)
})
