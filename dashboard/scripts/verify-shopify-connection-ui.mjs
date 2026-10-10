/**
 * Real-browser UI verification for the dormant Shopify connection UI.
 * Local / manual only (needs node_modules + Chromium); CI runs the
 * source-level check-shopify-connection-ui.mts instead.
 *
 *  - Two targets, same scenarios: the Vite dev server (React StrictMode
 *    double mount active) and the SHIPPED production build (vite build into a
 *    scratch dir, served by a plain static server that adds no headers). Each
 *    target has a UI-flag-ON and a flag-OFF instance. The API base is an
 *    unresolvable `.invalid` host.
 *  - Every browser request is routed: the app origin goes to the local dev
 *    server, the API host to in-process mocks (including the Salla / Zid /
 *    WhatsApp status calls of /integrations), the Sentry DSN host to a
 *    recorder, `*.myshopify.com` to a stub page; anything else is ABORTED and
 *    reported. Nothing can reach a live system.
 *  - Sentinels (handle, code, state, hmac, provider text) are generated at
 *    runtime. After each scenario every request URL / header / body, console
 *    line, page error, telemetry call (posthog / gtag fakes), Sentry envelope,
 *    localStorage, sessionStorage, IndexedDB, history.state, cookie, final
 *    URL and DOM is scanned. The only allowed transport is the handle in the
 *    POST /complete body (and the state in the validated Shopify URL). The
 *    explicit test navigation itself (its URL may carry a query, sent before
 *    any client code) is classified separately; no subresource Referer,
 *    telemetry or Sentry data is exempted.
 *
 * Run (from dashboard/): node scripts/verify-shopify-connection-ui.mjs [scratchDir]
 */
import { spawn, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, extname, join, normalize, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const API = 'https://api.shopify-ui-test.invalid'
const SENTRY_HOST = 'sentry.shopify-ui-test.invalid'
const PORT_ON = 3191
const PORT_OFF = 3192
const PORT_PROD_ON = 3193
const PORT_PROD_OFF = 3194
const SCRATCH = process.argv[2] || join(tmpdir(), 'shopify-ui-verify')
const EXEC = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium'
const COMPLETE = '/integrations/shopify/complete'
const SHOP = 'my-store.myshopify.com'
const FIRST_SHOP = 'first-tenant-store.myshopify.com'

let passed = 0
let failed = 0
const summary = []
function check(name, ok, detail = '') {
  if (ok) {
    passed++
    console.log(`OK   ${name}`)
  } else {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

const tok = (bytes) => randomBytes(bytes).toString('base64url')
const b64u = (o) => Buffer.from(JSON.stringify(o)).toString('base64url')
function jwt(claims) {
  return `${b64u({ alg: 'HS256', typ: 'JWT' })}.${b64u(claims)}.${tok(16)}`
}
function merchantToken(extra = {}) {
  return jwt({
    sub: 'merchant@example.test', tenant_id: 101, user_id: 7, role: 'merchant', jti: tok(12),
    exp: Math.floor(Date.now() / 1000) + 7 * 24 * 3600, ...extra,
  })
}
function newSentinels() {
  return {
    handle: tok(32), // 43 url-safe chars, like secrets.token_urlsafe(32)
    code: `code${tok(12)}`,
    state: `state${tok(24)}`,
    hmac: `hmac${tok(16)}`,
    provider: `provider${tok(12)}`,
  }
}
function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
/** Bound any await on browser state so a stuck page can never wedge the run. */
const bounded = (promise, ms, fallback) => Promise.race([promise, sleep(ms).then(() => fallback)])
async function waitFor(fn, timeout = 10_000, label = 'condition') {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (await fn()) return true
    await sleep(50)
  }
  throw new Error(`timeout waiting for ${label}`)
}

// ── Vite dev servers ──────────────────────────────────────────────────────────
function testEnv(flagOn) {
  return {
    ...process.env,
    VITE_API_BASE: API,
    VITE_SHOPIFY_CONNECTION_UI: flagOn ? '1' : '0',
    VITE_SENTRY_DSN: `https://publickey@${SENTRY_HOST}/1`,
    VITE_SENTRY_TRACES_SAMPLE_RATE: '1',
    VITE_SENTRY_ENV: 'shopify-ui-test',
    BROWSER: 'none',
  }
}

/** Production build into the scratch dir (never the tracked dist/). */
function buildProd(flagOn) {
  const outDir = join(SCRATCH, flagOn ? 'dist-flag-on' : 'dist-flag-off')
  mkdirSync(SCRATCH, { recursive: true })
  const r = spawnSync('npx', ['vite', 'build', '--outDir', outDir, '--emptyOutDir', '--logLevel', 'error'], {
    cwd: ROOT, env: testEnv(flagOn), encoding: 'utf8',
  })
  if (r.status !== 0) throw new Error(`vite build failed (${flagOn ? 'on' : 'off'}): ${r.stderr || r.stdout}`)
  return outDir
}

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.json': 'application/json', '.txt': 'text/plain', '.svg': 'image/svg+xml', '.xml': 'application/xml', '.woff2': 'font/woff2' }
/** Plain static server for the shipped build: files, else index.html. Adds no policy headers. */
function startStatic(dir, port) {
  const server = createServer((req, res) => {
    const raw = String(req.url || '/').split('?')[0]
    let file = ''
    try {
      const candidate = normalize(join(dir, decodeURIComponent(raw)))
      if (candidate.startsWith(dir + sep) && existsSync(candidate) && statSync(candidate).isFile()) file = candidate
    } catch { /* fall back to the SPA shell */ }
    if (!file) file = join(dir, 'index.html')
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' })
    res.end(readFileSync(file))
  })
  return new Promise((resolve) => server.listen(port, '127.0.0.1', () => resolve(server)))
}

function startVite(port, flagOn) {
  const env = testEnv(flagOn)
  // The local binary (not npx) so SIGTERM reaches the server itself.
  const child = spawn(join(ROOT, 'node_modules', '.bin', 'vite'), ['--port', String(port), '--strictPort', '--host', '127.0.0.1'], {
    cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let out = ''
  child.stdout.on('data', (d) => { out += d })
  child.stderr.on('data', (d) => { out += d })
  return {
    child,
    ready: waitFor(async () => {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/`)
        return r.ok
      } catch {
        return false
      }
    }, 60_000, `vite :${port}`).catch((e) => { throw new Error(`${e.message}\n${out}`) }),
  }
}

// ── Scenario context ──────────────────────────────────────────────────────────
const ACTIVE = { shop_domain: SHOP, status: 'active', scopes: ['read_products'], connected_at: '2026-10-10T08:00:00+00:00', disconnected_at: null, disconnect_reason: null, needs_reconnect: false }
const TOMB = { ...ACTIVE, status: 'disconnected', disconnected_at: '2026-10-10T09:00:00+00:00', disconnect_reason: 'tenant_disconnect', needs_reconnect: true }
const statusBody = (connections, available = true) => ({ available, reason: available ? null : 'client_credentials_missing', requested_scopes: ['read_products'], connections })

async function scenario(browser, origin, opts = {}) {
  const ctx = await browser.newContext({ serviceWorkers: 'block', locale: 'en-US' })
  const trace = {
    requests: [], pending: [], console: [], pageErrors: [], telemetry: [], sentry: [], external: [],
    unmockedApi: new Set(), shopifyNavs: [], api: [], fulfillAfterAbort: [], failed: [],
  }
  const mocks = {
    'GET /merchant/integrations/shopify/status': async () => ({ status: 200, body: statusBody([]) }),
    'GET /salla/whoami': async () => ({ status: 200, body: { salla_integration: { connected: false } } }),
    'GET /whatsapp/status': async () => ({ status: 200, body: { connected: false } }),
    'GET /zid/status': async () => ({ status: 200, body: { connected: false } }),
    ...(opts.mocks || {}),
  }
  const hooks = { holdDocument: null, docCount: 0, stripInlineCapture: false, stripped: false, cancelReload: false, reloadTargets: [] }
  const cors = {
    'access-control-allow-origin': origin,
    'access-control-allow-headers': 'Authorization, Content-Type, X-Tenant-ID',
    'access-control-allow-methods': 'GET, POST, OPTIONS',
    vary: 'Origin',
  }

  await ctx.exposeBinding('__recordTelemetry', (_src, name, payload) => {
    trace.telemetry.push(JSON.stringify([name, payload]))
  })
  await ctx.addInitScript(({ token, role, initFlag }) => {
    try {
      if (token && !localStorage.getItem('nahla_token')) {
        localStorage.setItem('nahla_auth', '1')
        localStorage.setItem('nahla_token', token)
        localStorage.setItem('nahla_role', role)
        localStorage.setItem('nahla_tenant_id', '101')
        localStorage.setItem('nahla_user_id', '7')
      }
      if (!localStorage.getItem('nahla-lang')) localStorage.setItem('nahla-lang', 'en')
    } catch { /* storage blocked */ }
    window.posthog = { capture: (n, p) => window.__recordTelemetry(n, p) }
    window.gtag = (_c, n, p) => window.__recordTelemetry(n, p)
    window.addEventListener('pageshow', (e) => { window.__pwPageshowPersisted = e.persisted })
    if (initFlag === 'throwReplaceWhileDirty') {
      const real = History.prototype.replaceState
      History.prototype.replaceState = function (...args) {
        if (/shopify/i.test(location.search + location.hash)) throw new DOMException('test: replaceState refused', 'SecurityError')
        return real.apply(this, args)
      }
    }
  }, { token: opts.token === undefined ? merchantToken() : opts.token, role: opts.role || 'merchant', initFlag: opts.initFlag || '' })

  await ctx.route('**/*', async (route, req) => {
    const url = new URL(req.url())
    if (url.origin === origin) {
      if (req.resourceType() === 'document') {
        hooks.docCount++
        if (hooks.cancelReload && hooks.docCount > 1) {
          // Record the fail-closed reload target, then answer 204: the
          // navigation is cancelled and the blocked document stays
          // inspectable (a held navigation leaves no usable context).
          hooks.reloadTargets.push(req.url())
          return route.fulfill({ status: 204, body: '' })
        }
        if (hooks.holdDocument && hooks.docCount > 1) await hooks.holdDocument.promise
        if (hooks.stripInlineCapture && hooks.docCount === 1) {
          // Simulate the inline head capture not running: bootCapture.ts alone must fail closed.
          const resp = await route.fetch()
          const body = await resp.text()
          const stripped = body.replace(/<script>\s*\/\* Shopify connection return[\s\S]*?<\/script>/, '')
          hooks.stripped = stripped !== body
          return route.fulfill({ response: resp, body: stripped })
        }
      }
      return route.continue()
    }
    if (url.origin === API) {
      if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
      const key = `${req.method()} ${url.pathname}`
      trace.api.push({ key, body: req.postData() || '' })
      const handler = mocks[key]
      let res
      if (handler) res = await handler(req)
      else {
        trace.unmockedApi.add(key)
        res = { status: 404, body: { detail: 'not_mocked_in_test' } }
      }
      try {
        await route.fulfill({ status: res.status, headers: { ...cors, 'content-type': 'application/json' }, body: JSON.stringify(res.body ?? {}) })
      } catch {
        trace.fulfillAfterAbort.push(key)
      }
      return
    }
    if (url.hostname === SENTRY_HOST) {
      trace.sentry.push(req.postData() || '')
      return route.fulfill({ status: 200, headers: { 'access-control-allow-origin': '*' }, body: '{}' })
    }
    if (url.hostname.endsWith('.myshopify.com')) {
      const headers = await req.allHeaders()
      trace.shopifyNavs.push({ url: req.url(), referer: headers.referer || '' })
      return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>stub</title><p>stub Shopify consent page (test)</p>' })
    }
    trace.external.push(`${url.origin}${url.pathname}`)
    return route.abort('blockedbyclient')
  })

  ctx.on('request', (req) => {
    const entry = { url: req.url(), method: req.method(), body: req.postData() || '', headers: {}, doc: hooks.docCount }
    trace.requests.push(entry)
    trace.pending.push(req.allHeaders().then((h) => { entry.headers = h }).catch(() => {}))
  })
  ctx.on('requestfailed', (req) => trace.failed.push(`${req.method()} ${new URL(req.url()).pathname}`))
  const attach = (p) => {
    p.on('console', (m) => trace.console.push(m.text()))
    p.on('pageerror', (e) => trace.pageErrors.push(String(e && e.stack ? e.stack : e)))
  }
  ctx.on('page', attach)
  const page = await ctx.newPage()
  return { ctx, page, trace, mocks, hooks }
}

async function dump(page) {
  try {
    return await bounded(page.evaluate(async () => {
      let idb = ''
      try { idb = JSON.stringify(indexedDB.databases ? await indexedDB.databases() : []) } catch { idb = 'unavailable' }
      return {
        local: JSON.stringify({ ...localStorage }),
        session: JSON.stringify({ ...sessionStorage }),
        historyState: JSON.stringify(history.state),
        href: location.href,
        cookie: document.cookie,
        idb,
        html: document.documentElement.outerHTML,
        perf: performance.getEntries().map((e) => e.name).join('\n'),
      }
    }), 5_000, { error: 'evaluate timed out' })
  } catch (e) {
    return { error: String(e) }
  }
}

/**
 * Leak scan. ``allow.completeBody``: the POST /complete body may carry exactly
 * the handle. ``allow.shopifyNav``: the validated Shopify URL may carry the
 * state. ``allow.initialUrl``: the first document request (sent by the test
 * itself) may carry the query sentinels — never the fragment.
 */
const withoutFragment = (u) => (u ? u.split('#')[0] : u)

async function scan(label, s, sentinels, allow = {}) {
  await bounded(Promise.all(s.trace.pending), 5_000, null)
  // Only the explicit test navigation (first document request) is classified
  // separately; a later identical request is scanned like any other.
  const initialReq = allow.initialUrl ? s.trace.requests.find((r) => r.url === withoutFragment(allow.initialUrl)) : null
  const dumps = []
  for (const p of s.ctx.pages()) dumps.push(await dump(p))
  const stuck = dumps.filter((d) => d.error).length
  if (stuck) check(`${label}: every page could be inspected`, false, `${stuck} page(s) not inspectable`)
  const hits = []
  const look = (where, text) => {
    if (!text) return
    for (const [name, value] of Object.entries(sentinels)) if (String(text).includes(value)) hits.push(`${name} in ${where}`)
  }
  for (const r of s.trace.requests) {
    const short = r.url.slice(0, 90)
    const isComplete = r.method === 'POST' && r.url === `${API}/merchant/integrations/shopify/complete`
    if (r === initialReq) {
      if (allow.initialMayCarry !== 'handle' && r.url.includes(sentinels.handle)) hits.push('handle in initial request URL')
    } else if (!(allow.shopifyNav && r.url === allow.shopifyNav)) {
      look(`request URL ${short}`, r.url)
    }
    look(`request headers ${short}`, JSON.stringify(r.headers))
    if (isComplete) {
      if (r.body !== JSON.stringify({ handle: sentinels.handle })) hits.push(`unexpected /complete body`)
    } else {
      look(`request body ${short}`, r.body)
    }
  }
  s.trace.console.forEach((l, i) => look(`console[${i}]`, l))
  s.trace.pageErrors.forEach((l, i) => look(`pageerror[${i}]`, l))
  s.trace.telemetry.forEach((l, i) => look(`telemetry[${i}]`, l))
  s.trace.sentry.forEach((l, i) => look(`sentry envelope[${i}]`, l))
  dumps.forEach((d, i) => {
    for (const key of ['local', 'session', 'historyState', 'href', 'cookie', 'idb', 'html']) look(`page${i}.${key}`, d[key])
  })
  const perfHits = dumps.filter((d) => d.perf && Object.values(sentinels).some((v) => d.perf.includes(v))).length
  check(`${label}: no sentinel outside the allowed protocol transport`, hits.length === 0, hits.join('; '))
  if (allow.originOnlyReferer) {
    // Scoped to the documents that were Shopify routes / carried a marker
    // (an ordinary route loaded later uses the restored default policy).
    const { origin, docs } = allow.originOnlyReferer
    const bad = s.trace.requests.filter((r) => r !== initialReq && docs.includes(r.doc) && r.headers.referer && r.headers.referer !== `${origin}/`)
    check(`${label}: every Referer is absent or origin-only`, bad.length === 0,
      bad.map((r) => `${new URL(r.url).pathname.slice(0, 30)} ← ${r.headers.referer.length} chars`).join(', '))
  }
  check(`${label}: no request left the sandbox`, s.trace.external.every((u) => u.startsWith('https://fonts.')), s.trace.external.join(', '))
  const refs = s.trace.requests.filter((r) => r !== initialReq)
  const spanCount = s.trace.sentry.reduce((n, e) => n + (e.match(/"op":"browser\./g) || []).length, 0)
  summary.push({
    label,
    requests: s.trace.requests.length,
    refererAbsent: refs.filter((r) => !r.headers.referer).length,
    refererOriginOnly: refs.filter((r) => r.headers.referer && /^https?:\/\/[^/]+\/$/.test(r.headers.referer)).length,
    refererWithPath: refs.filter((r) => r.headers.referer && !/^https?:\/\/[^/]+\/$/.test(r.headers.referer)).length,
    sentryEnvelopes: s.trace.sentry.length,
    sentryBrowserSpans: spanCount,
    telemetry: s.trace.telemetry.length,
    console: s.trace.console.length,
    blockedExternal: [...new Set(s.trace.external)].length,
    unmockedApi: [...s.trace.unmockedApi],
    browserTimelineEntriesWithSentinel: perfHits,
  })
}

const countApi = (s, key) => s.trace.api.filter((a) => a.key === key).length
const phase = (page) => page.getAttribute('[data-testid="shopify-complete"]', 'data-phase')
const waitPhase = (page, ph, timeout = 15_000) => page.waitForSelector(`[data-testid="shopify-complete"][data-phase="${ph}"]`, { timeout })
const COMPLETE_KEY = 'POST /merchant/integrations/shopify/complete'
const STATUS_KEY = 'GET /merchant/integrations/shopify/status'
const START_KEY = 'POST /merchant/integrations/shopify/start'
const DISCONNECT_KEY = 'POST /merchant/integrations/shopify/disconnect'
const connectedBody = { status: 'connected', connection: ACTIVE }

async function run(name, fn) {
  try {
    const guard = sleep(90_000).then(() => { throw new Error('scenario exceeded 90 s') })
    await Promise.race([fn(), guard])
  } catch (e) {
    check(`${name}: scenario completed`, false, String(e && e.message ? e.message : e).split('\n')[0])
  }
}

// ── Scenarios (one suite per target) ──────────────────────────────────────────
async function suite(browser, T, ON, OFF) {
  const L = (name) => `[${T}] ${name}`

  // 1. Completion via every rendered route alias, with hostile query values.
  for (const path of [COMPLETE, '/Integrations/Shopify/Complete', `${COMPLETE}/`, '//integrations//shopify/complete', '/integrations/shopify/%63omplete']) {
    const name = L(`alias ${path}`)
    await run(name, async () => {
      const sn = newSentinels()
      const s = await scenario(browser, ON, { mocks: { [COMPLETE_KEY]: async () => ({ status: 200, body: connectedBody }) } })
      const initial = `${ON}${path}?code=${sn.code}&state=${sn.state}&hmac=${sn.hmac}&shop=${SHOP}#shopify_handle=${sn.handle}`
      await s.page.goto(initial)
      await waitPhase(s.page, 'connected')
      await sleep(T === 'dev' ? 300 : 1500)
      check(`${name}: exactly one POST /complete`, countApi(s, COMPLETE_KEY) === 1, String(countApi(s, COMPLETE_KEY)))
      check(`${name}: URL canonical and clean`, s.page.url() === `${ON}${COMPLETE}`, s.page.url())
      check(`${name}: next step says import not started`, await s.page.isVisible('[data-testid="shopify-complete-next"]'))
      await scan(name, s, sn, { initialUrl: initial, originOnlyReferer: { origin: ON, docs: [1] } })
      await s.ctx.close()
    })
  }

  // 2. Encoded / re-cased key: refused and scrubbed, never POSTed.
  await run(L('encoded key'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON)
    await s.page.goto(`${ON}${COMPLETE}#shopify%5Fhandle=${sn.handle}`)
    await waitPhase(s.page, 'invalid')
    check(L('encoded key: no POST /complete'), countApi(s, COMPLETE_KEY) === 0)
    check(L('encoded key: URL clean'), s.page.url() === `${ON}${COMPLETE}`)
    await scan(L('encoded key'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })

  // 3. Stray marker on another route, and a //external-host path (clean + forced failure).
  await run(L('stray marker'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON)
    await s.page.goto(`${ON}/integrations#shopify_handle=${sn.handle}`)
    await s.page.waitForSelector('[data-testid="shopify-connection-card"]')
    check(L('stray marker: fragment dropped'), s.page.url() === `${ON}/integrations`, s.page.url())
    check(L('stray marker: no POST /complete'), countApi(s, COMPLETE_KEY) === 0)
    await scan(L('stray marker'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })
  for (const forced of [false, true]) {
    const name = L(`//external-host path${forced ? ' + forced cleanup failure' : ''}`)
    await run(name, async () => {
      const sn = newSentinels()
      const s = await scenario(browser, ON, forced ? { initFlag: 'throwReplaceWhileDirty' } : {})
      const initial = `${ON}//external-host.invalid/path?shopify_handle=${sn.handle}`
      await s.page.goto(initial)
      await waitFor(async () => !s.page.url().includes('shopify_handle'), 10_000, 'clean URL')
      await s.page.waitForLoadState('load')
      await sleep(800)
      check(`${name}: stays on the app origin`, new URL(s.page.url()).origin === ON, s.page.url())
      check(`${name}: path kept, query dropped`, s.page.url() === `${ON}//external-host.invalid/path`, s.page.url())
      check(`${name}: no request to external-host`, s.trace.requests.every((r) => new URL(r.url).hostname !== 'external-host.invalid'))
      if (forced) check(`${name}: fail-closed same-origin reload happened`, s.hooks.docCount >= 2, String(s.hooks.docCount))
      // The handle was in this test navigation's own query: only that request may carry it.
      await scan(name, s, sn, { initialUrl: initial, initialMayCarry: 'handle', originOnlyReferer: { origin: ON, docs: [1] } })
      await s.ctx.close()
    })
  }

  // 4. Forced cleanup failure on the completion page: boot halted, same-origin clean reload.
  for (const stripInline of [false, true]) {
    const name = L(stripInline ? 'blocked boot (inline script absent)' : 'blocked boot')
    await run(name, async () => {
      const sn = newSentinels()
      const s = await scenario(browser, ON, { initFlag: 'throwReplaceWhileDirty' })
      s.hooks.cancelReload = true
      s.hooks.stripInlineCapture = stripInline
      const initial = `${ON}/Integrations/Shopify/Complete?code=${sn.code}#shopify_handle=${sn.handle}`
      await s.page.goto(initial, { waitUntil: 'commit' })
      await waitFor(async () => s.hooks.reloadTargets.length > 0, 10_000, 'fail-closed reload')
      await sleep(2000)
      if (stripInline) check(`${name}: inline script really stripped`, s.hooks.stripped)
      check(`${name}: reload target is the same-origin clean path`,
        s.hooks.reloadTargets.every((u) => u === `${ON}${COMPLETE}`), s.hooks.reloadTargets.map((u) => u.length).join(','))
      const fixedText = await bounded(s.page.isVisible('[data-testid="shopify-return-blocked"]'), 5_000, 'uninspectable')
      const appMounted = await bounded(s.page.isVisible('[data-testid="shopify-complete"]'), 5_000, 'uninspectable')
      if (stripInline) {
        check(`${name}: bootCapture shows the fixed text`, fixedText === true, String(fixedText))
      } else {
        console.log(`INFO [${T}] blocked boot (inline): fixed text ${fixedText === true ? 'shown' : `not rendered (${fixedText === false ? 'parser stopped by the fail-closed reload' : fixedText})`}`)
      }
      check(`${name}: app never mounted`, appMounted === false, String(appMounted))
      check(`${name}: no API, Sentry or telemetry`, s.trace.api.length === 0 && s.trace.sentry.length === 0 && s.trace.telemetry.length === 0,
        `${s.trace.api.length}/${s.trace.sentry.length}/${s.trace.telemetry.length}`)
      // Complete the reload the browser asked for (it was answered 204 above).
      s.hooks.cancelReload = false
      await s.page.goto(s.hooks.reloadTargets[0])
      await waitPhase(s.page, 'nothing')
      check(`${name}: clean reload has nothing to complete, nothing POSTed`, s.page.url() === `${ON}${COMPLETE}` && countApi(s, COMPLETE_KEY) === 0)
      await scan(name, s, sn, { initialUrl: initial, originOnlyReferer: { origin: ON, docs: [1, 2, 3] } })
      await s.ctx.close()
    })
  }

  // Real SDK error envelopes through the configured beforeSend.
  await run(L('sentry sdk error envelopes'), async () => {
    const sn = newSentinels()
    const control = `control${tok(8)}`
    const s = await scenario(browser, ON)
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-connection-card"]')
    const result = await s.page.evaluate(({ handle, api, control }) => {
      const S = window.__SENTRY__
      if (!S) return 'no __SENTRY__'
      let scope = null
      for (const c of [S, S[S.version]].filter(Boolean)) {
        for (const v of Object.values(c)) {
          if (v && typeof v === 'object' && typeof v.captureEvent === 'function' && typeof v.getClient === 'function' && v.getClient()) { scope = v; break }
        }
        if (scope) break
      }
      if (!scope) return 'no scope with a client'
      const complete = `${api}/merchant/integrations/shopify/complete`
      scope.captureEvent({ message: 'shopify-ui-test object body', level: 'error', request: { url: complete, method: 'POST', data: { handle } } })
      scope.captureEvent({ message: 'shopify-ui-test json body', level: 'error', request: { url: complete, method: 'POST', data: JSON.stringify({ handle }) } })
      scope.captureEvent({ message: 'shopify-ui-test control', level: 'error', request: { url: `${api}/orders/1`, method: 'POST', data: { handle: control } }, extra: { handle: control } })
      return 'ok'
    }, { handle: sn.handle, api: API, control })
    check(L('sentry sdk: events captured through the live client'), result === 'ok', result)
    await waitFor(async () => ['object body', 'json body', 'control'].every((m) => s.trace.sentry.some((e) => e.includes(`shopify-ui-test ${m}`))), 10_000, 'error envelopes')
    const env = (m) => s.trace.sentry.find((e) => e.includes(`shopify-ui-test ${m}`)) || ''
    check(L('sentry sdk: object {handle} body scrubbed in the sent envelope'), !env('object body').includes(sn.handle) && env('object body').includes('[scrubbed]'))
    check(L('sentry sdk: JSON-string {handle} body scrubbed in the sent envelope'), !env('json body').includes(sn.handle) && env('json body').includes('[scrubbed]'))
    check(L('sentry sdk: unrelated control event keeps its data'), env('control').includes(control))
    await scan(L('sentry sdk error envelopes'), s, sn)
    await s.ctx.close()
  })

  // 5. Start: canonical input, repeated clicks, validated outbound navigation, back.
  await run(L('start'), async () => {
    const sn = newSentinels()
    const gate = deferred()
    const authorize = `https://${SHOP}/admin/oauth/authorize?${new URLSearchParams({ client_id: 'abc123', scope: 'read_products', redirect_uri: 'https://api.example.test/merchant/integrations/shopify/callback', state: sn.state })}`
    const s = await scenario(browser, ON, {
      mocks: { [START_KEY]: async () => { await gate.promise; return { status: 200, body: { authorize_url: authorize, expires_in: 600 } } } },
    })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-start-form"]')
    check(L('start: Salla / Zid / WhatsApp status calls still made'), ['GET /salla/whoami', 'GET /whatsapp/status', 'GET /zid/status'].every((k) => countApi(s, k) >= 1))
    check(L('start: onboarding — authorization todo, import not started'),
      (await s.page.getAttribute('[data-testid="shopify-step-authorization"]', 'data-step')) === 'todo'
      && (await s.page.getAttribute('[data-testid="shopify-step-import"]', 'data-step')) === 'not_started')
    await s.page.fill('#shopify-shop-input', '  My-Store ')
    check(L('start: canonical preview'), (await s.page.textContent('[data-testid="shopify-shop-preview"]')).includes(SHOP))
    await s.page.click('[data-testid="shopify-connect-button"]')
    await s.page.click('[data-testid="shopify-connect-button"]', { force: true, timeout: 1000 }).catch(() => {})
    await sleep(200)
    check(L('start: repeated clicks → one POST /start'), countApi(s, START_KEY) === 1)
    check(L('start: POST body is the canonical shop'), s.trace.api.find((a) => a.key === START_KEY)?.body === JSON.stringify({ shop: SHOP }))
    gate.resolve()
    await waitFor(async () => s.trace.shopifyNavs.length > 0, 10_000, 'Shopify navigation')
    check(L('start: navigated to exactly the validated authorize URL'), s.trace.shopifyNavs[0].url === authorize)
    check(L('start: Shopify request Referer is origin-only'), !s.trace.shopifyNavs[0].referer || s.trace.shopifyNavs[0].referer === `${ON}/`, `${s.trace.shopifyNavs[0].referer.length} chars`)
    await s.page.goBack()
    await s.page.waitForSelector('[data-testid="shopify-connect-button"]')
    const persisted = await s.page.evaluate(() => window.__pwPageshowPersisted === true)
    check(L(`start: back from Shopify leaves the form usable (bfcache restore: ${persisted ? 'yes' : 'no, fresh load'})`),
      await s.page.isEnabled('#shopify-shop-input'))
    await scan(L('start'), s, sn, { shopifyNav: authorize })
    await s.ctx.close()
  })
  await run(L('start bad url'), async () => {
    const sn = newSentinels()
    const evil = `https://evil.example/admin/oauth/authorize?${new URLSearchParams({ client_id: 'abc123', scope: 'read_products', redirect_uri: 'https://api.example.test/merchant/integrations/shopify/callback', state: sn.state })}`
    const s = await scenario(browser, ON, { mocks: { [START_KEY]: async () => ({ status: 200, body: { authorize_url: evil, expires_in: 600 } }) } })
    await s.page.goto(`${ON}/integrations`)
    await s.page.fill('#shopify-shop-input', SHOP)
    await s.page.click('[data-testid="shopify-connect-button"]')
    await s.page.waitForSelector('[data-testid="shopify-start-error"]')
    await sleep(300)
    check(L('start bad url: no navigation away'), s.page.url() === `${ON}/integrations` && s.trace.shopifyNavs.length === 0)
    check(L('start bad url: nothing requested from the foreign host'), s.trace.requests.every((r) => !r.url.startsWith('https://evil.example')))
    await scan(L('start bad url'), s, sn)
    await s.ctx.close()
  })

  // 6. Replay with provider text, exchange_in_progress retry + TTL expiry (fake clock).
  await run(L('replay'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, {
      mocks: { [COMPLETE_KEY]: async () => ({ status: 409, body: { detail: { error: 'state_replayed', message: sn.provider } } }) },
    })
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await waitPhase(s.page, 'refused')
    check(L('replay: no retry offered'), !(await s.page.isVisible('[data-testid="shopify-complete-retry"]')))
    await scan(L('replay'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })
  await run(L('exchange_in_progress expiry'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, {
      mocks: { [COMPLETE_KEY]: async () => ({ status: 409, body: { detail: { error: 'exchange_in_progress', message: sn.provider } } }) },
    })
    await s.page.clock.install()
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await s.page.waitForSelector('[data-testid="shopify-complete-retry"]')
    await s.page.click('[data-testid="shopify-complete-retry"]')
    await waitFor(async () => countApi(s, COMPLETE_KEY) === 2, 10_000, 'second POST')
    await s.page.waitForSelector('[data-testid="shopify-complete-retry"]')
    await s.page.clock.fastForward('05:01')
    await waitPhase(s.page, 'expired')
    check(L('expiry: retry gone after TTL'), !(await s.page.isVisible('[data-testid="shopify-complete-retry"]')))
    await sleep(300)
    check(L('expiry: no POST after TTL'), countApi(s, COMPLETE_KEY) === 2)
    await scan(L('exchange_in_progress expiry'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })

  // 7. Uncertain outcome → status check, never an automatic re-POST.
  await run(L('uncertain'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, {
      mocks: {
        [COMPLETE_KEY]: async () => ({ status: 502, body: { detail: sn.provider } }),
        [STATUS_KEY]: async () => ({ status: 200, body: statusBody([ACTIVE]) }),
      },
    })
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await waitPhase(s.page, 'uncertain')
    await sleep(300)
    check(L('uncertain: one POST only'), countApi(s, COMPLETE_KEY) === 1)
    await s.page.click('[data-testid="shopify-complete-check"]')
    await s.page.waitForSelector('[data-testid="shopify-complete-status"]')
    check(L('uncertain: status check shows the authorized store'), (await s.page.textContent('[data-testid="shopify-complete-status"]')).includes(SHOP))
    await scan(L('uncertain'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })

  // 8. Fragment "connected" is never trusted.
  await run(L('fragment connected'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, { mocks: { [STATUS_KEY]: async () => ({ status: 200, body: statusBody([]) }) } })
    await s.page.goto(`${ON}${COMPLETE}#shopify_connection=connected`)
    await waitPhase(s.page, 'unconfirmed')
    await s.page.waitForSelector('[data-testid="shopify-complete-status"]')
    check(L('fragment connected: no POST, never the connected phase'), countApi(s, COMPLETE_KEY) === 0 && (await phase(s.page)) === 'unconfirmed')
    await scan(L('fragment connected'), s, sn, { originOnlyReferer: { origin: ON, docs: [1] } })
    await s.ctx.close()
  })

  // 9. Session change while the POST is pending (another tab logs in as someone else).
  await run(L('session change (completion)'), async () => {
    const sn = newSentinels()
    const gate = deferred()
    const s = await scenario(browser, ON, { mocks: { [COMPLETE_KEY]: async () => { await gate.promise; return { status: 200, body: connectedBody } } } })
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await waitFor(async () => countApi(s, COMPLETE_KEY) === 1, 10_000, 'POST')
    const other = await s.ctx.newPage()
    await other.goto(`${ON}/robots.txt`)
    await other.evaluate((t) => localStorage.setItem('nahla_token', t), merchantToken({ tenant_id: 202, user_id: 9 }))
    await waitPhase(s.page, 'session_changed')
    gate.resolve()
    await sleep(500)
    check(L('session change: late success never shown'), (await phase(s.page)) === 'session_changed')
    check(L('session change: pending POST aborted'), s.trace.failed.includes('POST /merchant/integrations/shopify/complete'))
    await scan(L('session change (completion)'), s, sn)
    await s.ctx.close()
  })

  // 10. Session change with the disconnect dialog open and a typed shop: no stale tenant data.
  await run(L('session change (card dialog)'), async () => {
    const sn = newSentinels()
    const typed = `typed-${tok(6).toLowerCase().replace(/[^a-z0-9]/g, 'x')}`
    const s = await scenario(browser, ON, {
      mocks: {
        [STATUS_KEY]: async (req) => {
          const auth = (await req.allHeaders()).authorization || ''
          const claims = JSON.parse(Buffer.from(auth.split('.')[1] || 'e30', 'base64url').toString() || '{}')
          return { status: 200, body: statusBody([{ ...ACTIVE, shop_domain: claims.tenant_id === 202 ? 'second-tenant.myshopify.com' : FIRST_SHOP }]) }
        },
      },
    })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-disconnect-button"]')
    await s.page.evaluate(() => {}) // settle
    await s.page.click('[data-testid="shopify-disconnect-button"]')
    await s.page.waitForSelector('[role="dialog"]')
    check(L('card dialog: confirmation names the first tenant shop'), (await s.page.textContent('[role="dialog"]')).includes(FIRST_SHOP))
    const other = await s.ctx.newPage()
    await other.goto(`${ON}/robots.txt`)
    await other.evaluate((t) => localStorage.setItem('nahla_token', t), merchantToken({ tenant_id: 202, user_id: 9 }))
    await s.page.waitForSelector('[role="dialog"]', { state: 'detached', timeout: 10_000 })
    await s.page.waitForSelector('text=second-tenant.myshopify.com', { timeout: 10_000 })
    const html = await s.page.content()
    check(L('card dialog: dialog closed by the session change'), !(await s.page.isVisible('[role="dialog"]')))
    check(L('card dialog: first tenant shop not visible after the new status'), !html.includes(FIRST_SHOP))
    check(L('card dialog: no disconnect sent'), countApi(s, DISCONNECT_KEY) === 0)
    await scan(L('session change (card dialog)'), s, { ...sn, typed })
    await s.ctx.close()
  })
  await run(L('session change (typed shop)'), async () => {
    const sn = newSentinels()
    const typed = `typed${tok(6).toLowerCase().replace(/[^a-z0-9]/g, 'x')}`
    const s = await scenario(browser, ON)
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('#shopify-shop-input')
    await s.page.fill('#shopify-shop-input', typed)
    const other = await s.ctx.newPage()
    await other.goto(`${ON}/robots.txt`)
    await other.evaluate((t) => localStorage.setItem('nahla_token', t), merchantToken({ tenant_id: 202, user_id: 9 }))
    await waitFor(async () => (await s.page.inputValue('#shopify-shop-input').catch(() => typed)) === '', 10_000, 'input reset')
    check(L('typed shop: cleared by the session change'), !(await s.page.content()).includes(typed))
    await s.ctx.close()
  })

  // 11. Genuine SPA leave while the POST is pending; back never resubmits.
  await run(L('leave while pending'), async () => {
    const sn = newSentinels()
    const gate = deferred()
    const s = await scenario(browser, ON, { mocks: { [COMPLETE_KEY]: async () => { await gate.promise; return { status: 200, body: connectedBody } } } })
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await waitFor(async () => countApi(s, COMPLETE_KEY) === 1, 10_000, 'POST')
    await s.page.click('[data-testid="platform-up-nav"] button')
    await s.page.waitForURL(`${ON}/integrations`)
    await waitFor(async () => s.trace.failed.includes('POST /merchant/integrations/shopify/complete'), 5_000, 'aborted POST')
    gate.resolve()
    await sleep(400)
    check(L('leave: pending POST aborted by the client'), s.trace.failed.includes('POST /merchant/integrations/shopify/complete'))
    await s.page.goBack()
    await waitPhase(s.page, 'nothing')
    await sleep(300)
    check(L('leave: back shows nothing to complete, no resubmit'), countApi(s, COMPLETE_KEY) === 1)
    await s.page.goto(`${ON}/robots.txt`) // flush the route transaction to (mock) Sentry
    await sleep(1500)
    await scan(L('leave while pending'), s, sn)
    await s.ctx.close()
  })

  // 12. Disconnect: proved tombstone, and malformed / active / wrong-shop answers.
  for (const [variant, body, proved] of [
    ['tombstone', { connection: TOMB }, true],
    ['malformed 200', { ok: true }, false],
    ['active 200', { connection: ACTIVE }, false],
    ['wrong-shop 200', { connection: { ...TOMB, shop_domain: 'other-store.myshopify.com' } }, false],
  ]) {
    const name = L(`disconnect ${variant}`)
    await run(name, async () => {
      const sn = newSentinels()
      let disconnected = false
      const s = await scenario(browser, ON, {
        mocks: {
          [STATUS_KEY]: async () => ({ status: 200, body: statusBody([disconnected && proved ? TOMB : ACTIVE]) }),
          [DISCONNECT_KEY]: async () => { disconnected = true; return { status: 200, body } },
        },
      })
      await s.page.goto(`${ON}/integrations`)
      await s.page.waitForSelector('[data-testid="shopify-disconnect-button"]')
      check(`${name}: onboarding shows authorization done, import not started`,
        (await s.page.getAttribute('[data-testid="shopify-step-authorization"]', 'data-step')) === 'done'
        && (await s.page.getAttribute('[data-testid="shopify-step-import"]', 'data-step')) === 'not_started')
      const before = countApi(s, STATUS_KEY)
      await s.page.click('[data-testid="shopify-disconnect-button"]')
      await s.page.click('[role="dialog"] button.btn-primary')
      await waitFor(async () => countApi(s, STATUS_KEY) > before, 10_000, 'status re-read')
      await sleep(300)
      check(`${name}: body is the canonical shop`, s.trace.api.find((a) => a.key === DISCONNECT_KEY)?.body === JSON.stringify({ shop: SHOP }))
      if (proved) {
        check(`${name}: done + reconnect offered`, await s.page.isVisible('[data-testid="shopify-disconnect-done"]') && await s.page.isVisible('[data-testid="shopify-reconnect-button"]'))
      } else {
        check(`${name}: uncertain, never "done"`, await s.page.isVisible('[data-testid="shopify-disconnect-error"]') && !(await s.page.isVisible('[data-testid="shopify-disconnect-done"]')))
        check(`${name}: status re-read reconciles (still active)`, await s.page.isVisible('[data-testid="shopify-disconnect-button"]'))
      }
      await scan(name, s, sn)
      await s.ctx.close()
    })
  }

  // 13. Start / disconnect serialization in the UI.
  await run(L('serialization'), async () => {
    const sn = newSentinels()
    const gate = deferred()
    const s = await scenario(browser, ON, {
      mocks: {
        [STATUS_KEY]: async () => ({ status: 200, body: statusBody([ACTIVE, { ...TOMB, shop_domain: 'second-store.myshopify.com' }]) }),
        [START_KEY]: async () => { await gate.promise; return { status: 429, body: { detail: { error: 'too_many_pending_authorizations', message: sn.provider } } } },
      },
    })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-reconnect-button"]')
    await s.page.click('[data-testid="shopify-reconnect-button"]')
    await waitFor(async () => countApi(s, START_KEY) === 1, 5_000, 'start')
    check(L('serialization: disconnect disabled while a start is pending'), await s.page.isDisabled('[data-testid="shopify-disconnect-button"]'))
    gate.resolve()
    await s.page.waitForSelector('[data-testid="shopify-start-error"]')
    check(L('serialization: disconnect usable again after the start ends'), await s.page.isEnabled('[data-testid="shopify-disconnect-button"]'))
    await scan(L('serialization'), s, sn)
    await s.ctx.close()
  })

  // 14. Support impersonation is read-only.
  await run(L('support read-only'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, {
      token: merchantToken({ impersonation: true, role: 'merchant', actor_sub: 'support@example.test' }),
      mocks: { [STATUS_KEY]: async () => ({ status: 200, body: statusBody([ACTIVE]) }) },
    })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-read-only"]')
    check(L('support read-only: no start form, no disconnect button'),
      !(await s.page.isVisible('[data-testid="shopify-start-form"]')) && !(await s.page.isVisible('[data-testid="shopify-disconnect-button"]')))
    check(L('support read-only: no mutation requested'), countApi(s, START_KEY) + countApi(s, DISCONNECT_KEY) === 0)
    await scan(L('support read-only'), s, sn)
    await s.ctx.close()
  })

  // 15. Backend disabled / unavailable.
  await run(L('backend disabled'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, {
      mocks: {
        [STATUS_KEY]: async () => ({ status: 404, body: { detail: { error: 'not_found' } } }),
        [COMPLETE_KEY]: async () => ({ status: 404, body: { detail: { error: 'not_found' } } }),
      },
    })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('text=WhatsApp Business API')
    await sleep(500)
    check(L('backend disabled: card hidden, other cards present'), !(await s.page.isVisible('[data-testid="shopify-connection-card"]')))
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await waitPhase(s.page, 'feature_off')
    await scan(L('backend disabled'), s, sn)
    await s.ctx.close()
  })
  await run(L('backend unavailable'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, { mocks: { [STATUS_KEY]: async () => ({ status: 200, body: statusBody([], false) }) } })
    await s.page.goto(`${ON}/integrations`)
    await s.page.waitForSelector('[data-testid="shopify-unavailable"]')
    check(L('backend unavailable: no start form'), !(await s.page.isVisible('[data-testid="shopify-start-form"]')))
    await scan(L('backend unavailable'), s, sn)
    await s.ctx.close()
  })

  // 16. No session: protected route refuses, handle never POSTed.
  await run(L('no session'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, ON, { token: null })
    await s.page.goto(`${ON}${COMPLETE}#shopify_handle=${sn.handle}`)
    await s.page.waitForURL(`${ON}/landing`)
    await sleep(800)
    await s.page.goto(`${ON}/robots.txt`) // flush the pageload transaction to (mock) Sentry
    await sleep(1500)
    check(L('no session: handle never POSTed'), countApi(s, COMPLETE_KEY) === 0)
    check(L('no session: Sentry received the pageload transaction (scrubbed)'), s.trace.sentry.length > 0, String(s.trace.sentry.length))
    await scan(L('no session'), s, sn)
    await s.ctx.close()
  })

  // 17. Dashboard flag OFF build.
  await run(L('flag off'), async () => {
    const sn = newSentinels()
    const s = await scenario(browser, OFF)
    await s.page.goto(`${OFF}/integrations`)
    await s.page.waitForSelector('text=WhatsApp Business API')
    await sleep(500)
    check(L('flag off: no card and no Shopify status call'), !(await s.page.isVisible('[data-testid="shopify-connection-card"]')) && countApi(s, STATUS_KEY) === 0)
    const initial = `${OFF}${COMPLETE}?state=${sn.state}#shopify_handle=${sn.handle}`
    await s.page.goto(initial)
    await waitPhase(s.page, 'feature_off')
    check(L('flag off: handle never POSTed, URL clean'), countApi(s, COMPLETE_KEY) === 0 && s.page.url() === `${OFF}${COMPLETE}`)
    await scan(L('flag off'), s, sn, { initialUrl: initial, originOnlyReferer: { origin: OFF, docs: [2] } })
    await s.ctx.close()
  })
}

async function main() {
  const devOn = startVite(PORT_ON, true)
  const devOff = startVite(PORT_OFF, false)
  const prodOnDir = buildProd(true)
  const prodOffDir = buildProd(false)
  const shipped = readFileSync(join(prodOnDir, 'index.html'), 'utf8')
  check('[prod] shipped index.html: static strict-origin meta precedes every script and link',
    shipped.indexOf('<meta name="referrer" content="strict-origin" />') !== -1
    && shipped.indexOf('<meta name="referrer" content="strict-origin" />') < shipped.indexOf('<script')
    && shipped.indexOf('<meta name="referrer" content="strict-origin" />') < shipped.indexOf('<link'))
  const prodOn = await startStatic(prodOnDir, PORT_PROD_ON)
  const prodOff = await startStatic(prodOffDir, PORT_PROD_OFF)
  const browser = await chromium.launch({ executablePath: EXEC })
  try {
    await devOn.ready
    await devOff.ready
    await suite(browser, 'dev', `http://127.0.0.1:${PORT_ON}`, `http://127.0.0.1:${PORT_OFF}`)
    await suite(browser, 'prod', `http://127.0.0.1:${PORT_PROD_ON}`, `http://127.0.0.1:${PORT_PROD_OFF}`)
  } finally {
    await browser.close()
    devOn.child.kill('SIGTERM')
    devOff.child.kill('SIGTERM')
    prodOn.close()
    prodOff.close()
  }

  console.log('\n── Trace summary (per scenario; counts only, no values) ──')
  for (const row of summary) console.log(JSON.stringify(row))
  console.log(`\n${passed} passed, ${failed} failed`)
  if (failed) process.exit(1)
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
