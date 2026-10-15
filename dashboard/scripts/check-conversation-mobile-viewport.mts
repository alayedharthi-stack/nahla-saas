/**
 * Mobile conversation viewport regression (Chromium emulation, real app shell).
 *
 * Renders the real app (index.html -> App -> ProtectedRoute -> Layout ->
 * Conversations) from the Vite dev server against a local mock API with one
 * synthetic conversation, opens it with ?phone=, and measures the bottom of
 * the mobile fullscreen chat (stop-AI strip, composer, "Nahlah handles the
 * conversation" hint) under emulated iPhone safe-area insets
 * (CDP Emulation.setSafeAreaInsetsOverride):
 *   * the space between the hint and the bottom of the chat must be the
 *     reply row's own 8px padding plus the bottom inset ONCE;
 *   * the fullscreen chat must cover the viewport (nothing of the page
 *     behind it visible at the bottom edge);
 *   * textarea, send and stop-AI controls stay in the viewport, above the
 *     bottom inset, unobstructed; the latest message stays in view;
 *   * clicks still reach the same handlers (ai-pause / reply requests);
 *   * desktop / tablet / landscape (non-fullscreen) render exactly as before.
 * Scenarios: 390 and 320 portrait (inset zero / non-zero), landscape,
 * rotation round-trip, Android-style keyboard resize open/close, RTL/LTR,
 * Safari-like (shorter viewport, no inset) and standalone-like (full height
 * with insets), tablet and desktop.
 *
 * "before" serves the base revision's (VIEWPORT_BASE, default 0bc8836)
 * Conversations.tsx from git as a positive control: it must show the bottom
 * inset applied twice. This is Chromium emulation only: it does not
 * reproduce iOS WebKit viewport behaviour (visual viewport offset after the
 * keyboard, Safari toolbar, home-screen web app chrome).
 *
 * Run: node scripts/check-conversation-mobile-viewport.mts
 * Env: PW_CHROMIUM, VIEWPORT_EVIDENCE_DIR, VIEWPORT_BASE.
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium, type BrowserContext, type Page } from 'playwright'
import { createServer as createViteServer, type Plugin } from 'vite'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const BASE = process.env.VIEWPORT_BASE || '0bc8836f2a2c36e209615e816388fc8819e1b5f0'
const EVIDENCE = process.env.VIEWPORT_EVIDENCE_DIR || join(tmpdir(), 'nahla-mobile-viewport-evidence')
mkdirSync(EVIDENCE, { recursive: true })
const PHONE = '966500000001'
const REPLY_ROW_PADDING = 8 // `py-2` on the mobile reply row

const startedAt = Date.now()
const step = (m: string) => console.log(`[${((Date.now() - startedAt) / 1000).toFixed(1)}s] ${m}`)

// ── Mock API with one synthetic conversation ──────────────────────────
async function freePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const s = createNetServer()
    s.listen(0, '127.0.0.1', () => {
      const a = s.address()
      if (!a || typeof a === 'string') return reject(new Error('no port'))
      s.close(() => resolve(a.port))
    })
  })
}
const now = Date.now()
const conversation = {
  id: 'c-1', customer: 'عميل تجريبي', phone: PHONE, lastMsg: 'شكراً لكم', time: new Date(now).toISOString(),
  isAI: true, status: 'active', unread: 0, windowOpen: true, aiPaused: false, aiPausedReason: null,
}
const messages = Array.from({ length: 14 }, (_, i) => ({
  id: String(100 + i),
  direction: i % 2 ? 'out' : 'in',
  body: i % 2 ? `رد تجريبي رقم ${i} عن طلب قميص قطني أزرق.` : `رسالة تجريبية رقم ${i} من العميل.`,
  time: new Date(now - (14 - i) * 60_000).toISOString(),
  isAI: i % 2 === 1,
  eventType: i % 2 ? 'ai' : 'customer',
  media: null,
}))
const apiRequests: Array<{ mode: string; scenario: string; method: string; path: string }> = []
let currentMode = ''
let currentScenario = ''
const API_PORT = await freePort()
const API = `http://127.0.0.1:${API_PORT}`
const api = createServer(async (req, res) => {
  const cors = {
    'access-control-allow-origin': req.headers.origin || '*',
    'access-control-allow-headers': req.headers['access-control-request-headers'] || '*',
    'access-control-allow-methods': 'GET,POST,PATCH,PUT,DELETE,OPTIONS',
  }
  if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return }
  for await (const _ of req) { /* drain */ }
  const url = new URL(req.url || '/', API)
  apiRequests.push({ mode: currentMode, scenario: currentScenario, method: req.method || 'GET', path: url.pathname })
  const json = (status: number, body: unknown) => {
    res.writeHead(status, { ...cors, 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }
  if (req.method === 'GET' && url.pathname === '/conversations') {
    json(200, { conversations: [conversation], has_more: false, total_count: 1, filter_counts: {} })
  } else if (req.method === 'GET' && url.pathname === `/conversations/messages/${PHONE}`) {
    json(200, { messages, has_more: false })
  } else if (req.method === 'POST' && url.pathname === '/conversations/ai-pause') {
    json(200, { ok: true, aiPaused: true, aiPausedReason: 'manual_pause', aiPausedAt: new Date().toISOString() })
  } else if (req.method === 'POST' && ['/conversations/reply', '/conversations/mark-read'].includes(url.pathname)) {
    json(200, { ok: true })
  } else {
    json(404, { detail: 'not_found' })
  }
})
await new Promise<void>(r => api.listen(API_PORT, '127.0.0.1', r))

// ── Scenarios ─────────────────────────────────────────────────────────
interface Insets { top: number; bottom: number; left: number; right: number }
interface Scenario {
  name: string; width: number; height: number; mobile: boolean; insets: Insets; lang: 'ar' | 'en'
  kind: 'fullscreen' | 'shell'; note: string
}
const IPHONE_PORTRAIT: Insets = { top: 47, bottom: 34, left: 0, right: 0 }
const IPHONE_LANDSCAPE: Insets = { top: 0, bottom: 21, left: 47, right: 47 }
const NONE: Insets = { top: 0, bottom: 0, left: 0, right: 0 }
const SCENARIOS: Scenario[] = [
  { name: 'standalone-390-rtl', width: 390, height: 844, mobile: true, insets: IPHONE_PORTRAIT, lang: 'ar', kind: 'fullscreen', note: 'home-screen-like: full height, Face ID iPhone portrait insets' },
  { name: 'standalone-390-ltr', width: 390, height: 844, mobile: true, insets: IPHONE_PORTRAIT, lang: 'en', kind: 'fullscreen', note: 'same, English / LTR' },
  { name: 'safari-like-390', width: 390, height: 664, mobile: true, insets: NONE, lang: 'ar', kind: 'fullscreen', note: 'browser-like: toolbars take height, no bottom inset' },
  { name: 'safari-like-390-inset', width: 390, height: 750, mobile: true, insets: IPHONE_PORTRAIT, lang: 'ar', kind: 'fullscreen', note: 'browser-like with a bottom inset reported' },
  { name: 'narrow-320-inset', width: 320, height: 640, mobile: true, insets: IPHONE_PORTRAIT, lang: 'ar', kind: 'fullscreen', note: '320 wide, non-zero insets' },
  { name: 'narrow-320-zero', width: 320, height: 568, mobile: true, insets: NONE, lang: 'ar', kind: 'fullscreen', note: '320 wide, zero insets (no home indicator)' },
  { name: 'landscape-844', width: 844, height: 390, mobile: true, insets: IPHONE_LANDSCAPE, lang: 'ar', kind: 'shell', note: 'landscape (>= 768px wide, non-fullscreen layout)' },
  { name: 'tablet-820', width: 820, height: 1180, mobile: true, insets: { top: 24, bottom: 20, left: 0, right: 0 }, lang: 'ar', kind: 'shell', note: 'tablet portrait with insets' },
  { name: 'desktop-1280', width: 1280, height: 900, mobile: false, insets: NONE, lang: 'ar', kind: 'shell', note: 'desktop' },
]

type Mode = 'before' | 'after'
interface Measure {
  innerHeight: number; vvHeight: number; vvOffsetTop: number; insetBottomCss: number
  fullscreen: boolean
  root: { top: number; bottom: number; paddingBottom: number }
  composer: { bottom: number; paddingBottom: number }
  hintBottom: number | null
  blankBelowHint: number | null
  shellSpacer: number | null
  pageBehindAtBottom: string | null
  controls: Record<string, { top: number; bottom: number; visible: boolean; hit: boolean }>
  scroller: { gapToBottom: number; lastBubbleBottom: number; scrollerBottom: number }
}

async function measure(page: Page): Promise<Measure> {
  return await page.evaluate(() => {
    const probe = document.createElement('div')
    probe.style.cssText = 'position:fixed;visibility:hidden;padding-bottom:env(safe-area-inset-bottom,0px)'
    document.body.appendChild(probe)
    const insetBottomCss = parseFloat(getComputedStyle(probe).paddingBottom)
    probe.remove()
    const textarea = document.querySelector('textarea') as HTMLTextAreaElement
    const composer = textarea.closest('div.border-t') as HTMLElement
    const root = (textarea.closest('.fixed.inset-0') || textarea.closest('[class*="100dvh"]')) as HTMLElement
    const hint = composer.querySelector('p')
    const rr = root.getBoundingClientRect()
    const cr = composer.getBoundingClientRect()
    const shellSpacer = document.querySelector('main + div.pb-safe-bottom') as HTMLElement | null
    // What is painted at the bottom edge: the chat, or the page behind it?
    let pageBehindAtBottom: string | null = null
    for (const x of [8, innerWidth / 2, innerWidth - 8]) {
      const el = document.elementFromPoint(x, innerHeight - 1)
      if (el && !root.contains(el)) pageBehindAtBottom = `${el.tagName.toLowerCase()}.${(el as HTMLElement).className}`.slice(0, 80)
    }
    const send = composer.querySelector('button:not([type])') as HTMLElement | null
    const stopAi = composer.querySelector('button[type="button"]') as HTMLElement | null
    const controls: Record<string, { top: number; bottom: number; visible: boolean; hit: boolean }> = {}
    for (const [name, el] of [['textarea', textarea], ['send', send], ['stop-ai', stopAi]] as const) {
      if (!el) continue
      const r = el.getBoundingClientRect()
      const hitEl = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      controls[name] = {
        top: r.top, bottom: r.bottom,
        visible: r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= innerHeight - insetBottomCss + 0.5,
        hit: !!hitEl && (hitEl === el || el.contains(hitEl)),
      }
    }
    const scroller = Array.from(document.querySelectorAll('div')).find(d => d.style.background.includes('linear-gradient') && d.className.includes('overflow-y-auto')) as HTMLElement
    const bubbles = scroller.querySelectorAll('[data-presentation-kind]')
    const last = bubbles[bubbles.length - 1] as HTMLElement
    return {
      innerHeight, vvHeight: visualViewport?.height ?? innerHeight, vvOffsetTop: visualViewport?.offsetTop ?? 0, insetBottomCss,
      fullscreen: root.classList.contains('fixed'),
      root: { top: rr.top, bottom: rr.bottom, paddingBottom: parseFloat(getComputedStyle(root).paddingBottom) },
      composer: { bottom: cr.bottom, paddingBottom: parseFloat(getComputedStyle(composer).paddingBottom) },
      hintBottom: hint ? hint.getBoundingClientRect().bottom : null,
      blankBelowHint: hint ? Math.min(rr.bottom, innerHeight) - hint.getBoundingClientRect().bottom : null,
      shellSpacer: shellSpacer ? shellSpacer.getBoundingClientRect().height : null,
      pageBehindAtBottom,
      controls,
      scroller: {
        gapToBottom: scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight,
        lastBubbleBottom: last.getBoundingClientRect().bottom,
        scrollerBottom: scroller.getBoundingClientRect().bottom,
      },
    }
  })
}

function revisionPlugin(rev: string): Plugin {
  const rel = 'src/pages/Conversations.tsx'
  const source = execFileSync('git', ['show', `${rev}:dashboard/${rel}`], { cwd: ROOT, encoding: 'utf8' })
  return { name: 'mobile-viewport-base', enforce: 'pre', load: id => (id.split('?')[0] === join(ROOT, rel) ? source : null) }
}

function token(): string {
  const part = (v: unknown) => Buffer.from(JSON.stringify(v)).toString('base64url')
  return [part({ alg: 'none' }), part({ sub: 'merchant@example.test', tenant_id: 77, role: 'merchant', exp: Math.floor(now / 1000) + 30 * 86400 }), 'synthetic'].join('.')
}

async function openChat(context: BrowserContext, app: string, scenario: Scenario): Promise<Page> {
  const page = await context.newPage()
  const cdp = await context.newCDPSession(page)
  const setInsets = async (i: Insets) => cdp.send('Emulation.setSafeAreaInsetsOverride', {
    insets: { top: i.top, topMax: i.top, bottom: i.bottom, bottomMax: i.bottom, left: i.left, leftMax: i.left, right: i.right, rightMax: i.right },
  })
  await setInsets(scenario.insets)
  ;(page as Page & { setInsets?: typeof setInsets }).setInsets = setInsets
  await page.goto(`${app}/conversations?phone=${PHONE}`)
  await page.waitForSelector('textarea', { timeout: 60_000 })
  await page.waitForFunction(() => document.querySelectorAll('[data-presentation-kind]').length >= 14, null, { timeout: 30_000 })
  await page.waitForTimeout(2300) // pinConversationToLatestAfterLayout settle window
  return page
}

const results: Record<Mode, Record<string, Measure>> = { before: {}, after: {} }
const extra: Record<Mode, Record<string, unknown>> = { before: {}, after: {} }
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})
try {
  for (const mode of ['before', 'after'] as Mode[]) {
    currentMode = mode
    const port = await freePort()
    const vite = await createViteServer({
      configFile: join(ROOT, 'vite.config.ts'), root: ROOT, logLevel: 'warn', clearScreen: false,
      server: { host: '127.0.0.1', port, strictPort: true },
      plugins: mode === 'before' ? [revisionPlugin(BASE)] : [],
    })
    await vite.listen()
    const app = `http://127.0.0.1:${port}`
    try {
      for (const scenario of SCENARIOS) {
        currentScenario = scenario.name
        const context = await browser.newContext({
          viewport: { width: scenario.width, height: scenario.height }, isMobile: scenario.mobile, hasTouch: scenario.mobile,
          deviceScaleFactor: scenario.mobile ? 3 : 1, serviceWorkers: 'block',
        })
        await context.route(url => url.hostname !== '127.0.0.1', route => route.abort())
        await context.addInitScript(({ api, tok, lang }) => {
          localStorage.setItem('nahla_auth', '1')
          localStorage.setItem('nahla_token', tok)
          localStorage.setItem('nahla_role', 'merchant')
          localStorage.setItem('nahla_tenant_id', '77')
          localStorage.setItem('nahla_api_base_override', api)
          localStorage.setItem('nahla-lang', lang)
        }, { api: API, tok: token(), lang: scenario.lang })
        const page = await openChat(context, app, scenario)
        results[mode][scenario.name] = await measure(page)
        await page.screenshot({ path: join(EVIDENCE, `${mode}-${scenario.name}.png`) })
        if (scenario.name === 'standalone-390-rtl') {
          const setInsets = (page as Page & { setInsets: (i: Insets) => Promise<unknown> }).setInsets
          // Rotation round-trip: portrait -> landscape -> portrait.
          await page.setViewportSize({ width: 844, height: 390 })
          await setInsets(IPHONE_LANDSCAPE)
          await page.waitForTimeout(400)
          const landscape = await measure(page)
          await page.setViewportSize({ width: 390, height: 844 })
          await setInsets(IPHONE_PORTRAIT)
          await page.waitForTimeout(400)
          const backToPortrait = await measure(page)
          // Android-style keyboard (layout viewport resize): open, then close.
          await page.locator('textarea').focus()
          await page.setViewportSize({ width: 390, height: 844 - 336 })
          await page.waitForTimeout(400)
          const keyboardOpen = await measure(page)
          const focused = await page.evaluate(() => document.activeElement?.tagName)
          await page.screenshot({ path: join(EVIDENCE, `${mode}-${scenario.name}-keyboard-open.png`) })
          await page.setViewportSize({ width: 390, height: 844 })
          await page.locator('textarea').blur()
          await page.waitForTimeout(400)
          const keyboardClosed = await measure(page)
          // Same handlers: stop-AI and send still issue their requests.
          const before = apiRequests.length
          await page.locator('textarea').fill('رسالة تجريبية')
          await page.locator('textarea').locator('xpath=following-sibling::button').click()
          await page.waitForTimeout(600)
          const stop = page.locator('div.border-t button[type="button"]').first()
          await stop.click()
          await page.waitForTimeout(600)
          const calls = apiRequests.slice(before).filter(r => r.method === 'POST').map(r => r.path)
          extra[mode][scenario.name] = { landscape, backToPortrait, keyboardOpen, keyboardClosed, focused, calls }
          await page.screenshot({ path: join(EVIDENCE, `${mode}-${scenario.name}-after-interactions.png`) })
        }
        await context.close()
        step(`${mode}/${scenario.name}: measured`)
      }
    } finally {
      await vite.close()
    }
  }
} finally {
  await browser.close()
  api.close()
}
writeFileSync(join(EVIDENCE, 'measurements.json'), JSON.stringify({ results, extra }, null, 2))

// ── Report + assertions ───────────────────────────────────────────────
const failures: string[] = []
const check = (ok: unknown, msg: string) => { if (!ok) failures.push(msg) }
const fmt = (n: number | null) => (n === null ? '—' : String(Math.round(n * 10) / 10))
console.log('\nbottom of the conversation (CSS px):')
console.log('  mode    scenario               inset  fullscreen  hint->bottom  root-pb  composer-pb  shell-spacer  page-behind-at-bottom')
for (const mode of ['before', 'after'] as Mode[]) {
  for (const s of SCENARIOS) {
    const m = results[mode][s.name]
    console.log(`  ${mode.padEnd(7)} ${s.name.padEnd(22)} ${fmt(m.insetBottomCss).padStart(5)}  ${String(m.fullscreen).padEnd(10)}  ${fmt(m.blankBelowHint).padStart(12)}  ${fmt(m.root.paddingBottom).padStart(7)}  ${fmt(m.composer.paddingBottom).padStart(11)}  ${fmt(m.shellSpacer).padStart(12)}  ${m.pageBehindAtBottom ?? 'none'}`)
  }
}

for (const s of SCENARIOS) {
  const b = results.before[s.name]
  const a = results.after[s.name]
  check(b.insetBottomCss === s.insets.bottom && a.insetBottomCss === s.insets.bottom, `${s.name}: inset emulation not applied`)
  check(a.fullscreen === (s.kind === 'fullscreen'), `${s.name}: unexpected layout mode`)
  if (s.kind === 'fullscreen') {
    // Positive control: the base applies the bottom inset twice below the hint.
    check(Math.abs((b.blankBelowHint ?? 0) - (REPLY_ROW_PADDING + 2 * s.insets.bottom)) <= 1,
      `positive control ${s.name}: base hint->bottom ${fmt(b.blankBelowHint)}px, expected ${REPLY_ROW_PADDING + 2 * s.insets.bottom}px`)
    check(Math.abs((a.blankBelowHint ?? 0) - (REPLY_ROW_PADDING + s.insets.bottom)) <= 1,
      `${s.name}: hint->bottom ${fmt(a.blankBelowHint)}px, expected ${REPLY_ROW_PADDING + s.insets.bottom}px (inset once)`)
    check(Math.abs(a.root.top) <= 0.5 && Math.abs(a.root.bottom - a.innerHeight) <= 0.5, `${s.name}: fullscreen chat does not cover the viewport`)
    check(a.pageBehindAtBottom === null, `${s.name}: page behind the chat visible at the bottom edge (${a.pageBehindAtBottom})`)
  } else {
    // Non-fullscreen layouts (landscape >= 768px, tablet, desktop) are out of
    // scope and must render exactly as in the base revision.
    check(JSON.stringify([b.root, b.composer, b.hintBottom, b.shellSpacer]) === JSON.stringify([a.root, a.composer, a.hintBottom, a.shellSpacer]),
      `${s.name}: non-fullscreen layout changed (${JSON.stringify([b.composer, b.hintBottom])} -> ${JSON.stringify([a.composer, a.hintBottom])})`)
  }
  if (s.kind === 'fullscreen') {
    for (const [name, c] of Object.entries(a.controls)) {
      check(c.visible && c.hit, `${s.name}: ${name} not visible/reachable above the bottom inset (${JSON.stringify(c)})`)
    }
    check(Object.keys(a.controls).length === 3, `${s.name}: controls missing (${Object.keys(a.controls).join(',')})`)
    check(a.scroller.gapToBottom <= 2 && a.scroller.lastBubbleBottom <= a.scroller.scrollerBottom + 0.5,
      `${s.name}: latest message not in view (${JSON.stringify(a.scroller)})`)
  }
}
{
  const e = extra.after['standalone-390-rtl'] as {
    landscape: Measure; backToPortrait: Measure; keyboardOpen: Measure; keyboardClosed: Measure; focused: string; calls: string[]
  }
  const eb = extra.before['standalone-390-rtl'] as { calls: string[] }
  const first = results.after['standalone-390-rtl']
  console.log('\nstandalone-390-rtl interactions (after):')
  console.log(`  rotation -> landscape: fullscreen=${e.landscape.fullscreen} composer-pb=${fmt(e.landscape.composer.paddingBottom)} shell-spacer=${fmt(e.landscape.shellSpacer)}`)
  console.log(`  rotation -> portrait:  hint->bottom ${fmt(e.backToPortrait.blankBelowHint)}px (initial ${fmt(first.blankBelowHint)}px)`)
  console.log(`  keyboard open (viewport 508px): hint->bottom ${fmt(e.keyboardOpen.blankBelowHint)}px, focused=${e.focused}, controls ${Object.entries(e.keyboardOpen.controls).map(([k, v]) => `${k}:${v.visible && v.hit}`).join(' ')}`)
  console.log(`  keyboard closed: hint->bottom ${fmt(e.keyboardClosed.blankBelowHint)}px; POST calls ${JSON.stringify(e.calls)} (before: ${JSON.stringify(eb.calls)})`)
  check(Math.abs((e.backToPortrait.blankBelowHint ?? 0) - (first.blankBelowHint ?? 0)) <= 1, 'rotation round-trip changed the bottom space')
  check(Math.abs((e.keyboardClosed.blankBelowHint ?? 0) - (first.blankBelowHint ?? 0)) <= 1, 'keyboard open/close left residual bottom space')
  check(e.focused === 'TEXTAREA', 'textarea lost focus while the keyboard was open')
  for (const [name, c] of Object.entries(e.keyboardOpen.controls)) check(c.visible && c.hit, `keyboard open: ${name} not reachable`)
  check(JSON.stringify(e.calls) === JSON.stringify(eb.calls) && e.calls.includes('/conversations/reply') && e.calls.includes('/conversations/ai-pause'),
    `control handlers changed: ${JSON.stringify(eb.calls)} -> ${JSON.stringify(e.calls)}`)
}
console.log(`\nevidence written to ${EVIDENCE}`)
if (failures.length) {
  console.error(`\nconversation mobile viewport check: ${failures.length} failure(s)`)
  for (const f of failures) console.error(`  ✗ ${f}`)
  process.exit(1)
}
assert.ok(true)
console.log('\nconversation mobile viewport check: ok')
