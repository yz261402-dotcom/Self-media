/**
 * 端到端冒烟（离线 / mock）：验证七入口工作台前四步的制作闭环与状态流转。
 * 不调用任何付费服务、不产生真实媒体；仅用于 L0 结构与恢复检查。
 * 用法：node scripts/e2e_smoke.mjs
 */
const BASE = process.env.BASE || 'http://127.0.0.1:8000'
const api = async (p, init) => {
  const r = await fetch(BASE + p, {
    headers: { 'Content-Type': 'application/json' },
    ...(init || {}),
  })
  const t = await r.text()
  return { status: r.status, body: t ? JSON.parse(t) : null }
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const assert = (cond, msg) => {
  if (!cond) throw new Error('断言失败：' + msg)
  console.log('  ✓', msg)
}

async function main() {
  console.log('1) 创建作品')
  const w = await post('/api/v1/works', {
    name: 'E2E 冒烟',
    topic: '生成一期关于 RAG 的 AI 科普视频',
    audience: '对 AI 感兴趣的年轻受众',
    keywords: ['RAG', '检索'],
    target_duration_ms: 60000,
  })
  assert(w.status === 200, '作品创建成功')
  const workId = w.body.id

  console.log('2) 生成三稿')
  const s = await post(`/api/v1/works/${workId}/scripts`, { topic: '生成一期关于 RAG 的 AI 科普视频' })
  assert(s.status === 200 && s.body.candidates.length === 3, '返回三个不同角度候选')
  assert(new Set(s.body.candidates.map((c) => c.angle)).size === 3, '三个角度互不相同')

  console.log('3) 未通过硬门禁的稿件必须被拒收')
  const bad = s.body.candidates.find((c) => c.quality_report.overall === 'fail')
  const badSel = await post(`/api/v1/works/${workId}/script-selection`, { script_id: bad.id })
  assert(badSel.status === 422 && badSel.body.error.code === 'QUALITY_GATE_FAILED', '硬门禁失败稿被拒收')

  console.log('4) 采用合格稿件')
  const good = s.body.candidates.find((c) => c.quality_report.overall === 'pass')
  const ok = await post(`/api/v1/works/${workId}/script-selection`, { script_id: good.id })
  assert(ok.status === 200 && ok.body.ok, '采用成功')

  console.log('5) 全文合成与确认')
  const a = await post(`/api/v1/works/${workId}/audio`, {})
  assert(a.status === 200, '全文音频已合成')
  const ac = await post(`/api/v1/works/${workId}/audio-confirmation`, { audio_id: a.body.id })
  assert(ac.status === 200 && ac.body.ok, '音频确认成功')

  console.log('6) 未确认预算时提交必须被阻断')
  const t0 = await post('/api/v1/tasks', { work_id: workId })
  assert(t0.status === 200 && t0.body.state === 'blocked', 'blocked：COST_CONFIRMATION_REQUIRED')

  console.log('7) 确认路线与预算后提交')
  const rc = await post(`/api/v1/works/${workId}/route`, { route: 'M2' })
  assert(rc.status === 200, '路线已确认')
  const t1 = await post('/api/v1/tasks', { work_id: workId })
  assert(t1.status === 200 && t1.body.state === 'running', '任务进入执行')

  console.log('8) 轮询至完成')
  let task = t1.body
  for (let i = 0; i < 60; i++) {
    await sleep(1000)
    const r = await api(`/api/v1/tasks/${task.id}`)
    task = r.body
    if (['succeeded', 'failed', 'cancelled', 'blocked'].includes(task.state)) break
  }
  assert(task.state === 'succeeded', '任务成功完成')
  assert(task.percent === 100 || task.percent === null, '进度状态合法')

  console.log('9) 发布就绪包')
  const pkg = await api(`/api/v1/works/${workId}/publish-package`)
  assert(pkg.status === 200 && pkg.body && pkg.body.state === 'ready', '包已就绪')
  assert(pkg.body.files.length >= 8, '包内文件清单完整')

  console.log('10) 后三项入口状态')
  const f = await api('/api/v1/features')
  const byId = Object.fromEntries(f.body.map((x) => [x.feature_id, x]))
  assert(byId.publish.feature_state === 'planned', '发布为规划中')
  assert(byId.metrics.feature_state === 'planned', '指标监控为规划中')
  assert(byId.models.feature_state === 'developing', '模型选择为开发中')

  console.log('\n全部通过 ✅')
}

main().catch((e) => {
  console.error('冒烟失败：', e.message)
  process.exit(1)
})
