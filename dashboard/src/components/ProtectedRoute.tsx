import { useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import {
  bootstrapAuthSession,
  getApiBase,
  getRole,
  getTenantId,
  getToken,
  installSessionRefreshLoop,
  isAuthenticated,
  isImpersonating,
  isPlatformStaffRole,
  logout,
} from '../auth'
import type { ReactNode } from 'react'

/**
 * Routes whose UI is rooted in the platform-owner scope.
 * Anything outside this set is treated as a merchant-scoped route and is
 * forbidden for owners that are NOT impersonating a merchant — opening such
 * routes would issue merchant-scoped API calls with the owner's JWT, whose
 * `tenant_id` claim points at the platform's demo tenant by convention and
 * would therefore leak that tenant's data into the owner UI.
 */
const OWNER_ALLOWED_PREFIXES = [
  '/admin',
  '/merchants',
  '/system-status',
  '/settings/security',
] as const

function isOwnerAllowedPath(pathname: string): boolean {
  return OWNER_ALLOWED_PREFIXES.some(
    p => pathname === p || pathname.startsWith(`${p}/`),
  )
}

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const location = useLocation()
  const [bootState, setBootState] = useState<'loading' | 'ok' | 'denied'>('loading')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (!isAuthenticated() || !getToken()) {
        if (!cancelled) setBootState('denied')
        return
      }
      const ok = await bootstrapAuthSession()
      if (!cancelled) setBootState(ok ? 'ok' : 'denied')
    })()
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (bootState !== 'ok') return
    return installSessionRefreshLoop()
  }, [bootState])

  useEffect(() => {
    if (bootState !== 'ok') return
    // eslint-disable-next-line no-console
    console.info('[auth] session bootstrap', {
      pathname: location.pathname,
      role:     getRole(),
      tenantId: getTenantId(),
      apiBase:  getApiBase(),
    })
  }, [bootState, location.pathname])

  if (bootState === 'loading') {
    return (
      <div className="min-h-dvh flex items-center justify-center bg-slate-50 text-slate-500 text-sm">
        جارٍ استعادة الجلسة…
      </div>
    )
  }

  if (bootState === 'denied') {
    logout()
    return <Navigate to="/landing" replace />
  }

  const role        = getRole()
  const isOwner     = isPlatformStaffRole(role)
  const wantsAdmin  =
    location.pathname === '/admin' || location.pathname.startsWith('/admin/')

  if (wantsAdmin && !isOwner) {
    return <Navigate to="/overview" replace />
  }

  if (isOwner && !isImpersonating() && !isOwnerAllowedPath(location.pathname)) {
    return <Navigate to="/admin" replace />
  }

  return <>{children}</>
}
