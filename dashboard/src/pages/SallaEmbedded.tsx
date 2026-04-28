/**
 * SallaEmbedded.tsx
 * -----------------
 * Zero-Friction Entry for Salla merchants.
 *
 * Routes that render this page:
 *   /app/salla   — primary embedded entry (partner-portal iframe URL)
 *   /salla       — legacy entry (kept for backwards compatibility)
 *
 * Bootstrap sequence (≤ 2 s target):
 *   1. Show skeleton (< 1 s)
 *   2. Check existing session  → navigate /overview immediately if live
 *   3. Load Salla SDK + handshake (parallel with step 2)
 *   4. POST /salla/token-login with URL ?token → receive Nahla JWT
 *   5. Persist JWT → navigate /overview or /onboarding
 *
 * Error path: inline message inside the embedded frame — never shows Login UI.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

type Phase = 'skeleton' | 'checking' | 'login' | 'success' | 'error'

interface LoginResponse {
  access_token: string
  role: string
  tenant_id: number
  store_name: string
  email: string
  is_new: boolean
  wa_connected: boolean
  redirect_to: string
  detail?: string
}

interface SessionResponse {
  connected: boolean
  tenant_id: number
  token: string
}

const SDK_URL =
  'https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js'

const TIMEOUT_MS = 3000

function decodeJwtPayload(token: string): Record<string, unknown> {
  const parts = token.split('.')
  return JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
}

// ── Salla SDK ─────────────────────────────────────────────────────────────────

function signalReady() {
  const msg = {
    event: 'embedded::ready',
    payload: {},
    timestamp: Date.now(),
    source: 'embedded-app',
    metadata: { version: '0.2.4' },
  }
  try { window.parent.postMessage(msg, '*') } catch { /* cross-origin */ }
  try { window.parent.postMessage({ event: 'app.ready', type: 'app.ready' }, '*') } catch { /* cross-origin */ }
}

function initSdkHandshake() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sdk = (window as any).Salla?.embedded
  if (!sdk) { signalReady(); return }
  sdk.init({ debug: false })
    .then(() => { sdk.ready(); signalReady() })
    .catch(() => signalReady())
}

function loadSdk(): Promise<void> {
  return new Promise((resolve) => {
    if (document.querySelector(`script[src="${SDK_URL}"]`)) { resolve(); return }
    const s = document.createElement('script')
    s.src     = SDK_URL
    s.onload  = () => resolve()
    s.onerror = () => resolve()
    document.head.appendChild(s)
  })
}

// ── Session persistence ────────────────────────────────────────────────────────

function persistSession(data: LoginResponse | SessionResponse, storeId?: string) {
  const jwt = 'access_token' in data ? data.access_token : data.token
  const claims = decodeJwtPayload(jwt)

  ;['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
    'nahla_tenant_id', 'nahla_user_id'].forEach(k => localStorage.removeItem(k))

  localStorage.setItem('nahla_auth',      '1')
  localStorage.setItem('nahla_token',     jwt)
  localStorage.setItem('nahla_role',      String(claims.role      ?? 'merchant'))
  localStorage.setItem('nahla_email',     String(claims.sub       ?? ''))
  localStorage.setItem('nahla_tenant_id', String(claims.tenant_id ?? ''))
  localStorage.setItem('nahla_user_id',   String(claims.user_id   ?? ''))

  if ('store_name' in data && data.store_name) {
    localStorage.setItem('nahla_salla_store_name', data.store_name)
    localStorage.setItem('nahla_store_name', data.store_name)
  }
  if (storeId) {
    localStorage.setItem('nahla_salla_store_id', storeId)
  }
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function SallaEmbedded() {
  const navigate    = useNavigate()
  const [phase, setPhase]           = useState<Phase>('skeleton')
  const [statusText, setStatusText] = useState('')
  const [errorDetail, setErrorDetail] = useState('')
  const bootedRef = useRef(false)

  const params     = new URLSearchParams(window.location.search)
  const sallaToken = params.get('token')    || ''
  const storeId    = params.get('store_id') || ''
  const appId      = params.get('app_id')   || ''

  // ── Inline error (never navigate away from iframe) ────────────────────────
  const showError = useCallback((msg: string) => {
    setPhase('error')
    setErrorDetail(msg)
  }, [])

  // ── Navigate to Smart Entry Screen (always, regardless of is_new) ──────
  const enterDashboard = useCallback((_dest?: string) => {
    // Mark this session as originating from Salla embedded
    localStorage.setItem('nahla_salla_embedded', '1')
    setPhase('success')
    // Always route through the Smart Entry Screen — never to a blank dashboard
    setTimeout(() => navigate('/app/entry', { replace: true }), 600)
  }, [navigate])

  // ── Step 2: check existing Nahla session ──────────────────────────────────
  const checkSession = useCallback(async (): Promise<boolean> => {
    const stored = localStorage.getItem('nahla_token')
    if (!stored) return false

    try {
      const ctrl = new AbortController()
      const tid  = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
      const res  = await fetch(`${API_BASE}/api/salla/session`, {
        headers: { Authorization: `Bearer ${stored}` },
        signal: ctrl.signal,
      })
      clearTimeout(tid)

      if (res.ok) {
        const data: SessionResponse = await res.json()
        persistSession(data, storeId)
        enterDashboard('/overview')
        return true
      }
    } catch {
      /* network error or abort → fall through to token exchange */
    }
    return false
  }, [storeId, enterDashboard])

  // ── Step 4: Salla token exchange → Nahla JWT ─────────────────────────────
  const doLogin = useCallback(async () => {
    if (!sallaToken) {
      showError('لم يتم إرسال رمز المصادقة من سلة. أعد فتح التطبيق من لوحة سلة.')
      return
    }

    setPhase('login')
    setStatusText('جاري التحقق من هويتك...')

    try {
      const ctrl = new AbortController()
      const tid  = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
      const res  = await fetch(`${API_BASE}/salla/token-login`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ token: sallaToken, app_id: appId || undefined }),
        signal:  ctrl.signal,
      })
      clearTimeout(tid)

      const data: LoginResponse = await res.json()

      if (!data.access_token) {
        showError(data.detail || 'تعذّر التحقق من هويتك. حاول إغلاق التطبيق وإعادة فتحه.')
        return
      }

      persistSession(data, storeId)

      setStatusText(
        data.is_new
          ? 'مرحباً! جاري إعداد حسابك...'
          : `مرحباً بعودتك${data.store_name ? ` ${data.store_name}` : ''}`
      )

      enterDashboard()
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === 'AbortError'
      showError(
        aborted
          ? 'استغرق الخادم وقتاً طويلاً. تحقق من اتصالك وأعد المحاولة.'
          : 'تعذر الوصول إلى الخادم. تحقق من اتصالك بالإنترنت.'
      )
    }
  }, [sallaToken, appId, storeId, showError, enterDashboard])

  // ── Bootstrap ─────────────────────────────────────────────────────────────
  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true

    ;(async () => {
      // Skeleton for a brief moment while we kick off parallel work
      setPhase('skeleton')

      // SDK handshake + session check run in parallel
      const [, sessionAlive] = await Promise.all([
        (async () => {
          await loadSdk()
          initSdkHandshake()
          // Belt-and-suspenders: re-signal after 3 s in case iframe missed it
          setTimeout(signalReady, 3000)
        })(),
        (async () => {
          setPhase('checking')
          return checkSession()
        })(),
      ])

      // Session was live → already navigated inside checkSession
      if (sessionAlive) return

      // No live session → use the Salla token from the URL
      await doLogin()
    })()
  }, [checkSession, doLogin])

  // ── Retry ─────────────────────────────────────────────────────────────────
  const handleRetry = useCallback(() => {
    bootedRef.current = false
    setPhase('skeleton')
    setErrorDetail('')
    bootedRef.current = true
    doLogin()
  }, [doLogin])

  // ── Render ────────────────────────────────────────────────────────────────
  const isSkeleton = phase === 'skeleton' || phase === 'checking'

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center px-4 py-6"
      style={{
        fontFamily: "'Cairo', system-ui, sans-serif",
        background: '#0f172a',
        backgroundImage:
          'radial-gradient(ellipse 80% 60% at 50% -10%, rgba(245,158,11,0.08) 0%, transparent 70%)',
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
              background:  'rgba(245,158,11,0.15)',
              border:      '1px solid rgba(245,158,11,0.35)',
              boxShadow:   '0 0 10px rgba(245,158,11,0.3)',
              color:       '#f59e0b',
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
          background:    'rgba(255,255,255,0.03)',
          border:        '1px solid rgba(245,158,11,0.2)',
          backdropFilter: 'blur(16px)',
        }}
      >
        {phase === 'error' ? (
          /* Error state */
          <div className="text-center space-y-4">
            <div className="text-5xl">⚠️</div>
            <p className="text-white font-semibold text-base">تعذّر الدخول</p>
            <p className="text-slate-400 text-sm leading-relaxed">{errorDetail}</p>
            <div className="flex flex-col gap-3 pt-2">
              <button
                onClick={handleRetry}
                className="w-full py-3 px-6 rounded-xl font-bold text-sm transition-all"
                style={{
                  background: '#f59e0b',
                  color:      '#0f172a',
                  boxShadow:  '0 4px 20px rgba(245,158,11,0.35)',
                }}
              >
                إعادة المحاولة
              </button>
            </div>
          </div>
        ) : phase === 'success' ? (
          /* Success state */
          <div className="text-center space-y-4">
            <div className="text-5xl">✅</div>
            <p className="text-white font-semibold text-base">{statusText || 'جاري الدخول...'}</p>
            <p className="text-slate-400 text-sm">جاري تحويلك للوحة التحكم...</p>
          </div>
        ) : isSkeleton ? (
          /* Skeleton state — pulse instead of spin (≤ 1 s, non-blocking feel) */
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <div
                className="w-10 h-10 rounded-full animate-pulse shrink-0"
                style={{ background: 'rgba(245,158,11,0.15)' }}
              />
              <div className="flex-1 space-y-2">
                <div
                  className="h-3 rounded animate-pulse"
                  style={{ background: 'rgba(255,255,255,0.06)', width: '60%' }}
                />
                <div
                  className="h-2.5 rounded animate-pulse"
                  style={{ background: 'rgba(255,255,255,0.04)', width: '40%' }}
                />
              </div>
            </div>
            <div
              className="h-10 rounded-xl animate-pulse mt-4"
              style={{ background: 'rgba(245,158,11,0.08)' }}
            />
            <div
              className="h-10 rounded-xl animate-pulse"
              style={{ background: 'rgba(255,255,255,0.04)' }}
            />
          </div>
        ) : (
          /* Login-in-progress state */
          <div className="text-center space-y-4">
            <div className="relative w-14 h-14 mx-auto">
              <div className="absolute inset-0 rounded-full border-4 border-amber-400/20" />
              <div className="absolute inset-0 rounded-full border-4 border-t-amber-400 animate-spin" />
              <span className="absolute inset-0 flex items-center justify-center text-xl">🐝</span>
            </div>
            <p className="text-white font-semibold text-base">{statusText || 'جاري التحقق...'}</p>
          </div>
        )}
      </div>

      {/* Footer */}
      <p className="mt-5 text-xs" style={{ color: '#334155' }}>
        بأيدي سعودية 100% 🇸🇦 · Nahla AI
      </p>
    </div>
  )
}
