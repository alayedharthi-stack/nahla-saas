import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { bootstrapAuthSession, getDefaultRoute, isAuthenticated, getToken } from '../auth'

/**
 * PWA home-screen opens `/` — this gate restores the session then routes
 * merchants to the dashboard instead of always sending them to /landing.
 */
export default function RootRedirect() {
  const [target, setTarget] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (isAuthenticated() && getToken()) {
        const ok = await bootstrapAuthSession()
        if (!cancelled) setTarget(ok ? getDefaultRoute() : '/login')
        return
      }
      if (!cancelled) setTarget('/landing')
    })()
    return () => { cancelled = true }
  }, [])

  if (!target) {
    return (
      <div className="min-h-dvh flex items-center justify-center bg-slate-50 text-slate-500 text-sm">
        جارٍ استعادة الجلسة…
      </div>
    )
  }

  return <Navigate to={target} replace />
}
