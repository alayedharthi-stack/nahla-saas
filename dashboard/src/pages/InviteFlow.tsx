/**
 * InviteFlow.tsx
 * --------------
 * Direct-invite onboarding for merchants arriving via /invite/:code
 *
 * Flow:
 *   1. Check existing Nahla session → enter dashboard immediately
 *   2. POST /api/invite/redeem with code → receive JWT → enter dashboard
 *   3. If code invalid → show clean error with link to /login
 *
 * Never shows a landing page. Never navigates to / or /landing.
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { API_BASE } from '../api/client'

type Phase = 'checking' | 'redeeming' | 'success' | 'error'

interface RedeemResponse {
  access_token: string
  tenant_id: number
  role: string
  email: string
  store_name?: string
  is_new?: boolean
  detail?: string
}

function decodePayload(token: string): Record<string, unknown> {
  const parts = token.split('.')
  return JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
}

function persistJwt(jwt: string, storeName?: string) {
  const claims = decodePayload(jwt)
  ;['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
    'nahla_tenant_id', 'nahla_user_id'].forEach(k => localStorage.removeItem(k))
  localStorage.setItem('nahla_auth',      '1')
  localStorage.setItem('nahla_token',     jwt)
  localStorage.setItem('nahla_role',      String(claims.role      ?? 'merchant'))
  localStorage.setItem('nahla_email',     String(claims.sub       ?? ''))
  localStorage.setItem('nahla_tenant_id', String(claims.tenant_id ?? ''))
  localStorage.setItem('nahla_user_id',   String(claims.user_id   ?? ''))
  if (storeName) {
    localStorage.setItem('nahla_store_name', storeName)
  }
}

export default function InviteFlow() {
  const { code }   = useParams<{ code: string }>()
  const navigate   = useNavigate()
  const bootedRef  = useRef(false)
  const [phase, setPhase]           = useState<Phase>('checking')
  const [statusText, setStatusText] = useState('جاري التحقق من الدعوة...')
  const [errorMsg, setErrorMsg]     = useState('')

  const enterDashboard = useCallback((dest: string) => {
    setPhase('success')
    setTimeout(() => navigate(dest, { replace: true }), 500)
  }, [navigate])

  const bootstrap = useCallback(async () => {
    // 1. Live session? Enter immediately.
    const stored = localStorage.getItem('nahla_token')
    if (stored) {
      try {
        const res = await fetch(`${API_BASE}/api/salla/session`, {
          headers: { Authorization: `Bearer ${stored}` },
          signal: AbortSignal.timeout(3000),
        })
        if (res.ok) {
          enterDashboard('/overview')
          return
        }
      } catch { /* fall through */ }
    }

    // 2. No live session — redeem the invite code.
    if (!code) {
      setPhase('error')
      setErrorMsg('رمز الدعوة مفقود أو غير صالح.')
      return
    }

    setPhase('redeeming')
    setStatusText('جاري تفعيل الدعوة...')

    try {
      const res = await fetch(`${API_BASE}/api/invite/redeem`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ code }),
        signal:  AbortSignal.timeout(5000),
      })
      const data: RedeemResponse = await res.json()

      if (!res.ok || !data.access_token) {
        setPhase('error')
        setErrorMsg(data.detail || 'رمز الدعوة غير صالح أو منتهي الصلاحية.')
        return
      }

      persistJwt(data.access_token, data.store_name)
      setStatusText(data.is_new ? 'مرحباً! جاري إعداد حسابك...' : `مرحباً بعودتك ${data.store_name ?? ''}`)
      enterDashboard(data.is_new ? '/onboarding' : '/overview')
    } catch {
      setPhase('error')
      setErrorMsg('تعذر الوصول إلى الخادم. تحقق من اتصالك وحاول مجدداً.')
    }
  }, [code, enterDashboard])

  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true
    bootstrap()
  }, [bootstrap])

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center px-4 py-8"
      style={{
        fontFamily:      "'Cairo', system-ui, sans-serif",
        background:      '#0f172a',
        backgroundImage: 'radial-gradient(ellipse 70% 50% at 50% 0%, rgba(245,158,11,0.07) 0%, transparent 70%)',
      }}
    >
      {/* Logo */}
      <div className="flex flex-col items-center mb-8">
        <img
          src="https://app.nahlah.ai/logo.png"
          alt="نحلة"
          className="w-16 h-16 object-contain mb-3"
          style={{ filter: 'drop-shadow(0 0 14px rgba(245,158,11,0.4))' }}
          onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
        />
        <h1 className="text-2xl font-black text-slate-100">نحلة AI</h1>
        <p className="text-slate-500 text-sm mt-1">مساعد المبيعات الذكي لمتجرك</p>
      </div>

      {/* Card */}
      <div
        className="w-full max-w-sm rounded-2xl p-7"
        style={{
          background:     'rgba(255,255,255,0.03)',
          border:         '1px solid rgba(245,158,11,0.18)',
          backdropFilter: 'blur(14px)',
        }}
      >
        {phase === 'error' ? (
          <div className="text-center space-y-4">
            <div className="text-5xl">⚠️</div>
            <p className="text-white font-semibold">تعذّر تفعيل الدعوة</p>
            <p className="text-slate-400 text-sm leading-relaxed">{errorMsg}</p>
            <a
              href="/login"
              className="block w-full py-3 rounded-xl font-bold text-sm text-center mt-3"
              style={{
                background: '#f59e0b',
                color:      '#0f172a',
                boxShadow:  '0 4px 18px rgba(245,158,11,0.3)',
              }}
            >
              تسجيل الدخول يدوياً
            </a>
          </div>
        ) : phase === 'success' ? (
          <div className="text-center space-y-4">
            <div className="text-5xl">✅</div>
            <p className="text-white font-semibold">{statusText}</p>
            <p className="text-slate-400 text-sm">جاري توجيهك للوحة التحكم...</p>
          </div>
        ) : (
          <div className="text-center space-y-5">
            {/* Pulse skeleton */}
            <div className="space-y-3">
              <div
                className="h-3 rounded-full animate-pulse mx-auto"
                style={{ background: 'rgba(245,158,11,0.12)', width: '55%' }}
              />
              <div
                className="h-2.5 rounded-full animate-pulse mx-auto"
                style={{ background: 'rgba(255,255,255,0.05)', width: '35%' }}
              />
            </div>
            <p className="text-slate-400 text-sm">{statusText}</p>
            {code && (
              <p className="text-slate-600 text-xs font-mono">
                code: {code.slice(0, 8)}…
              </p>
            )}
          </div>
        )}
      </div>

      <p className="mt-6 text-xs text-slate-700">بأيدي سعودية 100% 🇸🇦</p>
    </div>
  )
}
