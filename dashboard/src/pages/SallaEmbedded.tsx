/**
 * SallaEmbedded.tsx
 * -----------------
 * Single React page that replaces the two-step Salla flow:
 *   OLD:  /salla/app (backend HTML) → /salla-callback (React)
 *   NEW:  /salla (React) — handles everything in one place
 *
 * Runs inside Salla's embedded app iframe.
 *
 * Flow:
 *   1. Salla opens  https://app.nahlah.ai/salla?token=SALLA_TOKEN&store_id=...
 *   2. Page loads Salla Embedded SDK → handshake (dismiss skeleton)
 *   3. POST /salla/token-login with the Salla token → receive Nahla JWT
 *   4. Persist JWT to localStorage (same keys as SallaCallback.tsx)
 *   5. Navigate to /onboarding (new) or /overview (returning)
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

type Phase = 'init' | 'handshake' | 'login' | 'success' | 'error'

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

const SDK_URL = 'https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js'

function decodeJwtPayload(token: string): Record<string, unknown> {
  const parts = token.split('.')
  return JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
}

export default function SallaEmbedded() {
  const navigate = useNavigate()
  const [phase, setPhase] = useState<Phase>('init')
  const [statusText, setStatusText] = useState('جاري تهيئة الاتصال بسلة...')
  const [errorDetail, setErrorDetail] = useState('')
  const attemptedRef = useRef(false)

  const params   = new URLSearchParams(window.location.search)
  const sallaToken = params.get('token') || ''
  const storeId    = params.get('store_id') || ''
  const appId      = params.get('app_id') || ''

  // ── Salla SDK handshake ───────────────────────────────────────────────────
  const signalReady = useCallback(() => {
    const msg = {
      event: 'embedded::ready',
      payload: {},
      timestamp: Date.now(),
      source: 'embedded-app',
      metadata: { version: '0.2.4' },
    }
    try { window.parent.postMessage(msg, '*') } catch { /* ignore */ }
    try { window.parent.postMessage({ event: 'app.ready', type: 'app.ready' }, '*') } catch { /* ignore */ }
  }, [])

  const initSdkHandshake = useCallback(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sdk = (window as any).Salla?.embedded
    if (!sdk) { signalReady(); return }
    sdk.init({ debug: false })
      .then(() => { sdk.ready(); signalReady() })
      .catch(() => signalReady())
  }, [signalReady])

  // ── Load SDK script dynamically ───────────────────────────────────────────
  const loadSdk = useCallback((): Promise<void> => {
    return new Promise((resolve) => {
      if (document.querySelector(`script[src="${SDK_URL}"]`)) {
        resolve()
        return
      }
      const s = document.createElement('script')
      s.src = SDK_URL
      s.onload = () => resolve()
      s.onerror = () => resolve()
      document.head.appendChild(s)
    })
  }, [])

  // ── Persist session to localStorage ───────────────────────────────────────
  const persistSession = useCallback((data: LoginResponse) => {
    const jwt = data.access_token
    const claims = decodeJwtPayload(jwt)

    const staleKeys = [
      'nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
      'nahla_tenant_id', 'nahla_user_id',
    ]
    staleKeys.forEach(k => localStorage.removeItem(k))

    localStorage.setItem('nahla_auth',      '1')
    localStorage.setItem('nahla_token',     jwt)
    localStorage.setItem('nahla_role',      String(claims.role      || 'merchant'))
    localStorage.setItem('nahla_email',     String(claims.sub       || ''))
    localStorage.setItem('nahla_tenant_id', String(claims.tenant_id ?? ''))
    localStorage.setItem('nahla_user_id',   String(claims.user_id   ?? ''))

    if (data.store_name) {
      localStorage.setItem('nahla_salla_store_name', data.store_name)
      localStorage.setItem('nahla_store_name', data.store_name)
    }
    if (storeId) {
      localStorage.setItem('nahla_salla_store_id', storeId)
    }
  }, [storeId])

  // ── Token exchange + login ────────────────────────────────────────────────
  const doLogin = useCallback(async () => {
    if (!sallaToken) {
      setPhase('error')
      setErrorDetail('لم يتم العثور على رمز المصادقة من سلة')
      return
    }

    setPhase('login')
    setStatusText('جاري التحقق من هويتك...')

    try {
      const res = await fetch(`${API_BASE}/salla/token-login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: sallaToken, app_id: appId || undefined }),
      })

      const data: LoginResponse = await res.json()

      if (!data.access_token) {
        setPhase('error')
        setErrorDetail(data.detail || 'تعذّر التحقق من هويتك')
        return
      }

      persistSession(data)

      setPhase('success')
      setStatusText(
        data.is_new
          ? 'مرحباً! جاري إعداد حسابك...'
          : `مرحباً بعودتك ${data.store_name || ''}`
      )

      const dest = data.redirect_to || (data.is_new ? '/onboarding' : '/overview')
      setTimeout(() => navigate(dest, { replace: true }), 900)
    } catch {
      setPhase('error')
      setErrorDetail('تعذر الوصول إلى الخادم — تحقق من اتصالك بالإنترنت')
    }
  }, [sallaToken, appId, persistSession, navigate])

  // ── Main effect: SDK → handshake → login ──────────────────────────────────
  useEffect(() => {
    if (attemptedRef.current) return
    attemptedRef.current = true

    ;(async () => {
      setPhase('handshake')
      setStatusText('جاري الاتصال بسلة...')

      await loadSdk()
      initSdkHandshake()
      setTimeout(signalReady, 3000)

      await doLogin()
    })()
  }, [loadSdk, initSdkHandshake, signalReady, doLogin])

  // ── Retry handler ─────────────────────────────────────────────────────────
  const handleRetry = () => {
    attemptedRef.current = false
    setPhase('init')
    setStatusText('جاري إعادة المحاولة...')
    setErrorDetail('')
    attemptedRef.current = true
    doLogin()
  }

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center px-4 py-6"
      style={{
        fontFamily: "'Cairo', system-ui, sans-serif",
        background: '#0f172a',
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
              (e.target as HTMLImageElement).style.display = 'none'
              const fallback = document.getElementById('salla-fallback-emoji')
              if (fallback) fallback.style.display = 'block'
            }}
          />
          <span id="salla-fallback-emoji" className="hidden text-6xl absolute inset-0 flex items-center justify-center">
            🐝
          </span>
        </div>
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-black text-slate-100 tracking-tight">نحلة</h1>
          <span
            className="text-xs font-black px-2 py-0.5 rounded-md"
            style={{
              background: 'rgba(245,158,11,0.15)',
              border: '1px solid rgba(245,158,11,0.35)',
              boxShadow: '0 0 10px rgba(245,158,11,0.3)',
              color: '#f59e0b',
              letterSpacing: '0.5px',
            }}
          >
            AI
          </span>
        </div>
      </div>

      {/* Status card */}
      <div
        className="w-full max-w-sm rounded-2xl p-7"
        style={{
          background: 'rgba(255,255,255,0.03)',
          border: '1px solid rgba(245,158,11,0.2)',
          backdropFilter: 'blur(16px)',
        }}
      >
        {phase === 'error' ? (
          /* ── Error state ────────────────────────────────────── */
          <div className="text-center space-y-4">
            <div className="text-5xl">⚠️</div>
            <p className="text-white font-semibold text-base">تعذّر تسجيل الدخول</p>
            <p className="text-slate-400 text-sm leading-relaxed">{errorDetail}</p>
            <div className="flex flex-col gap-3 pt-2">
              <button
                onClick={handleRetry}
                className="w-full py-3 px-6 rounded-xl font-bold text-sm transition-all"
                style={{
                  background: '#f59e0b',
                  color: '#0f172a',
                  boxShadow: '0 4px 20px rgba(245,158,11,0.35)',
                }}
              >
                إعادة المحاولة
              </button>
              <a
                href="https://app.nahlah.ai/login"
                target="_blank"
                rel="noopener noreferrer"
                className="text-amber-400 text-xs hover:underline"
              >
                أو سجّل الدخول يدوياً
              </a>
            </div>
          </div>
        ) : phase === 'success' ? (
          /* ── Success state ──────────────────────────────────── */
          <div className="text-center space-y-4">
            <div className="text-5xl">✅</div>
            <p className="text-white font-semibold text-base">{statusText}</p>
            <p className="text-slate-400 text-sm">جاري تحويلك للوحة التحكم...</p>
          </div>
        ) : (
          /* ── Loading state ──────────────────────────────────── */
          <div className="text-center space-y-4">
            <div className="relative w-16 h-16 mx-auto">
              <div className="absolute inset-0 rounded-full border-4 border-amber-400/20" />
              <div className="absolute inset-0 rounded-full border-4 border-t-amber-400 animate-spin" />
              <span className="absolute inset-0 flex items-center justify-center text-2xl">🐝</span>
            </div>
            <p className="text-white font-semibold text-base">{statusText}</p>
            {storeId && (
              <p className="text-slate-500 text-xs font-mono">store: {storeId}</p>
            )}
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
