/**
 * Embedded Salla login timing + retry helpers (testable without React).
 */
export const EMBEDDED_SESSION_TIMEOUT_MS = 12_000
export const EMBEDDED_LOGIN_TIMEOUT_MS   = 28_000
/** Must exceed login timeout × max attempts + session slack. */
export const EMBEDDED_WATCHDOG_TIMEOUT_MS = 52_000
export const EMBEDDED_LOGIN_MAX_ATTEMPTS  = 2

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
