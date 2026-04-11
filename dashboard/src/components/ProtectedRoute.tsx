import { Navigate, useLocation } from 'react-router-dom'
import { getRole, getToken, isAuthenticated, isPlatformStaffRole, logout } from '../auth'
import type { ReactNode } from 'react'

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const location = useLocation()
  // Both conditions must be true: the auth flag AND a non-empty JWT token.
  // A missing token means the session is from the pre-JWT era — force re-login.
  if (!isAuthenticated() || !getToken()) {
    logout() // clear any stale flags
    return <Navigate to="/landing" replace />
  }

  const role = getRole()
  const wantsAdminSurface =
    location.pathname === '/admin' || location.pathname.startsWith('/admin/')
  if (wantsAdminSurface && !isPlatformStaffRole(role)) {
    return <Navigate to="/overview" replace />
  }
  return <>{children}</>
}
