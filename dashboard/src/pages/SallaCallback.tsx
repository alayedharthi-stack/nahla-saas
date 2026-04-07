/**
 * SallaCallback.tsx
 * -----------------
 * Landing page after a merchant installs Nahla from the Salla store.
 *
 * The backend redirects here with:
 *   ?token=JWT&status=connected&store=STORE_ID&name=STORE_NAME&new=1
 *
 * This page:
 *   1. Reads the JWT from the URL
 *   2. Persists it to localStorage (same keys used by the rest of the app)
 *   3. Redirects to /onboarding (new merchant) or /overview (returning merchant)
 *   4. Shows a friendly loading screen while doing so
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function SallaCallback() {
  const navigate = useNavigate()
  const [error, setError] = useState('')

  useEffect(() => {
    const params   = new URLSearchParams(window.location.search)
    const token    = params.get('token')
    const status   = params.get('status')
    const isNew    = params.get('new') === '1'
    const store    = params.get('store')   || ''
    const name     = params.get('name')    || ''

    if (!token || status !== 'connected') {
      const reason = params.get('reason') || 'oauth_failed'
      setError(reason)
      // Redirect to login after 3 s so the merchant can try manually
      setTimeout(() => navigate('/login?from=salla&error=' + reason, { replace: true }), 3000)
      return
    }

    // Decode minimal JWT payload (base64 middle part) to extract claims
    try {
      const parts   = token.split('.')
      const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))

      // Persist the session — same localStorage keys used by auth.ts
      localStorage.setItem('nahla_auth',      '1')           // required by isAuthenticated()
      localStorage.setItem('nahla_token',     token)
      localStorage.setItem('nahla_role',      payload.role      || 'merchant')
      localStorage.setItem('nahla_email',     payload.sub       || '')
      localStorage.setItem('nahla_tenant_id', String(payload.tenant_id ?? ''))
      if (store) localStorage.setItem('nahla_salla_store_id', store)
      if (name)  localStorage.setItem('nahla_salla_store_name', name)

      // Route: new merchants → onboarding, returning → overview
      const dest = isNew ? '/onboarding' : '/overview'
      setTimeout(() => navigate(dest, { replace: true }), 800)
    } catch (e) {
      setError('invalid_token')
      setTimeout(() => navigate('/login?from=salla&error=invalid_token', { replace: true }), 3000)
    }
  }, [navigate])

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center bg-slate-900 gap-5"
    >
      {error ? (
        /* ── Error state ─────────────────────────────────────── */
        <div className="text-center space-y-3 px-6">
          <div className="text-4xl">⚠️</div>
          <p className="text-white font-semibold">حدث خطأ أثناء ربط المتجر</p>
          <p className="text-slate-400 text-sm">جاري تحويلك لصفحة تسجيل الدخول...</p>
          <code className="text-amber-400 text-xs">{error}</code>
        </div>
      ) : (
        /* ── Loading state ───────────────────────────────────── */
        <div className="text-center space-y-4">
          <div className="relative w-16 h-16 mx-auto">
            <div className="absolute inset-0 rounded-full border-4 border-amber-400/20" />
            <div className="absolute inset-0 rounded-full border-4 border-t-amber-400 animate-spin" />
            <span className="absolute inset-0 flex items-center justify-center text-2xl">🐝</span>
          </div>
          <div>
            <p className="text-white font-semibold text-lg">جاري تسجيل الدخول...</p>
            <p className="text-slate-400 text-sm mt-1">تم ربط متجرك بنحلة AI بنجاح</p>
          </div>
        </div>
      )}
    </div>
  )
}
