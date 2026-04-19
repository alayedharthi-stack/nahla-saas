import { Navigate, useLocation } from 'react-router-dom'
import {
  getRole,
  getToken,
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
  '/merchants',          // owner-side tenant directory
  '/system-status',
] as const

function isOwnerAllowedPath(pathname: string): boolean {
  return OWNER_ALLOWED_PREFIXES.some(
    p => pathname === p || pathname.startsWith(`${p}/`),
  )
}

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const location = useLocation()

  // Both conditions must be true: the auth flag AND a non-empty JWT token.
  // A missing token means the session is from the pre-JWT era — force re-login.
  if (!isAuthenticated() || !getToken()) {
    logout() // clear any stale flags
    return <Navigate to="/landing" replace />
  }

  const role        = getRole()
  const isOwner     = isPlatformStaffRole(role)
  const wantsAdmin  =
    location.pathname === '/admin' || location.pathname.startsWith('/admin/')

  // Merchants must never reach the admin surface.
  if (wantsAdmin && !isOwner) {
    return <Navigate to="/overview" replace />
  }

  // Tenant-isolation guard:
  // Platform owners that are NOT actively impersonating a merchant must stay
  // inside owner-scoped routes. This prevents the owner-token-with-tenant_id=1
  // class of leaks where merchant pages render the demo tenant's data inside
  // the owner UI.
  if (isOwner && !isImpersonating() && !isOwnerAllowedPath(location.pathname)) {
    return <Navigate to="/admin" replace />
  }

  return <>{children}</>
}
