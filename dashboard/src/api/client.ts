// ── Shared API client ─────────────────────────────────────────────────────────
// All dashboard API modules import apiCall from here so the auth token
// is automatically attached to every request.

import { getToken } from '../auth'

export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

export async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken()
  const res = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': '1',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options?.headers ?? {}),
    },
    ...options,
  })

  // Token expired or missing — clear auth state and send user to login
  if (res.status === 401) {
    import('../auth').then(({ logout }) => logout())
    window.location.href = '/login'
    throw new Error('Session expired')
  }

  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}
