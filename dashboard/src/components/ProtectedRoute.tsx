import { Navigate } from 'react-router-dom'
import { isAuthenticated, getToken, logout } from '../auth'
import type { ReactNode } from 'react'

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  // Both conditions must be true: the auth flag AND a non-empty JWT token.
  // A missing token means the session is from the pre-JWT era — force re-login.
  if (!isAuthenticated() || !getToken()) {
    logout() // clear any stale flags
    return <Navigate to="/login" replace />
  }
  return <>{children}</>
}
