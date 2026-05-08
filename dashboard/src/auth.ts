// Defined locally to avoid circular dependency with api/client.ts
const API_BASE = import.meta.env.VITE_API_BASE ?? 'https://api.nahlah.ai'

const AUTH_KEY        = 'nahla_auth'
const TOKEN_KEY       = 'nahla_token'
const ROLE_KEY        = 'nahla_role'
const EMAIL_KEY       = 'nahla_email'
const TENANT_ID_KEY   = 'nahla_tenant_id'
const USER_ID_KEY     = 'nahla_user_id'
const STORE_NAME_KEY  = 'nahla_store_name'
const IMPERSONATE_KEY = 'nahla_impersonate'   // JSON: { token, storeName, adminToken }

/** Decode the middle (payload) segment of a JWT without verifying the signature. */
function _decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(b64))
  } catch {
    return {}
  }
}

/** Persist a session from any token + optional metadata. */
function _persistSession(
  token: string,
  overrides: {
    role?: string
    email?: string
    tenant_id?: number | string
    user_id?: number | string
    store_name?: string
  } = {},
): void {
  const payload = _decodeJwtPayload(token)
  localStorage.setItem(AUTH_KEY,       '1')
  localStorage.setItem(TOKEN_KEY,      token)
  localStorage.setItem(ROLE_KEY,       String(overrides.role      ?? payload.role      ?? 'merchant'))
  localStorage.setItem(EMAIL_KEY,      String(overrides.email     ?? payload.sub       ?? ''))
  localStorage.setItem(TENANT_ID_KEY,  String(overrides.tenant_id ?? payload.tenant_id ?? ''))
  localStorage.setItem(USER_ID_KEY,    String(overrides.user_id   ?? payload.user_id   ?? ''))
  if (overrides.store_name) {
    localStorage.setItem(STORE_NAME_KEY, overrides.store_name)
  }
}

export async function login(email: string, password: string): Promise<boolean> {
  // Client-side timeout: when the API hangs (Railway cold start, network
  // glitch, deploy in flight), give up after 20s so the UI never gets
  // stuck on "جارٍ تسجيل الدخول…" indefinitely. The user gets a clear
  // "invalid credentials / connection error" instead of a frozen spinner.
  const controller = new AbortController()
  const timeoutId  = setTimeout(() => controller.abort(), 20_000)
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      signal: controller.signal,
    })
    if (!res.ok) return false
    const data = await res.json()
    _persistSession(data.access_token ?? '', {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    return true
  } catch (e) {
    // AbortError → timeout. Surface a console warning so we can correlate
    // with backend logs.
    if (e instanceof Error && e.name === 'AbortError') {
      console.warn('[auth] login request timed out after 20s')
    }
    return false
  } finally {
    clearTimeout(timeoutId)
  }
}

export function logout(): void {
  localStorage.removeItem(AUTH_KEY)
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ROLE_KEY)
  localStorage.removeItem(EMAIL_KEY)
  localStorage.removeItem(TENANT_ID_KEY)
  localStorage.removeItem(USER_ID_KEY)
  localStorage.removeItem(STORE_NAME_KEY)
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

export function isPlatformStaffRole(role: string): boolean {
  return ['admin', 'owner', 'super_admin', 'platform_admin', 'platform_owner'].includes(role)
}

/** Returns the email (sub) of the logged-in user. */
export function getEmail(): string {
  return localStorage.getItem(EMAIL_KEY) ?? ''
}

/** Returns the store name if known (set during Salla/Zid OAuth). */
export function getStoreName(): string {
  return (
    localStorage.getItem('nahla_salla_store_name') ||
    localStorage.getItem('nahla_zid_store_name')   ||
    localStorage.getItem(STORE_NAME_KEY)            ||
    ''
  )
}

/** Returns the tenant_id from the current session (read from JWT claim, cached in localStorage). */
export function getTenantId(): number | null {
  const raw = localStorage.getItem(TENANT_ID_KEY)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return isNaN(n) ? null : n
}

/** Returns the user_id from the current session (read from JWT claim, cached in localStorage). */
export function getUserId(): number | null {
  const raw = localStorage.getItem(USER_ID_KEY)
  if (!raw) return null
  const n = parseInt(raw, 10)
  return isNaN(n) ? null : n
}

// Role hierarchy
// owner / super_admin → Platform Owner Dashboard
// staff              → Staff Dashboard
// merchant_admin / merchant_user / merchant → Merchant Dashboard

export function isAdmin(): boolean {
  return isPlatformStaffRole(getRole())
}

export function isPlatformOwner(): boolean {
  return isPlatformStaffRole(getRole())
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
  // Save the admin's full session state before switching
  const adminToken    = localStorage.getItem(TOKEN_KEY)     ?? ''
  const adminRole     = localStorage.getItem(ROLE_KEY)      ?? 'admin'
  const adminEmail    = localStorage.getItem(EMAIL_KEY)     ?? ''
  const adminTenantId = localStorage.getItem(TENANT_ID_KEY) ?? ''
  const adminUserId   = localStorage.getItem(USER_ID_KEY)   ?? ''
  localStorage.setItem(IMPERSONATE_KEY, JSON.stringify({
    storeName, merchantEmail, adminToken, adminRole, adminEmail, adminTenantId, adminUserId,
  }))
  // Switch to merchant session
  _persistSession(merchantToken, { role: 'merchant', email: merchantEmail, store_name: storeName })
}

export function stopImpersonation(): void {
  const raw = localStorage.getItem(IMPERSONATE_KEY)
  if (!raw) return
  const saved = JSON.parse(raw) as ImpersonationInfo & {
    adminToken: string; adminRole: string; adminEmail: string
    adminTenantId: string; adminUserId: string
  }
  // Restore the admin session
  _persistSession(saved.adminToken, {
    role:      saved.adminRole,
    email:     saved.adminEmail,
    tenant_id: saved.adminTenantId,
    user_id:   saved.adminUserId,
  })
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
    _persistSession(data.access_token ?? '', {
      role:      data.role,
      tenant_id: data.tenant_id,
      user_id:   data.user_id,
    })
    return { ok: true }
  } catch {
    return { ok: false, error: 'تعذّر الاتصال بالخادم' }
  }
}
