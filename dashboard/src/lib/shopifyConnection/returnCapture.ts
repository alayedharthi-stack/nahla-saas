/**
 * Shopify connection return: capture and scrub the one-time completion data.
 *
 * The backend callback (``GET /merchant/integrations/shopify/callback``) binds
 * nothing. It redirects the browser to the fixed dashboard path
 * ``/integrations/shopify/complete`` with exactly one fragment value:
 *
 *   #shopify_handle=<opaque, 5-minute, single-use completion handle>
 *   #shopify_connection=<fixed result code>      (callback refusals only)
 *
 * The handle is only usable by the authenticated ``POST /complete`` of the
 * same tenant, user and JWT session that started the authorization, but it
 * is still a credential-like value: it must never reach the router, auth
 * bootstrap, analytics, Sentry, console logs, storage or a referrer.
 *
 * Order of defence:
 *   1. ``index.html`` runs a route-scoped inline script before any other
 *      script or subresource. On every path form React Router would render
 *      as the completion page (case-insensitive, percent-decoded, repeated or
 *      trailing slashes) it sends no referrer, moves the raw fragment into a
 *      closure behind a one-shot getter (``EARLY_RETURN_GETTER``) and replaces
 *      the URL with the canonical bare path. On any other path a stray Shopify
 *      return marker is dropped unread. If the clean URL cannot be verified
 *      it keeps nothing, flags ``EARLY_BLOCKED_FLAG`` and reloads the bare path.
 *   2. ``bootCapture.ts`` is the first import of ``main.tsx``: it repeats the
 *      same checks (the inline script may not have run), keeps the parsed
 *      result in module memory only, and halts the boot with fixed text when
 *      the URL could not be cleaned.
 *   3. The completion page takes it once (``takeShopifyReturn``). Nothing is
 *      persisted: a reload or a new tab finds nothing and must restart.
 *
 * Keep the inline script's path normalisation and marker in sync with
 * ``normalizeRoutePath`` and ``RETURN_MARKER_RE`` (checked by
 * ``scripts/check-shopify-connection-ui.mts``). No imports.
 */

export const SHOPIFY_COMPLETE_PATH = '/integrations/shopify/complete'
/** Name of the one-shot getter defined by the inline script in index.html. */
export const EARLY_RETURN_GETTER = '__nahlaShopifyReturn'
/** Set by the inline script when it could not verify a clean URL. */
export const EARLY_BLOCKED_FLAG = '__nahlaShopifyReturnBlocked'
/** Backend ``COMPLETION_TTL_SECONDS`` (300 s); a held handle is dropped after it. */
export const COMPLETION_TTL_MS = 300_000

// Exact raw forms the backend emits (no decoding: an encoded or re-cased key
// is not something the backend produces and is refused, though still scrubbed).
const HANDLE_FRAGMENT_RE = /^shopify_handle=([A-Za-z0-9_-]{32,128})$/
const RESULT_FRAGMENT_RE = /^shopify_connection=([^&=#]{0,128})$/
const CODE_RE = /^[a-z_]{1,64}$/
const MAX_FRAGMENT = 512
/** A Shopify return key in any case or percent-encoding of ``_``. */
export const RETURN_MARKER_RE = /shopify(?:_|%5f|%255f)(?:handle|connection)/i

export type ParsedReturn =
  | { kind: 'none' }
  | { kind: 'invalid' }
  | { kind: 'handle'; handle: string }
  /** A fixed callback code. Format-checked only: callers map it to fixed copy. */
  | { kind: 'result'; code: string }

export type TakenReturn =
  | { kind: 'none' }
  | { kind: 'invalid' }
  | { kind: 'expired' }
  | { kind: 'handle'; handle: string; capturedAt: number }
  | { kind: 'result'; code: string }

/** Parse a raw fragment (with or without ``#``). Only the exact backend forms are accepted. */
export function parseShopifyReturnFragment(raw: string): ParsedReturn {
  const text = String(raw || '').replace(/^#/, '')
  if (!text) return { kind: 'none' }
  if (text.length > MAX_FRAGMENT) return { kind: 'invalid' }
  const handle = HANDLE_FRAGMENT_RE.exec(text)
  if (handle) return { kind: 'handle', handle: handle[1] }
  const result = RESULT_FRAGMENT_RE.exec(text)
  if (result) return { kind: 'result', code: CODE_RE.test(result[1]) ? result[1] : 'error' }
  return { kind: 'invalid' }
}

/** Path form as React Router matches it: decoded, case-folded, single slashes, no trailing slash. */
export function normalizeRoutePath(pathname: string): string {
  let value = String(pathname || '')
  try {
    value = decodeURIComponent(value)
  } catch {
    /* malformed escape: compare the raw form */
  }
  return value.toLowerCase().replace(/\/{2,}/g, '/').replace(/\/+$/, '')
}

export function isShopifyCompletePath(pathname: string): boolean {
  return normalizeRoutePath(pathname) === SHOPIFY_COMPLETE_PATH
}

export interface CaptureEnv {
  /** Must be live (``window.location``): it is re-read after scrubbing. */
  location: { origin: string; pathname: string; search: string; hash: string }
  history: { replaceState(data: unknown, unused: string, url?: string | null): void }
  /** The inline script's one-shot getter, when it ran. */
  takeEarly?: () => string
  /** The inline script could not verify a clean URL. */
  earlyBlocked?: boolean
  /** Fail-closed reload of the clean URL (``location.replace``). */
  replaceLocation?: (url: string) => void
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (id: unknown) => void
}

export interface CaptureResult {
  /** The URL still carries (or may carry) return data: the boot must stop. */
  blocked: boolean
}

let held: { parsed: ParsedReturn; capturedAt: number } | null = null
let expiryTimer: unknown = null
let clearTimerFn: ((id: unknown) => void) | null = null

function dropTimer(): void {
  if (expiryTimer !== null && clearTimerFn) {
    try { clearTimerFn(expiryTimer) } catch { /* ignore */ }
  }
  expiryTimer = null
}

function failClosed(env: CaptureEnv, target: string): CaptureResult {
  held = null
  dropTimer()
  try {
    env.replaceLocation?.(target)
  } catch {
    /* the boot is stopped either way */
  }
  return { blocked: true }
}

/**
 * Capture once at boot. On the completion path (any form) the query and
 * fragment are removed and the path is canonicalised; anywhere else only a
 * URL carrying a Shopify return marker is cleaned (to its bare path), unread.
 * Returns ``blocked`` when the cleanup cannot be verified.
 */
export function captureShopifyReturn(env: CaptureEnv, now: number): CaptureResult {
  const loc = env.location
  const onPath = isShopifyCompletePath(loc.pathname)
  const stray = !onPath && (RETURN_MARKER_RE.test(loc.hash) || RETURN_MARKER_RE.test(loc.search))
  // Foreign paths keep their path on an explicit same-origin URL: a bare
  // pathname starting with // would be read as protocol-relative (another host).
  const target = onPath ? SHOPIFY_COMPLETE_PATH : loc.origin + loc.pathname
  let early = ''
  try {
    early = env.takeEarly ? String(env.takeEarly() || '') : ''
  } catch {
    early = ''
  }
  if (env.earlyBlocked) return failClosed(env, target)
  if (!onPath && !stray) return { blocked: false }
  const raw = onPath ? early || loc.hash : ''
  const clean = () => loc.hash === '' && loc.search === '' && (!onPath || loc.pathname === SHOPIFY_COMPLETE_PATH)
  if (!clean()) {
    try {
      env.history.replaceState(null, '', target)
    } catch {
      /* verified below */
    }
  }
  if (!clean()) return failClosed(env, target)
  dropTimer()
  held = null
  if (!onPath) return { blocked: false }
  const parsed = parseShopifyReturnFragment(raw)
  if (parsed.kind === 'none') return { blocked: false }
  held = { parsed, capturedAt: now }
  if (env.setTimer) {
    clearTimerFn = env.clearTimer ?? null
    // Clear transient memory once the backend would refuse the handle anyway.
    expiryTimer = env.setTimer(() => {
      held = null
      expiryTimer = null
    }, COMPLETION_TTL_MS)
  }
  return { blocked: false }
}

/** One-shot: returns the captured value and forgets it. */
export function takeShopifyReturn(now: number): TakenReturn {
  const current = held
  held = null
  dropTimer()
  if (!current) return { kind: 'none' }
  const { parsed, capturedAt } = current
  if (parsed.kind === 'handle') {
    if (now - capturedAt >= COMPLETION_TTL_MS || now < capturedAt) return { kind: 'expired' }
    return { kind: 'handle', handle: parsed.handle, capturedAt }
  }
  return parsed
}

/** Forget anything captured (session change, feature off, cancel, page left). */
export function discardShopifyReturn(): void {
  held = null
  dropTimer()
}

export function hasCapturedShopifyReturn(): boolean {
  return held !== null
}
