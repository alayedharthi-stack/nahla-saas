// ── Shared API client ─────────────────────────────────────────────────────────
// All dashboard API modules import apiCall from here so the auth token
// is automatically attached to every request.

import { getToken, getTenantId, logout, getApiBase } from '../auth'

const DEFAULT_FETCH_TIMEOUT_MS = 25_000

/** Optional `timeoutMs` is stripped before `fetch` (not a standard RequestInit field). */
export type ApiCallOptions = RequestInit & { timeoutMs?: number }

/** Combines timeout + caller AbortSignal (either abort aborts the request). */
function combinedAbortSignals(timeoutMs: number, userSignal?: AbortSignal | null): AbortSignal {
  const timeoutSig =
    typeof AbortSignal.timeout === 'function'
      ? AbortSignal.timeout(timeoutMs)
      : (() => {
          const c = new AbortController()
          setTimeout(() => c.abort(), timeoutMs)
          return c.signal
        })()
  if (!userSignal) return timeoutSig
  if (typeof AbortSignal.any === 'function') {
    return AbortSignal.any([timeoutSig, userSignal])
  }
  const merged = new AbortController()
  const forward = () => {
    try {
      merged.abort()
    } catch {
      /* noop */
    }
  }
  if (timeoutSig.aborted || userSignal.aborted) {
    forward()
    return merged.signal
  }
  timeoutSig.addEventListener('abort', forward, { once: true })
  userSignal.addEventListener('abort', forward, { once: true })
  return merged.signal
}

/**
 * Coerces to string via toString/valueOf so legacy `${API_BASE}/path` keeps working
 * while always resolving the current getApiBase() (matches auth + localStorage).
 */
export const API_BASE = {
  toString: () => getApiBase(),
  valueOf: () => getApiBase(),
} as unknown as string

// Error codes that mean the JWT itself is invalid — caller MUST re-login.
// These are rare and unambiguous; safe to act on regardless of which endpoint
// surfaced them.
const HARD_LOGOUT_CODES = new Set([
  'token_expired',
  'invalid_token',
])

// Soft codes — only force logout when the FAILING URL is an auth/session route.
// A 401 on a secondary endpoint (support-access, access-requests, notifications,
// /whatsapp/* status, etc.) was previously dragging the whole UI to /login and
// erasing every page mid-session, even when the user's token was perfectly valid.
const SOFT_LOGOUT_CODES = new Set([
  'missing_token',
  'no_tenant_claim',
])

// Endpoints whose 401 conclusively means "the session is gone".
const AUTH_PATH_RE = /\/auth\/(me|session|refresh|verify|whoami|login)\b/i

function classifyNetworkError(error: unknown, timeoutMsForAbortMessage: number = DEFAULT_FETCH_TIMEOUT_MS): string {
  const msg = error instanceof Error ? error.message : String(error ?? '')
  const lowered = msg.toLowerCase()

  if (lowered.includes('failed to fetch') || lowered.includes('load failed') || lowered.includes('networkerror')) {
    return 'تعذر الوصول إلى الخادم. قد يكون السبب CORS أو انقطاع الشبكة أو خطأ مؤقت في API.'
  }
  if (
    lowered.includes('abort') ||
    lowered.includes('signal timed out') ||
    (error instanceof DOMException && error.name === 'AbortError')
  ) {
    return `انتهت مهلة الطلب (${timeoutMsForAbortMessage / 1000}s). تحقق من الخادم أو الشبكة.`
  }
  return msg || 'حدث خطأ غير متوقع أثناء الاتصال بالخادم.'
}

export async function apiCall<T>(path: string, options?: ApiCallOptions): Promise<T> {
  const token    = getToken()
  const tenantId = getTenantId()
  const base     = getApiBase()
  const url      = `${base}${path}`

  const timeoutApplied = options?.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS
  const signal = combinedAbortSignals(timeoutApplied, options?.signal)

  let res: Response
  try {
    const { signal: _omit, timeoutMs: __omitT, headers: optHeaders, ...rest } = options ?? {}
    res = await fetch(url, {
      cache: 'no-store',
      mode: 'cors',
      signal,
      headers: {
        'Content-Type': 'application/json',
        ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(optHeaders ?? {}),
      },
      ...rest,
    })
  } catch (error) {
    throw new Error(classifyNetworkError(error, timeoutApplied))
  }

  // ── Structured-error helper ────────────────────────────────────────────────
  // Backends may return either:
  //   { detail: "حصل خطأ نصي" }
  //   { detail: { code: "subscription_inactive", message: "..."} }
  // Surface as a normal Error whose `.message` is the human Arabic text and
  // whose `.code` (when present) is the machine-readable reason.
  const buildApiError = (body: any, fallback: string): Error & { code?: string; status?: number; validation?: unknown } => {
    let msg  = fallback
    let code: string | undefined
    let validation: unknown
    const d = body?.detail
    if (typeof d === 'string') {
      msg = d
    } else if (Array.isArray(d)) {
      // FastAPI validation errors arrive as:
      //   detail: [{ loc:[...], msg:"...", type:"..." }, ...]
      // Without this branch, all 422s collapsed into the generic
      // "API error 422" message — which is exactly what users
      // saw when a campaign launch failed validation.
      validation = d
      const parts: string[] = []
      for (const item of d) {
        if (item && typeof item === 'object') {
          const loc = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : ''
          const m = typeof item.msg === 'string' ? item.msg : ''
          if (loc && m) parts.push(`${loc}: ${m}`)
          else if (m) parts.push(m)
        }
      }
      if (parts.length > 0) {
        msg = `بيانات الطلب غير صالحة — ${parts.join('؛ ')}`
      }
    } else if (d && typeof d === 'object') {
      if (typeof d.message === 'string' && d.message.trim()) msg = d.message
      else if (typeof d.detail === 'string') msg = d.detail
      if (typeof d.code === 'string') code = d.code
    }
    const err = new Error(msg) as Error & { code?: string; status?: number; validation?: unknown }
    err.code       = code
    err.status     = res.status
    err.validation = validation
    return err
  }

  // 401 — narrowly scoped logout policy.
  //
  // We previously logged the user out on ANY 401 that carried one of a handful
  // of error codes. That dragged the whole dashboard back to /login whenever
  // a secondary endpoint (e.g. /merchant/support-access, /merchant/notifications,
  // /whatsapp/connection/...) refused the request for non-session reasons —
  // the user perceived it as "the platform keeps logging me out".
  //
  // New policy:
  //   * `token_expired` / `invalid_token` are unambiguous — log out.
  //   * `missing_token` / `no_tenant_claim` are *also* used by the JWT
  //     middleware, but only mean "session lost" when the FAILING request
  //     is an auth-class route (/auth/me, /auth/session, /auth/refresh, ...).
  //   * Anything else surfaces as a normal API error so the affected page
  //     can show a banner without nuking the whole app.
  if (res.status === 401) {
    let body: any = null
    try { body = await res.clone().json() } catch { /* ignore */ }
    const code = body?.code ?? body?.detail?.code ?? ''

    const isHardLogout = HARD_LOGOUT_CODES.has(code)
    const isSoftLogout = SOFT_LOGOUT_CODES.has(code) && AUTH_PATH_RE.test(path)

    if (isHardLogout || isSoftLogout) {
      // eslint-disable-next-line no-console
      console.warn('[auth] session invalid / refresh — forcing logout', { code, url, hard: isHardLogout })
      logout()
      window.location.href = '/login'
      throw new Error('انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً')
    }

    // 401 from a non-auth endpoint with a soft code or no code: keep the
    // session intact, surface as a normal error for the caller to render.
    // eslint-disable-next-line no-console
    console.warn('[auth] 401 on secondary endpoint — keeping session', { code, url })
    throw buildApiError(body, 'غير مصرح')
  }

  if (!res.ok) {
    let body: any = null
    try { body = await res.json() } catch { /* ignore */ }
    throw buildApiError(body, `API error ${res.status}`)
  }

  return res.json() as Promise<T>
}
