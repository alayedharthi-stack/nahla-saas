/**
 * Embedded Salla login timing + retry helpers (testable without React).
 */
export const EMBEDDED_SESSION_TIMEOUT_MS = 12_000
export const EMBEDDED_LOGIN_TIMEOUT_MS   = 28_000
/** Must exceed login timeout × max attempts + session slack. */
export const EMBEDDED_WATCHDOG_TIMEOUT_MS = 52_000
export const EMBEDDED_LOGIN_MAX_ATTEMPTS  = 2

export const SALLA_STORE_LINK_REQUIRED_CODE = 'salla_store_link_required'
export const SALLA_OAUTH_SYNC_ACTION = 'oauth_sync'
export const SALLA_EMBEDDED_OAUTH_START_PATH =
  '/api/salla/oauth/start?embedded_reconcile=1'

/** Backend routing guard failures — clear session and block dashboard entry. */
export const SALLA_ROUTING_BLOCK_DETAILS = new Set([
  'store_not_registered',
  'store_tenant_mismatch',
  'merchant_identity_not_canonical',
  'store_id_required',
])

export const SALLA_ROUTING_BLOCK_CODES = new Set([
  SALLA_STORE_LINK_REQUIRED_CODE,
])

export interface SallaStoreLinkPayload {
  detail?: string
  code?: string
  identity_source?: string
  has_canonical_store_id?: boolean
  next_action?: string
  oauth_start_path?: string
  merchant_account_id?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
}

/** Normalize FastAPI error bodies (string detail or structured dict). */
export function parseSallaStoreLinkPayload(data: unknown): SallaStoreLinkPayload | null {
  if (!isRecord(data)) return null

  const nested = isRecord(data.detail) ? data.detail : data
  if (!isRecord(nested)) return null

  const code = typeof nested.code === 'string' ? nested.code : ''
  const detail = typeof nested.detail === 'string' ? nested.detail : ''
  if (
    code !== SALLA_STORE_LINK_REQUIRED_CODE
    && detail !== 'merchant_identity_not_canonical'
    && nested.next_action !== SALLA_OAUTH_SYNC_ACTION
  ) {
    return null
  }

  return {
    detail: detail || undefined,
    code: code || undefined,
    identity_source:
      typeof nested.identity_source === 'string' ? nested.identity_source : undefined,
    has_canonical_store_id:
      typeof nested.has_canonical_store_id === 'boolean'
        ? nested.has_canonical_store_id
        : undefined,
    next_action:
      typeof nested.next_action === 'string' ? nested.next_action : undefined,
    oauth_start_path:
      typeof nested.oauth_start_path === 'string' ? nested.oauth_start_path : undefined,
    merchant_account_id:
      typeof nested.merchant_account_id === 'string'
        ? nested.merchant_account_id
        : undefined,
  }
}

export function extractApiErrorDetail(data: unknown): string {
  if (!isRecord(data)) return ''
  if (typeof data.detail === 'string') return data.detail
  if (isRecord(data.detail) && typeof data.detail.detail === 'string') {
    return data.detail.detail
  }
  return ''
}

export function isSallaRoutingBlockDetail(detail: unknown): boolean {
  if (typeof detail !== 'string' || !detail) return false
  return SALLA_ROUTING_BLOCK_DETAILS.has(detail)
}

export function isSallaRoutingBlockResponse(data: unknown): boolean {
  const detail = extractApiErrorDetail(data)
  if (isSallaRoutingBlockDetail(detail)) return true
  const payload = parseSallaStoreLinkPayload(data)
  if (payload?.code && SALLA_ROUTING_BLOCK_CODES.has(payload.code)) return true
  return false
}

export function isSallaStoreLinkRequired(data: unknown): boolean {
  const payload = parseSallaStoreLinkPayload(data)
  return (
    payload?.code === SALLA_STORE_LINK_REQUIRED_CODE
    || (
      payload?.detail === 'merchant_identity_not_canonical'
      && payload?.next_action === SALLA_OAUTH_SYNC_ACTION
    )
  )
}

export function resolveOauthReconcileStartUrl(
  apiBase: string,
  payload?: SallaStoreLinkPayload | null,
): string {
  const path = payload?.oauth_start_path || SALLA_EMBEDDED_OAUTH_START_PATH
  const base = apiBase.replace(/\/$/, '')
  return path.startsWith('http') ? path : `${base}${path}`
}

export function clearSallaEmbeddedSession(): void {
  ;[
    'nahla_auth', 'nahla_token', 'nahla_role', 'nahla_email',
    'nahla_tenant_id', 'nahla_user_id', 'nahla_salla_store_id',
    'nahla_salla_store_name', 'nahla_store_name',
    'nahla_salla_is_new', 'nahla_salla_wa_connected', 'nahla_salla_embedded',
  ].forEach(k => {
    try { localStorage.removeItem(k) } catch { /* private mode */ }
  })
  try {
    for (let i = sessionStorage.length - 1; i >= 0; i -= 1) {
      const key = sessionStorage.key(i)
      if (key && (key.startsWith('nahla_salla') || key.startsWith('nahla_'))) {
        sessionStorage.removeItem(key)
      }
    }
  } catch { /* private mode */ }
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
