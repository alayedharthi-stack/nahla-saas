// Defined locally to avoid circular dependency with api/client.ts
const API_BASE = import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai'

const AUTH_KEY          = 'nahla_auth'
const TOKEN_KEY         = 'nahla_token'
const ROLE_KEY          = 'nahla_role'
const IMPERSONATE_KEY   = 'nahla_impersonate'   // JSON: { token, storeName, adminToken }

export async function login(email: string, password: string): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    if (!res.ok) return false
    const data = await res.json()
    localStorage.setItem(AUTH_KEY,  '1')
    localStorage.setItem(TOKEN_KEY, data.access_token ?? '')
    localStorage.setItem(ROLE_KEY,  data.role ?? 'merchant')
    return true
  } catch {
    return false
  }
}

export function logout(): void {
  localStorage.removeItem(AUTH_KEY)
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ROLE_KEY)
}

export function isAuthenticated(): boolean {
  return localStorage.getItem(AUTH_KEY) === '1'
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function getRole(): string {
  return localStorage.getItem(ROLE_KEY) ?? 'merchant'
}

// Role hierarchy
// owner / super_admin → Platform Owner Dashboard
// staff              → Staff Dashboard
// merchant_admin / merchant_user / merchant → Merchant Dashboard

export function isAdmin(): boolean {
  const r = getRole()
  return r === 'admin' || r === 'owner' || r === 'super_admin'
}

export function isPlatformOwner(): boolean {
  const r = getRole()
  return r === 'owner' || r === 'super_admin' || r === 'admin'
}

export function isStaff(): boolean {
  return getRole() === 'staff'
}

export function isMerchant(): boolean {
  const r = getRole()
  return r === 'merchant' || r === 'merchant_admin' || r === 'merchant_user'
}

export function getDefaultRoute(): string {
  if (isPlatformOwner()) return '/admin'
  if (isStaff())         return '/overview'
  return '/overview'
}

// ── Impersonation helpers ──────────────────────────────────────────────────────

export interface ImpersonationInfo {
  storeName: string
  merchantEmail: string
  adminToken: string   // original admin token to restore on exit
}

export function startImpersonation(
  merchantToken: string,
  storeName: string,
  merchantEmail: string,
): void {
  const adminToken = localStorage.getItem(TOKEN_KEY) ?? ''
  localStorage.setItem(IMPERSONATE_KEY, JSON.stringify({ storeName, merchantEmail, adminToken }))
  localStorage.setItem(TOKEN_KEY, merchantToken)
  localStorage.setItem(ROLE_KEY, 'merchant')
}

export function stopImpersonation(): void {
  const raw = localStorage.getItem(IMPERSONATE_KEY)
  if (!raw) return
  const { adminToken } = JSON.parse(raw) as ImpersonationInfo & { adminToken: string }
  localStorage.setItem(TOKEN_KEY, adminToken)
  localStorage.setItem(ROLE_KEY, 'admin')
  localStorage.removeItem(IMPERSONATE_KEY)
}

export function getImpersonation(): (ImpersonationInfo & { adminToken: string }) | null {
  const raw = localStorage.getItem(IMPERSONATE_KEY)
  return raw ? JSON.parse(raw) : null
}

export function isImpersonating(): boolean {
  return !!localStorage.getItem(IMPERSONATE_KEY)
}

export async function register(
  email: string,
  password: string,
  storeName: string,
  phone: string = '',
  inviteToken: string = '',
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, store_name: storeName, phone, invite_token: inviteToken }),
    })
    const data = await res.json()
    if (!res.ok) return { ok: false, error: data.detail ?? 'فشل التسجيل' }
    localStorage.setItem(AUTH_KEY,  '1')
    localStorage.setItem(TOKEN_KEY, data.access_token ?? '')
    localStorage.setItem(ROLE_KEY,  data.role ?? 'merchant')
    return { ok: true }
  } catch {
    return { ok: false, error: 'تعذّر الاتصال بالخادم' }
  }
}
