/**
 * ZidCallback.tsx
 * ---------------
 * Landing page after a merchant installs/opens Nahla from the Zid store.
 *
 * The backend redirects here with:
 *   ?token=JWT&redirect=/overview  (or /onboarding for new merchants)
 *
 * This page:
 *   1. Reads the JWT from the URL
 *   2. Clears any old session from localStorage
 *   3. Persists the new merchant session
 *   4. Redirects to the appropriate dashboard page
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export default function ZidCallback() {
  const navigate = useNavigate()
  const [error, setError]     = useState('')
  const [storeName, setStoreName] = useState('')

  useEffect(() => {
    const params   = new URLSearchParams(window.location.search)
    const token    = params.get('token')
    const redirect = params.get('redirect') || '/overview'

    if (!token) {
      const reason = params.get('reason') || 'missing_token'
      setError(reason)
      setTimeout(() => navigate('/login?from=zid&error=' + reason, { replace: true }), 3000)
      return
    }

    try {
      const parts   = token.split('.')
      const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))

      // Clear any existing session before storing the new merchant's JWT
      localStorage.removeItem('nahla_token')
      localStorage.removeItem('nahla_role')
      localStorage.removeItem('nahla_email')
      localStorage.removeItem('nahla_tenant_id')
      localStorage.removeItem('nahla_salla_store_id')
      localStorage.removeItem('nahla_salla_store_name')

      // Persist the Zid merchant session
      localStorage.setItem('nahla_auth',      '1')
      localStorage.setItem('nahla_token',     token)
      localStorage.setItem('nahla_role',      payload.role      || 'merchant')
      localStorage.setItem('nahla_email',     payload.sub       || '')
      localStorage.setItem('nahla_tenant_id', String(payload.tenant_id ?? ''))
      localStorage.setItem('nahla_platform',  'zid')

      setStoreName(payload.store_name || '')

      // Route to onboarding (new) or overview (returning)
      setTimeout(() => navigate(redirect, { replace: true }), 900)
    } catch {
      setError('invalid_token')
      setTimeout(() => navigate('/login?from=zid&error=invalid_token', { replace: true }), 3000)
    }
  }, [navigate])

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center bg-slate-900 gap-5"
    >
      {error ? (
        <div className="text-center space-y-3 px-6">
          <div className="text-4xl">⚠️</div>
          <p className="text-white font-semibold">حدث خطأ أثناء ربط المتجر</p>
          <p className="text-slate-400 text-sm">جاري تحويلك لصفحة تسجيل الدخول...</p>
          <code className="text-amber-400 text-xs">{error}</code>
        </div>
      ) : (
        <div className="text-center space-y-4">
          <div className="relative w-16 h-16 mx-auto">
            <div className="absolute inset-0 rounded-full border-4 border-violet-400/20" />
            <div className="absolute inset-0 rounded-full border-4 border-t-violet-400 animate-spin" />
            <span className="absolute inset-0 flex items-center justify-center text-2xl">🐝</span>
          </div>
          <div>
            <p className="text-white font-semibold text-lg">جاري تسجيل الدخول...</p>
            {storeName ? (
              <p className="text-slate-400 text-sm mt-1">
                تم ربط متجر <span className="text-violet-400 font-medium">{storeName}</span> بنحلة AI
              </p>
            ) : (
              <p className="text-slate-400 text-sm mt-1">تم ربط متجرك بنحلة AI بنجاح</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
