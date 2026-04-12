// ── Shared API client ─────────────────────────────────────────────────────────
// All dashboard API modules import apiCall from here so the auth token
// is automatically attached to every request.

import { getToken, getTenantId, logout } from '../auth'

// In production the frontend talks directly to the backend domain.
// This avoids nginx acting as a proxy (which caused POST failures on Railway Edge).
// CORS on the backend already allows https://api.nahlah.ai as origin.
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai'

// Error codes that mean the session is truly invalid and the user must re-login.
// Do NOT logout on every 401 — some endpoints may return 401 for reasons unrelated
// to the session (e.g. support-access checks, impersonation guards).
const SESSION_EXPIRED_CODES = new Set([
  'missing_token',
  'invalid_token',
  'no_tenant_claim',
])

function classifyNetworkError(error: unknown): string {
  const msg = error instanceof Error ? error.message : String(error ?? '')
  const lowered = msg.toLowerCase()

  if (lowered.includes('failed to fetch') || lowered.includes('load failed') || lowered.includes('networkerror')) {
    return 'تعذر الوصول إلى الخادم. قد يكون السبب CORS أو انقطاع الشبكة أو خطأ مؤقت في API.'
  }
  return msg || 'حدث خطأ غير متوقع أثناء الاتصال بالخادم.'
}

export async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const token    = getToken()
  const tenantId = getTenantId()

  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      cache: 'no-store',
      mode: 'cors',
      headers: {
        'Content-Type': 'application/json',
        ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options?.headers ?? {}),
      },
      ...options,
    })
  } catch (error) {
    throw new Error(classifyNetworkError(error))
  }

  // 401 — only logout when the backend signals the session/token itself is invalid.
  // A 401 for other reasons (e.g. permission checks) should surface as a normal error.
  if (res.status === 401) {
    let code = ''
    try {
      const body = await res.clone().json()
      code = body?.code ?? ''
    } catch { /* ignore */ }

    if (SESSION_EXPIRED_CODES.has(code)) {
      // True session expiry — clear state and send user to login
      logout()
      window.location.href = '/login'
      throw new Error('انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً')
    }

    // Other 401 (e.g. impersonation blocked) — surface as a normal error
    let detail = 'غير مصرح'
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }

  if (!res.ok) {
    let detail = `API error ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* ignore */ }
    throw new Error(detail)
  }

  return res.json() as Promise<T>
}
