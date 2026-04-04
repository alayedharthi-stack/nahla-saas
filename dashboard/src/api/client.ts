// ── Shared API client ─────────────────────────────────────────────────────────
// All dashboard API modules import apiCall from here so the auth token
// is automatically attached to every request.

import { getToken, logout } from '../auth'

// In production the frontend talks directly to the backend domain.
// This avoids nginx acting as a proxy (which caused POST failures on Railway Edge).
// CORS on the backend already allows https://api.nahlaai.com as origin.
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai'

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
    logout()
    window.location.href = '/login'
    throw new Error('Session expired')
  }

  if (!res.ok) {
    let detail = `API error ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* ignore parse errors */ }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}
