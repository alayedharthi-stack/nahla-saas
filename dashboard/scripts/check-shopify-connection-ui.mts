/**
 * CI guard: Shopify connection merchant UI (dormant, flag off).
 *
 * Source-level and state-machine checks only (no browser, no network, no
 * package imports), so it runs with `npx tsx` in `dashboard-platform-policy`:
 *
 *  - the one-time completion fragment is parsed only in its exact backend
 *    form, and every path form React Router renders as the completion page is
 *    captured and cleaned — by the TS boot module AND by the real inline
 *    script in index.html (executed here in a VM against the same cases);
 *  - cleanup that cannot be verified fails closed and keeps nothing;
 *  - the authorize URL validator refuses anything but the exact Shopify URL;
 *  - errors are projected to fixed codes (no message / provider text);
 *  - completion and card controllers: single flight, StrictMode re-attach,
 *    genuine leave, late responses after leave or session change, retained
 *    handle expiry, start/disconnect serialization, tombstone proof,
 *    out-of-order status reads;
 *  - Sentry URL redaction matches the early capture's recognition;
 *  - wiring: first import, inline script first in <head>, flag default off,
 *    no storage / console use in the new modules.
 *
 * Run: npm run check:shopify-connection-ui (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import {
  COMPLETION_TTL_MS,
  RETURN_MARKER_RE,
  SHOPIFY_COMPLETE_PATH,
  captureShopifyReturn,
  discardShopifyReturn,
  isShopifyCompletePath,
  parseShopifyReturnFragment,
  takeShopifyReturn,
} from '../src/lib/shopifyConnection/returnCapture.ts'
import {
  KNOWN_ERROR_CODES,
  type MessageKey,
  canonicalShopDomain,
  isSafeAuthorizeUrl,
  messageKeyForCode,
  normalizeShopInput,
  normalizeStatus,
  onboardingSteps,
  parseCompleteResponse,
  projectApiFailure,
  redactShopifyUrl,
  sanitizeSentryEvent,
} from '../src/lib/shopifyConnection/model.ts'
import { createCompletionController } from '../src/lib/shopifyConnection/completionController.ts'
import { createConnectionController } from '../src/lib/shopifyConnection/connectionController.ts'
import { shopifyConnectionAr, shopifyConnectionEn } from '../src/i18n/shopifyConnectionLabels.ts'

let failed = 0
let passed = 0
function assert(name: string, ok: boolean, detail = ''): void {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    passed++
    console.log(`OK   ${name}`)
  }
}
const src = (rel: string) => readFileSync(new URL(rel, import.meta.url), 'utf8')
const flush = () => new Promise<void>((r) => setImmediate(r))

// Runtime-generated fake sentinels only.
const rand = (n: number) => Array.from({ length: n }, () => 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'[Math.floor(Math.random() * 64)]).join('')
const HANDLE = rand(43)
const PROVIDER_TEXT = `provider-detail-${rand(16)}`

// ── 1. Fragment parsing ───────────────────────────────────────────────────────
{
  const h = parseShopifyReturnFragment(`#shopify_handle=${HANDLE}`)
  assert('exact handle fragment is accepted', h.kind === 'handle' && h.handle === HANDLE)
  assert('result code fragment', JSON.stringify(parseShopifyReturnFragment('#shopify_connection=state_expired')) === '{"kind":"result","code":"state_expired"}')
  assert('malformed result code → error', JSON.stringify(parseShopifyReturnFragment('#shopify_connection=%3Cx%3E')) === '{"kind":"result","code":"error"}')
  for (const [name, frag] of [
    ['encoded key', `#shopify%5Fhandle=${HANDLE}`],
    ['upper-case key', `#SHOPIFY_HANDLE=${HANDLE}`],
    ['duplicate key', `#shopify_handle=${HANDLE}&shopify_handle=${HANDLE}`],
    ['extra key', `#shopify_handle=${HANDLE}&x=1`],
    ['short handle', '#shopify_handle=abc'],
    ['bad handle chars', `#shopify_handle=${HANDLE.slice(0, 40)}%2F%2F`],
    ['both keys', `#shopify_handle=${HANDLE}&shopify_connection=connected`],
    ['too long', `#shopify_handle=${'a'.repeat(600)}`],
    ['foreign key', '#token=abc'],
  ] as const) {
    assert(`refused: ${name}`, parseShopifyReturnFragment(frag).kind === 'invalid')
  }
  assert('empty fragment → none', parseShopifyReturnFragment('').kind === 'none')
}

// ── 2. Boot capture (TS) on every rendered path form ──────────────────────────
const ORIGIN = 'https://app.test'
/**
 * Browser URL semantics for the fakes: the *document* URL is origin + raw
 * path (a browser shows a //-path as a path), while every target handed to
 * replaceState / location.replace resolves against the current URL like
 * ``new URL(target, location.href)`` — so a target starting with // is
 * protocol-relative (another host). Cross-origin replaceState throws like the
 * browser's SecurityError; location.replace records the resolved absolute URL.
 */
function browserLocation(path: string, throwReplace = false) {
  const u = new URL(`${ORIGIN}${path}`)
  const loc = {
    origin: ORIGIN, pathname: u.pathname, search: u.search, hash: u.hash,
    get href() { return `${this.origin}${this.pathname}${this.search}${this.hash}` },
    replace(target: string) { navigations.push(new URL(String(target), loc.href).href) },
  }
  const navigations: string[] = []
  const replaceState = (_d: unknown, _u: string, target?: string | null) => {
    if (throwReplace) throw new Error('SecurityError')
    const n = new URL(String(target), loc.href)
    if (n.origin !== loc.origin) throw new Error('SecurityError: cross-origin replaceState')
    loc.pathname = n.pathname
    loc.search = n.search
    loc.hash = n.hash
  }
  return { loc, navigations, replaceState }
}

function fakeEnv(url: string, opts: { throwReplace?: boolean; early?: string; earlyBlocked?: boolean } = {}) {
  const b = browserLocation(url, opts.throwReplace)
  const timers: Array<() => void> = []
  return {
    env: {
      location: b.loc,
      history: { replaceState: b.replaceState },
      takeEarly: opts.early !== undefined ? () => opts.early as string : undefined,
      earlyBlocked: opts.earlyBlocked,
      replaceLocation: (t: string) => b.loc.replace(t),
      setTimer: (fn: () => void) => { timers.push(fn); return timers.length },
      clearTimer: () => {},
    },
    location: b.loc,
    replaced: b.navigations,
    timers,
  }
}

const ALIASES = [
  '/integrations/shopify/complete',
  '/integrations/shopify/complete/',
  '/Integrations/Shopify/Complete',
  '/INTEGRATIONS/SHOPIFY/COMPLETE/',
  '//integrations//shopify/complete',
  '/integrations/shopify/%63omplete',
  '/integrations/shopify/%43OMPLETE//',
]
for (const path of ALIASES) {
  assert(`route form recognised: ${path}`, isShopifyCompletePath(path))
  discardShopifyReturn()
  const f = fakeEnv(`${path}?code=${rand(10)}&state=${rand(10)}&hmac=${rand(10)}#shopify_handle=${HANDLE}`)
  const r = captureShopifyReturn(f.env, 1_000)
  assert(`capture cleans ${path}`, !r.blocked && f.location.pathname === SHOPIFY_COMPLETE_PATH && f.location.search === '' && f.location.hash === '',
    JSON.stringify(f.location))
  const t = takeShopifyReturn(2_000)
  assert(`capture holds the handle for ${path}`, t.kind === 'handle' && t.handle === HANDLE)
  assert(`one-shot take for ${path}`, takeShopifyReturn(2_000).kind === 'none')
}
for (const path of ['/integrations/shopify/completed', '/integrations/shopify', '/integrations', '/app/salla', '/integrations/shopify/complete/x']) {
  assert(`not the completion route: ${path}`, !isShopifyCompletePath(path))
}
{
  discardShopifyReturn()
  const f = fakeEnv(`/integrations/shopify/complete#shopify%5Fhandle=${HANDLE}`)
  captureShopifyReturn(f.env, 0)
  assert('encoded-key return is scrubbed', f.location.hash === '' && f.location.pathname === SHOPIFY_COMPLETE_PATH)
  assert('encoded-key return is refused (invalid, no handle)', takeShopifyReturn(1).kind === 'invalid')
}
{
  const f = fakeEnv(`/overview?x=1#shopify_handle=${HANDLE}`)
  captureShopifyReturn(f.env, 0)
  assert('stray return marker on another route is dropped unread', f.location.pathname === '/overview' && f.location.hash === '' && f.location.search === '')
  assert('stray marker never becomes a handle', takeShopifyReturn(1).kind === 'none')
  const g = fakeEnv(`/login?next=%2Fx#SHOPIFY%255FHANDLE=${HANDLE}`)
  captureShopifyReturn(g.env, 0)
  assert('double-encoded / upper-case stray marker dropped', g.location.hash === '' && g.location.search === '')
}
{
  // A foreign path that starts with // must never become a protocol-relative target.
  const EXT = '//external-host.invalid/path'
  assert('harness: a bare //-path target is protocol-relative (the hazard)', new URL(EXT, ORIGIN).origin !== ORIGIN)
  const f = fakeEnv(`${EXT}?shopify_handle=${HANDLE}`)
  const r = captureShopifyReturn(f.env, 0)
  assert('//external-host stray marker: cleaned on the same origin, path kept',
    !r.blocked && f.location.origin === ORIGIN && f.location.pathname === EXT && f.location.search === '' && f.replaced.length === 0,
    f.location.href)
  const g = fakeEnv(`${EXT}?shopify_handle=${HANDLE}`, { throwReplace: true })
  const rg = captureShopifyReturn(g.env, 0)
  assert('//external-host forced cleanup failure: fallback stays same-origin',
    rg.blocked && g.replaced.length === 1 && new URL(g.replaced[0]).origin === ORIGIN && g.replaced[0] === `${ORIGIN}${EXT}`,
    g.replaced.join(','))
  assert('…and holds nothing', takeShopifyReturn(1).kind === 'none')
}
{
  const f = fakeEnv('/app/salla/launch?token=abc&lang=ar#section')
  const r = captureShopifyReturn(f.env, 0)
  assert('unrelated Salla URL untouched', !r.blocked && f.location.search === '?token=abc&lang=ar' && f.location.hash === '#section')
}
{
  discardShopifyReturn()
  const f = fakeEnv(`/Integrations/Shopify/Complete#shopify_handle=${HANDLE}`, { throwReplace: true })
  const r = captureShopifyReturn(f.env, 0)
  assert('replaceState failure fails closed', r.blocked && f.replaced[0] === `${ORIGIN}${SHOPIFY_COMPLETE_PATH}`)
  assert('fail-closed keeps nothing', takeShopifyReturn(1).kind === 'none')
  const g = fakeEnv('/integrations/shopify/complete', { early: `#shopify_handle=${HANDLE}`, earlyBlocked: true })
  assert('inline-script block flag fails closed', captureShopifyReturn(g.env, 0).blocked && takeShopifyReturn(1).kind === 'none')
}
{
  discardShopifyReturn()
  const f = fakeEnv('/integrations/shopify/complete', { early: `#shopify_handle=${HANDLE}` })
  captureShopifyReturn(f.env, 5_000)
  assert('inline-script value is preferred when the URL is already clean', takeShopifyReturn(5_001).kind === 'handle')
  const g = fakeEnv(`/integrations/shopify/complete#shopify_handle=${HANDLE}`)
  captureShopifyReturn(g.env, 0)
  assert('held handle expires at TTL', takeShopifyReturn(COMPLETION_TTL_MS).kind === 'expired')
  const h = fakeEnv(`/integrations/shopify/complete#shopify_handle=${HANDLE}`)
  captureShopifyReturn(h.env, 0)
  h.timers.forEach((fn) => fn())
  assert('expiry timer clears memory without a take', takeShopifyReturn(1).kind === 'none')
}

// ── 3. The real inline <head> script (VM) agrees with the TS capture ─────────
const indexHtml = src('../index.html')
const headStart = indexHtml.indexOf('<head>')
const firstScript = indexHtml.indexOf('<script>', headStart)
const inline = indexHtml.slice(firstScript + '<script>'.length, indexHtml.indexOf('</script>', firstScript))
{
  const beforeScript = indexHtml.slice(headStart, firstScript).replace(/<!--[\s\S]*?-->/g, '')
  assert('only <meta charset> and the static early referrer policy precede the inline capture',
    /^<head>\s*<meta charset="UTF-8" \/>\s*<meta name="referrer" content="strict-origin" \/>\s*$/.test(beforeScript), beforeScript)
  assert('inline capture precedes every <link> and module script',
    firstScript < indexHtml.indexOf('<link') && firstScript < indexHtml.indexOf('<script type="module"'))
  assert('inline script uses the same marker as RETURN_MARKER_RE', inline.includes(`/${RETURN_MARKER_RE.source}/i`))
  assert('inline script uses the canonical path', inline.includes(`'${SHOPIFY_COMPLETE_PATH}'`))
}
function runInline(url: string, throwReplace = false) {
  const b = browserLocation(url, throwReplace)
  const appended: Array<{ name?: string; content?: string }> = []
  const win: Record<string, unknown> = {
    location: b.loc,
    history: { replaceState: b.replaceState },
    document: { head: { appendChild: (el: { name?: string; content?: string }) => appended.push(el) }, createElement: () => ({}) },
  }
  win.window = win
  runInNewContext(inline, win)
  return { win, location: b.loc, replaced: b.navigations, appended }
}
for (const path of ALIASES) {
  const r = runInline(`${path}?code=${rand(8)}#shopify_handle=${HANDLE}`)
  const getter = r.win.__nahlaShopifyReturn as (() => string) | undefined
  assert(`inline: cleans and canonicalises ${path}`,
    r.location.pathname === SHOPIFY_COMPLETE_PATH && r.location.search === '' && r.location.hash === '')
  assert(`inline: no-referrer on ${path}`, r.appended.some((m) => m.name === 'referrer' && m.content === 'no-referrer'))
  const raw = getter ? getter() : ''
  assert(`inline: one-shot getter returns the raw fragment for ${path}`, raw === `#shopify_handle=${HANDLE}` && !('__nahlaShopifyReturn' in r.win))
}
{
  const r = runInline(`/overview#shopify_handle=${HANDLE}`)
  assert('inline: stray marker dropped unread', r.location.hash === '' && r.location.pathname === '/overview'
    && (r.win.__nahlaShopifyReturn as () => string)() === '')
  const ext = runInline(`//external-host.invalid/path?shopify_handle=${HANDLE}`)
  assert('inline: //external-host stray marker cleaned on the same origin',
    ext.location.origin === ORIGIN && ext.location.pathname === '//external-host.invalid/path' && ext.location.search === '' && ext.replaced.length === 0)
  const extFail = runInline(`//external-host.invalid/path?shopify_handle=${HANDLE}`, true)
  assert('inline: //external-host forced failure → same-origin location.replace',
    extFail.win.__nahlaShopifyReturnBlocked === true && extFail.replaced.length === 1
    && extFail.replaced[0] === `${ORIGIN}//external-host.invalid/path`, extFail.replaced.join(','))
  const s = runInline('/app/salla/launch?token=abc#x')
  assert('inline: unrelated URL untouched, no getter, browser-default referrer policy restored',
    s.location.search === '?token=abc' && s.location.hash === '#x' && !('__nahlaShopifyReturn' in s.win)
    && s.appended.length === 1 && s.appended[0].name === 'referrer' && s.appended[0].content === 'strict-origin-when-cross-origin')
  assert('inline: stray marker route gets no-referrer', r.appended.length === 1 && r.appended[0].content === 'no-referrer')
  const b = runInline(`/Integrations/Shopify/Complete/#shopify_handle=${HANDLE}`, true)
  assert('inline: unverifiable cleanup → blocked flag, nothing held, location.replace to bare path',
    b.win.__nahlaShopifyReturnBlocked === true && (b.win.__nahlaShopifyReturn as () => string)() === '' && b.replaced[0] === `${ORIGIN}${SHOPIFY_COMPLETE_PATH}`)
}

// ── 4. Shop input and authorize URL ───────────────────────────────────────────
{
  assert('bare name → myshopify', normalizeShopInput('  My-Store ') === 'my-store.myshopify.com')
  assert('https URL with slash', normalizeShopInput('https://my-store.myshopify.com/') === 'my-store.myshopify.com')
  for (const bad of ['mystore.com', 'a.b.myshopify.com', 'my store', 'my-store-.myshopify.com', 'xn--abc.myshopify.com',
    'my-store.myshopify.com/admin', 'user@my-store.myshopify.com', 'my-store.myshopify.com:443', 'ｍｙ-store', '-x.myshopify.com', '']) {
    assert(`shop input refused: ${JSON.stringify(bad)}`, normalizeShopInput(bad) === null)
  }
  assert('canonical parity: 63-char label ok', canonicalShopDomain(`${'a'.repeat(63)}.myshopify.com`) !== null)
  assert('canonical parity: 64-char label refused', canonicalShopDomain(`${'a'.repeat(64)}.myshopify.com`) === null)
  const shop = 'my-store.myshopify.com'
  const state = rand(43)
  const q = (o: Record<string, string>) => new URLSearchParams(o).toString()
  const good = `https://${shop}/admin/oauth/authorize?${q({ client_id: 'abc123', scope: 'read_products', redirect_uri: 'https://api.example.com/merchant/integrations/shopify/callback', state })}`
  assert('exact authorize URL accepted', isSafeAuthorizeUrl(good, shop))
  const cases: Array<[string, string]> = [
    ['other shop host', good.replace(shop, 'other.myshopify.com')],
    ['attacker host', `https://evil.example/admin/oauth/authorize?${good.split('?')[1]}`],
    ['http', good.replace('https://', 'http://')],
    ['port', good.replace(`${shop}/`, `${shop}:8443/`)],
    ['userinfo', good.replace('https://', 'https://u:p@')],
    ['fragment', `${good}#x`],
    ['extra param', `${good}&grant_options%5B%5D=per-user`],
    ['write scope', good.replace('read_products', 'write_products')],
    ['extra scope', good.replace('read_products', 'read_products%2Cread_orders')],
    ['callback path', good.replace('shopify%2Fcallback', 'shopify%2Fcallback2')],
    ['http callback', good.replace('https%3A%2F%2Fapi', 'http%3A%2F%2Fapi')],
    ['IP callback', good.replace('api.example.com', '10.0.0.1')],
    ['javascript', 'javascript:alert(1)'],
    ['protocol-relative', `//${shop}/admin/oauth/authorize?${good.split('?')[1]}`],
  ]
  for (const [name, url] of cases) assert(`authorize URL refused: ${name}`, !isSafeAuthorizeUrl(url, shop))
}

// ── 5. Status, response shapes, error projection ──────────────────────────────
{
  const st = normalizeStatus({ available: true, reason: 'secret-ish', requested_scopes: ['read_products'], connections: [
    { shop_domain: 'a.myshopify.com', status: 'active', scopes: ['read_products'], connected_at: '2026-10-10T00:00:00+00:00' },
    { shop_domain: 'EVIL.com', status: 'active' },
    { shop_domain: 'b.myshopify.com', status: 'weird' },
  ] })
  assert('status keeps only canonical shops and drops reason', !!st && st.connections.length === 2 && !('reason' in st))
  assert('unknown status is "unknown"', st?.connections[1].status === 'unknown')
  assert('onboarding: product import is never inferred', onboardingSteps(st).productImport === 'not_started' && onboardingSteps(st).authorization === 'done')
  assert('complete response requires status=connected + active summary',
    parseCompleteResponse({ status: 'connected', connection: { shop_domain: 'a.myshopify.com', status: 'active' } }) !== null
    && parseCompleteResponse({ status: 'ok', connection: { shop_domain: 'a.myshopify.com', status: 'active' } }) === null
    && parseCompleteResponse({ status: 'connected', connection: { shop_domain: 'a.myshopify.com', status: 'disconnected' } }) === null
    && parseCompleteResponse(null) === null)
  const apiErr = (status: number | undefined, code?: string) => Object.assign(new Error(PROVIDER_TEXT), { status, code, detail: { message: PROVIDER_TEXT } })
  const p = (e: unknown) => JSON.stringify(projectApiFailure(e))
  assert('network error → uncertain', p(new Error(PROVIDER_TEXT)) === '{"kind":"uncertain"}')
  assert('router 404 → feature_off', p(apiErr(404, 'not_found')) === '{"kind":"feature_off"}' && p(apiErr(404)) === '{"kind":"feature_off"}')
  assert('503 unavailable code → unavailable', p(apiErr(503, 'shopify_connection_unavailable')) === '{"kind":"refused","code":"unavailable"}')
  assert('5xx without code → uncertain', p(apiErr(502)) === '{"kind":"uncertain"}')
  assert('unknown code → error', p(apiErr(400, `x_${rand(4)}`)) === '{"kind":"refused","code":"error"}')
  assert('401 → session', p(apiErr(401, 'token_expired')) === '{"kind":"session"}')
  assert('projection never carries the message', ![404, 409, 500, 502, undefined].some((s) => p(apiErr(s, 'state_replayed')).includes(PROVIDER_TEXT)))
  const keys = Object.keys(shopifyConnectionEn.messages) as MessageKey[]
  for (const code of KNOWN_ERROR_CODES) {
    assert(`code ${code} maps to fixed copy in both languages`, !!shopifyConnectionAr.messages[messageKeyForCode(code)] && !!shopifyConnectionEn.messages[messageKeyForCode(code)])
  }
  assert('ar and en message keys match', JSON.stringify(Object.keys(shopifyConnectionAr.messages).sort()) === JSON.stringify(keys.sort()))
  const en = JSON.stringify(shopifyConnectionEn)
  assert('copy never claims sync or catalog readiness', !/\bsynced\b|\bimported\b|catalog is ready|products are ready/i.test(en))
}

// ── 6. Sentry URL redaction matches early capture ─────────────────────────────
{
  const cases: Array<[string, string]> = [
    [`https://app.test/integrations/shopify/complete#shopify_handle=${HANDLE}`, 'https://app.test/integrations/shopify/complete'],
    [`https://app.test/Integrations/Shopify/Complete/?code=x#shopify_handle=${HANDLE}`, 'https://app.test/Integrations/Shopify/Complete/'],
    [`/integrations/shopify/%63omplete#shopify_handle=${HANDLE}`, '/integrations/shopify/%63omplete'],
    [`//integrations//shopify/complete?state=x`, '//integrations//shopify/complete'],
    [`/overview#SHOPIFY%5FHANDLE=${HANDLE}`, '/overview'],
    [`/login?x=shopify%255Fconnection%3Dy`, '/login'],
    ['https://api.test/merchant/integrations/shopify/status?a=1', 'https://api.test/merchant/integrations/shopify/status'],
  ]
  for (const [input, expected] of cases) assert(`redact ${input.slice(0, 60)}`, redactShopifyUrl(input) === expected, redactShopifyUrl(input))
  assert('unrelated URL untouched', redactShopifyUrl('/reset-password?token=abc') === '/reset-password?token=abc')
  assert('bare return key without query/fragment is replaced', redactShopifyUrl(`shopify_handle=${HANDLE}`) === '[scrubbed]')

  // Transaction shaped like the real leak: browser.* spans from the navigation-timing entry.
  const pageUrl = `https://app.test/Integrations/Shopify/Complete?code=x#shopify_handle=${HANDLE}`
  const txn: Record<string, unknown> = {
    type: 'transaction', transaction: '/integrations/shopify/complete',
    request: { url: 'https://app.test/integrations/shopify/complete', headers: { 'User-Agent': 'x' } },
    spans: ['browser.connect', 'browser.cache', 'browser.DNS', 'browser.request', 'browser.response'].map((op) => ({ op, description: pageUrl, data: { 'sentry.op': op } })),
    contexts: { trace: { data: { url: pageUrl } } },
    breadcrumbs: [{ category: 'navigation', data: { from: pageUrl, to: '/integrations' } }],
  }
  sanitizeSentryEvent(txn)
  assert('transaction spans / contexts / breadcrumbs lose the handle', !JSON.stringify(txn).includes(HANDLE), JSON.stringify(txn).slice(0, 200))
  assert('span description keeps the bare path', (txn.spans as Array<{ description: string }>)[0].description === 'https://app.test/Integrations/Shopify/Complete')
  for (const [name, data] of [['object', { handle: HANDLE }], ['JSON string', JSON.stringify({ handle: HANDLE })]] as const) {
    const ev: Record<string, unknown> = { request: { url: 'https://api.test/merchant/integrations/shopify/complete', method: 'POST', data }, extra: { body: { handle: HANDLE } } }
    sanitizeSentryEvent(ev)
    assert(`Shopify endpoint request body (${name}) is scrubbed`, !JSON.stringify(ev).includes(HANDLE), JSON.stringify(ev))
  }
  const other: Record<string, unknown> = { request: { url: 'https://api.test/orders/1?x=1', data: { handle: 'keep-me', note: 'n' } }, extra: { handle: 'keep-me-too' } }
  const before = JSON.stringify(other)
  sanitizeSentryEvent(other)
  assert('unrelated event data unchanged', JSON.stringify(other) === before, JSON.stringify(other))

  const sentry = src('../src/lib/sentry.ts')
  assert('Sentry sanitizes errors, transactions, spans and breadcrumbs',
    /beforeBreadcrumb[\s\S]*redactShopifyDeep\(breadcrumb\)/.test(sentry)
    && /beforeSendTransaction[\s\S]*sanitizeSentryEvent/.test(sentry)
    && /beforeSendSpan[\s\S]*redactShopifyDeep\(span\)/.test(sentry)
    && /beforeSend\(event\)[\s\S]*sanitizeSentryEvent/.test(sentry))
}

// ── 7. Completion controller ──────────────────────────────────────────────────
type Deferred = { promise: Promise<unknown>; resolve: (v: unknown) => void; reject: (e: unknown) => void; signal?: AbortSignal }
function deferred(): Deferred {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<unknown>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const apiErr = (status: number, code?: string) => Object.assign(new Error(PROVIDER_TEXT), { status, code })
const SUMMARY = { shop_domain: 'my-store.myshopify.com', status: 'active', scopes: ['read_products'], connected_at: '2026-10-10T00:00:00+00:00' }

function completionHarness(taken: ReturnType<typeof takeShopifyReturn>, opts: { enabled?: boolean } = {}) {
  let now = 10_000
  let session = 'tenant1|user1|merchant|jti-a|'
  const completes: Deferred[] = []
  const statuses: Deferred[] = []
  const timers: Array<{ at: number; fn: () => void; live: boolean }> = []
  let discarded = 0
  let takes = 0
  const c = createCompletionController({
    enabled: () => opts.enabled ?? true,
    sessionKey: () => session,
    now: () => now,
    take: () => { takes++; return takes === 1 ? taken : { kind: 'none' } },
    discard: () => { discarded++ },
    complete: (h, signal) => { const d = deferred(); d.signal = signal; (d as Deferred & { handle?: string }).handle = h; completes.push(d); return d.promise },
    status: (signal) => { const d = deferred(); d.signal = signal; statuses.push(d); return d.promise },
    schedule: (fn, ms) => { const t = { at: now + ms, fn, live: true }; timers.push(t); return () => { t.live = false } },
  })
  const runTimers = () => { for (const t of timers) if (t.live && t.at <= now) { t.live = false; t.fn() } }
  return {
    c, completes, statuses,
    get discarded() { return discarded },
    setSession: (s: string) => { session = s },
    advance: (ms: number) => { now += ms; runTimers() },
    runTimers,
  }
}
const handleTaken = () => ({ kind: 'handle' as const, handle: HANDLE, capturedAt: 10_000 })

{
  const h = completionHarness(handleTaken())
  h.c.attach(); h.c.detach(); h.c.attach() // StrictMode double mount
  h.runTimers()
  assert('StrictMode: one POST /complete', h.completes.length === 1 && (h.completes[0] as Deferred & { handle?: string }).handle === HANDLE)
  h.c.retry()
  assert('repeated retry/attach joins the single flight', h.completes.length === 1)
  h.completes[0].resolve({ status: 'connected', connection: SUMMARY })
  await flush()
  const s = h.c.getSnapshot()
  assert('success → connected with the server summary', s.view.phase === 'connected' && !h.c.holdsHandle())
  assert('state never contains the handle', !JSON.stringify(s).includes(HANDLE))
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].resolve({ status: 'connected', connection: { ...SUMMARY, status: 'quarantined' } })
  await flush()
  assert('200 without an active summary → uncertain, not connected', h.c.getSnapshot().view.phase === 'uncertain')
}
{
  const h = completionHarness({ kind: 'result', code: 'connected' })
  h.c.attach()
  assert('fragment "connected" is never trusted', h.c.getSnapshot().view.phase === 'unconfirmed' && h.completes.length === 0 && h.statuses.length === 1)
  h.statuses[0].resolve({ available: true, connections: [SUMMARY] })
  await flush()
  assert('…it only triggers a fresh status read', h.c.getSnapshot().check.kind === 'done' && h.c.getSnapshot().view.phase === 'unconfirmed')
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].reject(apiErr(409, 'state_replayed'))
  await flush()
  const v = h.c.getSnapshot().view
  assert('replayed handle → fixed alreadyUsed, not retryable', v.phase === 'refused' && v.message === 'alreadyUsed' && !v.retryable && !h.c.holdsHandle())
  assert('provider text never reaches state', !JSON.stringify(h.c.getSnapshot()).includes(PROVIDER_TEXT))
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].reject(new Error(PROVIDER_TEXT)) // network / timeout
  await flush()
  assert('uncertain outcome → no automatic re-POST, handle dropped', h.c.getSnapshot().view.phase === 'uncertain' && h.completes.length === 1 && !h.c.holdsHandle())
  h.c.retry()
  assert('retry is not offered after an uncertain outcome', h.completes.length === 1)
  h.c.checkStatus()
  assert('status check is the safe follow-up', h.statuses.length === 1)
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].reject(apiErr(409, 'exchange_in_progress'))
  await flush()
  const v = h.c.getSnapshot().view
  assert('exchange_in_progress keeps a retryable handle', v.phase === 'refused' && v.retryable && h.c.holdsHandle())
  h.c.retry()
  assert('explicit retry re-POSTs once', h.completes.length === 2)
  h.completes[1].reject(apiErr(409, 'exchange_in_progress'))
  await flush()
  h.advance(COMPLETION_TTL_MS)
  assert('retained handle is dropped from memory at TTL (timer)', !h.c.holdsHandle() && h.c.getSnapshot().view.phase === 'expired')
  h.c.retry()
  assert('no POST after expiry', h.completes.length === 2)
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].reject(apiErr(409, 'exchange_in_progress'))
  await flush()
  h.c.cancel()
  assert('cancel drops the handle; no POST afterwards', !h.c.holdsHandle() && h.c.getSnapshot().view.phase === 'cancelled')
  h.c.retry()
  assert('…and retry does nothing', h.completes.length === 1)
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.setSession('tenant2|user9|merchant|jti-b|')
  h.completes[0].resolve({ status: 'connected', connection: SUMMARY })
  await flush()
  assert('late success after a session change is discarded', h.c.getSnapshot().view.phase === 'session_changed' && !h.c.holdsHandle())
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.setSession('')
  h.c.onSessionMaybeChanged()
  assert('session signal mid-flight aborts and forgets', h.completes[0].signal?.aborted === true && !h.c.holdsHandle() && h.c.getSnapshot().view.phase === 'session_changed')
  h.completes[0].resolve({ status: 'connected', connection: SUMMARY })
  await flush()
  assert('…and the late answer is ignored', h.c.getSnapshot().view.phase === 'session_changed')
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.c.detach()
  h.runTimers() // genuine leave: still detached when the deferred check runs
  assert('leave while pending aborts the request and forgets the handle',
    h.completes[0].signal?.aborted === true && !h.c.holdsHandle() && h.discarded >= 1)
  h.completes[0].resolve({ status: 'connected', connection: SUMMARY })
  await flush()
  assert('late response after leave is ignored', h.c.getSnapshot().view.phase === 'idle')
  h.c.attach()
  assert('coming back never resubmits', h.completes.length === 1 && h.c.getSnapshot().view.phase === 'nothing')
}
{
  const h = completionHarness(handleTaken(), { enabled: false })
  h.c.attach()
  assert('feature off → no POST, capture discarded', h.completes.length === 0 && h.discarded === 1 && h.c.getSnapshot().view.phase === 'feature_off')
}
{
  const h = completionHarness(handleTaken())
  h.c.attach()
  h.completes[0].reject(apiErr(404, 'not_found'))
  await flush()
  assert('backend flag off at completion → feature_off', h.c.getSnapshot().view.phase === 'feature_off')
  const g = completionHarness(handleTaken())
  g.c.attach()
  g.completes[0].reject(apiErr(403, 'session_mismatch'))
  await flush()
  const v = g.c.getSnapshot().view
  assert('session_mismatch → restart message', v.phase === 'refused' && v.message === 'sessionChanged')
  const e = completionHarness({ kind: 'expired' })
  e.c.attach()
  assert('expired capture → no POST', e.completes.length === 0 && e.c.getSnapshot().view.phase === 'expired')
}

// ── 8. Connection card controller ─────────────────────────────────────────────
function cardHarness(opts: { readOnly?: boolean; enabled?: boolean } = {}) {
  let session = 'tenant1|user1|merchant|jti-a|'
  const calls = { status: [] as Deferred[], start: [] as Deferred[], disconnect: [] as Deferred[] }
  const navigated: string[] = []
  const c = createConnectionController({
    enabled: () => opts.enabled ?? true,
    readOnly: () => opts.readOnly ?? false,
    sessionKey: () => session,
    status: (signal) => { const d = deferred(); d.signal = signal; calls.status.push(d); return d.promise },
    start: (_shop, signal) => { const d = deferred(); d.signal = signal; calls.start.push(d); return d.promise },
    disconnect: (_shop, signal) => { const d = deferred(); d.signal = signal; calls.disconnect.push(d); return d.promise },
    navigate: (url) => { navigated.push(url) },
  })
  return { c, calls, navigated, setSession: (s: string) => { session = s } }
}
const STATE_SENTINEL = rand(43)
const AUTH_URL = `https://my-store.myshopify.com/admin/oauth/authorize?${new URLSearchParams({
  client_id: 'abc123', scope: 'read_products', redirect_uri: 'https://api.example.com/merchant/integrations/shopify/callback', state: STATE_SENTINEL,
}).toString()}`
const ACTIVE = { available: true, requested_scopes: ['read_products'], connections: [SUMMARY] }
const TOMB = { ...SUMMARY, status: 'disconnected', disconnected_at: '2026-10-10T01:00:00+00:00' }

{
  const h = cardHarness()
  h.c.refresh()
  h.calls.status[0].reject(apiErr(404))
  await flush()
  assert('backend flag off → card hidden', h.c.getSnapshot().load === 'hidden')
  h.c.refresh()
  h.calls.status[1].resolve({ ...ACTIVE, available: false, connections: [] })
  await flush()
  assert('available=false → unavailable', h.c.getSnapshot().load === 'unavailable')
  const off = cardHarness({ enabled: false })
  off.c.refresh()
  assert('dashboard flag off → hidden, no request', off.c.getSnapshot().load === 'hidden' && off.calls.status.length === 0)
}
{
  const h = cardHarness()
  h.c.refresh()
  h.c.refresh()
  h.calls.status[1].resolve({ ...ACTIVE, connections: [] })
  await flush()
  h.calls.status[0].resolve(ACTIVE)
  await flush()
  assert('out-of-order status: older answer never overwrites newer', h.c.getSnapshot().status?.connections.length === 0)
}
{
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve({ ...ACTIVE, connections: [] }); await flush()
  h.c.start('not a shop')
  assert('invalid shop → fixed error, no request', h.calls.start.length === 0 && h.c.getSnapshot().start.phase === 'error')
  h.c.start('My-Store'); h.c.start('My-Store'); h.c.start('other')
  assert('repeated clicks → one POST /start', h.calls.start.length === 1)
  h.calls.start[0].resolve({ authorize_url: AUTH_URL, expires_in: 600 })
  await flush()
  assert('validated URL → navigate once', h.navigated.length === 1 && h.navigated[0] === AUTH_URL && h.c.getSnapshot().start.phase === 'redirecting')
  assert('authorize URL / state never kept in state', !JSON.stringify(h.c.getSnapshot()).includes(STATE_SENTINEL))
  h.c.onPageShow(true)
  assert('bfcache back → form usable again + status re-read', h.c.getSnapshot().start.phase === 'idle' && h.calls.status.length === 2)
}
{
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve({ ...ACTIVE, connections: [] }); await flush()
  h.c.start('my-store')
  h.calls.start[0].resolve({ authorize_url: AUTH_URL.replace('my-store.myshopify.com', 'evil.myshopify.com'), expires_in: 600 })
  await flush()
  assert('unexpected authorize URL → no navigation', h.navigated.length === 0 && h.c.getSnapshot().start.phase === 'error')
  h.c.start('my-store')
  h.setSession('tenant1|user1|merchant|jti-b|')
  h.calls.start[1].resolve({ authorize_url: AUTH_URL, expires_in: 600 })
  await flush()
  assert('session change during start → never navigates', h.navigated.length === 0 && h.c.getSnapshot().start.phase === 'idle')
}
{
  const h = cardHarness({ readOnly: true })
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.start('my-store'); h.c.disconnect('my-store.myshopify.com')
  assert('support/owner impersonation is read-only', h.calls.start.length === 0 && h.calls.disconnect.length === 0 && h.c.getSnapshot().readOnly)
}
{
  // start then explicit disconnect: the late start answer must not navigate
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.start('my-store')
  h.c.disconnect('my-store.myshopify.com')
  assert('disconnect fences an in-flight start (aborted)', h.calls.start[0].signal?.aborted === true && h.calls.disconnect.length === 1)
  h.calls.start[0].resolve({ authorize_url: AUTH_URL, expires_in: 600 })
  await flush()
  assert('late start answer after disconnect never navigates', h.navigated.length === 0)
  h.c.start('my-store')
  assert('no start while a disconnect runs', h.calls.start.length === 1)
  h.calls.disconnect[0].resolve({ connection: TOMB })
  await flush()
  assert('proved tombstone → done', h.c.getSnapshot().disconnect.phase === 'done')
}
{
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.start('my-store')
  h.calls.start[0].resolve({ authorize_url: AUTH_URL, expires_in: 600 })
  await flush()
  h.c.disconnect('my-store.myshopify.com')
  assert('no disconnect once the browser is leaving for Shopify', h.calls.disconnect.length === 0)
}
for (const [name, body] of [
  ['malformed 200', { ok: true }],
  ['active 200', { connection: SUMMARY }],
  ['wrong-shop 200', { connection: { ...TOMB, shop_domain: 'other.myshopify.com' } }],
  ['unknown-status 200', { connection: { ...TOMB, status: 'weird' } }],
] as const) {
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.disconnect('my-store.myshopify.com')
  h.calls.disconnect[0].resolve(body)
  await flush()
  const d = h.c.getSnapshot().disconnect
  assert(`disconnect ${name} → uncertain + status re-read, never "done"`,
    d.phase === 'error' && d.message === 'uncertain' && h.calls.status.length === 2
    && h.c.getSnapshot().status?.connections[0].status === 'active')
}
{
  // a status read that started before the disconnect is dropped
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.refresh()
  h.c.disconnect('my-store.myshopify.com')
  h.calls.disconnect[0].resolve({ connection: TOMB })
  await flush()
  h.calls.status[1].resolve(ACTIVE) // stale pre-disconnect answer arrives late
  await flush()
  assert('stale pre-disconnect status never reverts the tombstone', h.c.getSnapshot().status?.connections[0].status === 'disconnected')
  h.calls.status[2].resolve({ ...ACTIVE, connections: [TOMB] })
  await flush()
  assert('post-disconnect status reconciles', h.c.getSnapshot().status?.connections[0].status === 'disconnected')
}
{
  // typed shop + open confirmation are cleared by a session change
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.setShopInput('old-tenant-typed')
  h.c.openConfirm('my-store.myshopify.com')
  assert('confirmation opens for a disconnectable shop', h.c.getSnapshot().confirmShop === 'my-store.myshopify.com')
  h.setSession('tenant2|user2|merchant|jti-z|')
  h.c.onSessionMaybeChanged()
  assert('session change clears typed shop and open confirmation',
    h.c.getSnapshot().shopInput === '' && h.c.getSnapshot().confirmShop === null && h.c.getSnapshot().status === null)
  h.calls.status[1].resolve({ ...ACTIVE, connections: [{ ...SUMMARY, shop_domain: 'new-tenant.myshopify.com' }] })
  await flush()
  const snap = JSON.stringify(h.c.getSnapshot())
  assert('old tenant shop / input never reappear after the new status loads', !snap.includes('my-store.myshopify.com') && !snap.includes('old-tenant-typed'))
  h.c.confirmDisconnect()
  assert('stale confirmation cannot fire after the reset', h.calls.disconnect.length === 0)
}
{
  const h = cardHarness()
  h.c.refresh(); h.calls.status[0].resolve(ACTIVE); await flush()
  h.c.disconnect('my-store.myshopify.com')
  h.setSession('tenant2|user2|merchant|jti-z|')
  h.calls.disconnect[0].resolve({ connection: TOMB })
  await flush()
  assert('late disconnect answer after session change is dropped and state reset',
    h.c.getSnapshot().disconnect.phase === 'idle' && h.c.getSnapshot().status === null)
}

// ── 9. Wiring and hygiene ─────────────────────────────────────────────────────
{
  const main = src('../src/main.tsx')
  const firstImport = main.split('\n').find((l) => l.startsWith('import '))
  assert('bootCapture is the first import of main.tsx', firstImport === "import './lib/shopifyConnection/bootCapture'", firstImport)
  const boot = src('../src/lib/shopifyConnection/bootCapture.ts')
  assert('boot module only imports returnCapture', (boot.match(/^import /gm) ?? []).length === 1 && boot.includes("from './returnCapture'"))
  assert('blocked boot throws a fixed error', boot.includes("throw new Error('shopify_return_scrub_failed')"))
  for (const rel of ['../src/lib/shopifyConnection/returnCapture.ts', '../src/lib/shopifyConnection/model.ts']) {
    const imports = (src(rel).match(/^import .*$/gm) ?? []).filter((l) => !l.includes("'./returnCapture'"))
    assert(`${rel.split('/').pop()} has no package imports`, imports.length === 0, imports.join('; '))
  }
  const files = [
    '../src/lib/shopifyConnection/returnCapture.ts', '../src/lib/shopifyConnection/bootCapture.ts',
    '../src/lib/shopifyConnection/model.ts', '../src/lib/shopifyConnection/completionController.ts',
    '../src/lib/shopifyConnection/connectionController.ts', '../src/api/shopifyConnection.ts',
    '../src/components/integrations/ShopifyConnectionCard.tsx', '../src/components/integrations/shopifySessionSignals.ts',
    '../src/pages/ShopifyConnectionComplete.tsx',
  ]
  for (const rel of files) {
    const code = src(rel).replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    const name = rel.split('/').pop()
    assert(`${name}: no browser storage`, !/localStorage|sessionStorage|indexedDB|document\.cookie/.test(code))
    assert(`${name}: no console logging`, !/console\./.test(code))
    assert(`${name}: never renders an error message`, !/\b(err|error|e|exc)\.message\b/.test(code))
    assert(`${name}: no history.state writes beyond null`, !/\.(pushState|replaceState)\((?!null)/.test(code))
  }
  const card = src('../src/components/integrations/ShopifyConnectionCard.tsx')
  assert('card navigates only through the validated controller path', (card.match(/location\.(assign|href|replace)/g) ?? []).length === 1 && card.includes('navigate: (url) => window.location.assign(url)'))
  const flags = src('../src/lib/platformFeatureFlags.ts')
  const fn = flags.slice(flags.indexOf('export function isShopifyConnectionUiEnabled'), flags.indexOf('}', flags.indexOf('export function isShopifyConnectionUiEnabled')))
  assert('UI flag is env-only and default off', fn.includes('isTruthyEnv(import.meta.env.VITE_SHOPIFY_CONNECTION_UI)') && !fn.includes('localStorage'))
  const app = src('../src/App.tsx')
  assert('completion route registered', app.includes('<Route path="integrations/shopify/complete" element={<ShopifyConnectionComplete />} />'))
  const integrations = src('../src/pages/Integrations.tsx')
  assert('Integrations keeps Salla / Zid / WhatsApp summary unchanged', integrations.includes('`${connectedCount} / 3`')
    && integrations.includes('const connectedCount = [sallaStatus, waStatus, zidStatus].filter(s => s.connected).length'))
  assert('Integrations renders the dormant Shopify card', integrations.includes('<ShopifyConnectionCard />'))
}

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) {
  console.error(`${failed} shopify connection UI check(s) failed`)
  process.exit(1)
}
console.log('shopify connection UI checks OK')
