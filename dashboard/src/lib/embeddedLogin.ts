/**
 * Embedded Salla login timing + retry helpers (testable without React).
 */
export const EMBEDDED_SESSION_TIMEOUT_MS = 12_000
export const EMBEDDED_LOGIN_TIMEOUT_MS   = 28_000
/** Must exceed login timeout × max attempts + session slack. */
export const EMBEDDED_WATCHDOG_TIMEOUT_MS = 52_000
export const EMBEDDED_LOGIN_MAX_ATTEMPTS  = 2

/** Backend routing guard failures — clear session and block dashboard entry. */
export const SALLA_ROUTING_BLOCK_DETAILS = new Set([
  'store_not_registered',
  'store_tenant_mismatch',
  'merchant_identity_not_canonical',
  'store_id_required',
])

export function isSallaRoutingBlockDetail(detail: unknown): boolean {
  if (typeof detail !== 'string' || !detail) return false
  return SALLA_ROUTING_BLOCK_DETAILS.has(detail)
}

export function clearSallaEmbeddedSession(): void {
  ;[
    'nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
    'nahla_tenant_id', 'nahla_user_id', 'nahla_salla_store_id',
    'nahla_salla_store_name', 'nahla_store_name',
    'nahla_salla_is_new', 'nahla_salla_wa_connected',
  ].forEach(k => {
    try { localStorage.removeItem(k) } catch { /* private mode */ }
  })
}

export function shouldRetryEmbeddedLogin(error: unknown, attempt: number): boolean {
  if (attempt >= EMBEDDED_LOGIN_MAX_ATTEMPTS) return false
  return error instanceof DOMException && error.name === 'AbortError'
}

export function describeLoginFailure(error: unknown): string {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return 'client_timeout_abort'
  }
  if (error instanceof Error) return error.message
  return String(error)
}
