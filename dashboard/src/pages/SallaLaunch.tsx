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
import { clearSallaEmbeddedSession, isSallaRoutingBlockDetail } from '../lib/embeddedLogin'
import { useEmbeddedLocale } from '../hooks/useEmbeddedLocale'

// Signal Salla host frame immediately — this page may be loaded in the top
// context after breaking out of the iframe so the signal is a no-op there,
// but keeps consistency with other Salla pages.
;(function immediateReady() {
  try { window.parent.postMessage({ type: 'app.ready' }, '*') } catch { /* cross-origin */ }
})()

function clearEmbeddedSession(): void {
  clearSallaEmbeddedSession()
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const parts = token.split('.')
    return JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return {}
  }
}

export default function SallaLaunch() {
  const navigate = useNavigate()
  const { isRTL, t } = useEmbeddedLocale()
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  useEffect(() => {
    const params    = new URLSearchParams(window.location.search)
    const token     = params.get('token') || ''
    const nextPath  = params.get('next')  || '/overview'

    if (!token) {
      setErrorMsg(t.launch.errorInvalidLink)
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
          store_id?:    string | null
        }

        const claims   = decodeJwtPayload(data.access_token)
        const storeId  = String(data.store_id || claims.store_id || '').trim()
        const prevStore = (() => {
          try { return localStorage.getItem('nahla_salla_store_id') || '' } catch { return '' }
        })()

        if (prevStore && storeId && prevStore !== storeId) {
          console.warn(
            '[SallaLaunch] store switch detected — clearing previous session | %s → %s',
            prevStore, storeId,
          )
        }

        clearEmbeddedSession()

        try {
          localStorage.setItem('nahla_auth',             '1')
          localStorage.setItem('nahla_token',            data.access_token)
          localStorage.setItem('nahla_tenant_id',        String(data.tenant_id))
          localStorage.setItem('nahla_email',            data.email)
          localStorage.setItem('nahla_role',             data.role)
          localStorage.setItem('nahla_salla_store_name', data.store_name)
          localStorage.setItem('nahla_salla_embedded',   '1')
          if (storeId) {
            localStorage.setItem('nahla_salla_store_id', storeId)
          }
          if (claims.user_id != null) {
            localStorage.setItem('nahla_user_id', String(claims.user_id))
          }
        } catch {
          // localStorage blocked (private browsing?) — navigate anyway
        }

        console.info(
          '[SallaLaunch] session persisted | tenant_id=%s store_id=%s email=%s role=%s next=%s',
          data.tenant_id, storeId || '(missing)', data.email, data.role, nextPath,
        )

        navigate(nextPath, { replace: true })
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e)
        if (isSallaRoutingBlockDetail(msg)) {
          clearEmbeddedSession()
          console.warn('[SallaLaunch] routing block — session cleared | detail=%s', msg)
        }
        const looksLocalized = msg.length > 0 && /[\u0600-\u06FFa-zA-Z]/.test(msg)
        setErrorMsg(looksLocalized ? msg : t.launch.errorGeneric)
      }
    })()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Loading state ────────────────────────────────────────────────────────────
  if (!errorMsg) {
    return (
      <div
        dir={isRTL ? 'rtl' : 'ltr'}
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
          alt={t.app.brand}
          width={56}
          height={56}
          style={{ borderRadius: 14, boxShadow: '0 4px 14px rgba(0,0,0,0.10)' }}
          onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
        />
        <p style={{ fontSize: 15, fontWeight: 700, color: '#0f172a', margin: 0 }}>
          {t.launch.loadingTitle}
        </p>
        <p style={{ fontSize: 12, color: '#94a3b8', margin: 0 }}>
          {t.launch.loadingSubtitle}
        </p>
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
      dir={isRTL ? 'rtl' : 'ltr'}
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
        {t.launch.errorTitle}
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
        {t.launch.btnBackToSalla}
      </button>
    </div>
  )
}
