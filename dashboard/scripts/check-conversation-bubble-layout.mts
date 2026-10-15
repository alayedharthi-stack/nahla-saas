/**
 * Layout/colour regression for conversation bubbles (Chromium, real CSS).
 *
 * Checks, on the real MessagePresentationCard / InboundMediaPreview inside a
 * copy of the Conversations.tsx bubble markup (verified against the page
 * source below):
 *   * inbound image frames hug the displayed image (no blank band), keep the
 *     natural aspect ratio without cropping, stay within 280x280 and the
 *     bubble width, sit at the inline-start edge (RTL and LTR), and leave the
 *     caption / description blocks at full bubble width;
 *   * outbound template and order-lifecycle bubbles use the same surface,
 *     border and text colour as an ordinary outbound text bubble, and EVERY
 *     visible text in those bubbles (body, template label and name, footer,
 *     order reference/status, media placeholder, buttons) and every message
 *     timestamp reaches WCAG AA 4.5:1 — in light and dark mode, desktop,
 *     mobile and 320px, RTL and LTR. Contrast is computed from computed
 *     colours with alpha, CSS opacity and the list's background gradient
 *     (worst stop);
 *   * inbound bubbles, other outbound kinds, event badges and the failed /
 *     not-sent styling are unchanged from the base commit; their contrast is
 *     measured and reported (pre-existing values are listed, not enforced).
 *
 *   * template / order-lifecycle header images (TemplateHeaderImage) hug the
 *     loaded image at its natural aspect ratio (1600x900 artwork with edge
 *     and corner markers and intentional white margins, plus portrait,
 *     square, extreme and small images): no letterbox, crop or trim, checked
 *     in the DOM and by sampling screenshot pixels; body, footer and buttons
 *     keep the full bubble width; loading (delayed source), failure and
 *     source recovery states; product and outbound media cards (SafeImage)
 *     keep their exact frames.
 *
 * Positive controls (components and the timestamp class served from git by
 * the dev server only): "before" (BUBBLE_LAYOUT_BASE) must reproduce the
 * reported blank image frame; "previous" (BUBBLE_LAYOUT_PREVIOUS) must fail
 * the 4.5:1 text check; "parent" (BUBBLE_LAYOUT_PARENT) must reproduce the
 * letterboxed 4:3 template header frame.
 * Synthetic images and text only; any request that is not to the two local
 * servers is aborted.
 *
 * Run: node scripts/check-conversation-bubble-layout.mts
 * Env: PW_CHROMIUM (Chromium executable), BUBBLE_LAYOUT_EVIDENCE_DIR
 *      (screenshots + measurements.json; default: OS temp dir),
 *      BUBBLE_LAYOUT_BASE (default 3a3867c), BUBBLE_LAYOUT_PREVIOUS
 *      (default a8b3573), BUBBLE_LAYOUT_PARENT (default 5446a32).
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { crc32, deflateSync, inflateSync } from 'node:zlib'

import { chromium } from 'playwright'
import { createServer as createViteServer, type Plugin } from 'vite'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const BASE = process.env.BUBBLE_LAYOUT_BASE || '3a3867cf5cec2d2315f9b7d9ecc6bc5ad83fcecb'
const PREVIOUS = process.env.BUBBLE_LAYOUT_PREVIOUS || 'a8b3573f677bc2f3de99085477c02e3164dbe507'
const PARENT = process.env.BUBBLE_LAYOUT_PARENT || '5446a32781c6874672cae6eb6bef4dc3e5624224'
const HEADER_CAP = 288
const DELAY_MS = 2500
const AA = 4.5
const EVIDENCE = process.env.BUBBLE_LAYOUT_EVIDENCE_DIR || join(tmpdir(), 'nahla-bubble-layout-evidence')
const TOKEN = 'synthetic-layout-token'
const IMAGE_CAP = 280
mkdirSync(EVIDENCE, { recursive: true })

const startedAt = Date.now()
const step = (m: string) => console.log(`[${((Date.now() - startedAt) / 1000).toFixed(1)}s] ${m}`)

// ── 1. The harness markup is a faithful copy of Conversations.tsx ──────
{
  const page = readFileSync(join(ROOT, 'src/pages/Conversations.tsx'), 'utf8')
  for (const fragment of [
    'flex-1 overflow-y-auto py-4 px-3 md:px-5 space-y-1',
    "background: 'linear-gradient(180deg, #f8f9fb 0%, #f1f3f6 100%)'",
    "`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1`",
    "`flex flex-col ${isOut ? 'items-end' : 'items-start'} max-w-[78%] md:max-w-md`",
    'inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border mb-0.5',
    'w-full space-y-1',
    "'ring-1 ring-red-200 rounded-2xl'",
    'inline-flex px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider bg-red-500 text-white shadow-sm',
    "cls: 'bg-blue-50 text-blue-600 border-blue-200'",
    "cls: 'bg-amber-50 text-amber-600 border-amber-200'",
    "`flex items-center gap-1 mt-0.5 px-1 ${isOut ? 'flex-row-reverse' : ''}`",
  ]) {
    assert.ok(page.includes(fragment), `harness markup drifted from Conversations.tsx: ${fragment}`)
  }
}

/** The timestamp span class of Conversations.tsx at a revision (null = working tree). */
function timestampClassAt(rev: string | null): string {
  const source = rev
    ? execFileSync('git', ['show', `${rev}:dashboard/src/pages/Conversations.tsx`], { cwd: ROOT, encoding: 'utf8' })
    : readFileSync(join(ROOT, 'src/pages/Conversations.tsx'), 'utf8')
  const match = /<span className="([^"]+)">\s*\{formatRiyadhTime\(m\.time\)\}/.exec(source)
  assert.ok(match, `timestamp markup not found in Conversations.tsx at ${rev ?? 'working tree'}`)
  return match[1]
}

// ── 2. Synthetic images: coloured edge band + diagonal, so cropping shows ──
function pngChunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'latin1'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(td) >>> 0)
  return Buffer.concat([len, td, crc])
}
function makePng(w: number, h: number): Buffer {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0)
  ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8
  ihdr[9] = 2
  const edge = Math.max(4, Math.round(Math.min(w, h) * 0.04))
  const raw = Buffer.alloc((w * 3 + 1) * h)
  for (let y = 0; y < h; y += 1) {
    const row = y * (w * 3 + 1)
    for (let x = 0; x < w; x += 1) {
      const p = row + 1 + x * 3
      const onEdge = x < edge || y < edge || x >= w - edge || y >= h - edge
      const onDiagonal = Math.abs(x / w - y / h) < 0.01
      if (onEdge) { raw[p] = 0xe1; raw[p + 1] = 0x1d; raw[p + 2] = 0x48 } else if (onDiagonal) {
        raw[p] = 0x1e; raw[p + 1] = 0x29; raw[p + 2] = 0x3b
      } else { raw[p] = 0x60 + Math.round((y / h) * 0x60); raw[p + 1] = 0xa5; raw[p + 2] = 0xfa - Math.round((x / w) * 0x60) }
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr), pngChunk('IDAT', deflateSync(raw)), pngChunk('IEND', Buffer.alloc(0)),
  ])
}
/** 16:9 header artwork: 24px red edge band, 48px green corner squares, an
 *  intentional white margin (120px / 90px) inside the band, blue content. */
const ART = { w: 1600, h: 900, edge: 24, corner: 48, marginX: 120, marginY: 90 }
const COLORS = { edge: [0xe1, 0x1d, 0x48], corner: [0x16, 0xa3, 0x4a], white: [0xff, 0xff, 0xff], content: [0x25, 0x63, 0xeb] }
function makeArtworkPng(): Buffer {
  const { w, h, edge, corner, marginX, marginY } = ART
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0)
  ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8
  ihdr[9] = 2
  const raw = Buffer.alloc((w * 3 + 1) * h)
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const isCorner = (x < corner || x >= w - corner) && (y < corner || y >= h - corner)
      const isEdge = x < edge || y < edge || x >= w - edge || y >= h - edge
      const isMargin = x < edge + marginX || x >= w - edge - marginX || y < edge + marginY || y >= h - edge - marginY
      const c = isCorner ? COLORS.corner : isEdge ? COLORS.edge : isMargin ? COLORS.white : COLORS.content
      raw.set(c, y * (w * 3 + 1) + 1 + x * 3)
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr), pngChunk('IDAT', deflateSync(raw)), pngChunk('IEND', Buffer.alloc(0)),
  ])
}

/** Minimal PNG decoder (8-bit RGB/RGBA, non-interlaced) for screenshot sampling. */
function decodePng(buf: Buffer): { w: number; h: number; px: (x: number, y: number) => number[] } {
  let pos = 8
  let w = 0
  let h = 0
  let type = 0
  const idat: Buffer[] = []
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos)
    const kind = buf.toString('latin1', pos + 4, pos + 8)
    const data = buf.subarray(pos + 8, pos + 8 + len)
    if (kind === 'IHDR') { w = data.readUInt32BE(0); h = data.readUInt32BE(4); type = data[9]; assert.equal(data[8], 8); assert.equal(data[12], 0) }
    if (kind === 'IDAT') idat.push(data)
    pos += 12 + len
  }
  const bpp = type === 6 ? 4 : 3
  const raw = inflateSync(Buffer.concat(idat))
  const stride = w * bpp
  const out = Buffer.alloc(stride * h)
  for (let y = 0; y < h; y += 1) {
    const filter = raw[y * (stride + 1)]
    for (let i = 0; i < stride; i += 1) {
      const x = raw[y * (stride + 1) + 1 + i]
      const a = i >= bpp ? out[y * stride + i - bpp] : 0
      const b = y > 0 ? out[(y - 1) * stride + i] : 0
      const c = i >= bpp && y > 0 ? out[(y - 1) * stride + i - bpp] : 0
      const pa = Math.abs(b - c)
      const pb = Math.abs(a - c)
      const pc = Math.abs(a + b - 2 * c)
      const pred = filter === 1 ? a : filter === 2 ? b : filter === 3 ? (a + b) >> 1
        : filter === 4 ? (pa <= pb && pa <= pc ? a : pb <= pc ? b : c) : 0
      out[y * stride + i] = (x + pred) & 0xff
    }
  }
  return { w, h, px: (x, y) => Array.from(out.subarray(y * stride + x * bpp, y * stride + x * bpp + 3)) }
}

const IMAGES: Record<string, [number, number]> = {
  'portrait-1200x1600.png': [1200, 1600],
  'landscape-1600x1200.png': [1600, 1200],
  'square-1200x1200.png': [1200, 1200],
  'wide-1600x300.png': [1600, 300],
  'tall-300x1600.png': [300, 1600],
  'summer-offer-1600x1200.png': [1600, 1200],
  'header-900x1600.png': [900, 1600],
  'header-1600x200.png': [1600, 200],
  'header-200x1600.png': [200, 1600],
  'header-300x150.png': [300, 150],
}
const pngs = new Map(Object.entries(IMAGES).map(([name, [w, h]]) => [name, makePng(w, h)]))
const artwork = makeArtworkPng()
pngs.set('header-1600x900-margins.png', artwork)
pngs.set('delayed-header-1600x900.png', artwork)
const NATURAL: Record<string, [number, number]> = {
  ...IMAGES, 'header-1600x900-margins.png': [ART.w, ART.h], 'delayed-header-1600x900.png': [ART.w, ART.h],
}

// ── 3. Local API: authenticated inbound media + a public template image ──
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
const API_PORT = await freePort()
const API = `http://127.0.0.1:${API_PORT}`
const api = createServer((req, res) => {
  const origin = req.headers.origin || '*'
  const cors = { 'access-control-allow-origin': origin, 'access-control-allow-headers': 'authorization, x-tenant-id', vary: 'Origin' }
  if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return }
  const url = new URL(req.url || '/', API)
  const inbound = /^\/media\/inbound\/77\/([\w.-]+)$/.exec(url.pathname)
  const pub = /^\/public\/([\w.-]+)$/.exec(url.pathname)
  const name = inbound?.[1] ?? pub?.[1]
  if (inbound && req.headers.authorization !== `Bearer ${TOKEN}`) { res.writeHead(401, cors); res.end(); return }
  const body = name ? pngs.get(name) : undefined
  if (!body) { res.writeHead(404, cors); res.end(); return }
  const send = () => {
    res.writeHead(200, { ...cors, 'content-type': 'image/png', 'content-length': String(body.length) })
    res.end(body)
  }
  if (name === 'delayed-header-1600x900.png') setTimeout(send, DELAY_MS)
  else send()
})
await new Promise<void>(r => api.listen(API_PORT, '127.0.0.1', r))

// ── 4. Measurement ────────────────────────────────────────────────────
type Mode = 'before' | 'previous' | 'parent' | 'after'
interface Config { name: string; viewport: { width: number; height: number }; mobile: boolean; dir: 'rtl' | 'ltr'; theme: 'light' | 'dark' }
const DESKTOP = { width: 1280, height: 900 }
const MOBILE = { width: 390, height: 844 }
const CONFIGS: Config[] = [
  { name: 'desktop-rtl', viewport: DESKTOP, mobile: false, dir: 'rtl', theme: 'light' },
  { name: 'desktop-rtl-dark', viewport: DESKTOP, mobile: false, dir: 'rtl', theme: 'dark' },
  { name: 'desktop-ltr', viewport: DESKTOP, mobile: false, dir: 'ltr', theme: 'light' },
  { name: 'desktop-ltr-dark', viewport: DESKTOP, mobile: false, dir: 'ltr', theme: 'dark' },
  { name: 'mobile-rtl', viewport: MOBILE, mobile: true, dir: 'rtl', theme: 'light' },
  { name: 'mobile-rtl-dark', viewport: MOBILE, mobile: true, dir: 'rtl', theme: 'dark' },
  { name: 'mobile-ltr', viewport: MOBILE, mobile: true, dir: 'ltr', theme: 'light' },
  { name: 'mobile-ltr-dark', viewport: MOBILE, mobile: true, dir: 'ltr', theme: 'dark' },
  { name: 'narrow-rtl', viewport: { width: 320, height: 720 }, mobile: true, dir: 'rtl', theme: 'light' },
]
const CONTROL_CONFIGS = ['desktop-rtl', 'desktop-rtl-dark', 'mobile-rtl']
const MODE_REVISION: Record<Mode, string | null> = { before: BASE, previous: PREVIOUS, parent: PARENT, after: null }
const INBOUND_IMAGE_CASES = ['in-portrait-long', 'in-landscape', 'in-square', 'in-wide', 'in-tall', 'in-portrait-protected']
/** Template / order-lifecycle bubbles with a loadable image header. */
const TEMPLATE_HEADER_CASES = ['out-template', 'out-template-failed', 'tpl-16x9', 'tpl-portrait', 'tpl-square', 'tpl-wide',
  'tpl-tall', 'tpl-small', 'tpl-delayed', 'tpl-recovery', 'lifecycle-image']
const TEMPLATE_CASES = ['out-template', 'out-lifecycle', 'out-template-failed', 'out-template-broken-image',
  ...TEMPLATE_HEADER_CASES.filter(id => !['out-template', 'out-template-failed'].includes(id))]
/** Other SafeImage consumers whose frames must stay exactly as in the parent commit. */
const SAFE_IMAGE_CASES = ['out-product', 'out-media']
/** Ordinary outbound + template bubbles: every visible text must reach AA. */
const ENFORCED_CASES = ['out-plain', ...TEMPLATE_CASES]
const UNCHANGED_CASES = ['in-text', 'in-portrait-long', 'in-landscape', 'out-interactive', 'out-media', 'out-product']
const SCREENSHOT_CASES = ['in-portrait-long', 'out-plain', 'out-template', 'out-lifecycle', 'out-template-failed', 'out-template-broken-image',
  'tpl-16x9', 'tpl-portrait', 'tpl-square', 'tpl-wide', 'tpl-tall', 'tpl-small', 'tpl-recovery', 'lifecycle-image', 'out-product']

function partOf(text: string, role: string): string {
  if (role !== 'text') return role
  if (text === 'قالب واتساب' || text === 'تحديث طلب') return 'template-label'
  if (text.startsWith('·')) return 'template-name'
  if (text === 'متجر تجريبي عام') return 'footer'
  if (text === '#RRRD1234' || text === 'shipped') return 'order-details'
  if (text === 'تعذر عرض الوسائط') return 'media-placeholder'
  return 'body'
}

interface ImageMeasure {
  natural: [number, number]; rendered: [number, number]; frame: [number, number]; frameInner: [number, number]
  parentContentWidth: number; startGap: number; blankWidth: number; textBlockWidths: number[]; wrapperWidth: number
}
interface TextMeasure { text: string; role: 'button' | 'text'; color: string; background: string; ratio: number }
interface ExtraMeasure { role: 'timestamp' | 'not-sent-badge' | 'event-badge'; text: string; color: string; background: string; ratio: number }
type Rect = [number, number, number, number]
interface HeaderMeasure {
  status: string; frame: Rect; img: Rect | null; natural: [number, number] | null; src: string | null
  contentWidth: number; startGap: number; frameText: string
  bodyWidth: number | null; footerWidth: number | null; actionsWidth: number | null; cardInnerWidth: number
}
interface CaseMeasure {
  card: { bg: string; color: string; border: string; width: number; scrollOverflow: number }
  image?: ImageMeasure
  header?: HeaderMeasure
  safeImage?: { frame: Rect; img: Rect }
  texts: TextMeasure[]
  extras: ExtraMeasure[]
  stackShadow: string; badgeBg: string | null; eventBadgeBg: string | null
}

async function measure(page: import('playwright').Page): Promise<{ cases: Record<string, CaseMeasure>; pageOverflow: number }> {
  return await page.evaluate(() => {
    const parse = (c: string): [number, number, number, number] => {
      const m = c.match(/rgba?\(([^)]+)\)/)
      if (!m) return [0, 0, 0, 0]
      const parts = m[1].split(/[ ,/]+/).filter(Boolean).map(Number)
      return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1]
    }
    const lum = ([r, g, b]: number[]) => {
      const f = (v: number) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 }
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    }
    const over = (top: number[], under: number[]) => [0, 1, 2].map(i => top[3] * top[i] + (1 - top[3]) * under[i])
    /** Contrast of an element's text against what is painted behind it: translucent
     *  background layers composited over the first opaque layer, or over every stop
     *  of an ancestor background gradient (the worst stop is reported). */
    const contrastOf = (el: Element) => {
      const layers: number[][] = []
      let bases: number[][] | null = null
      for (let n: Element | null = el; n; n = n.parentElement) {
        const st = getComputedStyle(n)
        const c = parse(st.backgroundColor)
        if (c[3] > 0) layers.push(c)
        if (c[3] >= 1) { bases = [[0, 0, 0]]; break }
        const stops = (st.backgroundImage.match(/rgba?\([^)]+\)/g) || []).map(parse)
        if (stops.length) { bases = stops.map(stop => stop.slice(0, 3)); break }
      }
      const fg = parse(getComputedStyle(el).color)
      // CSS opacity on the element or an ancestor fades the glyphs too.
      for (let n: Element | null = el; n; n = n.parentElement) fg[3] *= parseFloat(getComputedStyle(n).opacity)
      let worst = { ratio: Infinity, bg: [255, 255, 255] }
      for (const base of bases ?? [[255, 255, 255]]) {
        let bg = base
        for (const layer of [...layers].reverse()) bg = over(layer, bg)
        const [l1, l2] = [lum(over(fg, bg)), lum(bg)].sort((a, b) => b - a)
        const ratio = (l1 + 0.05) / (l2 + 0.05)
        if (ratio < worst.ratio) worst = { ratio, bg }
      }
      return {
        color: getComputedStyle(el).color,
        background: `rgb(${worst.bg.map(v => Math.round(v)).join(', ')})`,
        ratio: Math.floor(worst.ratio * 100) / 100,
      }
    }
    const out: Record<string, unknown> = {}
    document.querySelectorAll('[data-case]').forEach(row => {
      const id = row.getAttribute('data-case')!
      const card = row.querySelector('[data-presentation-kind]') as HTMLElement
      const cs = getComputedStyle(card)
      const m: Record<string, unknown> = {
        card: {
          bg: cs.backgroundColor, color: cs.color, border: cs.borderTopColor,
          width: card.getBoundingClientRect().width, scrollOverflow: card.scrollWidth - card.clientWidth,
        },
        stackShadow: getComputedStyle(row.querySelector('[data-presentation-stack]')!).boxShadow,
        badgeBg: (() => { const b = row.querySelector('[data-not-sent-badge]'); return b ? getComputedStyle(b).backgroundColor : null })(),
        eventBadgeBg: (() => { const b = row.querySelector('[data-event-badge]'); return b ? getComputedStyle(b).backgroundColor : null })(),
      }
      const rect = (el: Element): [number, number, number, number] => {
        const r = el.getBoundingClientRect()
        return [r.left, r.top, r.width, r.height]
      }
      const header = card.querySelector('[data-template-header-image]') as HTMLElement | null
      if (header) {
        const himg = header.querySelector('img') as HTMLImageElement | null
        const content = header.parentElement as HTMLElement
        const ccs = getComputedStyle(content)
        const cr = content.getBoundingClientRect()
        const cLeft = cr.left + parseFloat(ccs.paddingLeft)
        const cRight = cr.right - parseFloat(ccs.paddingRight)
        const hr = header.getBoundingClientRect()
        const rtl = ccs.direction === 'rtl'
        const body = card.querySelector('.whitespace-pre-wrap')
        const footer = Array.from(content.children).find(c => c.textContent === 'متجر تجريبي عام')
        const actions = card.querySelector('[data-presentation-action]')?.parentElement
        m.header = {
          status: header.getAttribute('data-template-header-image'),
          frame: rect(header),
          img: himg ? rect(himg) : null,
          natural: himg && himg.complete && himg.naturalWidth ? [himg.naturalWidth, himg.naturalHeight] : null,
          src: himg?.getAttribute('src') ?? null,
          contentWidth: cRight - cLeft,
          startGap: rtl ? cRight - hr.right : hr.left - cLeft,
          frameText: (header.innerText || '').trim(),
          bodyWidth: body ? body.getBoundingClientRect().width : null,
          footerWidth: footer ? footer.getBoundingClientRect().width : null,
          actionsWidth: actions ? actions.getBoundingClientRect().width : null,
          cardInnerWidth: card.clientWidth,
        }
      }
      const plainImg = card.querySelector('img:not([src^="blob:"])') as HTMLImageElement | null
      if (plainImg && !header) m.safeImage = { frame: rect(plainImg.parentElement!), img: rect(plainImg) }
      const img = card.querySelector('img[src^="blob:"]') as HTMLImageElement | null
      if (img) {
        const frame = img.parentElement as HTMLElement
        const parent = frame.parentElement as HTMLElement
        const fr = frame.getBoundingClientRect()
        const fcs = getComputedStyle(frame)
        const bx = parseFloat(fcs.borderLeftWidth) + parseFloat(fcs.borderRightWidth)
        const by = parseFloat(fcs.borderTopWidth) + parseFloat(fcs.borderBottomWidth)
        const ir = img.getBoundingClientRect()
        const pr = parent.getBoundingClientRect()
        const pcs = getComputedStyle(parent)
        const contentLeft = pr.left + parseFloat(pcs.paddingLeft) + parseFloat(pcs.borderLeftWidth)
        const contentRight = pr.right - parseFloat(pcs.paddingRight) - parseFloat(pcs.borderRightWidth)
        const rtl = getComputedStyle(parent).direction === 'rtl'
        const siblings = Array.from(parent.children).filter(c => c !== frame && (c.textContent || '').trim()) as HTMLElement[]
        m.image = {
          natural: [img.naturalWidth, img.naturalHeight],
          rendered: [ir.width, ir.height],
          frame: [fr.width, fr.height],
          frameInner: [fr.width - bx, fr.height - by],
          parentContentWidth: contentRight - contentLeft,
          startGap: rtl ? contentRight - fr.right : fr.left - contentLeft,
          blankWidth: fr.width - bx - ir.width,
          textBlockWidths: siblings.map(s => s.getBoundingClientRect().width),
          wrapperWidth: contentRight - contentLeft,
        }
      }
      /** Painted = has boxes and is not clipped away by an overflow ancestor. */
      const painted = (el: Element) => {
        const r = el.getBoundingClientRect()
        if (!el.getClientRects().length || r.width < 1 || r.height < 1) return false
        for (let n = el.parentElement; n; n = n.parentElement) {
          if (getComputedStyle(n).overflow === 'visible') continue
          const c = n.getBoundingClientRect()
          if (r.bottom <= c.top || r.top >= c.bottom || r.right <= c.left || r.left >= c.right) return false
        }
        return true
      }
      const ownText = (el: Element) =>
        Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent || '').join('').trim()
      const texts: unknown[] = []
      card.querySelectorAll('*').forEach(el => {
        const own = ownText(el)
        if (!own || !painted(el)) return
        texts.push({ text: own.slice(0, 40), role: el.closest('[data-presentation-action]') ? 'button' : 'text', ...contrastOf(el) })
      })
      m.texts = texts
      const extras: unknown[] = []
      for (const [selector, role] of [['[data-timestamp]', 'timestamp'], ['[data-not-sent-badge]', 'not-sent-badge'], ['[data-event-badge]', 'event-badge']]) {
        row.querySelectorAll(selector).forEach(el => {
          if (painted(el)) extras.push({ role, text: (el.textContent || '').trim(), ...contrastOf(el) })
        })
      }
      m.extras = extras
      out[id] = m
    })
    return { cases: out as never, pageOverflow: document.documentElement.scrollWidth - window.innerWidth }
  })
}

function revisionSourcePlugin(rev: string): Plugin {
  const files = ['src/components/conversations/MessagePresentationCard.tsx', 'src/components/inbound/InboundMediaPreview.tsx']
  const byPath = new Map(files.map(rel => [join(ROOT, rel), execFileSync('git', ['show', `${rev}:dashboard/${rel}`], { cwd: ROOT, encoding: 'utf8' })]))
  return { name: 'bubble-layout-revision-sources', enforce: 'pre', load: id => byPath.get(id.split('?')[0]) ?? null }
}

const failures: string[] = []
const check = (ok: unknown, msg: string) => { if (!ok) failures.push(msg) }
const results: Record<string, Record<string, Awaited<ReturnType<typeof measure>>>> = { before: {}, previous: {}, parent: {}, after: {} }
interface HeaderEvents {
  loading?: HeaderMeasure; recoveryInitial?: HeaderMeasure; recovered: boolean; recoveredSrc?: string | null
  pixels: Record<string, { size: [number, number]; samples: Record<string, number[]>; whiteBandDevicePx: number; expectedWhiteBandDevicePx: number }>
}
const headerEvents: Record<string, Record<string, HeaderEvents>> = { before: {}, previous: {}, parent: {}, after: {} }
const near = (a: number[], b: number[], tol = 48) => a.every((v, i) => Math.abs(v - b[i]) <= tol)
const headerOf = (page: import('playwright').Page, id: string) => page.evaluate(caseId => {
  const header = document.querySelector(`[data-case="${caseId}"] [data-template-header-image]`) as HTMLElement | null
  if (!header) return null
  const img = header.querySelector('img') as HTMLImageElement | null
  const r = header.getBoundingClientRect()
  const content = header.parentElement!.getBoundingClientRect()
  return {
    status: header.getAttribute('data-template-header-image') || '',
    frame: [r.left, r.top, r.width, r.height] as [number, number, number, number],
    img: img ? (() => { const ir = img.getBoundingClientRect(); return [ir.left, ir.top, ir.width, ir.height] as [number, number, number, number] })() : null,
    natural: img && img.naturalWidth ? [img.naturalWidth, img.naturalHeight] as [number, number] : null,
    src: img?.getAttribute('src') ?? null,
    contentWidth: content.width - 20,
    startGap: 0, frameText: (header.innerText || '').trim(), bodyWidth: null, footerWidth: null, actionsWidth: null, cardInnerWidth: 0,
  }
}, id)

const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})
try {
  for (const mode of ['before', 'previous', 'parent', 'after'] as Mode[]) {
    const revision = MODE_REVISION[mode]
    const port = await freePort()
    const vite = await createViteServer({
      configFile: join(ROOT, 'vite.config.ts'), root: ROOT, logLevel: 'warn', clearScreen: false,
      server: { host: '127.0.0.1', port, strictPort: true },
      plugins: revision ? [revisionSourcePlugin(revision)] : [],
    })
    await vite.listen()
    const app = `http://127.0.0.1:${port}`
    try {
      for (const config of CONFIGS) {
        if (mode !== 'after' && !CONTROL_CONFIGS.includes(config.name)) continue
        const context = await browser.newContext({
          viewport: config.viewport, isMobile: config.mobile, hasTouch: config.mobile, deviceScaleFactor: config.mobile ? 2 : 1,
        })
        await context.route(url => url.hostname !== '127.0.0.1', route => route.abort())
        const page = await context.newPage()
        const q = new URLSearchParams({
          api: API, token: TOKEN, tenant: '77', dir: config.dir, theme: config.theme, ts: timestampClassAt(revision),
        })
        await page.goto(`${app}/scripts/fixtures/conversation-bubble-layout/harness.html?${q}`)
        await page.waitForFunction(() => document.documentElement.dataset.harnessReady === '1')
        const events: HeaderEvents = { recovered: false, pixels: {} }
        headerEvents[mode][config.name] = events
        if (mode === 'after') {
          // Delayed source: the loading placeholder, measured before the bytes arrive.
          await page.locator('[data-case="tpl-delayed"]').scrollIntoViewIfNeeded()
          await page.waitForTimeout(150)
          events.loading = (await headerOf(page, 'tpl-delayed')) ?? undefined
          await page.locator('[data-case="tpl-delayed"]').screenshot({ path: join(EVIDENCE, `${mode}-${config.name}-tpl-delayed-loading.png`) })
        }
        // Images are loading="lazy": scroll the conversation like a reader so every one loads.
        for (const row of await page.locator('[data-case]').all()) {
          await row.scrollIntoViewIfNeeded()
          await page.waitForTimeout(80)
        }
        await page.evaluate(() => window.scrollTo(0, 0))
        await page.waitForFunction(ids => ids.every(id => {
          const img = document.querySelector(`[data-case="${id}"] img[src^="blob:"]`) as HTMLImageElement | null
          return img && img.complete && img.naturalWidth > 0
        }) && Array.from(document.querySelectorAll('img')).every(i => i.complete || i.hidden), INBOUND_IMAGE_CASES, { timeout: 30_000 })
          .catch(async err => {
            const state = await page.evaluate(() => Array.from(document.querySelectorAll('[data-case]')).map(row => ({
              id: row.getAttribute('data-case'),
              imgs: Array.from(row.querySelectorAll('img')).map(i => ({ src: i.src.slice(0, 40), complete: i.complete, w: i.naturalWidth })),
              text: (row.textContent || '').slice(0, 80),
            })))
            throw new Error(`${mode}/${config.name}: media did not load: ${JSON.stringify(state, null, 1)}\n${err}`)
          })
        await page.waitForTimeout(300)
        // Source recovery: a failed header source is replaced by a valid one.
        if (mode === 'after' || mode === 'parent') {
          events.recoveryInitial = (await headerOf(page, 'tpl-recovery')) ?? undefined
          await page.locator('[data-case="tpl-recovery"]').screenshot({ path: join(EVIDENCE, `${mode}-${config.name}-tpl-recovery-failed.png`) })
          await page.evaluate(() => window.__recoverTemplateHeader?.())
          if (mode === 'after') {
            events.recovered = await page.waitForFunction(() => document.querySelector('[data-case="tpl-recovery"] [data-template-header-image="loaded"]'),
              null, { timeout: 15_000 }).then(() => true, () => false)
          } else {
            await page.waitForTimeout(1500)
            events.recovered = await page.evaluate(() => {
              const img = document.querySelector('[data-case="tpl-recovery"] img') as HTMLImageElement | null
              return !!img && img.complete && img.naturalWidth > 0 && getComputedStyle(img).display !== 'none'
            })
          }
          events.recoveredSrc = await page.evaluate(() => document.querySelector('[data-case="tpl-recovery"] img')?.getAttribute('src') ?? null)
        }
        await page.waitForTimeout(200)
        const measured = await measure(page)
        results[mode][config.name] = measured
        // On-screen pixels of the 16:9 artwork: edge band, corners, white margin, content.
        if (mode === 'after' || mode === 'parent') {
          for (const id of ['tpl-16x9', 'lifecycle-image', 'tpl-recovery']) {
            const img = page.locator(`[data-case="${id}"] [data-template-header-image] img, [data-case="${id}"] img`).first()
            if (!(await img.count()) || !(await img.isVisible())) continue
            const shot = decodePng(await img.screenshot())
            const at = (fx: number, fy: number) => shot.px(Math.min(shot.w - 1, Math.max(0, Math.round(fx * (shot.w - 1)))), Math.min(shot.h - 1, Math.max(0, Math.round(fy * (shot.h - 1)))))
            // 2 CSS px in from the screenshot edge: clear of the outward rounding of
            // fractional boxes, yet inside the zone any >= 6px corner rounding clips.
            const k = 2 * (config.mobile ? 2 : 1)
            const samples = {
              'edge-left': shot.px(k, Math.round(shot.h / 2)), 'edge-right': shot.px(shot.w - 1 - k, Math.round(shot.h / 2)),
              'edge-top': shot.px(Math.round(shot.w / 2), k), 'edge-bottom': shot.px(Math.round(shot.w / 2), shot.h - 1 - k),
              'corner-tl': shot.px(k, k), 'corner-tr': shot.px(shot.w - 1 - k, k), 'corner-bl': shot.px(k, shot.h - 1 - k), 'corner-br': shot.px(shot.w - 1 - k, shot.h - 1 - k),
              'white-left': at((ART.edge + ART.marginX / 2) / ART.w, 0.5), 'white-right': at(1 - (ART.edge + ART.marginX / 2) / ART.w, 0.5),
              'white-top': at(0.5, (ART.edge + ART.marginY / 2) / ART.h), 'white-bottom': at(0.5, 1 - (ART.edge + ART.marginY / 2) / ART.h),
              content: at(0.5, 0.5),
            }
            // Width of the white margin band on the middle row, left side.
            let x = 0
            const row = Math.round(shot.h / 2)
            while (x < shot.w && !near(shot.px(x, row), COLORS.white, 24)) x += 1
            let band = 0
            while (x + band < shot.w && near(shot.px(x + band, row), COLORS.white, 24)) band += 1
            events.pixels[id] = { size: [shot.w, shot.h], samples, whiteBandDevicePx: band, expectedWhiteBandDevicePx: (ART.marginX / ART.w) * shot.w }
          }
        }
        await page.screenshot({ path: join(EVIDENCE, `${mode}-${config.name}-full.png`), fullPage: true })
        for (const id of SCREENSHOT_CASES) {
          await page.locator(`[data-case="${id}"]`).screenshot({ path: join(EVIDENCE, `${mode}-${config.name}-${id}.png`) })
        }
        await context.close()
        step(`${mode}/${config.name}: measured ${Object.keys(measured.cases).length} bubbles`)
      }
    } finally {
      await vite.close()
    }
  }
} finally {
  await browser.close()
  api.close()
}
writeFileSync(join(EVIDENCE, 'measurements.json'), JSON.stringify({ results, headerEvents }, null, 2))

// ── 5. Assertions ─────────────────────────────────────────────────────
const fmt = (n: number) => Math.round(n * 10) / 10
const report: string[] = []

// Positive control: the base commit reproduces the reported blank frame.
{
  const before = results.before['desktop-rtl'].cases['in-portrait-long'].image!
  report.push(`before desktop-rtl in-portrait-long: natural ${before.natural.join('x')}, image ${before.rendered.map(fmt).join('x')}, frame ${before.frame.map(fmt).join('x')}, blank ${fmt(before.blankWidth)}px`)
  check(before.blankWidth > 150, `positive control: base commit should reproduce the blank frame (blank ${before.blankWidth}px)`)
}

for (const config of CONFIGS) {
  const { cases, pageOverflow } = results.after[config.name]
  check(pageOverflow <= 1, `${config.name}: horizontal page overflow ${pageOverflow}px`)
  for (const id of INBOUND_IMAGE_CASES) {
    const c = cases[id]
    const im = c.image
    if (!im) { failures.push(`${config.name}/${id}: no decoded image`); continue }
    const [nw, nh] = im.natural
    const [rw, rh] = im.rendered
    const aspectError = Math.abs(rw / rh - nw / nh) / (nw / nh)
    const maxW = Math.min(IMAGE_CAP, im.parentContentWidth - (im.frame[0] - im.frameInner[0]))
    const expectedScale = Math.min(1, IMAGE_CAP / nh, maxW / nw)
    report.push(`after  ${config.name.padEnd(16)} ${id.padEnd(22)} natural ${nw}x${nh} -> image ${fmt(rw)}x${fmt(rh)} frame ${fmt(im.frame[0])}x${fmt(im.frame[1])} blank ${fmt(im.blankWidth)}px start-gap ${fmt(im.startGap)}px bubble-content ${fmt(im.wrapperWidth)}px`)
    check(aspectError < 0.01, `${config.name}/${id}: aspect ratio changed (${fmt(rw)}x${fmt(rh)} vs ${nw}x${nh})`)
    check(Math.abs(rw - nw * expectedScale) <= 1 && Math.abs(rh - nh * expectedScale) <= 1,
      `${config.name}/${id}: expected ${fmt(nw * expectedScale)}x${fmt(nh * expectedScale)}, got ${fmt(rw)}x${fmt(rh)}`)
    check(Math.abs(im.frameInner[0] - rw) <= 1 && Math.abs(im.frameInner[1] - rh) <= 1,
      `${config.name}/${id}: frame ${fmt(im.frameInner[0])}x${fmt(im.frameInner[1])} does not hug image ${fmt(rw)}x${fmt(rh)}`)
    check(Math.abs(im.startGap) <= 1, `${config.name}/${id}: frame not at the inline-start edge (gap ${fmt(im.startGap)}px)`)
    check(im.frame[0] <= im.parentContentWidth + 0.5, `${config.name}/${id}: frame wider than the bubble`)
    for (const w of im.textBlockWidths) {
      check(Math.abs(w - im.wrapperWidth) <= 1, `${config.name}/${id}: caption/description block ${fmt(w)}px is not full bubble width ${fmt(im.wrapperWidth)}px`)
    }
    check(c.card.scrollOverflow <= 0, `${config.name}/${id}: card content overflows horizontally`)
  }
  if (config.name === 'desktop-rtl' || config.name === 'mobile-rtl') {
    const long = cases['in-portrait-long'].image!
    check(long.textBlockWidths.length >= 2 && Math.min(...long.textBlockWidths) > long.frame[0],
      `${config.name}: long caption/description should stay wider than the portrait frame`)
  }

  // Template bubbles use the ordinary outbound bubble colours.
  const plain = cases['out-plain']
  for (const id of TEMPLATE_CASES) {
    const c = cases[id]
    check(c.card.bg === plain.card.bg && c.card.color === plain.card.color && c.card.border === plain.card.border,
      `${config.name}/${id}: bubble ${c.card.bg}/${c.card.color}/${c.card.border} differs from ordinary outbound ${plain.card.bg}/${plain.card.color}/${plain.card.border}`)
  }
  // Every visible text in ordinary outbound and template bubbles: WCAG AA.
  for (const id of ENFORCED_CASES) {
    const parts = new Set(cases[id].texts.map(t => partOf(t.text, t.role)))
    check(parts.has('body') || id === 'out-template-broken-image', `${config.name}/${id}: no body text measured`)
    for (const t of cases[id].texts) {
      check(t.ratio >= AA, `${config.name}/${id}: ${partOf(t.text, t.role)} "${t.text}" ${t.ratio}:1 < ${AA}:1 (${t.color} on ${t.background})`)
    }
  }
  for (const [id, part] of [['out-template', 'template-label'], ['out-template', 'template-name'], ['out-template', 'footer'],
    ['out-template', 'button'], ['out-lifecycle', 'order-details'], ['out-template-broken-image', 'media-placeholder']]) {
    check(cases[id].texts.some(t => partOf(t.text, t.role) === part), `${config.name}/${id}: ${part} was not measured`)
  }
  // Message timestamps (every row; the class is shared by inbound and outbound).
  for (const [id, c] of Object.entries(cases)) {
    const stamps = c.extras.filter(e => e.role === 'timestamp')
    check(stamps.length === 1, `${config.name}/${id}: timestamp not measured`)
    for (const e of stamps) check(e.ratio >= AA, `${config.name}/${id}: timestamp ${e.ratio}:1 < ${AA}:1 (${e.color} on ${e.background})`)
  }
  const failed = cases['out-template-failed']
  check(/rgb\(254, 202, 202\)/.test(failed.stackShadow), `${config.name}: failed ring (ring-red-200) missing`)
  check(failed.badgeBg === 'rgb(239, 68, 68)', `${config.name}: not-sent badge colour changed (${failed.badgeBg})`)
}

// Positive control: the previous commit's bubbles must fail the AA text check.
for (const config of CONTROL_CONFIGS) {
  const { cases } = results.previous[config]
  const low = ENFORCED_CASES.flatMap(id => cases[id].texts.map(t => ({ id, ...t }))).filter(t => t.ratio < AA)
  const lowStamps = Object.values(cases).flatMap(c => c.extras).filter(e => e.role === 'timestamp' && e.ratio < AA)
  check(low.length > 0 && lowStamps.length > 0,
    `positive control: ${PREVIOUS.slice(0, 7)} should fail the ${AA}:1 check on ${config} (low texts ${low.length}, low timestamps ${lowStamps.length})`)
}

// Inbound bubbles, other outbound kinds and status badges: unchanged vs the base commit.
for (const config of CONTROL_CONFIGS) {
  const before = results.before[config].cases
  const after = results.after[config].cases
  for (const id of UNCHANGED_CASES) {
    check(JSON.stringify([before[id].card.bg, before[id].card.color, before[id].card.border]) === JSON.stringify([after[id].card.bg, after[id].card.color, after[id].card.border]),
      `${config}/${id}: bubble colours changed (${before[id].card.bg} -> ${after[id].card.bg})`)
    check(JSON.stringify(before[id].texts.map(t => [t.text, t.color])) === JSON.stringify(after[id].texts.map(t => [t.text, t.color])),
      `${config}/${id}: text colours changed`)
  }
  for (const id of ['out-template', 'out-lifecycle', 'out-template-failed']) {
    const pick = (c: CaseMeasure) => JSON.stringify(c.extras.filter(e => e.role !== 'timestamp').map(e => [e.role, e.color, e.background]))
    check(pick(before[id]) === pick(after[id]), `${config}/${id}: status/event badge colours changed`)
    check(before[id].eventBadgeBg === after[id].eventBadgeBg && before[id].badgeBg === after[id].badgeBg, `${config}/${id}: badge background changed`)
  }
}

// ── Template header images ────────────────────────────────────────────
const headerLines: string[] = []
for (const config of CONFIGS) {
  const { cases } = results.after[config.name]
  const ev = headerEvents.after[config.name]
  for (const id of TEMPLATE_HEADER_CASES) {
    const h = cases[id].header
    if (!h || !h.img || !h.natural) { failures.push(`${config.name}/${id}: template header image not loaded (${h?.status})`); continue }
    const [nw, nh] = h.natural
    const [, , fw, fh] = h.frame
    const [, , iw, ih] = h.img
    const scale = Math.min(1, h.contentWidth / nw, HEADER_CAP / nh)
    headerLines.push(`${config.name.padEnd(17)} ${id.padEnd(16)} natural ${nw}x${nh} -> image ${fmt(iw)}x${fmt(ih)} frame ${fmt(fw)}x${fmt(fh)} css-space ${fmt(fw * fh - iw * ih)}px² start-gap ${fmt(h.startGap)} body ${fmt(h.bodyWidth ?? 0)}/${fmt(h.contentWidth)}`)
    check(h.status === 'loaded', `${config.name}/${id}: header status ${h.status}`)
    check(Math.abs(iw - nw * scale) <= 1 && Math.abs(ih - nh * scale) <= 1,
      `${config.name}/${id}: header ${fmt(iw)}x${fmt(ih)}, expected ${fmt(nw * scale)}x${fmt(nh * scale)}`)
    check(Math.abs(iw / ih - nw / nh) / (nw / nh) < 0.01, `${config.name}/${id}: header aspect ratio changed`)
    check(Math.abs(fw - iw) <= 1 && Math.abs(fh - ih) <= 1, `${config.name}/${id}: frame ${fmt(fw)}x${fmt(fh)} does not hug image ${fmt(iw)}x${fmt(ih)}`)
    check(Math.abs(h.startGap) <= 1, `${config.name}/${id}: header not at the inline-start edge (${fmt(h.startGap)}px)`)
    check(h.bodyWidth === null || Math.abs(h.bodyWidth - h.contentWidth) <= 1, `${config.name}/${id}: body width tied to the header`)
    check(h.footerWidth === null || Math.abs(h.footerWidth - h.contentWidth) <= 1, `${config.name}/${id}: footer width tied to the header`)
    check(h.actionsWidth === null || Math.abs(h.actionsWidth - h.cardInnerWidth) <= 1, `${config.name}/${id}: buttons width tied to the header`)
  }
  check(Object.keys(ev.pixels).length === 3, `${config.name}: artwork pixels not sampled (${Object.keys(ev.pixels).join(',')})`)
  for (const [id, px] of Object.entries(ev.pixels)) {
    for (const [name, c] of Object.entries(px.samples)) {
      const expected = name.startsWith('edge') ? COLORS.edge : name.startsWith('corner') ? COLORS.corner : name.startsWith('white') ? COLORS.white : COLORS.content
      check(near(c, expected), `${config.name}/${id}: ${name} pixel ${JSON.stringify(c)} is not the artwork's ${JSON.stringify(expected)} (cropped, trimmed or letterboxed)`)
    }
    check(Math.abs(px.whiteBandDevicePx - px.expectedWhiteBandDevicePx) <= 3,
      `${config.name}/${id}: white artwork margin ${px.whiteBandDevicePx}px on screen, expected ${fmt(px.expectedWhiteBandDevicePx)}px`)
  }
  const l = ev.loading
  check(l && l.status === 'loading' && Math.abs(l.frame[2] - l.contentWidth) <= 1 && Math.abs(l.frame[3] - (l.frame[2] * 9) / 16) <= 1 && !l.frameText,
    `${config.name}: delayed header loading placeholder unexpected (${JSON.stringify(l)})`)
  check(ev.recoveryInitial?.status === 'error' && ev.recoveryInitial.frameText === 'تعذر عرض الوسائط',
    `${config.name}: failed header should show the media-unavailable state (${JSON.stringify(ev.recoveryInitial)})`)
  check(ev.recovered && /recovered=1/.test(ev.recoveredSrc ?? ''), `${config.name}: header did not recover after a source change`)
}
// Positive control: the parent commit letterboxes the 16:9 header in a 4:3 frame.
const parentLines: string[] = []
for (const config of CONTROL_CONFIGS) {
  const p16 = results.parent[config].cases['tpl-16x9'].safeImage
  if (!p16) { failures.push(`parent/${config}: tpl-16x9 frame not measured`); continue }
  const contentH = (p16.frame[2] * ART.h) / ART.w
  const extra = p16.frame[3] - contentH
  parentLines.push(`parent ${config.padEnd(17)} tpl-16x9 frame ${fmt(p16.frame[2])}x${fmt(p16.frame[3])} img-box ${fmt(p16.img[2])}x${fmt(p16.img[3])} image-content ${fmt(p16.frame[2])}x${fmt(contentH)} unnecessary-vertical ${fmt(extra)}px; recovery ${headerEvents.parent[config].recovered ? 'ok' : 'stays on placeholder'}`)
  check(extra > 30, `positive control: parent should letterbox the 16:9 header (extra ${fmt(extra)}px)`)
  // Other SafeImage consumers: identical frames to the parent commit.
  for (const id of SAFE_IMAGE_CASES) {
    const before = results.parent[config].cases[id].safeImage
    const after = results.after[config].cases[id].safeImage
    check(before && after && Math.abs(before.frame[2] - after.frame[2]) <= 0.5 && Math.abs(before.frame[3] - after.frame[3]) <= 0.5
      && Math.abs(before.img[2] - after.img[2]) <= 0.5 && Math.abs(before.img[3] - after.img[3]) <= 0.5,
      `${config}/${id}: SafeImage frame changed (${JSON.stringify(before)} -> ${JSON.stringify(after)})`)
  }
}
report.push('', 'template header images (after):', ...headerLines, '', 'template header positive control (parent):', ...parentLines)

// ── 6. Contrast tables ────────────────────────────────────────────────
const minBy = (items: Array<{ ratio: number }>) => (items.length ? Math.min(...items.map(i => i.ratio)) : NaN)
const COLUMNS: Array<[string, (c: Record<string, CaseMeasure>) => number]> = [
  ['plain body', c => minBy(c['out-plain'].texts)],
  ['tpl body', c => minBy(c['out-template'].texts.filter(t => partOf(t.text, t.role) === 'body'))],
  ['tpl label', c => minBy(c['out-template'].texts.filter(t => partOf(t.text, t.role) === 'template-label'))],
  ['tpl name', c => minBy(c['out-template'].texts.filter(t => partOf(t.text, t.role) === 'template-name'))],
  ['footer', c => minBy(c['out-template'].texts.filter(t => partOf(t.text, t.role) === 'footer'))],
  ['order', c => minBy(c['out-lifecycle'].texts.filter(t => partOf(t.text, t.role) === 'order-details'))],
  ['lifecycle', c => minBy(c['out-lifecycle'].texts.filter(t => t.role === 'text'))],
  ['placeholder', c => minBy(c['out-template-broken-image'].texts.filter(t => partOf(t.text, t.role) === 'media-placeholder'))],
  ['buttons', c => minBy(TEMPLATE_CASES.flatMap(id => c[id].texts.filter(t => t.role === 'button')))],
  ['timestamp', c => minBy(Object.values(c).flatMap(x => x.extras.filter(e => e.role === 'timestamp')))],
]
const tableLines: string[] = []
tableLines.push(`${'mode/config'.padEnd(26)}${COLUMNS.map(([h]) => h.padStart(12)).join('')}`)
for (const mode of ['previous', 'after'] as Mode[]) {
  for (const config of Object.keys(results[mode])) {
    const c = results[mode][config].cases
    tableLines.push(`${`${mode}/${config}`.padEnd(26)}${COLUMNS.map(([, f]) => f(c).toFixed(2).padStart(12)).join('')}`)
  }
}
const statusLines: string[] = []
for (const config of ['desktop-rtl', 'desktop-rtl-dark']) {
  const c = results.after[config].cases
  for (const id of ['out-template', 'out-lifecycle', 'out-template-failed']) {
    for (const e of c[id].extras.filter(x => x.role !== 'timestamp')) {
      statusLines.push(`${config.padEnd(17)} ${id.padEnd(20)} ${e.role.padEnd(15)} ${e.ratio.toFixed(2).padStart(5)}:1  "${e.text}" ${e.color} on ${e.background}${e.ratio < AA ? '  << pre-existing, below AA (unchanged)' : ''}`)
    }
  }
  for (const id of [...UNCHANGED_CASES, 'in-square']) {
    for (const t of c[id].texts.filter(x => x.ratio < AA)) {
      statusLines.push(`${config.padEnd(17)} ${id.padEnd(20)} ${t.role.padEnd(15)} ${t.ratio.toFixed(2).padStart(5)}:1  "${t.text}" ${t.color} on ${t.background}  << pre-existing, below AA (unchanged)`)
    }
  }
}
writeFileSync(join(EVIDENCE, 'contrast-table.txt'), `${tableLines.join('\n')}\n\nnot enforced (status badges / unchanged bubbles):\n${statusLines.join('\n')}\n`)
report.push('', 'minimum contrast per element (WCAG ratio, enforced >= 4.5 in "after"):', ...tableLines,
  '', 'status badges and unchanged bubbles (reported, not enforced):', ...statusLines)

console.log('\nmeasurements:')
for (const line of report) console.log(`  ${line}`)
console.log(`\nevidence written to ${EVIDENCE}`)
if (failures.length) {
  console.error(`\nconversation bubble layout check: ${failures.length} failure(s)`)
  for (const f of failures) console.error(`  ✗ ${f}`)
  process.exit(1)
}
console.log('\nconversation bubble layout check: ok')
