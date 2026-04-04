// Defined locally to avoid circular dependency with api/client.ts
const API_BASE = import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai'

const AUTH_KEY  = 'nahla_auth'
const TOKEN_KEY = 'nahla_token'
const ROLE_KEY  = 'nahla_role'

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

export function isAdmin(): boolean {
  return getRole() === 'admin'
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
