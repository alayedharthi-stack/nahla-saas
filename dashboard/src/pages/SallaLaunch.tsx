/**
 * SallaLaunch.tsx — /app/salla/launch
 * ─────────────────────────────────────
 * Consumes a short-lived launch token issued by
 * POST /salla/session/launch-dashboard on the backend.
 *
 * Flow:
 *   1. Read ?token= from URL.
 *   2. POST /salla/session/resolve-launch  { token }
 *   3. Save the returned full-lifetime JWT in localStorage (same keys the
 *      rest of the app uses: nahla_token, nahla_tenant_id, nahla_email …).
 *   4. Replace the URL (removing the token for security).
 *   5. Navigate to ?next= (default /overview).
 *
 * Error handling:
 *   If the token is missing, expired, or rejected by the backend the page
 *   shows a friendly Arabic error message.
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

// Signal Salla host frame immediately — this page may be loaded in the top
// context after breaking out of the iframe so the signal is a no-op there,
// but keeps consistency with other Salla pages.
;(function immediateReady() {
  try { window.parent.postMessage({ type: 'app.ready' }, '*') } catch { /* cross-origin */ }
})()

export default function SallaLaunch() {
  const navigate = useNavigate()
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  useEffect(() => {
    const params    = new URLSearchParams(window.location.search)
    const token     = params.get('token') || ''
    const nextPath  = params.get('next')  || '/overview'

    if (!token) {
      setErrorMsg('رابط الدخول غير صالح أو منتهي الصلاحية، حاول فتح التطبيق من سلة مجدداً.')
      return
    }

    // Remove the token from the URL immediately so it isn't visible or
    // bookmarked by the user, while keeping other params intact.
    const cleanParams = new URLSearchParams(params)
    cleanParams.delete('token')
    const cleanSearch = cleanParams.toString()
    const cleanUrl    = window.location.pathname + (cleanSearch ? `?${cleanSearch}` : '')
    window.history.replaceState(null, '', cleanUrl)

    // Exchange the short-lived launch token for a full session token.
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE}/salla/session/resolve-launch`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ token }),
        })

        if (!res.ok) {
          const err = await res.json().catch(() => ({})) as Record<string, unknown>
          throw new Error(String(err?.detail || `HTTP ${res.status}`))
        }

        const data = await res.json() as {
          access_token: string
          tenant_id:    number
          email:        string
          role:         string
          store_name:   string
        }

        // Persist the full session the same way the rest of the app expects.
        try {
          localStorage.setItem('nahla_token',            data.access_token)
          localStorage.setItem('nahla_tenant_id',        String(data.tenant_id))
          localStorage.setItem('nahla_email',            data.email)
          localStorage.setItem('nahla_role',             data.role)
          localStorage.setItem('nahla_salla_store_name', data.store_name)
          localStorage.setItem('nahla_salla_embedded',   '1')
        } catch {
          // localStorage blocked (private browsing?) — navigate anyway
        }

        // Navigate to the requested destination (replaces this transient page
        // so the user cannot "go back" to it).
        navigate(nextPath, { replace: true })
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e)
        // If the backend returned the Arabic error string, show it directly.
        const isArabic = /[\u0600-\u06FF]/.test(msg)
        setErrorMsg(
          isArabic
            ? msg
            : 'تعذر تسجيل الدخول من سلة، حاول فتح التطبيق مرة أخرى.',
        )
      }
    })()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Loading state ────────────────────────────────────────────────────────────
  if (!errorMsg) {
    return (
      <div
        dir="rtl"
        style={{
          minHeight:      '100dvh',
          display:        'flex',
          flexDirection:  'column',
          alignItems:     'center',
          justifyContent: 'center',
          gap:            16,
          background:     '#f9fafb',
          fontFamily:     'system-ui, -apple-system, sans-serif',
        }}
      >
        <img
          src="/logo.png"
          alt="Nahla"
          width={56}
          height={56}
          style={{ borderRadius: 14, boxShadow: '0 4px 14px rgba(0,0,0,0.10)' }}
          onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
        />
        <p style={{ fontSize: 15, fontWeight: 700, color: '#0f172a', margin: 0 }}>
          جارٍ تسجيل الدخول…
        </p>
        <p style={{ fontSize: 12, color: '#94a3b8', margin: 0 }}>
          سيتم توجيهك تلقائياً خلال لحظات
        </p>
        {/* Spinner */}
        <div
          style={{
            width:         32,
            height:        32,
            border:        '3px solid #f1f5f9',
            borderTop:     '3px solid #f59e0b',
            borderRadius:  '50%',
            animation:     'spin 0.8s linear infinite',
          }}
        />
        <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
      </div>
    )
  }

  // ── Error state ───────────────────────────────────────────────────────────────
  return (
    <div
      dir="rtl"
      style={{
        minHeight:      '100dvh',
        display:        'flex',
        flexDirection:  'column',
        alignItems:     'center',
        justifyContent: 'center',
        gap:            16,
        padding:        24,
        background:     '#f9fafb',
        fontFamily:     'system-ui, -apple-system, sans-serif',
        textAlign:      'center',
      }}
    >
      <span style={{ fontSize: 40 }}>⚠️</span>
      <p style={{ fontSize: 15, fontWeight: 700, color: '#0f172a', margin: 0 }}>
        تعذر تسجيل الدخول
      </p>
      <p style={{ fontSize: 13, color: '#64748b', margin: 0, maxWidth: 340, lineHeight: 1.6 }}>
        {errorMsg}
      </p>
      <button
        onClick={() => window.location.href = 'https://app.nahlah.ai/app/salla'}
        style={{
          marginTop:    8,
          padding:      '12px 24px',
          borderRadius: 12,
          fontSize:     14,
          fontWeight:   700,
          background:   '#f59e0b',
          color:        '#0f172a',
          border:       'none',
          cursor:       'pointer',
          fontFamily:   'inherit',
        }}
      >
        العودة إلى سلة
      </button>
    </div>
  )
}
