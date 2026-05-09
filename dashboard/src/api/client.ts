// ── Shared API client ─────────────────────────────────────────────────────────
// All dashboard API modules import apiCall from here so the auth token
// is automatically attached to every request.

import { getToken, getTenantId, logout, getApiBase } from '../auth'

const DEFAULT_FETCH_TIMEOUT_MS = 25_000

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

// Error codes that mean the session is truly invalid and the user must re-login.
// Do NOT logout on every 401 — some endpoints may return 401 for reasons unrelated
// to the session (e.g. support-access checks, impersonation guards).
const SESSION_EXPIRED_CODES = new Set([
  'missing_token',
  'invalid_token',
  'no_tenant_claim',
])

function classifyNetworkError(error: unknown): string {
  const msg = error instanceof Error ? error.message : String(error ?? '')
  const lowered = msg.toLowerCase()

  if (lowered.includes('failed to fetch') || lowered.includes('load failed') || lowered.includes('networkerror')) {
    return 'تعذر الوصول إلى الخادم. قد يكون السبب CORS أو انقطاع الشبكة أو خطأ مؤقت في API.'
  }
  if (lowered.includes('abort') || (error instanceof DOMException && error.name === 'AbortError')) {
    return `انتهت مهلة الطلب (${DEFAULT_FETCH_TIMEOUT_MS / 1000}s). تحقق من الخادم أو الشبكة.`
  }
  return msg || 'حدث خطأ غير متوقع أثناء الاتصال بالخادم.'
}

export async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const token    = getToken()
  const tenantId = getTenantId()
  const base     = getApiBase()
  const url      = `${base}${path}`

  const signal = combinedAbortSignals(DEFAULT_FETCH_TIMEOUT_MS, options?.signal)

  let res: Response
  try {
    const { signal: _omit, headers: optHeaders, ...rest } = options ?? {}
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
    throw new Error(classifyNetworkError(error))
  }

  // ── Structured-error helper ────────────────────────────────────────────────
  // Backends may return either:
  //   { detail: "حصل خطأ نصي" }
  //   { detail: { code: "subscription_inactive", message: "..."} }
  // Surface as a normal Error whose `.message` is the human Arabic text and
  // whose `.code` (when present) is the machine-readable reason.
  const buildApiError = (body: any, fallback: string): Error & { code?: string; status?: number } => {
    let msg  = fallback
    let code: string | undefined
    const d = body?.detail
    if (typeof d === 'string') {
      msg = d
    } else if (d && typeof d === 'object') {
      if (typeof d.message === 'string' && d.message.trim()) msg = d.message
      else if (typeof d.detail === 'string') msg = d.detail
      if (typeof d.code === 'string') code = d.code
    }
    const err = new Error(msg) as Error & { code?: string; status?: number }
    err.code   = code
    err.status = res.status
    return err
  }

  // 401 — only logout when the backend signals the session/token itself is invalid.
  // A 401 for other reasons (e.g. permission checks) should surface as a normal error.
  if (res.status === 401) {
    let body: any = null
    try { body = await res.clone().json() } catch { /* ignore */ }
    const code = body?.code ?? body?.detail?.code ?? ''

    if (SESSION_EXPIRED_CODES.has(code)) {
      // eslint-disable-next-line no-console
      console.warn('[auth] session invalid / refresh — forcing logout', { code, url })
      logout()
      window.location.href = '/login'
      throw new Error('انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً')
    }
    throw buildApiError(body, 'غير مصرح')
  }

  if (!res.ok) {
    let body: any = null
    try { body = await res.json() } catch { /* ignore */ }
    throw buildApiError(body, `API error ${res.status}`)
  }

  return res.json() as Promise<T>
}
