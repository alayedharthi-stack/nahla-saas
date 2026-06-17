import { setSentryUser, clearSentryUser } from './lib/sentry'

// Defined locally to avoid circular dependency with api/client.ts.
//
// Resolution order (first wins):
//   1. localStorage["nahla_api_base_override"] — runtime override set
//      by the diagnostics panel on the login page. Lets the operator
//      switch to the Railway-generated domain without a frontend rebuild
//      when the custom domain edge is broken.
//   2. Build-time env (first non-empty):
//        VITE_API_BASE, VITE_API_BASE_URL, VITE_API_URL,
//        NEXT_PUBLIC_API_URL, REACT_APP_API_URL
//   3. Default host — temporarily the Railway service URL while
//      api.nahlah.ai is unreliable; override via env for other environments.
//
// getApiBase() re-reads override + env on every call so api/client.ts and
// auth stay aligned after localStorage changes (still reload after toggling
// override so existing bundles pick up the new host consistently).
const _OVERRIDE_KEY = 'nahla_api_base_override'

/** Temporary production default when no env is set — Railway direct URL. */
const _DEFAULT_API_BASE = 'https://nahla-saas-production.up.railway.app'

function _trimEnv(key: string): string {
  const v = import.meta.env[key] as string | undefined
  return v ? String(v).trim() : ''
}

function _envApiBase(): string {
  return (
    _trimEnv('VITE_API_BASE') ||
    _trimEnv('VITE_API_BASE_URL') ||
    _trimEnv('VITE_API_URL') ||
    _trimEnv('NEXT_PUBLIC_API_URL') ||
    _trimEnv('REACT_APP_API_URL') ||
    ''
  ).replace(/\/+$/, '')
}

function _readApiBase(): string {
  if (typeof window !== 'undefined') {
    try {
      const ovr = window.localStorage.getItem(_OVERRIDE_KEY)
      if (ovr && /^https?:\/\//.test(ovr)) return ovr.replace(/\/+$/, '')
    } catch { /* private mode etc. */ }
  }
  const env = _envApiBase()
  return (env || _DEFAULT_API_BASE).replace(/\/+$/, '')
}

if (typeof window !== 'undefined') {
  // eslint-disable-next-line no-console
  console.info('[auth] API_BASE (initial) =', _readApiBase())
}

/** True when localStorage override is active (operator diagnostics panel). */
export function hasRuntimeApiBaseOverride(): boolean {
  if (typeof window === 'undefined') return false
  try {
    const ovr = window.localStorage.getItem(_OVERRIDE_KEY)
    return !!(ovr && /^https?:\/\//.test(ovr))
  } catch {
    return false
  }
}

/** Runtime API_BASE override — used by the login-page diagnostics panel. */
export function setApiBaseOverride(url: string | null): void {
  try {
    if (url && /^https?:\/\//.test(url)) {
      window.localStorage.setItem(_OVERRIDE_KEY, url.replace(/\/+$/, ''))
    } else {
      window.localStorage.removeItem(_OVERRIDE_KEY)
    }
  } catch { /* ignore */ }
}

export function getApiBase(): string {
  return _readApiBase()
}

/**
 * Force-clear any registered service worker + all caches. Used by the
 * login-page diagnostics panel when the user reports inconsistent
 * fetch failures — a stale SW from a previous deploy is the most
 * common cause of "ping works in address bar, fails from app".
 */
export async function clearServiceWorkersAndCaches(): Promise<{ swCount: number; cacheCount: number }> {
  let swCount = 0
  let cacheCount = 0
  try {
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations()
      for (const r of regs) {
        try { await r.unregister(); swCount++ } catch { /* ignore */ }
      }
    }
  } catch { /* ignore */ }
  try {
    if ('caches' in window) {
      const keys = await caches.keys()
      for (const k of keys) {
        try { await caches.delete(k); cacheCount++ } catch { /* ignore */ }
      }
    }
  } catch { /* ignore */ }
  // eslint-disable-next-line no-console
  console.info('[auth] cleared service workers=%s caches=%s', swCount, cacheCount)
  return { swCount, cacheCount }
}

/**
 * Diagnostic ping — verifies that the browser can talk to the API at
 * all (DNS, TLS, CORS, service-worker cache). Returns the server JSON
 * on success and an Error otherwise. Never throws unless the caller
 * explicitly rethrows.
 */
export async function pingAuth(): Promise<{ ok: boolean; status: number; body: unknown; durationMs: number; error?: string }> {
  const start = performance.now()
  const url   = `${getApiBase()}/auth/ping`
  const controller = new AbortController()
  const timeoutId  = setTimeout(() => controller.abort(), 8_000)
  try {
    const res = await fetch(url, {
      method:      'GET',
      signal:      controller.signal,
      cache:       'no-store',
      credentials: 'omit',
      mode:        'cors',
    })
    const txt = await res.text()
    let parsed: unknown = txt
    try { parsed = JSON.parse(txt) } catch { /* keep text */ }
    return {
      ok:         res.ok,
      status:     res.status,
      body:       parsed,
      durationMs: Math.round(performance.now() - start),
    }
  } catch (e) {
    return {
      ok:         false,
      status:     0,
      body:       null,
      durationMs: Math.round(performance.now() - start),
      error:      e instanceof Error ? `${e.name}: ${e.message}` : String(e),
    }
  } finally {
    clearTimeout(timeoutId)
  }
}

const AUTH_KEY        = 'nahla_auth'
const TOKEN_KEY       = 'nahla_token'
const ROLE_KEY        = 'nahla_role'
const EMAIL_KEY       = 'nahla_email'
const TENANT_ID_KEY   = 'nahla_tenant_id'
const USER_ID_KEY     = 'nahla_user_id'
const STORE_NAME_KEY  = 'nahla_store_name'
const IMPERSONATE_KEY = 'nahla_impersonate'   // JSON: { token, storeName, adminToken }

/** Decode the middle (payload) segment of a JWT without verifying the signature. */
function _decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(b64))
  } catch {
    return {}
  }
}

/** Persist a session from any token + optional metadata. */
function _persistSession(
  token: string,
  overrides: {
    role?: string
    email?: string
    tenant_id?: number | string
    user_id?: number | string
    store_name?: string
  } = {},
): void {
  const payload = _decodeJwtPayload(token)
  localStorage.setItem(AUTH_KEY,       '1')
  localStorage.setItem(TOKEN_KEY,      token)
  localStorage.setItem(ROLE_KEY,       String(overrides.role      ?? payload.role      ?? 'merchant'))
  localStorage.setItem(EMAIL_KEY,      String(overrides.email     ?? payload.sub       ?? ''))
  localStorage.setItem(TENANT_ID_KEY,  String(overrides.tenant_id ?? payload.tenant_id ?? ''))
  localStorage.setItem(USER_ID_KEY,    String(overrides.user_id   ?? payload.user_id   ?? ''))
  if (overrides.store_name) {
    localStorage.setItem(STORE_NAME_KEY, overrides.store_name)
  }

  // Phase 1A — attach minimal, PII-free user context to Sentry. Never
  // include the email; we only need an opaque identifier to group
  // events. No-op when Sentry is disabled.
  try {
    setSentryUser({
      userId:   overrides.user_id    ?? (payload.user_id    as number | string | undefined) ?? null,
      tenantId: overrides.tenant_id  ?? (payload.tenant_id  as number | string | undefined) ?? null,
      role:     String(overrides.role ?? payload.role ?? 'merchant'),
    })
  } catch { /* ignore */ }
}

export interface LoginResult {
  ok:       boolean
  reason?:  'timeout' | 'network' | 'http' | 'unauthorized' | 'parse'
  status?:  number
  message?: string
  /**
   * Set to true when the password was correct but the user has 2FA
   * enabled. The dashboard must then prompt for the OTP and call
   * `verifyTwoFactorLogin(challengeToken, otp)` to complete the
   * sign-in. No session is persisted yet at this point.
   */
  requires2fa?:     boolean
  /** Short-lived (5 min) JWT carrying the pending session claims. */
  challengeToken?:  string
  /** Time in seconds until the challenge token expires. */
  challengeTtlSec?: number
  /** Echo of the email the user typed — useful to show on the OTP screen. */
  email?:           string
}

/**
 * Attempt a login. Returns a structured result so the UI can render a
 * specific error message and console can show exactly what happened.
 *
 * Hard 20-second timeout via AbortController guarantees the UI is
 * never stuck on the spinner indefinitely. Every interesting outcome
 * is also logged to the browser console so the operator can paste
 * one screenshot of DevTools to diagnose any future hang.
 */
/**
 * One attempt against a specific transport (form-urlencoded or JSON).
 * Form-urlencoded is a CORS "simple request" — no OPTIONS preflight is
 * fired by the browser. JSON is a "non-simple" request and ALWAYS
 * fires a preflight first.
 *
 * Returns a `LoginResult`. On AbortError (timeout) or fetch failure
 * (TypeError "Failed to fetch", etc.) the caller can inspect
 * `reason === 'timeout' | 'network'` and fall back to the other
 * transport.
 */
async function _loginAttempt(
  transport: 'form' | 'json',
  email: string,
  password: string,
  timeoutMs: number,
): Promise<LoginResult> {
  const path = transport === 'form' ? '/auth/login-form' : '/auth/login'
  const url = `${getApiBase()}${path}`
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  const start = performance.now()
  // eslint-disable-next-line no-console
  console.info('[auth] login → POST', url, `(transport=${transport})`)

  let body: BodyInit
  let headers: Record<string, string>
  if (transport === 'form') {
    const params = new URLSearchParams()
    params.set('email', email)
    params.set('password', password)
    body = params
    // Intentionally DO NOT set Content-Type — letting the browser set
    // `application/x-www-form-urlencoded; charset=UTF-8` keeps this a
    // "simple request" so no preflight is sent.
    headers = {}
  } else {
    body = JSON.stringify({ email, password })
    headers = { 'Content-Type': 'application/json' }
  }

  try {
    const res = await fetch(url, {
      method:      'POST',
      headers,
      body,
      signal:      controller.signal,
      cache:       'no-store',
      // We use Authorization header tokens, not cookies. credentials:'omit'
      // keeps the request a true CORS "simple request" for the form
      // transport (cookies would force a preflight).
      credentials: 'omit',
      mode:        'cors',
    })
    const elapsed = Math.round(performance.now() - start)
    // eslint-disable-next-line no-console
    console.info(`[auth] login response transport=${transport} status=${res.status} elapsed=${elapsed}ms`)

    if (res.status === 401) {
      return { ok: false, reason: 'unauthorized', status: 401 }
    }
    if (!res.ok) {
      let bodyText = ''
      try { bodyText = (await res.text()).slice(0, 400) } catch { /* ignore */ }
      // eslint-disable-next-line no-console
      console.warn(`[auth] login non-OK transport=${transport} body=${bodyText}`)
      return { ok: false, reason: 'http', status: res.status, message: bodyText || `HTTP ${res.status}` }
    }

    let data: {
      access_token?:    string
      role?:            string
      tenant_id?:       number
      user_id?:         number
      requires_2fa?:    boolean
      challenge_token?: string
      challenge_ttl?:   number
      email?:           string
    }
    try {
      data = await res.json()
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error('[auth] login response JSON parse failed', e)
      return { ok: false, reason: 'parse', status: res.status, message: 'Bad server response' }
    }

    // ── 2FA gate ─────────────────────────────────────────────────────────
    // The backend returned a challenge token instead of an access token.
    // We must NOT persist any session yet — the user has only proven the
    // password. Bubble the challenge up to the Login page so it can
    // render an OTP input and exchange the token via /auth/2fa/login/verify.
    if (data.requires_2fa && data.challenge_token) {
      // eslint-disable-next-line no-console
      console.info('[auth] 2FA challenge issued transport=%s email=%s', transport, data.email)
      return {
        ok:              true,
        status:          res.status,
        requires2fa:     true,
        challengeToken:  data.challenge_token,
        challengeTtlSec: typeof data.challenge_ttl === 'number' ? data.challenge_ttl : 300,
        email:           data.email,
      }
    }

    _persistSession(data.access_token ?? '', {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    // eslint-disable-next-line no-console
    console.info('[auth] login success transport=%s role=%s tenant_id=%s', transport, data.role, data.tenant_id)
    return { ok: true, status: res.status }
  } catch (e) {
    if (e instanceof Error && e.name === 'AbortError') {
      // eslint-disable-next-line no-console
      console.warn(`[auth] login TIMEOUT transport=${transport} after ${timeoutMs}ms — backend or network unresponsive`)
      return { ok: false, reason: 'timeout', message: `Login request timed out after ${timeoutMs}ms` }
    }
    // eslint-disable-next-line no-console
    console.error(`[auth] login network error transport=${transport}`, e)
    return {
      ok:      false,
      reason:  'network',
      message: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
    }
  } finally {
    clearTimeout(timeoutId)
  }
}

/**
 * Attempt a login. Tries the form-urlencoded endpoint first because it
 * does NOT trigger a CORS preflight — browsers send it as a "simple
 * request". This bypasses the upstream proxies (Cloudflare / Railway
 * edge / corporate firewalls) that have been observed to drop or
 * reset OPTIONS requests with `NS_BINDING_ABORTED`.
 *
 * If the form transport fails for a reason that is consistent with a
 * proxy/network issue (timeout, network error), automatically
 * retries against the JSON endpoint. Auth-level failures
 * (`unauthorized`, `http`, `parse`) are returned as-is.
 */
export async function loginDetailed(email: string, password: string): Promise<LoginResult> {
  // eslint-disable-next-line no-console
  console.info('[auth] loginDetailed start', { apiBase: getApiBase() })
  // 1) Form transport (no preflight) — this is the path that should
  //    always succeed once the backend `/auth/login-form` endpoint is
  //    deployed.
  const formResult = await _loginAttempt('form', email, password, 20_000)
  if (formResult.ok) return formResult

  // Auth-level failures don't benefit from a retry.
  if (formResult.reason === 'unauthorized' || formResult.reason === 'http' || formResult.reason === 'parse') {
    return formResult
  }

  // 2) Form transport hit a network/timeout — fall back to JSON. If
  //    the form endpoint isn't deployed yet (older backend) the
  //    server returns a 404 which is `http`, NOT `network`, and we
  //    won't get here. Network/timeout means the request never
  //    reached the app — same problem the JSON endpoint has, but
  //    worth one retry.
  // eslint-disable-next-line no-console
  console.warn('[auth] form transport failed (%s); retrying JSON', formResult.reason)
  return _loginAttempt('json', email, password, 20_000)
}

/** Backwards-compatible wrapper — most callers just need a boolean. */
export async function login(email: string, password: string): Promise<boolean> {
  const r = await loginDetailed(email, password)
  return r.ok && !r.requires2fa
}

export interface TwoFactorLoginResult {
  ok:       boolean
  /** Structured failure reason; populated only when ok is false. */
  reason?:  'unauthorized' | 'locked' | 'expired' | 'network' | 'timeout' | 'parse' | 'http'
  status?:  number
  /** Human-friendly message localised by the backend. */
  message?: string
  /** Set when the server signals a temporary row-level lock; seconds. */
  secondsRemaining?: number
  /** Backend sets this true when the OTP we just consumed was a recovery code. */
  usedRecoveryCode?: boolean
}

/**
 * Exchange a /auth/login challenge_token + OTP for a real access_token.
 * On success, persists the session exactly like a normal login would
 * (same _persistSession call) so the rest of the dashboard doesn't need
 * a separate code path. On failure, surfaces a structured reason the
 * Login page can localise.
 */
export async function verifyTwoFactorLogin(
  challengeToken: string,
  otp: string,
): Promise<TwoFactorLoginResult> {
  const controller = new AbortController()
  const timeoutId  = setTimeout(() => controller.abort(), 20_000)
  try {
    const res = await fetch(`${getApiBase()}/auth/2fa/login/verify`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ challenge_token: challengeToken, otp }),
      signal:  controller.signal,
    })

    if (!res.ok) {
      let body: any = null
      try { body = await res.json() } catch { /* ignore */ }
      const detail = body?.detail
      if (res.status === 429 && detail?.code === 'totp_locked') {
        return {
          ok: false, reason: 'locked', status: 429,
          message: typeof detail.message === 'string' ? detail.message : 'تم القفل المؤقت.',
          secondsRemaining: typeof detail.seconds_remaining === 'number' ? detail.seconds_remaining : undefined,
        }
      }
      if (res.status === 401) {
        const msg = typeof detail === 'string'
          ? detail
          : (detail?.message ?? 'انتهت صلاحية جلسة التحقق. سجّل الدخول من جديد.')
        return { ok: false, reason: msg.includes('انتهت') ? 'expired' : 'unauthorized', status: 401, message: msg }
      }
      const msg = typeof detail === 'string' ? detail : (detail?.message ?? `HTTP ${res.status}`)
      return { ok: false, reason: 'http', status: res.status, message: msg }
    }

    let data: {
      access_token?:        string
      role?:                string
      tenant_id?:           number
      user_id?:             number
      email?:               string
      used_recovery_code?:  boolean
    }
    try {
      data = await res.json()
    } catch {
      return { ok: false, reason: 'parse', status: res.status, message: 'Bad server response' }
    }

    _persistSession(data.access_token ?? '', {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    return { ok: true, status: res.status, usedRecoveryCode: !!data.used_recovery_code }
  } catch (e) {
    if (e instanceof Error && e.name === 'AbortError') {
      return { ok: false, reason: 'timeout', message: 'Verify timed out.' }
    }
    return { ok: false, reason: 'network', message: e instanceof Error ? e.message : String(e) }
  } finally {
    clearTimeout(timeoutId)
  }
}

export function logout(): void {
  // Best-effort revoke the JWT on the backend so a stolen token (e.g.
  // from a shared device) cannot continue authenticating after logout.
  // Phase 1A: we don't await — even if the request fails we still want
  // to clear the local session immediately. The backend revocation is
  // backed by Redis (with in-process fallback) — see
  // ``backend/core/token_revocation.py``.
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
      void fetch(`${getApiBase()}/auth/logout`, {
        method:      'POST',
        headers:     { 'Authorization': `Bearer ${token}` },
        credentials: 'omit',
        cache:       'no-store',
        keepalive:   true,
      }).catch(() => { /* ignore — local cleanup below is the source of truth */ })
    }
  } catch { /* ignore */ }

  localStorage.removeItem(AUTH_KEY)
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ROLE_KEY)
  localStorage.removeItem(EMAIL_KEY)
  localStorage.removeItem(TENANT_ID_KEY)
  localStorage.removeItem(USER_ID_KEY)
  localStorage.removeItem(STORE_NAME_KEY)

  try { clearSentryUser() } catch { /* ignore */ }
}

export function isAuthenticated(): boolean {
  return localStorage.getItem(AUTH_KEY) === '1'
}

/** Milliseconds since epoch when the current JWT expires, or null. */
export function getTokenExpiryMs(): number | null {
  const exp = _decodeJwtPayload(getToken()).exp
  return typeof exp === 'number' ? exp * 1000 : null
}

/** Proactively refresh when fewer than this many ms remain before exp. */
const SESSION_REFRESH_SKEW_MS = 24 * 3600_000

/**
 * Exchange the current session JWT for a fresh one (rolling session).
 * Returns true when a new token was persisted.
 */
export async function refreshSession(): Promise<boolean> {
  const token = getToken()
  if (!token || !isAuthenticated()) return false

  const controller = new AbortController()
  const timeoutId  = setTimeout(() => controller.abort(), 15_000)
  try {
    const res = await fetch(`${getApiBase()}/auth/session/refresh`, {
      method:      'POST',
      headers:     { Authorization: `Bearer ${token}` },
      cache:       'no-store',
      credentials: 'omit',
      signal:      controller.signal,
    })
    if (!res.ok) return false
    const data = await res.json() as {
      access_token?: string
      role?:         string
      tenant_id?:    number
      user_id?:      number
    }
    if (!data.access_token) return false
    _persistSession(data.access_token, {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    return true
  } catch {
    return false
  } finally {
    clearTimeout(timeoutId)
  }
}

/**
 * Restore / extend a merchant session on PWA open or tab focus.
 * Returns false only when re-login is genuinely required.
 */
export async function bootstrapAuthSession(): Promise<boolean> {
  if (!isAuthenticated() || !getToken()) return false

  const expMs = getTokenExpiryMs()
  const now   = Date.now()
  if (expMs && expMs > now + SESSION_REFRESH_SKEW_MS) return true

  return refreshSession()
}

/** Refresh on visibility + every 12h while the dashboard stays open. */
export function installSessionRefreshLoop(): () => void {
  const tick = () => { void bootstrapAuthSession() }
  const onVis = () => {
    if (document.visibilityState === 'visible') tick()
  }
  document.addEventListener('visibilitychange', onVis)
  const id = window.setInterval(tick, 12 * 3600_000)
  return () => {
    document.removeEventListener('visibilitychange', onVis)
    window.clearInterval(id)
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function getRole(): string {
  return localStorage.getItem(ROLE_KEY) ?? 'merchant'
}

export function isPlatformStaffRole(role: string): boolean {
  return ['admin', 'owner', 'super_admin', 'platform_admin', 'platform_owner'].includes(role)
}

/** Returns the email (sub) of the logged-in user. */
export function getEmail(): string {
  return localStorage.getItem(EMAIL_KEY) ?? ''
}

/** Returns the store name if known (set during Salla/Zid OAuth). */
export function getStoreName(): string {
  return (
    localStorage.getItem('nahla_salla_store_name') ||
    localStorage.getItem('nahla_zid_store_name')   ||
    localStorage.getItem(STORE_NAME_KEY)            ||
    ''
  )
}

/** Returns the tenant_id from the current session (read from JWT claim, cached in localStorage). */
export function getTenantId(): number | null {
  const raw = localStorage.getItem(TENANT_ID_KEY)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return isNaN(n) ? null : n
}

/** Returns the user_id from the current session (read from JWT claim, cached in localStorage). */
export function getUserId(): number | null {
  const raw = localStorage.getItem(USER_ID_KEY)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return isNaN(n) ? null : n
}

// Role hierarchy
// owner / super_admin → Platform Owner Dashboard
// staff              → Staff Dashboard
// merchant_admin / merchant_user / merchant → Merchant Dashboard

export function isAdmin(): boolean {
  return isPlatformStaffRole(getRole())
}

export function isPlatformOwner(): boolean {
  return isPlatformStaffRole(getRole())
}

// ────────────────────────────────────────────────────────────────────
// Support-access / impersonation visibility
// ────────────────────────────────────────────────────────────────────
//
// The backend mints a SECOND kind of JWT when a platform admin enters a
// merchant tenant via `POST /admin/impersonate/{tenant_id}` — the
// "support impersonation" token. Its payload carries:
//
//   role:            "support_impersonation"
//   impersonation:   true
//   actor_sub:       <admin email>
//   actor_user_id:   <admin user.id>
//   session_version: <revocation counter>
//   sub:             <merchant email>          // NOT the admin's
//   tenant_id:       <merchant's tenant>       // NOT 1
//
// While this token is active, `getRole()` returns ``"support_impersonation"``
// — NOT one of the PLATFORM_ADMIN_ROLES — so `isAdmin()` is false. That's
// intentional: most merchant-scoped UI must render as the merchant sees
// it, so support actually experiences what the merchant is reporting.
//
// BUT there's a small set of internal-debug tools (media diagnostics,
// direct WhatsApp test send, etc.) that should remain available while
// impersonating. `canUseInternalDebug()` is the single source of truth
// for "show this debug-only UI" — true when either:
//   (a) the user is logged in as a platform admin directly, OR
//   (b) the user holds an active support-impersonation JWT.
//
// We deliberately read the JWT EACH CALL (not from localStorage role)
// because:
//   * `getRole()` reads `nahla_role` which the impersonation start path
//     may overwrite to "merchant" so merchant-scoped UI renders normally.
//   * The JWT itself is the authoritative source — it's also what the
//     backend sees and middleware enforces.

/** Cached single decode of the current token's claims. Re-decodes
 * whenever the token changes (login / impersonation / refresh). */
let _cachedTokenForClaims = ''
let _cachedClaims: Record<string, unknown> = {}

function _currentClaims(): Record<string, unknown> {
  const t = getToken()
  if (!t) {
    _cachedTokenForClaims = ''
    _cachedClaims = {}
    return _cachedClaims
  }
  if (t !== _cachedTokenForClaims) {
    _cachedTokenForClaims = t
    _cachedClaims = _decodeJwtPayload(t)
  }
  return _cachedClaims
}

/** True when the current JWT is a support-impersonation token issued
 *  via `POST /admin/impersonate/{tenant_id}`. */
export function isImpersonatingSupport(): boolean {
  const c = _currentClaims()
  return c.impersonation === true || c.role === 'support_impersonation'
}

/** The platform-admin email that started the current support session,
 *  or null when we're not in one. Useful for screen banners ("you are
 *  acting as <merchant> on behalf of <admin>"). */
export function getImpersonationActor(): string | null {
  const c = _currentClaims()
  const v = c.actor_sub
  return typeof v === 'string' && v ? v : null
}

/** Unified visibility gate for internal debug tools (media diagnostics,
 *  direct WhatsApp test send, support-only inspectors, etc.).
 *
 *  Returns true when the session is either a regular platform admin
 *  OR an admin actively impersonating a merchant. The corresponding
 *  backend endpoints are still gated by `require_admin` — this flag
 *  only decides whether to *show* the buttons. A merchant cookie / a
 *  spoofed `nahla_role=admin` in localStorage gets a 403 from the
 *  server regardless. */
export function canUseInternalDebug(): boolean {
  return isAdmin() || isImpersonatingSupport()
}

export function isStaff(): boolean {
  return getRole() === 'staff'
}

export function isMerchant(): boolean {
  const r = getRole()
  return r === 'merchant' || r === 'merchant_admin' || r === 'merchant_user'
}

export function getDefaultRoute(): string {
  if (isPlatformOwner()) return '/admin'
  if (isStaff())         return '/overview'
  return '/overview'
}

// ── Impersonation helpers ──────────────────────────────────────────────────────

export interface ImpersonationInfo {
  storeName: string
  merchantEmail: string
  adminToken: string   // original admin token to restore on exit
}

export function startImpersonation(
  merchantToken: string,
  storeName: string,
  merchantEmail: string,
): void {
  // Save the admin's full session state before switching
  const adminToken    = localStorage.getItem(TOKEN_KEY)     ?? ''
  const adminRole     = localStorage.getItem(ROLE_KEY)      ?? 'admin'
  const adminEmail    = localStorage.getItem(EMAIL_KEY)     ?? ''
  const adminTenantId = localStorage.getItem(TENANT_ID_KEY) ?? ''
  const adminUserId   = localStorage.getItem(USER_ID_KEY)   ?? ''
  localStorage.setItem(IMPERSONATE_KEY, JSON.stringify({
    storeName, merchantEmail, adminToken, adminRole, adminEmail, adminTenantId, adminUserId,
  }))
  // Switch to merchant session
  _persistSession(merchantToken, { role: 'merchant', email: merchantEmail, store_name: storeName })
}

export function stopImpersonation(): void {
  const raw = localStorage.getItem(IMPERSONATE_KEY)
  if (!raw) return
  const saved = JSON.parse(raw) as ImpersonationInfo & {
    adminToken: string; adminRole: string; adminEmail: string
    adminTenantId: string; adminUserId: string
  }
  // Restore the admin session
  _persistSession(saved.adminToken, {
    role:      saved.adminRole,
    email:     saved.adminEmail,
    tenant_id: saved.adminTenantId,
    user_id:   saved.adminUserId,
  })
  localStorage.removeItem(IMPERSONATE_KEY)
}

export function getImpersonation(): (ImpersonationInfo & { adminToken: string }) | null {
  const raw = localStorage.getItem(IMPERSONATE_KEY)
  return raw ? JSON.parse(raw) : null
}

export function isImpersonating(): boolean {
  return !!localStorage.getItem(IMPERSONATE_KEY)
}

export async function register(
  email: string,
  password: string,
  storeName: string,
  phone: string = '',
  inviteToken: string = '',
): Promise<{ ok: boolean; error?: string }> {
  try {
    const ctrl = new AbortController()
    const tid = setTimeout(() => ctrl.abort(), 25_000)
    let res: Response
    try {
      res = await fetch(`${getApiBase()}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, store_name: storeName, phone, invite_token: inviteToken }),
        signal: ctrl.signal,
        cache: 'no-store',
        mode: 'cors',
        credentials: 'omit',
      })
    } finally {
      clearTimeout(tid)
    }
    const data = await res.json()
    if (!res.ok) return { ok: false, error: data.detail ?? 'فشل التسجيل' }
    _persistSession(data.access_token ?? '', {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    return { ok: true }
  } catch {
    return { ok: false, error: 'تعذّر الاتصال بالخادم' }
  }
}
