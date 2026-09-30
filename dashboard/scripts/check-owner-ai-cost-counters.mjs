// Offline DOM proof against the real React component, with all AI API reads mocked.
// Install jsdom@26.1.0 into an isolated tools directory, then set
// OWNER_AI_COST_DOM_TOOLS=/path/to/tools/node_modules when running this script.
import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { execFileSync } from 'node:child_process'

const { JSDOM } = await import(process.env.OWNER_AI_COST_DOM_TOOLS
  ? pathToFileURL(path.join(process.env.OWNER_AI_COST_DOM_TOOLS, 'jsdom/lib/api.js')).href
  : 'jsdom')
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const baseline = process.argv.includes('--baseline')
const compiled = await build({
  stdin: { contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import AdminAiUsage from './src/pages/AdminAiUsage'; createRoot(document.getElementById('root')!).render(<AdminAiUsage/>);`, resolveDir: root, loader: 'tsx' },
  bundle: true, format: 'iife', write: false, jsx: 'automatic',
  define: { 'import.meta.env': '{}' },
  plugins: baseline ? [{ name: 'audited-baseline', setup(build) {
    build.onLoad({ filter: /\/src\/pages\/AdminAiUsage\.tsx$/ }, () => ({
      contents: execFileSync('git', ['show', '053a40c606a40f9e428651198b30ab388d9c8fc7:dashboard/src/pages/AdminAiUsage.tsx'], { cwd: root, encoding: 'utf8' }),
      loader: 'tsx', resolveDir: path.join(root, 'src/pages'),
    }))
  } }] : [],
})
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true })
const { window } = dom
const state = { extra: false, fail: false, hold: false, missingCost: false, requests: [], pending: [], timers: new Map() }
let timerId = 0
window.setInterval = (fn, ms) => { state.timers.set(++timerId, { fn, ms }); return timerId }
window.clearInterval = id => state.timers.delete(id)
window.AbortSignal.timeout = () => new window.AbortController().signal
window.Response = Response
function fixture(period) {
  const base = period === '24h' ? 0.004 : 0.0015
  const provenance = {
    period, period_start: '2026-09-23T00:00:00+00:00', period_end: '2026-09-30T14:40:00+00:00',
    period_timezone: 'UTC', cost_basis: 'tokens_x_versioned_rates',
    provider_reported_total_cost_usd: null, pricing_versions: { '2026-09-30-v2': 2 }, unpriced_calls: state.missingCost ? 1 : 0,
  }
  const rows = [1, 33].map(tenant_id => ({
    ...provenance, tenant_id, tenant_name: `Offline tenant ${tenant_id}`,
    turns_total: 1, turns_orchestrated: 1, ai_actions_logged: 0, avg_latency_ms: 10,
    calls_total: tenant_id === 1 && state.extra ? 2 : 1,
    actual_total_tokens: 1100, estimated_total_tokens: 0,
    actual_total_cost_usd: tenant_id === 1 && state.extra ? base * 2 : base,
    estimated_total_cost_usd: 0, unattributed_total_cost_usd: 0,
    providers: [{ provider: 'anthropic', count: 1 }],
    models: [{ model: 'claude-haiku-4-5-20251001', count: 1 }], reasons: [],
  }))
  return {
    usage: { tenants: rows, period },
    costs: { ...provenance, calls_total: rows.reduce((n, r) => n + r.calls_total, 0),
      actual_total_cost_usd: rows.reduce((n, r) => n + r.actual_total_cost_usd, 0),
      estimated_total_cost_usd: 0, unattributed_total_cost_usd: 0,
      actual_total_tokens: 2200, estimated_total_tokens: 0, providers: [], models: [], reasons: [], tenants: rows },
  }
}
window.fetch = async input => {
  const url = new URL(String(input), window.location.href)
  assert.match(url.pathname, /\/admin\/ai\//, 'All non-accounting requests are forbidden')
  state.requests.push(url.pathname)
  if (state.fail) throw new Error('offline read error')
  const period = url.searchParams.get('period') ?? '7d'
  const snapshot = fixture(period)
  const response = new Response(JSON.stringify(url.pathname.endsWith('/usage') ? snapshot.usage : snapshot.costs), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  })
  if (state.hold && period === '7d') return new Promise(resolve => state.pending.push(() => resolve(response)))
  return response
}
const row = tenant => [...window.document.querySelectorAll('tr')].find(r => r.textContent.includes(`Offline tenant ${tenant}`))
async function until(check) {
  for (let i = 0; i < 200; i++) {
    if (check()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
  throw new Error('Expected component update did not occur')
}
function tickRefresh() {
  assert.equal(state.timers.size, 1, 'One refresh interval only')
  const timer = [...state.timers.values()][0]
  assert.equal(timer.ms, 30_000)
  timer.fn()
}
try {
  window.eval(compiled.outputFiles[0].text)
  await until(() => row(1))
  if (baseline) {
    assert.match(row(1).textContent, /\$0\.0015/)
    assert.equal(state.requests.length, 2)
    state.extra = true
    await new Promise(resolve => setTimeout(resolve, 20))
    assert.equal(state.timers.size, 0)
    assert.equal(state.requests.length, 2)
    assert.match(row(1).textContent, /\$0\.0015/)
    assert.doesNotMatch(row(1).textContent, /\$0\.0030/)
    console.log('REPRO baseline: API fixture increases tenant 1, component schedules no refresh and keeps its old total')
  } else {
  assert.match(row(1).textContent, /\$0\.001500/)
  assert.match(row(33).textContent, /\$0\.001500/)
  console.log('PASS initial measured-token totals and two independent tenants')
  state.extra = true
  tickRefresh()
  await until(() => row(1)?.textContent.includes('$0.003000'))
  assert.match(row(33).textContent, /\$0\.001500/)
  console.log('PASS 30-second refresh increases tenant 1; tenant 33 unchanged')
  state.fail = true
  tickRefresh()
  await until(() => window.document.querySelector('[role="alert"]'))
  assert.equal(window.document.querySelector('table'), null)
  assert.doesNotMatch(window.document.body.textContent, /\$0\.000000/)
  console.log('PASS read failure shows unavailable; no zero or stale totals')
  state.fail = false
  window.document.querySelector('button').click()
  await until(() => row(1))
  console.log('PASS manual retry restores data')
  state.hold = true
  tickRefresh()
  await until(() => state.pending.length === 2)
  const selector = window.document.querySelector('select')
  selector.value = '24h'
  selector.dispatchEvent(new window.Event('change', { bubbles: true }))
  await until(() => row(1)?.textContent.includes('$0.008000'))
  state.pending.forEach(fn => fn())
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.match(row(1).textContent, /\$0\.008000/)
  assert.equal(state.timers.size, 1)
  console.log('PASS old period responses cannot replace the selected period; timer cleanup works')
  assert.match(window.document.body.textContent, /مبلغ الفاتورة الفعلي من المزود غير متاح/)
  assert.match(window.document.body.textContent, /UTC/)
  console.log('PASS recorded tokens, estimated tokens, unavailable provider bill and timezone are explicit')
  state.missingCost = true
  tickRefresh()
  await until(() => window.document.body.textContent.includes('إجمالي التكلفة غير مكتمل'))
  assert.doesNotMatch(window.document.body.textContent, /\$0\.000000/)
  console.log('PASS missing stored cost is incomplete and cannot masquerade as zero dollars')
  }
} finally {
  window.close()
}
