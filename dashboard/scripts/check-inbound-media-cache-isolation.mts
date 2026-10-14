/**
 * Real-browser HTTP-cache isolation regression for the authenticated inbound
 * media hooks (useAuthedMediaBlob, useAuthedDocumentBlob).
 *
 * Why: GET /media/inbound/<tenant>/<slug> answers
 * `Cache-Control: public, max-age=31536000, immutable` with `Vary: Origin`
 * only (no Vary on Authorization or tenant). Without `cache: 'no-store'` a
 * later session in the same browser can be served an earlier session's file
 * straight from the HTTP cache, without the server ever re-authorising it.
 *
 * What runs (local only, synthetic data, fake accounts):
 *   * a fake-account API on 127.0.0.1 that issues unsigned synthetic JWTs
 *     via POST /auth/login-form, revokes them on POST /auth/logout, and
 *     serves synthetic PNG/PDF files with the production response headers
 *     (FileResponse + CORSMiddleware: ACAO=<origin>, Vary: Origin,
 *     Cache-Control: public, max-age=31536000, immutable). The tenant comes
 *     from the token claim and a foreign tenant path answers 403, like
 *     routers/inbound_media.py + core/tenant.resolve_tenant_id;
 *   * the Vite dev server rendering the real auth.ts session code and the
 *     real MessagePresentationCard -> InboundMediaPreview -> hooks path, with
 *     the real public/sw.js registered as in index.html.
 *
 * Nothing intercepts or rewrites requests, mocks the service worker,
 * disables or clears the browser cache, or replaces the browser context
 * between account transitions. Each run uses ONE context and ONE page for
 * every account transition; the cache entries written by account A are
 * still there when account B acts.
 *
 * Runs:
 *   fixed                 hooks as committed (both set cache: 'no-store').
 *   control-media-hook    positive control: the Vite dev server serves
 *                         InboundMediaPreview.tsx with `cache: 'no-store'`
 *                         removed from useAuthedMediaBlob only (source on
 *                         disk untouched). Must leak A's cached image to B.
 *   control-document-hook same for useAuthedDocumentBlob only. Must leak
 *                         A's cached PDF to B.
 * A control that does not reproduce the leak fails the check: it would mean
 * the browser cache was not actually exercised.
 *
 * Transitions (each with its own synthetic files, image + PDF):
 *   T0 stale-entry  (fixed run only) A's response is put in the HTTP cache
 *                   by a default-mode fetch (what a pre-fix bundle did); a
 *                   second default fetch proves it is served from cache.
 *                   A logs out in-app, B logs in, the cards remount: the
 *                   hooks must go to the server (403) although the entry is
 *                   still cached, which a default-mode fetch as B confirms.
 *   T1 spa          A views media; route change unmounts the cards;
 *                   logout(); loginDetailed(B); cards remount (Header.tsx
 *                   logout + Login.tsx navigate, same document).
 *   T2 navigation   A views media; logout() + window.location.href (the
 *                   api/client.ts forced-logout path, new document, same
 *                   context); loginDetailed(B); cards mount.
 * After each switch B requests A's exact URLs and B's own URLs.
 *
 * Run: node scripts/check-inbound-media-cache-isolation.mts
 * Browser: PW_CHROMIUM=/path/to/chrome overrides the Chromium executable.
 */
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { crc32, deflateSync } from 'node:zlib'

import { chromium, type Page } from 'playwright'
import { createServer as createViteServer, type Plugin } from 'vite'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const HOOK_FILE = join(ROOT, 'src', 'components', 'inbound', 'InboundMediaPreview.tsx')
const HOOKS = ['useAuthedMediaBlob', 'useAuthedDocumentBlob'] as const
type HookName = typeof HOOKS[number]
type Kind = 'image' | 'document'
const HOOK_FOR_KIND: Record<Kind, HookName> = { image: 'useAuthedMediaBlob', document: 'useAuthedDocumentBlob' }

const startedAt = Date.now()
function step(message: string) {
  console.log(`[${((Date.now() - startedAt) / 1000).toFixed(1)}s] ${message}`)
}

// ── 1. Both hooks request with cache: 'no-store' (source contract) ─────

function hookSegment(source: string, hook: HookName): { start: number; end: number } {
  const start = source.indexOf(`function ${hook}(`)
  assert.ok(start >= 0, `${hook} not found in InboundMediaPreview.tsx`)
  const next = /\n(?:function |export |const |\/\*\*)/.exec(source.slice(start + 1))
  return { start, end: next ? start + 1 + next.index : source.length }
}

function fetchCalls(segment: string): string[] {
  const calls: string[] = []
  let from = 0
  for (;;) {
    const at = segment.indexOf('fetch(', from)
    if (at < 0) return calls
    let depth = 0
    let i = at + 'fetch'.length
    for (; i < segment.length; i += 1) {
      if (segment[i] === '(') depth += 1
      else if (segment[i] === ')' && --depth === 0) break
    }
    calls.push(segment.slice(at, i + 1))
    from = i
  }
}

{
  const source = readFileSync(HOOK_FILE, 'utf8')
  for (const hook of HOOKS) {
    const { start, end } = hookSegment(source, hook)
    const calls = fetchCalls(source.slice(start, end))
    assert.equal(calls.length, 1, `${hook}: expected exactly one fetch call`)
    const [call] = calls
    assert.match(call, /\bcache:\s*'no-store'/, `${hook}: fetch must set cache: 'no-store'`)
    assert.match(call, /mode:\s*'cors'/, `${hook}: fetch must stay a CORS request`)
    assert.match(call, /Authorization: `Bearer \$\{token\}`/, `${hook}: Bearer header must be kept`)
    assert.match(call, /'X-Tenant-ID': String\(tenantId\)/, `${hook}: X-Tenant-ID header must be kept`)
    assert.match(call, /\$\{base\}\$\{storage_url\}/, `${hook}: must fetch from the API base`)
  }
  step(`source contract: both hooks fetch with cache: 'no-store' (${HOOKS.join(', ')})`)
}

// ── 2. Synthetic files carrying an account/case marker ────────────────

const MARKER_RE = /synthetic-account=([AB]);case=([\w-]+);run=([\w-]+);end/

function pngChunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const typeAndData = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(typeAndData) >>> 0)
  return Buffer.concat([len, typeAndData, crc])
}

function makePng(width: number, height: number, marker: string, shade: number): Buffer {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8
  ihdr[9] = 2
  const raw = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const p = y * (width * 3 + 1) + 1 + x * 3
      raw[p] = shade
      raw[p + 1] = 0x40 + x * 4
      raw[p + 2] = 0x80
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('tEXt', Buffer.from(`Comment\0${marker}`, 'latin1')),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

function makePdf(marker: string): Buffer {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >>',
  ]
  let body = `%PDF-1.4\n% ${marker}\n`
  const offsets: number[] = []
  objects.forEach((obj, index) => {
    offsets.push(Buffer.byteLength(body, 'latin1'))
    body += `${index + 1} 0 obj\n${obj}\nendobj\n`
  })
  const xref = Buffer.byteLength(body, 'latin1')
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  for (const offset of offsets) body += `${String(offset).padStart(10, '0')} 00000 n \n`
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body, 'latin1')
}

// ── 3. Fake-account API with the production response headers ──────────

const ACCOUNTS = {
  A: { email: 'merchant-a@example.test', loginPhrase: 'fake-login-a', tenantId: 501, userId: 9501, png: [8, 6] },
  B: { email: 'merchant-b@example.test', loginPhrase: 'fake-login-b', tenantId: 502, userId: 9502, png: [5, 9] },
} as const
type AccountKey = keyof typeof ACCOUNTS

interface StoredFile { type: string; body: Buffer; marker: string; mtime: Date }
interface Hit {
  run: string
  phase: string
  method: string
  path: string
  account: AccountKey | null
  xTenant: string | null
  status: number
}

const files = new Map<string, StoredFile>()
const tokens = new Map<string, AccountKey>()
const hits: Hit[] = []
const allowedOrigins = new Set<string>()
let currentRun = 'none'
let currentPhase = 'boot'

function header(req: IncomingMessage, name: string): string | null {
  const v = req.headers[name]
  return Array.isArray(v) ? v.join(',') : v ?? null
}

/** CORSMiddleware(allow_origins=[...], allow_credentials=True, expose_headers=[...]) simple-response headers. */
function corsHeaders(origin: string | null): Record<string, string> {
  if (!origin || !allowedOrigins.has(origin)) return {}
  return {
    'access-control-allow-credentials': 'true',
    'access-control-expose-headers': 'X-Nahla-Error-Code, X-Nahla-Error-Type, X-Fast-Path',
    'access-control-allow-origin': origin,
    vary: 'Origin',
  }
}

function sendJson(res: ServerResponse, origin: string | null, status: number, body: unknown) {
  const payload = Buffer.from(JSON.stringify(body))
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': String(payload.length), ...corsHeaders(origin) })
  res.end(payload)
}

function issueToken(key: AccountKey): string {
  const account = ACCOUNTS[key]
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  const token = [
    part({ alg: 'none', typ: 'JWT' }),
    part({
      sub: account.email,
      tenant_id: account.tenantId,
      user_id: account.userId,
      role: 'merchant',
      jti: randomUUID(),
      exp: Math.floor(Date.now() / 1000) + 3600,
    }),
    'synthetic-unsigned',
  ].join('.')
  tokens.set(token, key)
  return token
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  for await (const chunk of req) chunks.push(chunk as Buffer)
  return Buffer.concat(chunks).toString('utf8')
}

const apiServer = createServer(async (req, res) => {
  const origin = header(req, 'origin')
  const url = new URL(req.url || '/', 'http://127.0.0.1')
  try {
    if (req.method === 'OPTIONS') {
      // CORSMiddleware preflight (allow_methods=["*"], allow_headers=["*"]).
      const allowed = !!origin && allowedOrigins.has(origin)
      res.writeHead(allowed ? 200 : 400, allowed ? {
        vary: 'Origin',
        'access-control-allow-methods': 'DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT',
        'access-control-max-age': '600',
        'access-control-allow-credentials': 'true',
        'access-control-allow-headers': header(req, 'access-control-request-headers') || '',
        'access-control-allow-origin': origin!,
        'content-type': 'text/plain; charset=utf-8',
      } : { 'content-type': 'text/plain; charset=utf-8' })
      res.end(allowed ? 'OK' : 'Disallowed CORS origin')
      return
    }

    if (req.method === 'POST' && url.pathname === '/auth/login-form') {
      const form = new URLSearchParams(await readBody(req))
      const key = (Object.keys(ACCOUNTS) as AccountKey[]).find(k =>
        ACCOUNTS[k].email === form.get('email') && ACCOUNTS[k].loginPhrase === form.get('password'))
      if (!key) {
        sendJson(res, origin, 401, { detail: 'invalid_credentials' })
        return
      }
      const account = ACCOUNTS[key]
      sendJson(res, origin, 200, {
        access_token: issueToken(key),
        token_type: 'bearer',
        role: 'merchant',
        tenant_id: account.tenantId,
        user_id: account.userId,
      })
      return
    }

    const bearer = (header(req, 'authorization') || '').replace(/^Bearer\s+/i, '')
    const account = tokens.get(bearer) ?? null

    if (req.method === 'POST' && url.pathname === '/auth/logout') {
      tokens.delete(bearer)
      sendJson(res, origin, 200, { ok: true })
      return
    }

    const match = /^\/media\/inbound\/(\d+)\/([0-9a-f]{64}\.[a-z0-9]{2,5})$/.exec(url.pathname)
    if (req.method !== 'GET' || !match) {
      sendJson(res, origin, 404, { detail: 'Not Found' })
      return
    }
    const hit: Hit = {
      run: currentRun,
      phase: currentPhase,
      method: req.method,
      path: url.pathname,
      account,
      xTenant: header(req, 'x-tenant-id'),
      status: 0,
    }
    hits.push(hit)
    const reply = (status: number, body: unknown) => {
      hit.status = status
      sendJson(res, origin, status, body)
    }
    if (!account) {
      reply(401, { detail: 'Not authenticated' })
      return
    }
    // resolve_tenant_id: the JWT claim is authoritative; X-Tenant-ID is not trusted.
    if (Number(match[1]) !== ACCOUNTS[account].tenantId) {
      reply(403, { detail: 'cross_tenant_denied' })
      return
    }
    const file = files.get(url.pathname)
    if (!file) {
      reply(404, { detail: 'not_found' })
      return
    }
    hit.status = 200
    // starlette FileResponse + the router's Cache-Control + CORSMiddleware.
    res.writeHead(200, {
      'content-type': file.type,
      'content-disposition': `attachment; filename="${match[2]}"`,
      'content-length': String(file.body.length),
      'last-modified': file.mtime.toUTCString(),
      etag: `"${createHash('md5').update(`${file.mtime.getTime() / 1000}-${file.body.length}`).digest('hex')}"`,
      'cache-control': 'public, max-age=31536000, immutable',
      ...corsHeaders(origin),
    })
    res.end(file.body)
  } catch (err) {
    if (!res.headersSent) sendJson(res, origin, 500, { detail: String(err) })
  }
})

async function pickFreePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = createNetServer()
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        reject(new Error('could not allocate a local port'))
        return
      }
      const port = address.port
      server.close(err => (err ? reject(err) : resolve(port)))
    })
  })
}

const API_PORT = await pickFreePort()
const API_ORIGIN = `http://127.0.0.1:${API_PORT}`
await new Promise<void>(resolve => apiServer.listen(API_PORT, '127.0.0.1', resolve))

// ── 4. Runs ───────────────────────────────────────────────────────────

type Mode = 'fixed' | 'control-media-hook' | 'control-document-hook'
const STRIPPED: Record<Mode, HookName[]> = {
  fixed: [],
  'control-media-hook': ['useAuthedMediaBlob'],
  'control-document-hook': ['useAuthedDocumentBlob'],
}

/** Positive control: serve the hook module with cache: 'no-store' removed from the given hooks. */
function noStoreMutation(hooks: HookName[], applied: string[]): Plugin {
  return {
    name: 'inbound-media-cache-positive-control',
    enforce: 'pre',
    load(id) {
      if (!hooks.length || id.split('?')[0] !== HOOK_FILE) return null
      let code = readFileSync(HOOK_FILE, 'utf8')
      for (const hook of hooks) {
        const { start, end } = hookSegment(code, hook)
        const segment = code.slice(start, end)
        const stripped = segment.replace(/^[ \t]*cache: 'no-store',[^\n]*\n/m, '')
        if (stripped === segment) throw new Error(`positive control: ${hook} has no cache: 'no-store' line`)
        code = code.slice(0, start) + stripped + code.slice(end)
        applied.push(hook)
      }
      return code
    },
  }
}

const failures: string[] = []
const evidence: string[] = []
function check(condition: unknown, message: string) {
  if (!condition) failures.push(message)
}

interface CaseRef { id: string; kind: Kind; url: string; marker: string; owner: AccountKey; transition: string }
interface Inspection { markers: string[]; imgs: Array<{ w: number; h: number; blob: boolean }>; text: string }

function makeCase(run: Mode, transition: string, owner: AccountKey, kind: Kind): CaseRef {
  const sha = createHash('sha256').update(`${run}|${transition}|${owner}|${kind}`).digest('hex')
  const url = `/media/inbound/${ACCOUNTS[owner].tenantId}/${sha}${kind === 'image' ? '.png' : '.pdf'}`
  const marker = `synthetic-account=${owner};case=${transition}-${kind};run=${run};end`
  const [w, h] = ACCOUNTS[owner].png
  files.set(url, kind === 'image'
    ? { type: 'image/png', body: makePng(w, h, marker, owner === 'A' ? 0x20 : 0xd0), marker, mtime: new Date(Date.UTC(2026, 9, 1)) }
    : { type: 'application/pdf', body: makePdf(marker), marker, mtime: new Date(Date.UTC(2026, 9, 1)) })
  return { id: `${transition}-${owner}-${kind}`, kind, url, marker, owner, transition }
}

function chromiumLaunchOptions(): { executablePath?: string } {
  return process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {}
}

async function runMode(mode: Mode) {
  currentRun = mode
  currentPhase = 'boot'
  const applied: string[] = []
  const vitePort = await pickFreePort()
  const appOrigin = `http://127.0.0.1:${vitePort}`
  allowedOrigins.add(appOrigin)
  const vite = await createViteServer({
    configFile: join(ROOT, 'vite.config.ts'),
    root: ROOT,
    logLevel: 'warn',
    clearScreen: false,
    server: { host: '127.0.0.1', port: vitePort, strictPort: true },
    plugins: [noStoreMutation(STRIPPED[mode], applied)],
  })
  await vite.listen()
  const harnessUrl = (doc: string) =>
    `${appOrigin}/scripts/fixtures/inbound-media-cache/harness.html?api=${encodeURIComponent(API_ORIGIN)}&doc=${doc}`
  const browser = await chromium.launch(chromiumLaunchOptions())
  try {
    // Warm the dev server (dependency pre-bundling) in a throwaway context,
    // before any account exists in the context under test.
    {
      const warm = await browser.newContext()
      const warmPage = await warm.newPage()
      await warmPage.goto(harnessUrl('warmup'))
      await warmPage.waitForFunction(() => document.documentElement.dataset.harnessReady === '1', null, { timeout: 60_000 })
      await warmPage.waitForTimeout(1000)
      await warm.close()
    }

    const context = await browser.newContext({ viewport: { width: 480, height: 900 } })
    const page = await context.newPage()
    const mainNavigations: string[] = []
    const foreignRequests: string[] = []
    page.on('framenavigated', frame => { if (frame === page.mainFrame()) mainNavigations.push(frame.url()) })
    page.on('request', request => {
      const u = request.url()
      if (!u.startsWith(appOrigin) && !u.startsWith(API_ORIGIN) && !u.startsWith('blob:') && !u.startsWith('data:')) {
        foreignRequests.push(u)
      }
    })

    const ready = () => page.waitForFunction(() => document.documentElement.dataset.harnessReady === '1', null, { timeout: 30_000 })
    const swControlled = () => page.evaluate(() => navigator.serviceWorker.controller?.scriptURL ?? null)
    const login = async (key: AccountKey) => {
      const { email, loginPhrase, tenantId } = ACCOUNTS[key]
      const result = await page.evaluate(([e, p]) => window.__cacheHarness!.login(e, p), [email, loginPhrase] as const)
      assert.ok(result.ok && result.tenantId === tenantId, `${mode}: login as ${key} failed (${JSON.stringify(result)})`)
    }
    const show = (cases: CaseRef[]) =>
      page.evaluate(list => window.__cacheHarness!.show(list), cases.map(({ id, kind, url }) => ({ id, kind, url })))
    const settle = async (cases: CaseRef[]) => {
      await page.waitForFunction(ids => ids.every(id => {
        const section = document.querySelector(`section[data-case="${id}"]`)
        if (!section) return false
        const img = section.querySelector('img[src^="blob:"]') as HTMLImageElement | null
        if (img) return img.complete
        return !!section.querySelector('a[href^="blob:"]') || (section.textContent || '').includes('تعذر')
      }), cases.map(c => c.id), { timeout: 20_000 })
    }
    const inspect = (c: CaseRef): Promise<Inspection> => page.evaluate(async ({ id, source }) => {
      const markerRe = new RegExp(source)
      const section = document.querySelector(`section[data-case="${id}"]`)!
      const urls = new Set<string>()
      section.querySelectorAll('[src^="blob:"]').forEach(el => urls.add(el.getAttribute('src')!))
      section.querySelectorAll('a[href^="blob:"]').forEach(el => urls.add(el.getAttribute('href')!))
      const markers: string[] = []
      for (const u of urls) {
        const bytes = new Uint8Array(await (await fetch(u)).arrayBuffer())
        let text = ''
        bytes.forEach(b => { text += String.fromCharCode(b) })
        markers.push(markerRe.exec(text)?.[0] ?? 'unmarked')
      }
      const imgs = Array.from(section.querySelectorAll('img')).map(img => ({
        w: img.naturalWidth, h: img.naturalHeight, blob: img.src.startsWith('blob:'),
      }))
      return { markers, imgs, text: (section as HTMLElement).innerText }
    }, { id: c.id, source: MARKER_RE.source })
    const hitsFor = (phase: string, c: CaseRef, account: AccountKey) =>
      hits.filter(h => h.run === mode && h.phase === phase && h.path === c.url && h.account === account)

    await page.goto(harnessUrl('boot'))
    await ready()
    await page.waitForFunction(() => !!navigator.serviceWorker.controller, null, { timeout: 20_000 })
    step(`${mode}: harness ready at ${appOrigin}; service worker controlling: ${await swControlled()}`)

    const expectPrimed = async (phase: string, cases: CaseRef[]) => {
      for (const c of cases) {
        const seen = await inspect(c)
        check(seen.markers.length === 1 && seen.markers[0] === c.marker,
          `${mode}/${phase}/${c.id}: A must see its own file (got ${JSON.stringify(seen.markers)})`)
        check(hitsFor(phase, c, 'A').some(h => h.status === 200), `${mode}/${phase}/${c.id}: A's GET never reached the API`)
      }
    }

    // B revisits A's URLs and opens its own; outcome depends on the hook under test.
    const expectAfterSwitch = async (phase: string, foreign: CaseRef[], own: CaseRef[]) => {
      check(await swControlled(), `${mode}/${phase}: service worker must be controlling the page`)
      for (const c of foreign) {
        const seen = await inspect(c)
        const bHits = hitsFor(phase, c, 'B')
        const leakExpected = STRIPPED[mode].includes(HOOK_FOR_KIND[c.kind])
        const served = bHits.length ? bHits.map(h => h.status).join('/') : 'no request (HTTP cache)'
        evidence.push(`${mode.padEnd(22)} ${phase.padEnd(14)} B -> A ${c.kind.padEnd(8)} server: ${served.padEnd(24)} shown: ${seen.markers.join(',') || 'nothing (error state)'}`)
        if (leakExpected) {
          // Positive control: proves this browser context really serves the cached A response to B.
          check(seen.markers.includes(c.marker) && bHits.length === 0,
            `${mode}/${phase}/${c.id}: POSITIVE CONTROL FAILED — without no-store B was expected to get A's cached file with no server request (server: ${served}, shown: ${JSON.stringify(seen.markers)})`)
          if (c.kind === 'image') {
            const [w, h] = ACCOUNTS.A.png
            check(seen.imgs.some(i => i.blob && i.w === w && i.h === h), `${mode}/${phase}/${c.id}: control must render A's ${w}x${h} image`)
          }
        } else {
          check(bHits.some(h => h.status === 403), `${mode}/${phase}/${c.id}: B's request for A's URL must reach the API and get 403 (server: ${served})`)
          check(bHits.every(h => h.status === 403), `${mode}/${phase}/${c.id}: API must never serve A's file to B`)
          check(seen.markers.length === 0, `${mode}/${phase}/${c.id}: A's content redisplayed to B: ${JSON.stringify(seen.markers)}`)
          check(!seen.imgs.length, `${mode}/${phase}/${c.id}: no image may render for a refused file`)
          check(seen.text.includes('مستأجراً آخر'), `${mode}/${phase}/${c.id}: the 403 state must be shown (${JSON.stringify(seen.text.slice(0, 120))})`)
        }
      }
      for (const c of own) {
        const seen = await inspect(c)
        const bHits = hitsFor(phase, c, 'B')
        evidence.push(`${mode.padEnd(22)} ${phase.padEnd(14)} B -> B ${c.kind.padEnd(8)} server: ${(bHits.map(h => h.status).join('/') || 'none').padEnd(24)} shown: ${seen.markers.join(',') || 'nothing'}`)
        check(seen.markers.length === 1 && seen.markers[0] === c.marker, `${mode}/${phase}/${c.id}: B's own file must load (got ${JSON.stringify(seen.markers)})`)
        check(bHits.some(h => h.status === 200), `${mode}/${phase}/${c.id}: B's own GET must be authorised by the API`)
        if (c.kind === 'image') {
          const [w, h] = ACCOUNTS.B.png
          check(seen.imgs.some(i => i.blob && i.w === w && i.h === h), `${mode}/${phase}/${c.id}: B's ${w}x${h} image must decode`)
        }
      }
      if (!STRIPPED[mode].length) {
        const everyMarker = await page.evaluate(async source => {
          const markerRe = new RegExp(source)
          const urls = new Set<string>()
          document.querySelectorAll('[src^="blob:"]').forEach(el => urls.add(el.getAttribute('src')!))
          document.querySelectorAll('a[href^="blob:"]').forEach(el => urls.add(el.getAttribute('href')!))
          const found: string[] = []
          for (const u of urls) {
            let text = ''
            new Uint8Array(await (await fetch(u)).arrayBuffer()).forEach(b => { text += String.fromCharCode(b) })
            found.push(markerRe.exec(text)?.[0] ?? 'unmarked')
          }
          return found
        }, MARKER_RE.source)
        check(everyMarker.every(m => m.startsWith('synthetic-account=B;')),
          `${mode}/${phase}: a non-B file is displayed in B's session: ${JSON.stringify(everyMarker)}`)
      }
    }

    // ── T0 stale entry (fixed run only) ──
    if (mode === 'fixed') {
      const phasePrime = 'T0-prime'
      const phaseAfter = 'T0-after'
      const aImg = makeCase(mode, 'T0', 'A', 'image')
      const aDoc = makeCase(mode, 'T0', 'A', 'document')
      const bImg = makeCase(mode, 'T0', 'B', 'image')
      const bDoc = makeCase(mode, 'T0', 'B', 'document')
      await login('A')
      currentPhase = phasePrime
      const defaultFetch = (url: string) => page.evaluate(async ({ api, path }) => {
        const token = localStorage.getItem('nahla_token') || ''
        const tenant = localStorage.getItem('nahla_tenant_id') || ''
        const res = await fetch(`${api}${path}`, {
          mode: 'cors',
          headers: { Authorization: `Bearer ${token}`, 'X-Tenant-ID': tenant },
        })
        let text = ''
        new Uint8Array(await res.arrayBuffer()).forEach(b => { text += String.fromCharCode(b) })
        return { status: res.status, marker: /synthetic-account=[AB];case=[\w-]+;run=[\w-]+;end/.exec(text)?.[0] ?? null }
      }, { api: API_ORIGIN, path: url })
      for (const c of [aImg, aDoc]) {
        const first = await defaultFetch(c.url)
        const afterFirst = hitsFor(phasePrime, c, 'A').length
        const second = await defaultFetch(c.url)
        const afterSecond = hitsFor(phasePrime, c, 'A').length
        evidence.push(`${mode.padEnd(22)} ${'T0-prime'.padEnd(14)} A default fetch ${c.kind.padEnd(8)} server GETs after 1st=${afterFirst} after 2nd=${afterSecond} (2nd served by HTTP cache: ${afterSecond === afterFirst})`)
        check(first.status === 200 && first.marker === c.marker && afterFirst === 1, `${mode}/T0/${c.id}: priming fetch failed`)
        check(second.status === 200 && second.marker === c.marker && afterSecond === 1,
          `${mode}/T0/${c.id}: the context's HTTP cache did not serve the primed entry — cache proof invalid`)
      }
      // In-app logout + login, then the conversation view mounts for B.
      await page.evaluate(() => window.__cacheHarness!.logout())
      await login('B')
      currentPhase = phaseAfter
      const cases = [aImg, aDoc, bImg, bDoc]
      await show(cases)
      await settle(cases)
      await expectAfterSwitch(phaseAfter, [aImg, aDoc], [bImg, bDoc])
      // The entry the hooks bypassed is still in the cache: a default-mode
      // fetch as B gets A's file without the server being asked.
      for (const c of [aImg, aDoc]) {
        const before = hitsFor(phaseAfter, c, 'B').length
        const stale = await defaultFetch(c.url)
        const after = hitsFor(phaseAfter, c, 'B').length
        evidence.push(`${mode.padEnd(22)} ${'T0-after'.padEnd(14)} B default fetch A ${c.kind.padEnd(8)} status=${stale.status} marker=${stale.marker} server GETs added=${after - before}`)
        check(stale.status === 200 && stale.marker === c.marker && after === before,
          `${mode}/T0/${c.id}: expected A's entry to still be cached (status=${stale.status}, marker=${stale.marker}, new GETs=${after - before})`)
      }
      await page.evaluate(() => window.__cacheHarness!.hide())
      await page.evaluate(() => window.__cacheHarness!.logout())
      step(`${mode}: T0 stale-entry done`)
    }

    // ── T1 spa: route change, logout(), loginDetailed(B), remount ──
    {
      const aCases = [makeCase(mode, 'T1', 'A', 'image'), makeCase(mode, 'T1', 'A', 'document')]
      const bCases = [makeCase(mode, 'T1', 'B', 'image'), makeCase(mode, 'T1', 'B', 'document')]
      await login('A')
      currentPhase = 'T1-prime'
      await show(aCases)
      await settle(aCases)
      await expectPrimed('T1-prime', aCases)
      await page.evaluate(() => window.__cacheHarness!.hide())
      await page.evaluate(() => window.__cacheHarness!.logout())
      check((await page.evaluate(() => window.__cacheHarness!.session())).hasToken === false, `${mode}/T1: logout must clear the token`)
      await login('B')
      currentPhase = 'T1-after'
      const cases = [...aCases, ...bCases]
      await show(cases)
      await settle(cases)
      await expectAfterSwitch('T1-after', aCases, bCases)
      await page.evaluate(() => window.__cacheHarness!.hide())
      await page.evaluate(() => window.__cacheHarness!.logout())
      step(`${mode}: T1 spa transition done`)
    }

    // ── T2 navigation: logout() + window.location.href, loginDetailed(B) ──
    {
      const aCases = [makeCase(mode, 'T2', 'A', 'image'), makeCase(mode, 'T2', 'A', 'document')]
      const bCases = [makeCase(mode, 'T2', 'B', 'image'), makeCase(mode, 'T2', 'B', 'document')]
      await login('A')
      currentPhase = 'T2-prime'
      await show(aCases)
      await settle(aCases)
      await expectPrimed('T2-prime', aCases)
      const target = harnessUrl('after-forced-logout')
      await Promise.all([
        page.waitForURL(u => u.searchParams.get('doc') === 'after-forced-logout', { timeout: 30_000 }),
        page.evaluate(url => window.__cacheHarness!.logoutAndNavigate(url), target).catch(() => undefined),
      ])
      await ready()
      check((await page.evaluate(() => window.__cacheHarness!.session())).hasToken === false, `${mode}/T2: logout must clear the token`)
      await login('B')
      currentPhase = 'T2-after'
      const cases = [...aCases, ...bCases]
      await show(cases)
      await settle(cases)
      await expectAfterSwitch('T2-after', aCases, bCases)
      step(`${mode}: T2 navigation transition done`)
    }

    // ── Run-level evidence ──
    check(mainNavigations.length === 2, `${mode}: unexpected document navigations (dev-server reload?): ${JSON.stringify(mainNavigations)}`)
    check(foreignRequests.length === 0, `${mode}: requests left the two local servers: ${JSON.stringify(foreignRequests)}`)
    check(JSON.stringify(applied.slice(0, STRIPPED[mode].length)) === JSON.stringify(STRIPPED[mode]),
      `${mode}: positive-control mutation not applied as configured (${JSON.stringify(applied)})`)
    // Chromium adds no request header for cache: 'no-store' (it only does for
    // 'reload' / 'no-cache'), so isolation is proven by behaviour above, not here.
    const hookHits = hits.filter(h => h.run === mode && h.phase !== 'T0-prime')
    for (const h of hookHits) {
      check(h.account !== null, `${mode}: hook request without a valid session token: ${h.path}`)
      check(h.xTenant === String(ACCOUNTS[h.account ?? 'A'].tenantId),
        `${mode}: X-Tenant-ID ${h.xTenant} does not match the session account ${h.account}`)
    }
    evidence.push(`${mode.padEnd(22)} API media requests from the hooks: ${hookHits.length} (all Bearer + matching X-Tenant-ID), stripped hooks: ${STRIPPED[mode].join(',') || 'none'}`)
    await context.close()
  } finally {
    await browser.close()
    await vite.close()
  }
}

try {
  for (const mode of ['fixed', 'control-media-hook', 'control-document-hook'] as Mode[]) {
    await runMode(mode)
  }
} finally {
  apiServer.close()
}

console.log('\nevidence:')
for (const line of evidence) console.log(`  ${line}`)
if (failures.length) {
  console.error(`\ninbound media cache isolation check: ${failures.length} failure(s)`)
  for (const failure of failures) console.error(`  ✗ ${failure}`)
  process.exit(1)
}
console.log('\ninbound media cache isolation check: ok (fixed hooks re-authorise every load; both positive controls reproduced the cross-account cache leak)')
