/**
 * Regression check: protected inbound media rendered through the canonical
 * presentation contract (presentation.media + legacy media block) must be
 * fetched from the API origin with the dashboard's Bearer + X-Tenant-ID
 * headers, decoded from a blob: object URL, and never requested from the
 * dashboard origin (where the SPA fallback answers 200 text/html).
 *
 * Local only, synthetic data only:
 *   * a mock API on 127.0.0.1 that enforces Bearer + X-Tenant-ID like
 *     GET /media/inbound/<tenant>/<slug>, serving generated fixtures
 *     (an 8x6 PNG, a 0.25 s WAV tone, a 16x16 WebM, a one-page PDF);
 *   * the Vite dev server rendering the real MessagePresentationCard
 *     (scripts/fixtures/inbound-media-presentation/harness.tsx);
 *   * every other host is answered or aborted inside the browser, so no
 *     production API, customer media or provider is reachable.
 *
 * Run: node scripts/check-inbound-media-presentation.mts
 * Browser: PW_CHROMIUM=/path/to/chrome overrides the Chromium executable.
 */
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { crc32, deflateSync } from 'node:zlib'

import { chromium, type Page } from 'playwright'

import {
  MEDIA_PATHS,
  PNG_HEIGHT,
  PNG_WIDTH,
  PUBLIC_MEDIA_ORIGIN,
  PUBLIC_PRODUCT_IMAGE,
  PUBLIC_TEMPLATE_IMAGE,
  PUBLIC_TRACKING_URL,
  SYNTHETIC_TENANT_ID,
  SYNTHETIC_TOKEN,
  VIDEO_SIZE,
  WAV_SECONDS,
} from './fixtures/inbound-media-presentation/cases.ts'
import {
  isProtectedInboundMediaPath,
  resolvePresentationMediaSource,
} from '../src/lib/inboundMediaSource.ts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SLOW_RESPONSE_MS = 1500

// ── 1. Pure routing contract ──────────────────────────────────────────

assert.equal(isProtectedInboundMediaPath(MEDIA_PATHS.imageWithLegacy), true)
assert.equal(isProtectedInboundMediaPath('/media/inbound/12/abc.ogg'), true)
assert.equal(isProtectedInboundMediaPath(PUBLIC_PRODUCT_IMAGE), false, 'public https media stays direct')
assert.equal(isProtectedInboundMediaPath('//evil.example/media/inbound/1/x.png'), false, 'protocol-relative is not a local path')
assert.equal(isProtectedInboundMediaPath('/media/library/1/x.png'), false)
assert.equal(isProtectedInboundMediaPath(null), false)
assert.equal(isProtectedInboundMediaPath(undefined), false)

const legacyImageBlock = { kind: 'image' as const, storage_url: MEDIA_PATHS.imageWithLegacy }
assert.equal(
  resolvePresentationMediaSource({ kind: 'image', url: MEDIA_PATHS.imageWithLegacy }, legacyImageBlock),
  'inbound_preview',
  'matching legacy block reuses the authenticated inbound preview',
)
assert.equal(
  resolvePresentationMediaSource({ kind: 'image', url: MEDIA_PATHS.imageWithLegacy }, null),
  'protected_fetch',
  'protected path without legacy block still goes through the authenticated fetch',
)
assert.equal(
  resolvePresentationMediaSource({ kind: 'image', url: MEDIA_PATHS.imageWithoutLegacy }, legacyImageBlock),
  'protected_fetch',
  'a legacy block for a different file is never shown in place of the presentation media',
)
assert.equal(
  resolvePresentationMediaSource({ kind: 'audio', url: MEDIA_PATHS.imageWithLegacy }, legacyImageBlock),
  'protected_fetch',
  'kind mismatch never reuses the legacy block',
)
assert.equal(
  resolvePresentationMediaSource({ kind: 'audio', url: null }, { kind: 'audio', storage_url: MEDIA_PATHS.missingUrlAudio }),
  'inbound_preview',
)
assert.equal(resolvePresentationMediaSource({ kind: 'image', url: null }, null), 'direct')
assert.equal(resolvePresentationMediaSource({ kind: 'image', url: PUBLIC_TEMPLATE_IMAGE }, null), 'direct')
assert.equal(
  resolvePresentationMediaSource({ kind: 'image', url: PUBLIC_TEMPLATE_IMAGE }, legacyImageBlock),
  'direct',
  'public media keeps its own URL even when a legacy block is present',
)

// ── 2. Synthetic fixtures ─────────────────────────────────────────────

function pngChunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const typeAndData = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(typeAndData) >>> 0)
  return Buffer.concat([len, typeAndData, crc])
}

function makePng(width: number, height: number): Buffer {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 2 // truecolour RGB
  const raw = Buffer.alloc((width * 3 + 1) * height)
  for (let y = 0; y < height; y += 1) {
    const row = y * (width * 3 + 1)
    raw[row] = 0
    for (let x = 0; x < width; x += 1) {
      raw[row + 1 + x * 3] = 0x22
      raw[row + 2 + x * 3] = 0x66 + x * 8
      raw[row + 3 + x * 3] = 0xcc
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

function makeWav(seconds: number): Buffer {
  const sampleRate = 8000
  const samples = Math.round(sampleRate * seconds)
  const data = Buffer.alloc(samples * 2)
  for (let i = 0; i < samples; i += 1) {
    data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * i) / sampleRate) * 2000), i * 2)
  }
  const header = Buffer.alloc(44)
  header.write('RIFF', 0, 'latin1')
  header.writeUInt32LE(36 + data.length, 4)
  header.write('WAVE', 8, 'latin1')
  header.write('fmt ', 12, 'latin1')
  header.writeUInt32LE(16, 16)
  header.writeUInt16LE(1, 20) // PCM
  header.writeUInt16LE(1, 22) // mono
  header.writeUInt32LE(sampleRate, 24)
  header.writeUInt32LE(sampleRate * 2, 28)
  header.writeUInt16LE(2, 32)
  header.writeUInt16LE(16, 34)
  header.write('data', 36, 'latin1')
  header.writeUInt32LE(data.length, 40)
  return Buffer.concat([header, data])
}

function makePdf(): Buffer {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] >>',
  ]
  let body = '%PDF-1.4\n'
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

const PNG = makePng(PNG_WIDTH, PNG_HEIGHT)
const WAV = makeWav(WAV_SECONDS)
const PDF = makePdf()
const SPA_FALLBACK_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'

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
const VITE_PORT = await pickFreePort()
const API_ORIGIN = `http://127.0.0.1:${API_PORT}`
const APP_ORIGIN = `http://127.0.0.1:${VITE_PORT}`
const HARNESS_URL = `${APP_ORIGIN}/scripts/fixtures/inbound-media-presentation/harness.html`

// ── 3. Mock API: enforces auth + tenant exactly like routers/inbound_media.py ──

interface ApiHit {
  method: string
  path: string
  authorization: string | null
  tenant: string | null
  origin: string | null
  status: number
}

const apiHits: ApiHit[] = []
let webm: Buffer = Buffer.alloc(0)

function headerValue(req: IncomingMessage, name: string): string | null {
  const value = req.headers[name]
  if (Array.isArray(value)) return value.join(',')
  return value ?? null
}

function send(res: ServerResponse, status: number, origin: string | null, headers: Record<string, string>, body?: Buffer | string) {
  const cors: Record<string, string> = origin === APP_ORIGIN
    ? { 'Access-Control-Allow-Origin': APP_ORIGIN, Vary: 'Origin' }
    : {}
  try {
    res.writeHead(status, { ...cors, ...headers })
    res.end(body)
  } catch {
    // client aborted (cancelled fetch) — nothing to deliver
  }
}

function fixtureFor(path: string): { type: string; body: Buffer } | null {
  if (path.endsWith('.png')) return { type: 'image/png', body: PNG }
  if (path.endsWith('.wav')) return { type: 'audio/wav', body: WAV }
  if (path.endsWith('.webm')) return { type: 'video/webm', body: webm }
  if (path.endsWith('.pdf')) return { type: 'application/pdf', body: PDF }
  return null
}

const apiServer = createServer((req, res) => {
  const url = new URL(req.url || '/', API_ORIGIN)
  const origin = headerValue(req, 'origin')
  if (req.method === 'OPTIONS') {
    send(res, 204, origin, {
      'Access-Control-Allow-Methods': 'GET',
      'Access-Control-Allow-Headers': 'Authorization, X-Tenant-ID',
      'Access-Control-Max-Age': '600',
    })
    return
  }
  const hit: ApiHit = {
    method: req.method || 'GET',
    path: url.pathname + url.search,
    authorization: headerValue(req, 'authorization'),
    tenant: headerValue(req, 'x-tenant-id'),
    origin,
    status: 0,
  }
  apiHits.push(hit)
  const reply = (status: number, headers: Record<string, string>, body?: Buffer | string) => {
    hit.status = status
    send(res, status, origin, headers, body)
  }

  const match = /^\/media\/inbound\/(\d+)\/([0-9a-f]{64})(\.[a-z0-9]{2,5})$/.exec(url.pathname)
  if (req.method !== 'GET' || !match) {
    reply(404, { 'Content-Type': 'application/json' }, '{"detail":"not_found"}')
    return
  }
  if (hit.authorization !== `Bearer ${SYNTHETIC_TOKEN}` || !hit.tenant) {
    reply(401, { 'Content-Type': 'application/json' }, '{"detail":"not_authenticated"}')
    return
  }
  if (url.pathname === MEDIA_PATHS.expiredImage) {
    reply(401, { 'Content-Type': 'application/json' }, '{"detail":"token_expired"}')
    return
  }
  if (Number(match[1]) !== Number(hit.tenant)) {
    reply(403, { 'Content-Type': 'application/json' }, '{"detail":"cross_tenant_denied"}')
    return
  }
  if (url.pathname === MEDIA_PATHS.missingImage) {
    reply(404, { 'Content-Type': 'application/json' }, '{"detail":"not_found"}')
    return
  }
  if (url.pathname === MEDIA_PATHS.htmlImage || url.pathname === MEDIA_PATHS.htmlDocument) {
    reply(200, { 'Content-Type': 'text/html; charset=utf-8' }, SPA_FALLBACK_HTML)
    return
  }
  const fixture = fixtureFor(url.pathname)
  if (!fixture) {
    reply(404, { 'Content-Type': 'application/json' }, '{"detail":"not_found"}')
    return
  }
  const deliver = () => reply(200, { 'Content-Type': fixture.type, 'Cache-Control': 'no-store' }, fixture.body)
  if (url.pathname === MEDIA_PATHS.slowImage) setTimeout(deliver, SLOW_RESPONSE_MS)
  else deliver()
})

await new Promise<void>(resolve => apiServer.listen(API_PORT, '127.0.0.1', resolve))

// The mock genuinely enforces auth: a bare request is refused, so every 200
// observed later proves the dashboard attached Bearer + X-Tenant-ID.
{
  const bare = await fetch(`${API_ORIGIN}${MEDIA_PATHS.imageWithLegacy}`)
  assert.equal(bare.status, 401, 'mock API must refuse unauthenticated media requests')
  const crossTenant = await fetch(`${API_ORIGIN}${MEDIA_PATHS.crossTenantImage}`, {
    headers: { Authorization: `Bearer ${SYNTHETIC_TOKEN}`, 'X-Tenant-ID': String(SYNTHETIC_TENANT_ID) },
  })
  assert.equal(crossTenant.status, 403, 'mock API must refuse another tenant\'s path')
  apiHits.length = 0
}

// ── 4. Vite dev server serving the real component tree ───────────────

function waitForViteReady(child: ReturnType<typeof spawn>, timeoutMs = 90_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout waiting for vite dev server')), timeoutMs)
    const onData = (chunk: Buffer | string) => {
      const text = String(chunk)
      if (text.includes('ready in') || text.includes('Local:')) {
        clearTimeout(timer)
        resolve()
      }
    }
    child.stdout?.on('data', onData)
    child.stderr?.on('data', onData)
    child.once('exit', code => reject(new Error(`vite exited early (${code})`)))
  })
}

const vite = spawn(
  process.execPath,
  [join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), '--host', '127.0.0.1', '--port', String(VITE_PORT), '--strictPort'],
  { cwd: ROOT, stdio: 'pipe', env: { ...process.env, BROWSER: 'none' } },
)

function chromiumLaunchOptions(): { executablePath?: string } {
  if (process.env.PW_CHROMIUM) return { executablePath: process.env.PW_CHROMIUM }
  return {}
}

async function recordWebm(page: Page): Promise<Buffer> {
  const encoded = await page.evaluate(async size => {
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const ctx = canvas.getContext('2d')!
    const mime = ['video/webm;codecs=vp8', 'video/webm'].find(t => MediaRecorder.isTypeSupported(t))
    if (!mime) return ''
    const recorder = new MediaRecorder(canvas.captureStream(15), { mimeType: mime })
    const chunks: Blob[] = []
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data) }
    const stopped = new Promise(resolve => { recorder.onstop = resolve })
    let frame = 0
    const paint = () => { ctx.fillStyle = frame++ % 2 ? '#2a7' : '#a27'; ctx.fillRect(0, 0, size, size) }
    paint()
    recorder.start(100)
    const timer = setInterval(paint, 50)
    await new Promise(resolve => setTimeout(resolve, 700))
    clearInterval(timer)
    recorder.stop()
    await stopped
    const bytes = new Uint8Array(await new Blob(chunks).arrayBuffer())
    let binary = ''
    bytes.forEach(b => { binary += String.fromCharCode(b) })
    return btoa(binary)
  }, VIDEO_SIZE)
  if (encoded) return Buffer.from(encoded, 'base64')
  // Fallback for browsers without MediaRecorder: a local ffmpeg, if any.
  const ff = spawnSync('ffmpeg', [
    '-loglevel', 'error', '-f', 'lavfi', '-i', `color=c=blue:s=${VIDEO_SIZE}x${VIDEO_SIZE}:d=0.5`,
    '-c:v', 'libvpx', '-f', 'webm', 'pipe:1',
  ], { maxBuffer: 1 << 20 })
  if (ff.status === 0 && ff.stdout.length) return ff.stdout
  throw new Error('cannot generate a synthetic WebM fixture (no MediaRecorder, no ffmpeg)')
}

const startedAt = Date.now()
function step(message: string) {
  console.log(`[${((Date.now() - startedAt) / 1000).toFixed(1)}s] ${message}`)
}

const failures: string[] = []
function check(condition: unknown, message: string) {
  if (!condition) failures.push(message)
}

try {
  await waitForViteReady(vite)
  step(`vite ready at ${APP_ORIGIN}; mock API at ${API_ORIGIN}`)

  const browser = await chromium.launch(chromiumLaunchOptions())
  try {
    {
      const scratch = await browser.newPage()
      webm = await recordWebm(scratch)
      await scratch.close()
    }
    assert.ok(webm.length > 0, 'synthetic WebM fixture is empty')
    step(`synthetic fixtures: png=${PNG.length}B wav=${WAV.length}B webm=${webm.length}B pdf=${PDF.length}B`)

    const context = await browser.newContext({ viewport: { width: 480, height: 900 } })
    await context.addInitScript(({ token, tenant, apiOrigin }) => {
      try {
        localStorage.setItem('nahla_token', token)
        localStorage.setItem('nahla_tenant_id', String(tenant))
        localStorage.setItem('nahla_api_base_override', apiOrigin)
      } catch { /* opaque origins (about:blank) */ }
      const created: Array<{ url: string; type: string }> = []
      const revoked: string[] = []
      const originalCreate = URL.createObjectURL.bind(URL)
      const originalRevoke = URL.revokeObjectURL.bind(URL)
      URL.createObjectURL = (obj: Blob | MediaSource) => {
        const url = originalCreate(obj)
        created.push({ url, type: obj instanceof Blob ? obj.type : 'mediasource' })
        return url
      }
      URL.revokeObjectURL = (url: string) => {
        revoked.push(url)
        originalRevoke(url)
      }
      ;(window as unknown as { __blobLedger: unknown }).__blobLedger = { created, revoked }
    }, { token: SYNTHETIC_TOKEN, tenant: SYNTHETIC_TENANT_ID, apiOrigin: API_ORIGIN })

    const dashboardMediaHits: string[] = []
    const publicMediaHits: Array<{ url: string; authorization: string | null; tenant: string | null }> = []
    const abortedOutside: string[] = []
    const failedRequests: Array<{ url: string; error: string }> = []
    const allRequests: string[] = []

    // Dashboard origin: emulate the production SPA fallback (200 text/html).
    await context.route(url => url.origin === APP_ORIGIN && url.pathname.startsWith('/media/'), async route => {
      dashboardMediaHits.push(route.request().url())
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: SPA_FALLBACK_HTML })
    })
    // Anything that is not one of the two local servers never leaves the browser.
    await context.route(url => url.hostname !== '127.0.0.1', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.origin === PUBLIC_MEDIA_ORIGIN) {
        const headers = await request.allHeaders()
        publicMediaHits.push({
          url: request.url(),
          authorization: headers.authorization ?? null,
          tenant: headers['x-tenant-id'] ?? null,
        })
        await route.fulfill({ status: 200, contentType: 'image/png', body: PNG })
        return
      }
      abortedOutside.push(request.url())
      await route.abort()
    })

    const page = await context.newPage()
    const pageErrors: string[] = []
    page.on('pageerror', err => pageErrors.push(String(err)))
    page.on('request', request => allRequests.push(request.url()))
    page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText || '' }))

    await page.goto(HARNESS_URL)
    await page.waitForSelector('section[data-case="agent-text"]', { timeout: 60_000 })
    step('harness rendered')

    const section = (id: string) => `section[data-case="${id}"]`

    // Every case is awaited concurrently so a broken build fails within one
    // timeout instead of one timeout per case.
    const WAIT_MS = 15_000
    const pending: Array<Promise<void>> = []
    const eventually = (label: string, wait: () => Promise<unknown>, onTimeout: () => Promise<string>) => {
      pending.push(wait().then(() => undefined, async () => { failures.push(`${label}: ${await onTimeout()}`) }))
    }
    const attr = (selector: string, name: string) =>
      page.locator(selector).first().getAttribute(name, { timeout: 1000 }).catch(() => null)

    // ── Decoded images from blob: URLs (with and without legacy block) ──
    for (const id of ['image-with-legacy', 'image-without-legacy', 'legacy-only-image']) {
      eventually(id, () => page.waitForFunction(({ selector, width, height }) => {
        const img = document.querySelector(`${selector} img`) as HTMLImageElement | null
        return Boolean(img && img.src.startsWith('blob:') && img.complete && img.naturalWidth === width && img.naturalHeight === height)
      }, { selector: section(id), width: PNG_WIDTH, height: PNG_HEIGHT }, { timeout: WAIT_MS }),
      async () => `image did not decode from an authenticated blob URL (img src=${await attr(`${section(id)} img`, 'src')})`)
    }

    // ── Audio / video metadata decoded from blob: URLs ──
    for (const id of ['audio-with-legacy', 'audio-without-legacy', 'missing-url-audio']) {
      eventually(id, () => page.waitForFunction(({ selector, seconds }) => {
        const el = document.querySelector(`${selector} audio`) as HTMLAudioElement | null
        return Boolean(el && el.src.startsWith('blob:') && el.readyState >= 1 && Math.abs(el.duration - seconds) < 0.05)
      }, { selector: section(id), seconds: WAV_SECONDS }, { timeout: WAIT_MS }),
      async () => `audio metadata did not decode from an authenticated blob URL (audio src=${await attr(`${section(id)} audio`, 'src')})`)
    }
    for (const id of ['video-with-legacy', 'video-without-legacy']) {
      eventually(id, () => page.waitForFunction(({ selector, size }) => {
        const el = document.querySelector(`${selector} video`) as HTMLVideoElement | null
        return Boolean(el && el.src.startsWith('blob:') && el.readyState >= 1 && el.videoWidth === size)
      }, { selector: section(id), size: VIDEO_SIZE }, { timeout: WAIT_MS }),
      async () => `video metadata did not decode from an authenticated blob URL (video src=${await attr(`${section(id)} video`, 'src')})`)
    }

    // ── Documents: blob link whose bytes are the PDF served by the API ──
    for (const id of ['document-with-legacy', 'document-without-legacy']) {
      eventually(id, async () => {
        await page.waitForSelector(`${section(id)} a[href^="blob:"]`, { timeout: WAIT_MS })
        const doc = await page.evaluate(async selector => {
          const href = (document.querySelector(`${selector} a[href^="blob:"]`) as HTMLAnchorElement).href
          const blob = await (await fetch(href)).blob()
          return { type: blob.type, head: (await blob.text()).slice(0, 5) }
        }, section(id))
        check(doc.type === 'application/pdf' && doc.head === '%PDF-', `${id}: document blob is not the API PDF (${JSON.stringify(doc)})`)
      }, async () => `no authenticated blob link for the document (href=${await attr(`${section(id)} a`, 'href')})`)
    }

    // ── Graceful failure states: a final (non-loading) state is shown ──
    const errorCases: Array<{ id: string; legacy: boolean; status?: number; text?: string }> = [
      { id: 'html200-with-legacy', legacy: true, text: 'unexpected_content_type:text/html' },
      { id: 'html200-without-legacy', legacy: false },
      { id: 'html200-document-without-legacy', legacy: false },
      { id: 'http401-with-legacy', legacy: true, text: 'الجلسة منتهية' },
      { id: 'http401-without-legacy', legacy: false, status: 401 },
      { id: 'http403-with-legacy', legacy: true, text: 'مستأجراً آخر' },
      { id: 'http403-without-legacy', legacy: false, status: 403 },
      { id: 'http404-with-legacy', legacy: true, text: 'غير موجود' },
      { id: 'http404-without-legacy', legacy: false, status: 404 },
    ]
    for (const c of errorCases) {
      eventually(c.id, () => (c.legacy
        ? page.waitForFunction(({ selector, text }) => {
          const el = document.querySelector(selector)
          return Boolean(el && el.textContent?.includes(text))
        }, { selector: section(c.id), text: c.text! }, { timeout: WAIT_MS })
        : page.waitForSelector(c.status
          ? `${section(c.id)} [data-inbound-media-state="error"][data-http-status="${c.status}"]`
          : `${section(c.id)} [data-inbound-media-state="error"]`, { timeout: WAIT_MS })),
      async () => `expected graceful error state, got: ${JSON.stringify((await page.locator(section(c.id)).innerText().catch(() => '')).slice(0, 160))}`)
    }

    // ── Public / non-media presentations untouched ──
    for (const [id, src] of [['public-template-image', PUBLIC_TEMPLATE_IMAGE], ['product-card', PUBLIC_PRODUCT_IMAGE]] as const) {
      eventually(id, () => page.waitForFunction(({ selector, src }) => {
        const img = document.querySelector(`${selector} img`) as HTMLImageElement | null
        return Boolean(img && img.getAttribute('src') === src && img.complete && img.naturalWidth > 0)
      }, { selector: section(id), src }, { timeout: WAIT_MS }),
      async () => 'public media URL must stay a direct <img src> and load')
    }

    await Promise.all(pending)

    // Rejected media is never bound to a blob or an <img>.
    for (const c of errorCases) {
      const bound = await page.locator(`${section(c.id)} [src^="blob:"], ${section(c.id)} a[href^="blob:"]`).count()
      check(bound === 0, `${c.id}: rejected media must not be bound to a blob URL`)
      const img = await page.locator(`${section(c.id)} img`).count()
      check(img === 0, `${c.id}: rejected media must not render an <img>`)
    }

    // ── Caption is shown once, not duplicated by card body + inbound preview ──
    {
      const text = await page.locator(section('image-with-legacy')).innerText()
      const occurrences = text.split('هل هذا القميص القطني الأزرق متوفر؟').length - 1
      check(occurrences === 1, `image-with-legacy: caption rendered ${occurrences} times`)
    }

    check(await page.locator(`${section('public-template-image')} a[data-presentation-action="url"]`).getAttribute('href') === `${PUBLIC_TEMPLATE_IMAGE}?cta=1`,
      'public-template-image: URL button must keep its href')
    check(await page.locator(`${section('public-template-image')} [data-presentation-action="quick_reply"]`).count() === 1,
      'public-template-image: quick reply must render')
    check((await page.locator(section('public-template-image')).innerText()).includes('متجر تجريبي عام'),
      'public-template-image: template footer must render')
    check(await page.locator(`${section('product-card')} a[data-presentation-action="open_product"]`).count() === 1,
      'product-card: open_product action must render')
    check((await page.locator(section('product-card')).innerText()).includes('حذاء رياضي أبيض'),
      'product-card: product name must render')
    {
      const lifecycle = page.locator(`${section('salla-order-lifecycle')} [data-presentation-kind="order_lifecycle"]`)
      check(await lifecycle.count() === 1, 'salla-order-lifecycle: card kind must stay order_lifecycle')
      const text = await page.locator(section('salla-order-lifecycle')).innerText()
      check(text.includes('#RRRD1234') && text.includes('shipped') && text.includes('order_shipped'),
        `salla-order-lifecycle: order reference/status/template must render (${JSON.stringify(text)})`)
      check(await page.locator(`${section('salla-order-lifecycle')} a[data-presentation-action="tracking"]`).getAttribute('href') === PUBLIC_TRACKING_URL,
        'salla-order-lifecycle: tracking button must keep its URL')
      check(await page.locator(`${section('salla-order-lifecycle')} [data-presentation-action="quick_reply"]`).count() === 1,
        'salla-order-lifecycle: quick reply must render')
    }
    {
      const card = page.locator(`${section('agent-text')} [data-presentation-kind="text"]`)
      check(await card.getAttribute('dir') === 'rtl', 'agent-text: text direction must be preserved')
      check((await card.innerText()).includes('أهلاً نورة، طلب عطر ورد 100ml قيد التجهيز.'), 'agent-text: agent text must render verbatim')
    }
    check(publicMediaHits.length >= 2, `public media must be requested directly (${publicMediaHits.length} hits)`)
    for (const hit of publicMediaHits) {
      check(!hit.authorization && !hit.tenant, `public media must not carry auth headers: ${hit.url}`)
    }

    step('rendering assertions done')
    // ── Cancellation: unmount while the authenticated fetch is in flight ──
    const ledgerBefore = await page.evaluate(() => {
      const ledger = (window as unknown as { __blobLedger: { created: Array<{ url: string }> } }).__blobLedger
      return ledger.created.map(entry => entry.url)
    })
    await page.evaluate(() => window.__inboundMediaHarness!.mountSlow())
    const slowStart = Date.now()
    while (!apiHits.some(hit => hit.path === MEDIA_PATHS.slowImage) && Date.now() - slowStart < 10_000) {
      await new Promise(resolve => setTimeout(resolve, 25))
    }
    check(apiHits.some(hit => hit.path === MEDIA_PATHS.slowImage), 'slow-unmount: request never reached the API')
    await page.evaluate(() => window.__inboundMediaHarness!.unmountSlow())
    await page.waitForTimeout(SLOW_RESPONSE_MS + 1000)
    const slowLedger = await page.evaluate(before => {
      const ledger = (window as unknown as {
        __blobLedger: { created: Array<{ url: string; type: string }>; revoked: string[] }
      }).__blobLedger
      const revoked = new Set(ledger.revoked)
      return ledger.created
        .filter(entry => !before.includes(entry.url))
        .filter(entry => !revoked.has(entry.url))
    }, ledgerBefore)
    check(slowLedger.length === 0, `slow-unmount: object URL created after unmount was leaked (${JSON.stringify(slowLedger)})`)
    check(failedRequests.some(r => r.url === `${API_ORIGIN}${MEDIA_PATHS.slowImage}` && /ABORTED/i.test(r.error)),
      'slow-unmount: in-flight authenticated fetch must be aborted on unmount')

    // ── Cleanup: every object URL is revoked once the cards unmount ──
    const liveBeforeUnmount = await page.evaluate(() => {
      const ledger = (window as unknown as { __blobLedger: { created: Array<{ url: string; type: string }> } }).__blobLedger
      return ledger.created
    })
    check(!liveBeforeUnmount.some(entry => entry.type.startsWith('text/html')), 'an HTML body was turned into an object URL')
    await page.evaluate(() => window.__inboundMediaHarness!.unmountMain())
    await page.waitForTimeout(300)
    const leaked = await page.evaluate(() => {
      const ledger = (window as unknown as {
        __blobLedger: { created: Array<{ url: string; type: string }>; revoked: string[] }
      }).__blobLedger
      const revoked = new Set(ledger.revoked)
      return { created: ledger.created.length, leaked: ledger.created.filter(entry => !revoked.has(entry.url)) }
    })
    check(leaked.created > 0, 'no object URLs were created at all')
    check(leaked.leaked.length === 0, `object URLs not revoked on unmount: ${JSON.stringify(leaked.leaked)}`)

    // ── Network evidence ──
    check(dashboardMediaHits.length === 0, `media requested from the dashboard origin: ${JSON.stringify(dashboardMediaHits)}`)
    check(!allRequests.some(u => u.startsWith(`${APP_ORIGIN}/media/`)), 'a /media/ request targeted the dashboard origin')
    check(!allRequests.some(u => u.includes(SYNTHETIC_TOKEN)), 'the session token appeared in a request URL')
    check(abortedOutside.length === 0, `unexpected outbound hosts: ${JSON.stringify(abortedOutside)}`)

    const expectedApiPaths = [
      MEDIA_PATHS.imageWithLegacy, MEDIA_PATHS.audioWithLegacy, MEDIA_PATHS.videoWithLegacy, MEDIA_PATHS.documentWithLegacy,
      MEDIA_PATHS.imageWithoutLegacy, MEDIA_PATHS.audioWithoutLegacy, MEDIA_PATHS.videoWithoutLegacy, MEDIA_PATHS.documentWithoutLegacy,
      MEDIA_PATHS.legacyOnlyImage, MEDIA_PATHS.missingUrlAudio, MEDIA_PATHS.htmlImage, MEDIA_PATHS.htmlDocument,
      MEDIA_PATHS.expiredImage, MEDIA_PATHS.crossTenantImage, MEDIA_PATHS.missingImage, MEDIA_PATHS.slowImage,
    ]
    for (const path of expectedApiPaths) {
      check(apiHits.some(hit => hit.path === path), `API origin never received ${path}`)
    }
    for (const hit of apiHits) {
      check(hit.authorization === `Bearer ${SYNTHETIC_TOKEN}`, `API media request without Bearer: ${hit.path}`)
      check(hit.tenant === String(SYNTHETIC_TENANT_ID), `API media request without X-Tenant-ID: ${hit.path}`)
      check(hit.origin === APP_ORIGIN, `API media request not a CORS request from the dashboard: ${hit.path}`)
    }
    check(apiHits.filter(h => h.path === MEDIA_PATHS.crossTenantImage).every(h => h.status === 403),
      'cross-tenant path must be refused by the API, not served')
    check(pageErrors.length === 0, `page errors: ${JSON.stringify(pageErrors)}`)

    await context.close()
  } finally {
    await browser.close()
  }
} finally {
  vite.kill('SIGTERM')
  apiServer.close()
}

if (failures.length) {
  console.error(`inbound media presentation check: ${failures.length} failure(s)`)
  for (const failure of failures) console.error(`  ✗ ${failure}`)
  process.exit(1)
}
console.log(`inbound media presentation check: ok (${apiHits.length} authenticated API media requests, 0 dashboard-origin media requests)`)
