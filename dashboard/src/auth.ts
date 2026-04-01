const AUTH_KEY = 'nahla_auth'
const ADMIN_EMAIL    = 'admin@nahlaai.com'
const ADMIN_PASSWORD = 'nahla-admin-2026'

export function login(email: string, password: string): boolean {
  if (email === ADMIN_EMAIL && password === ADMIN_PASSWORD) {
    localStorage.setItem(AUTH_KEY, '1')
    return true
  }
  return false
}

export function logout(): void {
  localStorage.removeItem(AUTH_KEY)
}

export function isAuthenticated(): boolean {
  return localStorage.getItem(AUTH_KEY) === '1'
}
