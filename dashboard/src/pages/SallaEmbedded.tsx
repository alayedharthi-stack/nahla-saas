/**
 * SallaEmbedded.tsx  —  /app/salla  &  /salla  (legacy)
 * -------------------------------------------------------
 * Zero-Friction embedded entry for Salla merchants.
 *
 * CRITICAL RULE: signalReady() MUST be called synchronously on first render,
 * BEFORE any async work.  Salla's host frame shows "لم يتم الاتصال بالتطبيق"
 * if the app.ready postMessage doesn't arrive within a few seconds.
 *
 * Bootstrap order:
 *   mount → signalReady() immediately (sync)
 *         → re-signal at 1 s + 3 s (belt-and-suspenders)
 *         → load Salla SDK in background (non-blocking)
 *         → check existing Nahla session (5 s timeout)
 *         → if none: POST /salla/token-login (10 s timeout)
 *         → navigate to /app/entry
 *   Global watchdog: 13 s → show error with retry button
 *
 * Error path: inline UI inside iframe — never navigates away to Landing/Login.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

// ── Immediate ready signal — fires before React even renders ───────────────────
// Salla requires app.ready within milliseconds of the iframe URL loading.
// Calling it here (module scope) guarantees it runs before any hook/effect.
;(function immediateReady() {
  try { window.parent.postMessage({ type: 'app.ready' }, '*') } catch { /* cross-origin */ }
  try { window.parent.postMessage({ event: 'embedded::ready', payload: {}, source: 'embedded-app' }, '*') } catch { /* cross-origin */ }
})()

// ── Constants ─────────────────────────────────────────────────────────────────

const SDK_URL          = 'https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js'
const SESSION_TIMEOUT  = 5_000   // ms — check existing session
const LOGIN_TIMEOUT    = 10_000  // ms — token-login (Salla introspect can be slow)
const WATCHDOG_TIMEOUT = 13_000  // ms — global stuck-skeleton guard

// ── Types ─────────────────────────────────────────────────────────────────────

// Phases:
//   init/checking/login → loading screens
//   ready               → auth complete, waiting for the merchant to click
//                         "ابدأ استخدام نحلة" — we DO NOT auto-navigate so
//                         that we never bypass Salla's "استخدام التطبيق"
//                         gating after a fresh install.
//   success             → user clicked, we're navigating to /app/entry
//   error               → inline error UI
type Phase = 'init' | 'checking' | 'login' | 'ready' | 'success' | 'error'

interface LoginResponse {
  access_token: string
  role:         string
  tenant_id:    number
  store_name:   string
  store_id:     string
  email:        string
  is_new:       boolean
  wa_connected: boolean
  redirect_to:  string
  needs_oauth?: boolean
  oauth_url?:   string
  detail?:      string
}

interface SessionResponse {
  connected:  boolean
  tenant_id:  number
  token:      string
}

// ── Salla SDK handshake ───────────────────────────────────────────────────────
// Must be called as early as possible. Salla's host frame listens for these
// events to dismiss its own loading overlay.

function signalReady() {
  console.info('[SallaEmbedded] → signaling app.ready to Salla host frame')
  const readyMsg = {
    event:     'embedded::ready',
    payload:   {},
    timestamp: Date.now(),
    source:    'embedded-app',
    metadata:  { version: '0.2.4' },
  }
  try { window.parent.postMessage(readyMsg, '*') } catch { /* cross-origin */ }
  try { window.parent.postMessage({ event: 'app.ready', type: 'app.ready' }, '*') } catch { /* cross-origin */ }
}

function loadSdk(): Promise<void> {
  return new Promise((resolve) => {
    if (document.querySelector(`script[src="${SDK_URL}"]`)) { resolve(); return }
    const s   = document.createElement('script')
    s.src     = SDK_URL
    s.onload  = () => {
      console.info('[SallaEmbedded] SDK loaded from CDN')
      resolve()
    }
    s.onerror = () => {
      console.warn('[SallaEmbedded] SDK CDN load failed — continuing without SDK')
      resolve()   // non-fatal: postMessage works without the SDK helper
    }
    document.head.appendChild(s)
  })
}

function initSdkHandshake() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sdk = (window as any).Salla?.embedded
  if (!sdk) {
    console.info('[SallaEmbedded] Salla.embedded not found — using raw postMessage only')
    signalReady()
    return
  }
  sdk.init({ debug: false })
    .then(() => { sdk.ready(); signalReady() })
    .catch((err: unknown) => {
      console.warn('[SallaEmbedded] sdk.init error:', err)
      signalReady()
    })
}

// ── JWT helpers ───────────────────────────────────────────────────────────────

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const parts = token.split('.')
    return JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return {}
  }
}

function persistSession(data: LoginResponse | SessionResponse) {
  const jwt    = 'access_token' in data ? data.access_token : data.token
  const claims = decodeJwtPayload(jwt)

  // Clear ALL old session keys before writing new ones — prevents cross-store leakage
  ;['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
    'nahla_tenant_id', 'nahla_user_id', 'nahla_salla_store_id',
    'nahla_salla_store_name', 'nahla_store_name',
    'nahla_salla_is_new', 'nahla_salla_wa_connected',
  ].forEach(k => localStorage.removeItem(k))

  localStorage.setItem('nahla_auth',           '1')
  localStorage.setItem('nahla_token',          jwt)
  localStorage.setItem('nahla_role',           String(claims.role      ?? 'merchant'))
  localStorage.setItem('nahla_email',          String(claims.sub       ?? ''))
  localStorage.setItem('nahla_tenant_id',      String(claims.tenant_id ?? ''))
  localStorage.setItem('nahla_user_id',        String(claims.user_id   ?? ''))
  localStorage.setItem('nahla_salla_embedded', '1')

  if ('store_name' in data && data.store_name) {
    localStorage.setItem('nahla_salla_store_name', data.store_name)
    localStorage.setItem('nahla_store_name',       data.store_name)
  }

  // store_id comes from salla_token_login response — the AUTHORITATIVE key
  if ('store_id' in data && data.store_id) {
    localStorage.setItem('nahla_salla_store_id', data.store_id)
  }

  if ('is_new' in data) {
    localStorage.setItem('nahla_salla_is_new',      data.is_new       ? '1' : '0')
    localStorage.setItem('nahla_salla_wa_connected', data.wa_connected ? '1' : '0')
  }
}

// ── Main component ────────────────────────────────────────────────────────────

export default function SallaEmbedded() {
  const navigate = useNavigate()

  const [phase, setPhase]               = useState<Phase>('init')
  const [statusText, setStatusText]     = useState('جاري تهيئة الاتصال...')
  const [errorDetail, setErrorDetail]   = useState('')
  const bootedRef                       = useRef(false)
  const watchdogRef                     = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Read URL params once
  const paramsRef  = useRef(new URLSearchParams(window.location.search))
  const sallaToken = paramsRef.current.get('token')    || ''
  const storeId    = paramsRef.current.get('store_id') || ''
  const appId      = paramsRef.current.get('app_id')   || ''

  // ── Helpers ───────────────────────────────────────────────────────────────

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current)
      watchdogRef.current = null
    }
  }, [])

  const showError = useCallback((msg: string) => {
    clearWatchdog()
    console.error('[SallaEmbedded] ✗ error:', msg)
    setPhase('error')
    setErrorDetail(msg)
  }, [clearWatchdog])

  // How long the brief welcome card stays on screen before we auto-navigate
  // the merchant into /app/entry.  Long enough to register the success state,
  // short enough to feel snappy.  This auto-navigate is fine here because
  // the merchant has ALREADY pressed "استخدام التطبيق" inside Salla — that
  // is the explicit gesture Salla policy requires; our iframe is the
  // post-gesture surface so we may transition automatically.
  const WELCOME_HOLD_MS = 1400

  // ── markReady: auth + session save complete, show brief welcome card,
  //            then auto-navigate to /app/entry.
  const markReady = useCallback(() => {
    clearWatchdog()
    setPhase('ready')
    console.info('[SallaEmbedded] ✓ auth complete → showing welcome, auto-entering /app/entry in', WELCOME_HOLD_MS, 'ms')
    setTimeout(() => {
      setPhase('success')
      setStatusText('جاري الدخول...')
      setTimeout(() => navigate('/app/entry', { replace: true }), 150)
    }, WELCOME_HOLD_MS)
  }, [clearWatchdog, navigate])

  // ── goToDashboard: kept for the explicit "Open dashboard" button on the
  //            welcome card — lets impatient merchants skip the 1.4 s hold.
  const goToDashboard = useCallback(() => {
    setPhase('success')
    console.info('[SallaEmbedded] user pressed CTA → navigating to /app/entry')
    setStatusText('جاري الدخول...')
    setTimeout(() => navigate('/app/entry', { replace: true }), 150)
  }, [navigate])

  // ── Step 1: check existing Nahla session ──────────────────────────────────

  const checkSession = useCallback(async (): Promise<boolean> => {
    const stored = localStorage.getItem('nahla_token')
    if (!stored) {
      console.info('[SallaEmbedded] no stored token — skipping session check')
      return false
    }

    // ── TENANT ISOLATION: if the current Salla store_id differs from the
    // stored one, the cached session belongs to a DIFFERENT store. Clear it
    // and force a fresh token-login so we never leak cross-store data.
    const storedStoreId = localStorage.getItem('nahla_salla_store_id') || ''
    if (storeId && storedStoreId && storeId !== storedStoreId) {
      console.warn(
        '[SallaEmbedded] ⚠️ store_id changed:',
        storedStoreId, '→', storeId, '— clearing stale session',
      )
      ;['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
        'nahla_tenant_id', 'nahla_user_id', 'nahla_salla_store_id',
        'nahla_salla_store_name', 'nahla_store_name',
        'nahla_salla_is_new', 'nahla_salla_wa_connected',
      ].forEach(k => localStorage.removeItem(k))
      return false
    }

    console.info('[SallaEmbedded] checking existing session...')
    setPhase('checking')
    setStatusText('جاري التحقق من جلستك...')

    try {
      const ctrl = new AbortController()
      const tid  = setTimeout(() => ctrl.abort(), SESSION_TIMEOUT)

      const sessionUrl = storeId
        ? `${API_BASE}/api/salla/session?store_id=${encodeURIComponent(storeId)}`
        : `${API_BASE}/api/salla/session`
      const res = await fetch(sessionUrl, {
        headers: { Authorization: `Bearer ${stored}` },
        signal:  ctrl.signal,
      })
      clearTimeout(tid)

      console.info('[SallaEmbedded] session check status:', res.status)

      if (res.ok) {
        const data: SessionResponse = await res.json()
        console.info('[SallaEmbedded] ✓ live session — tenant:', data.tenant_id)
        persistSession(data)
        markReady()
        return true
      }
      // 401 → token expired, fall through
    } catch (e) {
      console.warn('[SallaEmbedded] session check failed (will try token-login):', e)
    }
    return false
  }, [storeId, markReady])

  // ── Step 2: token exchange with Salla ─────────────────────────────────────

  const doLogin = useCallback(async () => {
    console.info('[SallaEmbedded] token-login start | token present:', !!sallaToken)

    if (!sallaToken) {
      showError(
        'لم يتم استقبال رمز المصادقة من سلة.\n' +
        'تأكد من أن رابط التطبيق في بوابة الشركاء يشير إلى:\n' +
        'https://app.nahlah.ai/app/salla',
      )
      return
    }

    setPhase('login')
    setStatusText('جاري التحقق من هويتك...')

    try {
      const ctrl = new AbortController()
      const tid  = setTimeout(() => ctrl.abort(), LOGIN_TIMEOUT)

      console.info('[SallaEmbedded] → POST /salla/token-login')
      const res = await fetch(`${API_BASE}/salla/token-login`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ token: sallaToken, app_id: appId || undefined }),
        signal:  ctrl.signal,
      })
      clearTimeout(tid)

      console.info('[SallaEmbedded] token-login status:', res.status)

      let data: LoginResponse
      try {
        data = await res.json()
      } catch {
        showError('الخادم أرجع استجابة غير صالحة. حاول مجدداً.')
        return
      }

      if (!res.ok || !data.access_token) {
        const detail = data?.detail || `HTTP ${res.status}`
        console.error('[SallaEmbedded] token-login failed:', detail, data)
        showError(data?.detail || 'تعذّر التحقق من هويتك. أغلق التطبيق وأعد فتحه.')
        return
      }

      console.info('[SallaEmbedded] ✓ token-login OK | tenant:', data.tenant_id, 'store_id:', data.store_id, 'is_new:', data.is_new, 'needs_oauth:', data.needs_oauth)
      console.info('[SallaEmbedded] needs_oauth=' + String(!!data.needs_oauth))
      persistSession(data)

      // ── Auto-trigger OAuth ONLY for legacy Custom OAuth integrations ──
      //
      // For Easy Mode merchants the backend now always returns
      // needs_oauth=false because Easy Mode apps have no registered
      // redirect_uri and accounts.salla.sa would 404 with
      // 'redirect_uri does not match'.  As a defence-in-depth against
      // an older backend deployment that still returns true, we also
      // refuse to redirect to any URL whose path is /oauth2/auth — the
      // merchant should NEVER see Salla's OAuth screen from inside the
      // embedded iframe.  Instead we just enter the dashboard and let
      // the app.store.authorize webhook (which already arrived for
      // Easy Mode) hydrate tokens server-side.
      if (data.needs_oauth && data.oauth_url) {
        const oauthIsExternalAuthorize =
          /accounts\.salla\.sa\/oauth2\/auth/i.test(data.oauth_url)
        if (oauthIsExternalAuthorize) {
          console.warn(
            '[SallaEmbedded] backend asked for external OAuth authorize ' +
            '(needs_oauth=true) — refusing because Easy Mode apps have no ' +
            'redirect_uri registered. Entering dashboard directly.',
          )
          // Fall through to markReady — the embedded session is enough
          // for the dashboard, and the orders poller / webhook handler
          // will populate refresh_token when Salla delivers it.
        } else {
          console.info('[SallaEmbedded] needs_oauth=true → redirecting to Salla OAuth')
          setStatusText('جاري إكمال الربط مع سلة...')
          if (window.top) {
            window.top.location.href = data.oauth_url
          } else {
            window.location.href = data.oauth_url
          }
          return
        }
      }

      markReady()
    } catch (e) {
      const isAbort = e instanceof DOMException && e.name === 'AbortError'
      console.error('[SallaEmbedded] token-login exception:', e)
      showError(
        isAbort
          ? 'استغرق الخادم وقتاً طويلاً. تحقق من اتصالك وأعد المحاولة.'
          : 'تعذر الوصول إلى الخادم. تحقق من اتصالك بالإنترنت.',
      )
    }
  }, [sallaToken, appId, storeId, showError, markReady])

  // ── Bootstrap ─────────────────────────────────────────────────────────────

  const bootstrap = useCallback(async () => {
    // Global watchdog: if still loading after WATCHDOG_TIMEOUT → show error
    watchdogRef.current = setTimeout(() => {
      console.error('[SallaEmbedded] ⏱ watchdog triggered — still loading after', WATCHDOG_TIMEOUT, 'ms')
      showError('استغرق التحميل وقتاً طويلاً. أعد فتح التطبيق أو تواصل مع الدعم.')
    }, WATCHDOG_TIMEOUT)

    // Load SDK in background — do NOT await before checking session/token
    loadSdk().then(initSdkHandshake)

    // ── TENANT ISOLATION: when Salla provides a fresh embedded token,
    // ALWAYS do a full token-login so the backend resolves the correct
    // tenant for THIS store via store_id.  Reusing a cached session would
    // show a DIFFERENT store's data if the user switched stores.
    if (sallaToken) {
      console.info('[SallaEmbedded] fresh Salla token present — doing full login (skipping cached session)')
      await doLogin()
      return
    }

    // No fresh Salla token (e.g. page refresh inside iframe) → try cached session
    const alive = await checkSession()
    if (alive) return

    // Last resort: try doLogin() in case Salla token is missing (might still
    // work if Salla SDK fills it in via postMessage), otherwise show error.
    await doLogin()
  }, [sallaToken, checkSession, doLogin, showError])

  // ── Mount effect ──────────────────────────────────────────────────────────
  // IMPORTANT: signalReady is called synchronously on mount, before any async.

  useEffect(() => {
    console.info('[SallaEmbedded] loaded')
    console.info(
      '[SallaEmbedded] mounted | path:', window.location.pathname,
      '| token present:', !!sallaToken,
      '| store_id:', storeId,
    )

    // ⚡ CRITICAL: signal Salla host frame IMMEDIATELY
    signalReady()
    const t1 = setTimeout(signalReady, 1000)  // re-signal in case iframe missed it
    const t2 = setTimeout(signalReady, 4000)  // final belt-and-suspenders

    if (bootedRef.current) return
    bootedRef.current = true

    bootstrap()

    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
      clearWatchdog()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Retry ─────────────────────────────────────────────────────────────────

  const handleRetry = useCallback(() => {
    console.info('[SallaEmbedded] retry triggered')
    bootedRef.current = false
    setPhase('init')
    setStatusText('جاري إعادة المحاولة...')
    setErrorDetail('')
    bootedRef.current = true
    // Re-signal and retry login
    signalReady()
    doLogin()
  }, [doLogin])

  // ── Render ────────────────────────────────────────────────────────────────

  const isLoading = phase === 'init' || phase === 'checking' || phase === 'login'

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center px-4 py-6"
      style={{
        fontFamily:      "'Cairo', system-ui, sans-serif",
        background:      '#0f172a',
        backgroundImage: 'radial-gradient(ellipse 80% 60% at 50% -10%, rgba(245,158,11,0.08) 0%, transparent 70%)',
      }}
    >
      {/* Logo */}
      <div className="flex flex-col items-center mb-7">
        <div className="relative w-20 h-20 mb-3">
          <img
            src="https://app.nahlah.ai/logo.png"
            alt="نحلة"
            className="w-full h-full object-contain"
            style={{ filter: 'drop-shadow(0 0 18px rgba(245,158,11,0.4))' }}
            onError={(e) => {
              ;(e.target as HTMLImageElement).style.display = 'none'
              const el = document.getElementById('salla-logo-fallback')
              if (el) el.style.display = 'flex'
            }}
          />
          <span
            id="salla-logo-fallback"
            className="text-6xl absolute inset-0 items-center justify-center hidden"
          >
            🐝
          </span>
        </div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-black text-slate-100 tracking-tight">نحلة</h1>
          <span
            className="text-xs font-black px-2 py-0.5 rounded-md"
            style={{
              background:    'rgba(245,158,11,0.15)',
              border:        '1px solid rgba(245,158,11,0.35)',
              boxShadow:     '0 0 10px rgba(245,158,11,0.3)',
              color:         '#f59e0b',
              letterSpacing: '0.5px',
            }}
          >
            AI
          </span>
        </div>
      </div>

      {/* Card */}
      <div
        className="w-full max-w-sm rounded-2xl p-7"
        style={{
          background:     'rgba(255,255,255,0.03)',
          border:         '1px solid rgba(245,158,11,0.2)',
          backdropFilter: 'blur(16px)',
        }}
      >
        {/* ── Error ─────────────────────────────────────────────────────── */}
        {phase === 'error' && (
          <div className="text-center space-y-4">
            <div className="text-5xl">⚠️</div>
            <p className="text-white font-semibold text-base">تعذّر الاتصال بسلة</p>
            <p className="text-slate-400 text-sm leading-relaxed whitespace-pre-line">{errorDetail}</p>
            <div className="flex flex-col gap-3 pt-2">
              <button
                onClick={handleRetry}
                className="w-full py-3 px-6 rounded-xl font-bold text-sm"
                style={{ background: '#f59e0b', color: '#0f172a', boxShadow: '0 4px 20px rgba(245,158,11,0.35)' }}
              >
                إعادة المحاولة
              </button>
              <a
                href="mailto:support@nahlah.ai"
                className="text-slate-500 text-xs text-center hover:text-slate-400"
              >
                تواصل مع الدعم
              </a>
            </div>
          </div>
        )}

        {/* ── Ready (brief welcome, auto-navigates to /app/entry shortly) ── */}
        {phase === 'ready' && (
          <div className="text-center space-y-4">
            <div className="text-5xl">🎉</div>
            <p className="text-white font-bold text-lg leading-snug">
              تم ربط متجرك بنجاح!
            </p>
            <p className="text-slate-300 text-sm">
              جاري فتح لوحة نحلة...
            </p>
            <button
              onClick={goToDashboard}
              className="text-amber-400 hover:text-amber-300 text-xs font-semibold underline underline-offset-4 transition-colors"
            >
              تخطي
            </button>
          </div>
        )}

        {/* ── Success (post-click, navigating away) ─────────────────────── */}
        {phase === 'success' && (
          <div className="text-center space-y-4">
            <div className="text-5xl">✅</div>
            <p className="text-white font-semibold text-base">{statusText}</p>
            <p className="text-slate-400 text-sm">جاري تحويلك...</p>
          </div>
        )}

        {/* ── Loading (skeleton + status) ───────────────────────────────── */}
        {isLoading && (
          <div className="space-y-5">
            {/* Skeleton rows */}
            <div className="flex items-center gap-3">
              <div
                className="w-10 h-10 rounded-full animate-pulse shrink-0"
                style={{ background: 'rgba(245,158,11,0.12)' }}
              />
              <div className="flex-1 space-y-2">
                <div className="h-3 rounded animate-pulse" style={{ background: 'rgba(255,255,255,0.06)', width: '55%' }} />
                <div className="h-2.5 rounded animate-pulse" style={{ background: 'rgba(255,255,255,0.04)', width: '35%' }} />
              </div>
            </div>
            <div className="h-10 rounded-xl animate-pulse" style={{ background: 'rgba(245,158,11,0.07)' }} />
            <div className="h-10 rounded-xl animate-pulse" style={{ background: 'rgba(255,255,255,0.04)' }} />

            {/* Live status text */}
            <p className="text-center text-slate-500 text-xs pt-1">{statusText}</p>
          </div>
        )}
      </div>

      <p className="mt-5 text-xs" style={{ color: '#334155' }}>
        بأيدي سعودية 100% 🇸🇦 · Nahla AI
      </p>
    </div>
  )
}
