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
 *   2. Show a confirmation screen telling the merchant the install
 *      succeeded and that the app is now visible in their Salla dashboard.
 *
 * IMPORTANT — we DO NOT auto-redirect anywhere:
 *   - https://salla.sa/dashboard often shows "المتجر مغلق" for stores in
 *     setup mode, which would be a confusing dead-end.
 *   - We don't know the merchant's specific store subdomain so we can't
 *     deep-link to their admin /apps page.
 *   - Salla's own UI surfaces the app + "استخدام التطبيق" button
 *     automatically once the merchant returns to their dashboard.
 *   The merchant will close this tab / use their browser back button to
 *   return to Salla, or click the "اذهب إلى تطبيقاتي" link below.
 */
import { useEffect, useState } from 'react'

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

    // Persist Salla store metadata for the next iframe load.
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
    // Path B: no token in URL — JWT already in localStorage from earlier
    // /salla/token-login call.  Nothing else to do.

    // Strip the token from the URL (no back-button replay).
    try {
      window.history.replaceState(null, '', window.location.pathname)
    } catch { /* noop */ }

    console.log('[SallaCallback] install OK — waiting for merchant to return to Salla',
      { isNew: newFlag, waConnected, store })
  }, [])

  const tryClose = () => {
    // window.close() only works for windows opened via JS or with a single
    // history entry — it silently no-ops otherwise.  We try it and fall
    // back to history.back().
    try { window.close() } catch { /* noop */ }
    try { window.history.back() } catch { /* noop */ }
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
        </div>
      ) : (
        /* ── Success state — clear, no auto-redirect ─────────── */
        <div className="text-center space-y-5 max-w-md">
          <div className="text-6xl">✅</div>
          <div>
            <p className="text-white font-bold text-2xl leading-snug">
              {isNew ? 'تم تثبيت نحلة بنجاح!' : 'تم تجديد الربط بنجاح!'}
            </p>
            {storeName && (
              <p className="text-slate-300 text-sm mt-2">
                المتجر: <span className="text-amber-400 font-semibold">{storeName}</span>
              </p>
            )}
          </div>

          <div className="bg-slate-800/60 border border-amber-400/20 rounded-2xl p-5 text-right">
            <p className="text-amber-300 text-sm font-bold mb-2 flex items-center gap-2">
              <span>📌</span>
              للبدء باستخدام نحلة:
            </p>
            <p className="text-slate-200 text-sm leading-relaxed">
              عُد إلى متجرك في سلة، وستجد نحلة الآن في قسم
              <span className="text-amber-400 font-bold"> «تطبيقاتي» </span>
              مع زر
              <span className="text-amber-400 font-bold"> «استخدام التطبيق» </span>
              جاهزاً للضغط.
            </p>
          </div>

          <button
            onClick={tryClose}
            className="text-slate-400 hover:text-slate-300 text-xs font-semibold underline underline-offset-4 transition-colors"
          >
            إغلاق هذه الصفحة
          </button>
        </div>
      )}
    </div>
  )
}
