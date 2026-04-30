/**
 * SallaCallback.tsx
 * -----------------
 * Landing page after a merchant installs Nahla from the Salla store.
 *
 * The backend redirects here with:
 *   ?token=JWT&status=connected&store=STORE_ID&name=STORE_NAME&new=1
 *
 * Behaviour:
 *   1. Persist the session (JWT + store metadata) to localStorage so the
 *      next iframe load is pre-authenticated.
 *   2. Show a brief 'تم التثبيت بنجاح' confirmation.
 *   3. Auto-redirect back to the merchant's Salla dashboard after ~2 s so
 *      Salla's own UI can surface the 'استخدام التطبيق' button — that
 *      button is the merchant's natural entry point and we should not
 *      duplicate or pre-empt it.
 */
import { useEffect, useState } from 'react'

const SALLA_DASHBOARD_URL: string =
  (import.meta.env.VITE_SALLA_DASHBOARD_URL as string | undefined) ||
  'https://salla.sa/dashboard'

const AUTO_REDIRECT_MS = 2200

export default function SallaCallback() {
  const [error,     setError]     = useState('')
  const [storeName, setStoreName] = useState('')
  const [isNew,     setIsNew]     = useState(false)

  useEffect(() => {
    const params      = new URLSearchParams(window.location.search)
    const token       = params.get('token')
    const status      = params.get('status')
    const newFlag     = params.get('new') === '1'
    const waConnected = params.get('wa_connected') === '1'
    const store       = params.get('store') || ''
    const name        = params.get('name')  || ''

    if (status !== 'connected') {
      const reason = params.get('reason') || 'oauth_failed'
      setError(reason)
      return
    }

    setIsNew(newFlag)
    setStoreName(name)

    // Common metadata writes — happen for BOTH 'fresh JWT in URL' and
    // 'existing localStorage JWT' paths.
    try {
      if (store) localStorage.setItem('nahla_salla_store_id',   store)
      if (name) {
        localStorage.setItem('nahla_salla_store_name', name)
        localStorage.setItem('nahla_store_name', name)
      }
      localStorage.setItem('nahla_salla_embedded',     '1')
      localStorage.setItem('nahla_salla_is_new',       newFlag ? '1' : '0')
      localStorage.setItem('nahla_salla_wa_connected', waConnected ? '1' : '0')
    } catch { /* localStorage blocked */ }

    // ── Path A: fresh JWT in URL (first-install via Salla App Store) ────────
    if (token) {
      try {
        const parts   = token.split('.')
        const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))

        ;['nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
          'nahla_tenant_id', 'nahla_user_id'].forEach(k => localStorage.removeItem(k))

        localStorage.setItem('nahla_auth',      '1')
        localStorage.setItem('nahla_token',     token)
        localStorage.setItem('nahla_role',      String(payload.role      || 'merchant'))
        localStorage.setItem('nahla_email',     String(payload.sub       || ''))
        localStorage.setItem('nahla_tenant_id', String(payload.tenant_id ?? ''))
        localStorage.setItem('nahla_user_id',   String(payload.user_id   ?? ''))

        console.log('[SallaCallback] persisted fresh JWT from URL', { isNew: newFlag, store })
      } catch (e) {
        console.error('[SallaCallback] invalid token in URL:', e)
        setError('invalid_token')
        return
      }
    }
    // Path B: no token in URL — the existing JWT in localStorage from the
    // earlier /salla/token-login call remains valid. Nothing to do here.

    // Strip the token from the URL (no back-button replay).
    try {
      window.history.replaceState(null, '', window.location.pathname)
    } catch { /* noop */ }

    console.log('[SallaCallback] install OK — auto-redirecting to Salla dashboard in', AUTO_REDIRECT_MS, 'ms')

    // Auto-redirect back to Salla so the merchant's normal flow continues:
    // Salla's UI shows the "استخدام التطبيق" button → click it → iframe
    // loads /app/salla → mini-dashboard. We never insert ourselves into
    // that flow with a manual 'go back' click.
    const t = setTimeout(() => {
      try {
        if (window.top) {
          window.top.location.href = SALLA_DASHBOARD_URL
          return
        }
      } catch { /* cross-origin */ }
      window.location.href = SALLA_DASHBOARD_URL
    }, AUTO_REDIRECT_MS)

    return () => clearTimeout(t)
  }, [])

  const goToSallaNow = () => {
    try {
      if (window.top) {
        window.top.location.href = SALLA_DASHBOARD_URL
        return
      }
    } catch { /* cross-origin */ }
    window.location.href = SALLA_DASHBOARD_URL
  }

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col items-center justify-center bg-slate-900 gap-6 px-5"
    >
      {error ? (
        /* ── Error state ─────────────────────────────────────── */
        <div className="text-center space-y-3 max-w-sm">
          <div className="text-4xl">⚠️</div>
          <p className="text-white font-semibold">حدث خطأ أثناء ربط المتجر</p>
          <p className="text-slate-400 text-sm">يمكنك إعادة المحاولة من متجر تطبيقات سلة.</p>
          <code className="text-amber-400 text-xs block bg-slate-800 rounded px-2 py-1">{error}</code>
          <button
            onClick={goToSallaNow}
            className="mt-3 inline-flex items-center gap-2 bg-amber-500 hover:bg-amber-600 text-slate-900 font-bold text-sm px-5 py-2.5 rounded-xl transition-colors"
          >
            العودة إلى سلة
          </button>
        </div>
      ) : (
        /* ── Brief success state — auto-redirects back to Salla ─── */
        <div className="text-center space-y-4 max-w-sm">
          <div className="text-5xl">✅</div>
          <p className="text-white font-bold text-lg">
            {isNew ? 'تم تثبيت نحلة بنجاح!' : 'تم تجديد الربط بنجاح!'}
          </p>
          {storeName && (
            <p className="text-slate-300 text-sm">
              المتجر: <span className="text-amber-400 font-semibold">{storeName}</span>
            </p>
          )}
          <div className="flex items-center justify-center gap-2 text-slate-400 text-sm pt-1">
            <div
              className="w-4 h-4 rounded-full border-2 border-amber-400/30 border-t-amber-400 animate-spin"
              aria-hidden
            />
            <span>جاري إعادتك إلى سلة...</span>
          </div>
          <button
            onClick={goToSallaNow}
            className="text-amber-400 hover:text-amber-300 text-xs font-semibold underline underline-offset-4 transition-colors"
          >
            تخطي الانتظار
          </button>
        </div>
      )}
    </div>
  )
}
