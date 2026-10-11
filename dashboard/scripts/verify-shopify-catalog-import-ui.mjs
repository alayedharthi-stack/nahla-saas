/**
 * Real-browser verification of the Shopify catalog import step (mounted
 * React, Vite dev server with StrictMode, Chromium). Local / manual only
 * (needs node_modules + Chromium); CI runs the source-level
 * check-shopify-catalog-import-ui.mts instead.
 *
 * Every browser request is routed: the app origin to the local dev server,
 * the API host (an unresolvable `.invalid` name) to in-process mocks, and
 * anything else is aborted and reported. Nothing can reach a live system.
 *
 * Scenarios
 *   1. completed import → same-session disconnect: the "complete" badge and
 *      the start action disappear as soon as the parent confirms the
 *      disconnect, and stay absent (fail closed) while the step re-reads.
 *   2. back/forward restore and pageshow: a parent refresh after the
 *      connection changed elsewhere removes the stale badge and start action.
 *   3. available=false never offers start, even when an entry says can_start.
 *   4. support impersonation sees the state, never a start action.
 *   5. backend import route off (404) → the card's original static step.
 *   6. UI flag off → the original static step; the import route is never called.
 *   7. evidence renders the API version; a products-only import says
 *      inventory was not read.
 *   8. a failed connection attempt (invalid shop, server refusal) leaves the
 *      already connected shop's import start available and working.
 *
 * Run (from dashboard/): node scripts/verify-shopify-catalog-import-ui.mjs
 */
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const API = 'https://api.shopify-import-ui-test.invalid'
const PORT_ON = 3291
const PORT_OFF = 3292
const ON = `http://127.0.0.1:${PORT_ON}`
const OFF = `http://127.0.0.1:${PORT_OFF}`
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium'
const SHOP = 'generic-shoes-store.myshopify.com'
const STATUS = 'GET /merchant/integrations/shopify/status'
const DISCONNECT = 'POST /merchant/integrations/shopify/disconnect'
const IMPORT_STATUS = 'GET /merchant/integrations/shopify/catalog-import/status'
const IMPORT_START = 'POST /merchant/integrations/shopify/catalog-import/start'
const CONNECT_START = 'POST /merchant/integrations/shopify/start'

let passed = 0
let failed = 0
function check(name, ok, detail = '') {
  if (ok) {
    passed++
    console.log(`OK   ${name}`)
  } else {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function waitFor(fn, timeout = 15_000, label = 'condition') {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (await fn()) return true
    await sleep(50)
  }
  throw new Error(`timeout waiting for ${label}`)
}
function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}
const tok = (n) => randomBytes(n).toString('base64url')
const b64u = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
const jwt = (claims) => `${b64u({ alg: 'HS256', typ: 'JWT' })}.${b64u(claims)}.${tok(16)}`
const merchantToken = (extra = {}) => jwt({
  sub: 'merchant@example.test', tenant_id: 101, user_id: 7, role: 'merchant', jti: tok(12),
  exp: Math.floor(Date.now() / 1000) + 7 * 24 * 3600, ...extra,
})

// ── Servers ───────────────────────────────────────────────────────────────────
function startVite(port, importUi) {
  const env = {
    ...process.env, VITE_API_BASE: API, VITE_SHOPIFY_CONNECTION_UI: '1',
    VITE_SHOPIFY_CATALOG_IMPORT_UI: importUi ? '1' : '0', BROWSER: 'none',
  }
  const child = spawn(join(ROOT, 'node_modules', '.bin', 'vite'), ['--port', String(port), '--strictPort', '--host', '127.0.0.1'],
    { cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] })
  let out = ''
  child.stdout.on('data', (d) => { out += d })
  child.stderr.on('data', (d) => { out += d })
  return {
    child,
    ready: waitFor(async () => {
      try { return (await fetch(`http://127.0.0.1:${port}/`)).ok } catch { return false }
    }, 60_000, `vite :${port}`).catch((e) => { throw new Error(`${e.message}\n${out}`) }),
  }
}

// ── Fixtures ──────────────────────────────────────────────────────────────────
const ACTIVE = { shop_domain: SHOP, status: 'active', scopes: ['read_products'], connected_at: '2026-10-10T08:00:00+00:00',
  disconnected_at: null, disconnect_reason: null, needs_reconnect: false }
const TOMB = { ...ACTIVE, status: 'disconnected', disconnected_at: '2026-10-11T09:00:00+00:00',
  disconnect_reason: 'tenant_disconnect', needs_reconnect: true }
const connectionStatus = (connections) => ({ available: true, reason: null, requested_scopes: ['read_products'], connections })
const evidence = (over = {}) => ({
  current_installation: true, completed_at: '2026-10-11T05:00:00+00:00', api_version: '2026-10', run: 1,
  products: 3, variants: 4, inventory_items: 4, removed: { products: 0, variants: 0 },
  inventory: { mode: 'scope_missing', locations: null, levels: null }, ...over,
})
const importStatus = (entries, available = true) => ({ available, reason: available ? null : 'client_credentials_missing',
  api_version: '2026-10', connections: entries })
const importEntry = (status, imp) => ({ shop_domain: SHOP, connection_status: status,
  import: { state: 'not_started', reason: null, started_at: null, last_success: null, can_start: true, ...imp } })
const IMPORTED = importEntry('active', { state: 'imported', last_success: evidence(), can_start: true })
const AFTER_DISCONNECT = importEntry('disconnected', {
  state: 'not_started', last_success: evidence({ current_installation: false }), can_start: false })

async function scenario(browser, origin, mocks, { token } = {}) {
  const ctx = await browser.newContext({ serviceWorkers: 'block', locale: 'en-US' })
  const trace = { api: [], external: [], pageErrors: [] }
  const cors = {
    'access-control-allow-origin': origin, 'access-control-allow-headers': 'Authorization, Content-Type, X-Tenant-ID',
    'access-control-allow-methods': 'GET, POST, OPTIONS', vary: 'Origin',
  }
  const base = {
    'GET /salla/whoami': async () => ({ status: 200, body: { salla_integration: { connected: false } } }),
    'GET /whatsapp/status': async () => ({ status: 200, body: { connected: false } }),
    'GET /zid/status': async () => ({ status: 200, body: { connected: false } }),
  }
  await ctx.addInitScript(({ t }) => {
    try {
      if (!localStorage.getItem('nahla_token')) {
        localStorage.setItem('nahla_auth', '1')
        localStorage.setItem('nahla_token', t)
        localStorage.setItem('nahla_role', 'merchant')
        localStorage.setItem('nahla_tenant_id', '101')
        localStorage.setItem('nahla_user_id', '7')
      }
      if (!localStorage.getItem('nahla-lang')) localStorage.setItem('nahla-lang', 'en')
    } catch { /* storage blocked */ }
    window.addEventListener('pageshow', (e) => { window.__pwPageshowPersisted = e.persisted })
  }, { t: token || merchantToken() })
  await ctx.route('**/*', async (route, req) => {
    const url = new URL(req.url())
    if (url.origin === origin) return route.continue()
    if (url.origin === API) {
      if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
      const key = `${req.method()} ${url.pathname}`
      trace.api.push(key)
      const handler = mocks[key] || base[key]
      const res = handler ? await handler(req) : { status: 404, body: { detail: 'not_mocked_in_test' } }
      try {
        await route.fulfill({ status: res.status, headers: { ...cors, 'content-type': 'application/json' },
          body: JSON.stringify(res.body ?? {}) })
      } catch { /* the page went away */ }
      return
    }
    trace.external.push(`${url.origin}${url.pathname}`)
    return route.abort('blockedbyclient')
  })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => trace.pageErrors.push(String(e)))
  return { ctx, page, trace }
}

const STEP = '[data-testid="shopify-step-import"]'
const START = '[data-testid="shopify-import-start"]'
const count = (s, key) => s.trace.api.filter((k) => k === key).length
async function stepState(page) {
  return page.$$eval(STEP, (els) => els.map((e) => e.getAttribute('data-step')))
}

async function run(name, fn) {
  try {
    await fn()
  } catch (e) {
    check(name, false, String(e && e.message ? e.message : e))
  }
}

async function main() {
  const on = startVite(PORT_ON, true)
  const off = startVite(PORT_OFF, false)
  const browser = await chromium.launch({ executablePath: EXEC })
  try {
    await on.ready
    await off.ready

    await run('completed import → same-session disconnect', async () => {
      const state = { connections: [ACTIVE], imports: [IMPORTED], hold: null }
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus(state.connections) }),
        [DISCONNECT]: async () => {
          state.connections = [TOMB]
          state.imports = [AFTER_DISCONNECT]
          state.hold = deferred()
          return { status: 200, body: { connection: TOMB } }
        },
        [IMPORT_STATUS]: async () => {
          if (state.hold) await state.hold.promise
          return { status: 200, body: importStatus(state.imports) }
        },
      })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector(`${STEP}[data-step="imported"]`)
      check('1: completed import shows complete + start', await s.page.isVisible(START))
      check('1: evidence renders the API version', (await s.page.textContent('[data-testid="shopify-import-api-version"]')).includes('2026-10'))
      check('1: products-only evidence says inventory was not read',
        (await s.page.getAttribute('[data-testid="shopify-import-evidence"]', 'data-inventory')) === 'scope_missing')
      await s.page.click('[data-testid="shopify-disconnect-button"]')
      await s.page.waitForSelector('[role="dialog"]')
      await s.page.click('[role="dialog"] button:text-is("Disconnect")')
      await s.page.waitForSelector('[data-testid="shopify-disconnect-done"]')
      // The parent confirmed the disconnect; the step's own re-read is held.
      await waitFor(async () => !(await s.page.$(`${STEP}[data-step="imported"]`)), 5_000, 'badge removed')
      check('1: complete badge gone while the step re-reads (fail closed)', !(await s.page.$(`${STEP}[data-step="imported"]`)))
      check('1: start action gone while the step re-reads', !(await s.page.$(START)))
      state.hold.resolve()
      await waitFor(async () => (await stepState(s.page)).includes('not_started'), 10_000, 'not_started after release')
      check('1: after the re-read: not started, no start action', !(await s.page.$(START))
        && !(await s.page.$(`${STEP}[data-step="imported"]`)))
      check('1: earlier success labelled as a previous connection',
        (await s.page.textContent('[data-testid="shopify-import-evidence"]')).includes('earlier connection'))
      // Same rule as verify-shopify-connection-ui.mjs: only the app shell's font
      // stylesheet is attempted outside the sandbox, and it is aborted here.
      check('1: nothing left the sandbox, no page error',
        s.trace.external.every((u) => u.startsWith('https://fonts.')) && s.trace.pageErrors.length === 0,
        JSON.stringify([s.trace.external, s.trace.pageErrors]))
      await s.ctx.close()
    })

    await run('back/forward restore and pageshow', async () => {
      const state = { connections: [ACTIVE], imports: [IMPORTED] }
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus(state.connections) }),
        [IMPORT_STATUS]: async () => ({ status: 200, body: importStatus(state.imports) }),
      })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector(`${STEP}[data-step="imported"]`)
      // Disconnected elsewhere (another tab / device): the page is not told.
      state.connections = [TOMB]
      state.imports = [AFTER_DISCONNECT]
      await s.page.goto(`${ON}/robots.txt`)
      await s.page.goBack()
      const persisted = await s.page.evaluate(() => window.__pwPageshowPersisted === true)
      if (!persisted) {
        // No real back/forward-cache restore here: deliver the same event.
        await s.page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
      }
      await waitFor(async () => !(await s.page.$(`${STEP}[data-step="imported"]`)) && !(await s.page.$(START)),
        10_000, 'stale badge removed')
      check(`2: after ${persisted ? 'a real bfcache restore' : 'pageshow'} the stale badge and start are gone`, true)
      await waitFor(async () => (await stepState(s.page)).includes('not_started'), 10_000, 'not_started')
      // Same tab, synthetic pageshow while still connected elsewhere: badge returns only with server evidence.
      state.connections = [ACTIVE]
      state.imports = [IMPORTED]
      await s.page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
      await s.page.waitForSelector(`${STEP}[data-step="imported"]`)
      check('2: a later authoritative read shows the committed state again', await s.page.isVisible(START))
      await s.ctx.close()
    })

    await run('available=false never offers start', async () => {
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus([ACTIVE]) }),
        [IMPORT_STATUS]: async () => ({ status: 200, body: importStatus([importEntry('active', { can_start: true })], false) }),
      })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector(`${STEP}[data-step="not_started"]`)
      await sleep(300)
      check('3: available=false → no start action', !(await s.page.$(START)))
      check('3: no start request', count(s, IMPORT_START) === 0)
      await s.ctx.close()
    })

    await run('support impersonation is read-only', async () => {
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus([ACTIVE]) }),
        [IMPORT_STATUS]: async () => ({ status: 200, body: importStatus([IMPORTED]) }),
      }, { token: merchantToken({ role: 'support_impersonation', impersonation: true }) })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector(`${STEP}[data-step="imported"]`)
      check('4: support session sees the state but no start action', !(await s.page.$(START)))
      await s.ctx.close()
    })

    await run('backend import route off', async () => {
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus([ACTIVE]) }),
        [IMPORT_STATUS]: async () => ({ status: 404, body: { detail: { error: 'not_found' } } }),
      })
      await s.page.goto(`${ON}/integrations`)
      await waitFor(async () => (await s.page.textContent('[data-testid="shopify-onboarding-steps"]') || '')
        .includes('not available at this stage'), 10_000, 'static step')
      check('5: 404 → the original static step', (await stepState(s.page)).join(',') === 'not_started' && !(await s.page.$(START)))
      await s.ctx.close()
    })

    await run('failed connect keeps the connected shop importable', async () => {
      const state = { imports: [importEntry('active', { can_start: true })] }
      const s = await scenario(browser, ON, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus([ACTIVE]) }),
        [CONNECT_START]: async () => ({ status: 409, body: { detail: { error: 'shop_unavailable' } } }),
        [IMPORT_STATUS]: async () => ({ status: 200, body: importStatus(state.imports) }),
        [IMPORT_START]: async () => {
          state.imports = [importEntry('active', { state: 'importing', started_at: '2026-10-11T06:00:00+00:00', can_start: false })]
          return { status: 202, body: { status: 'importing', shop_domain: SHOP } }
        },
      })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector(`${STEP}[data-step="not_started"]`)
      await s.page.waitForSelector(START)
      // (a) an invalid shop: refused locally, no request.
      await s.page.fill('#shopify-shop-input', 'not a shop!')
      await s.page.click('[data-testid="shopify-connect-button"]')
      await s.page.waitForSelector('[data-testid="shopify-start-error"]')
      check('8a: invalid connect attempt shows its error', count(s, CONNECT_START) === 0)
      check('8a: the connected shop can still start its import',
        await s.page.isVisible(START) && await s.page.isEnabled(START))
      // (b) a server refusal for another shop.
      await s.page.fill('#shopify-shop-input', 'another-generic-store.myshopify.com')
      await s.page.click('[data-testid="shopify-connect-button"]')
      await waitFor(async () => count(s, CONNECT_START) === 1, 10_000, 'connect refused')
      await s.page.waitForSelector('[data-testid="shopify-start-error"]')
      // The refusal makes the card re-read status; the step fails closed for
      // that re-read, then must offer the connected shop's start again.
      const reoffered = await waitFor(async () => {
        const el = await s.page.$(START)
        return !!el && await el.isEnabled()
      }, 10_000, 'import start re-offered').catch(() => false)
      check('8b: refused connect attempt leaves the import start enabled (after the re-read)',
        reoffered && await s.page.isVisible('[data-testid="shopify-start-error"]'))
      await s.page.click(START)
      await waitFor(async () => count(s, IMPORT_START) === 1, 10_000, 'import start request')
      await s.page.waitForSelector(`${STEP}[data-step="importing"]`)
      check('8b: the import start was sent and the step shows importing', !(await s.page.$(START)))
      await s.ctx.close()
    })

    await run('UI flag off', async () => {
      const s = await scenario(browser, OFF, {
        [STATUS]: async () => ({ status: 200, body: connectionStatus([ACTIVE]) }),
        [IMPORT_STATUS]: async () => ({ status: 200, body: importStatus([IMPORTED]) }),
      })
      await s.page.goto(`${OFF}/integrations`)
      await s.page.waitForSelector('[data-testid="shopify-onboarding-steps"]')
      await sleep(500)
      check('6: flag off → original static step', (await stepState(s.page)).join(',') === 'not_started'
        && (await s.page.textContent('[data-testid="shopify-onboarding-steps"]')).includes('not available at this stage'))
      check('6: flag off → the import route is never called', count(s, IMPORT_STATUS) === 0 && count(s, IMPORT_START) === 0)
      await s.ctx.close()
    })
  } finally {
    await browser.close()
    on.child.kill('SIGTERM')
    off.child.kill('SIGTERM')
  }
  console.log(`\n${passed} passed, ${failed} failed`)
  if (failed) process.exit(1)
  console.log('shopify catalog import browser verification OK')
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
