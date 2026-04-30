/**
 * SallaCallback.tsx
 * -----------------
 * Landing page after a merchant installs Nahla from the Salla store.
 *
 * The backend redirects here with:
 *   ?token=JWT&status=connected&store=STORE_ID&name=STORE_NAME&new=1
 *
 * IMPORTANT — Salla policy compliance:
 *   We do NOT auto-navigate to /app/entry on first install.  Salla expects
 *   the merchant to explicitly press the "استخدام التطبيق" button inside
 *   their dashboard before the embedded app opens.  Auto-jumping into the
 *   mini-dashboard at this stage bypasses that flow and risks rejection.
 *
 *   Instead we:
 *     1. Persist the session (token + tenant + store_id) to localStorage so
 *        the next iframe load is logged-in.
 *     2. Show a friendly "تم التثبيت" screen with a single CTA:
 *          "العودة إلى لوحة سلة" → window.top → s.salla.sa
 *     3. Provide a small secondary link for power users who explicitly want
 *        to enter Nahla now (rare — mainly internal testing).
 */
import { useEffect, useState } from 'react'

const SALLA_DASHBOARD_URL: string =
  (import.meta.env.VITE_SALLA_DASHBOARD_URL as string | undefined) ||
  'https://salla.sa/dashboard'

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

    // Common metadata writes — happen for BOTH "fresh JWT" and
    // "existing localStorage JWT" paths.
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
    } else {
      // ── Path B: no token in URL — relies on JWT already in localStorage
      // from the earlier /salla/token-login call (OAuth re-grant from iframe).
      const haveToken = localStorage.getItem('nahla_token')
      if (!haveToken) {
        console.warn('[SallaCallback] no token in URL AND no token in localStorage')
        setError('session_lost')
        return
      }
      console.log('[SallaCallback] no URL token — using existing localStorage session', { store })
    }

    // Strip the token from the URL (no back-button replay).
    try {
      window.history.replaceState(null, '', window.location.pathname)
    } catch { /* noop */ }

    console.log('[SallaCallback] install complete — waiting for "استخدام التطبيق" click in Salla',
      { isNew: newFlag, waConnected, store })
    // ↑ NO navigate(), NO window.location.href — Salla policy: merchant must
    //   press "استخدام التطبيق" themselves to open the app.
  }, [])

  const goToSalla = () => {
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
            onClick={goToSalla}
            className="mt-3 inline-flex items-center gap-2 bg-amber-500 hover:bg-amber-600 text-slate-900 font-bold text-sm px-5 py-2.5 rounded-xl transition-colors"
          >
            العودة إلى سلة
          </button>
        </div>
      ) : (
        /* ── Install-success state — gated CTA, no auto-navigate ─── */
        <div className="text-center space-y-5 max-w-md">
          <div className="text-5xl">✅</div>
          <div>
            <p className="text-white font-bold text-xl">
              {isNew ? 'تم تثبيت نحلة بنجاح!' : 'تم تجديد ربط نحلة بنجاح!'}
            </p>
            {storeName && (
              <p className="text-slate-300 text-sm mt-1">
                المتجر: <span className="text-amber-400 font-semibold">{storeName}</span>
              </p>
            )}
          </div>

          <div className="bg-slate-800/60 border border-amber-400/20 rounded-2xl p-5 text-right space-y-3">
            <p className="text-amber-300 text-sm font-bold flex items-center gap-2">
              <span>📌</span>
              الخطوة التالية
            </p>
            <ol className="text-slate-300 text-sm leading-relaxed space-y-2 list-decimal list-inside">
              <li>ارجع إلى لوحة تحكم متجرك في سلة</li>
              <li>اذهب إلى قسم <span className="text-amber-400 font-semibold">«تطبيقاتي»</span></li>
              <li>
                اضغط على زر <span className="text-amber-400 font-semibold">«استخدام التطبيق»</span> بجانب نحلة
              </li>
            </ol>
          </div>

          <button
            onClick={goToSalla}
            className="w-full inline-flex items-center justify-center gap-2 bg-amber-500 hover:bg-amber-400 text-slate-900 font-bold text-base px-5 py-3.5 rounded-xl transition-all hover:-translate-y-0.5"
            style={{ boxShadow: '0 4px 24px rgba(245,158,11,0.35)' }}
          >
            العودة إلى لوحة سلة ↗
          </button>

          <p className="text-slate-500 text-[11px]">
            تم حفظ جلستك. عند ضغط «استخدام التطبيق» في سلة، ستفتح لوحة نحلة مباشرةً.
          </p>
        </div>
      )}
    </div>
  )
}
