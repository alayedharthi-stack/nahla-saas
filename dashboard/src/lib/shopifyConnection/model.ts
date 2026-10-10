/**
 * Shopify connection — pure client model (no package imports, CI-checkable).
 *
 * Mirrors the backend contract of ``backend/routers/shopify_connection.py``:
 * canonical shop domains, the exact authorize URL ``POST /start`` returns,
 * the secret-free status / summary shapes, and the fixed refusal codes.
 *
 * Nothing here formats provider or server text for display: errors are
 * projected to a closed set of codes that map to fixed UI copy.
 */
import { RETURN_MARKER_RE } from './returnCapture'

export const SHOP_SUFFIX = '.myshopify.com'
/** The only scope the foundation requests or accepts (read-only catalog). */
export const REQUESTED_SCOPE = 'read_products'
export const CALLBACK_PATH = '/merchant/integrations/shopify/callback'
const AUTHORIZE_PATH = '/admin/oauth/authorize'
const MAX_LABEL = 63
const SHOP_RE = /^[a-z0-9][a-z0-9-]*\.myshopify\.com$/
const HOST_RE = /^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/
const ASCII_RE = /^[\x21-\x7e]*$/

/** Same rules as ``services/shopify_connection/shop_domain.canonical_shop_domain``. */
export function canonicalShopDomain(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  if (!raw || raw.length > MAX_LABEL + SHOP_SUFFIX.length || !ASCII_RE.test(raw)) return null
  const value = raw.toLowerCase()
  if (!SHOP_RE.test(value)) return null
  const label = value.slice(0, -SHOP_SUFFIX.length)
  if (label.length > MAX_LABEL || label.endsWith('-') || label.startsWith('xn--')) return null
  return value
}

/**
 * Merchant input → canonical shop domain, or null. Accepts the bare store
 * name (``my-store``), the myshopify domain, or that domain as an https URL
 * with an optional trailing slash. Custom domains, paths, ports, credentials
 * and non-ASCII look-alikes are refused (the server re-validates anyway).
 */
export function normalizeShopInput(raw: string): string | null {
  let value = String(raw ?? '').trim()
  if (!value || value.length > 120) return null
  value = value.replace(/^https?:\/\//i, '')
  if (value.endsWith('/')) value = value.slice(0, -1)
  if (/[\s/?#@:\\]/.test(value)) return null
  if (!value.includes('.')) value = `${value}${SHOP_SUFFIX}`
  return canonicalShopDomain(value)
}

/**
 * True only for exactly the URL the foundation builds:
 * ``https://<shop>/admin/oauth/authorize?client_id&scope=read_products&redirect_uri&state``
 * for the shop the merchant asked for, with an https callback on the exact
 * callback path. Anything else is refused before navigation (no open redirect).
 */
export function isSafeAuthorizeUrl(raw: unknown, shop: string): boolean {
  if (typeof raw !== 'string' || raw.length > 2048 || !ASCII_RE.test(raw)) return false
  const expected = canonicalShopDomain(shop)
  if (!expected || expected !== shop) return false
  if (!raw.startsWith(`https://${expected}${AUTHORIZE_PATH}?`)) return false
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return false
  }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return false
  if (url.hostname !== expected || url.pathname !== AUTHORIZE_PATH || url.hash) return false
  const keys = Array.from(url.searchParams.keys())
  const allowed = ['client_id', 'scope', 'redirect_uri', 'state']
  if (keys.length !== allowed.length || !allowed.every((k) => keys.includes(k))) return false
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(url.searchParams.get('client_id') ?? '')) return false
  if (url.searchParams.get('scope') !== REQUESTED_SCOPE) return false
  if (!/^[A-Za-z0-9_-]{16,256}$/.test(url.searchParams.get('state') ?? '')) return false
  const redirect = url.searchParams.get('redirect_uri') ?? ''
  let callback: URL
  try {
    callback = new URL(redirect)
  } catch {
    return false
  }
  return (
    callback.protocol === 'https:'
    && !callback.username && !callback.password && !callback.port
    && HOST_RE.test(callback.hostname)
    && !/^\d+$/.test(callback.hostname.split('.').pop() ?? '')
    && redirect === `https://${callback.hostname}${CALLBACK_PATH}`
  )
}

// ── Status / summary ──────────────────────────────────────────────────────────

export type ConnectionStatus = 'active' | 'quarantined' | 'reauth_required' | 'disconnected' | 'uninstalled' | 'unknown'
const KNOWN_STATUSES = new Set(['active', 'quarantined', 'reauth_required', 'disconnected', 'uninstalled'])

export interface ShopifyConnectionSummary {
  shopDomain: string
  status: ConnectionStatus
  scopes: string[]
  connectedAt: string | null
  disconnectedAt: string | null
}

export interface ShopifyStatus {
  /** Server availability (flag, configuration, storage). The reason is operator-only and not kept. */
  available: boolean
  requestedScopes: string[]
  connections: ShopifyConnectionSummary[]
}

function asRecord(raw: unknown): Record<string, unknown> | null {
  return raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null
}

function isoOrNull(raw: unknown): string | null {
  return typeof raw === 'string' && raw.length <= 64 && !Number.isNaN(Date.parse(raw)) ? raw : null
}

function scopeList(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((s): s is string => typeof s === 'string' && /^[a-z_]{1,64}$/.test(s)) : []
}

export function normalizeSummary(raw: unknown): ShopifyConnectionSummary | null {
  const r = asRecord(raw)
  if (!r) return null
  const shop = canonicalShopDomain(r.shop_domain)
  if (!shop || shop !== r.shop_domain) return null
  const status = typeof r.status === 'string' && KNOWN_STATUSES.has(r.status) ? (r.status as ConnectionStatus) : 'unknown'
  return {
    shopDomain: shop,
    status,
    scopes: scopeList(r.scopes),
    connectedAt: isoOrNull(r.connected_at),
    disconnectedAt: isoOrNull(r.disconnected_at),
  }
}

export function normalizeStatus(raw: unknown): ShopifyStatus | null {
  const r = asRecord(raw)
  if (!r || typeof r.available !== 'boolean' || !Array.isArray(r.connections)) return null
  return {
    available: r.available,
    requestedScopes: scopeList(r.requested_scopes),
    connections: r.connections.map(normalizeSummary).filter((c): c is ShopifyConnectionSummary => c !== null),
  }
}

/** ``POST /complete`` → the connected summary, only for the exact success shape. */
export function parseCompleteResponse(raw: unknown): ShopifyConnectionSummary | null {
  const r = asRecord(raw)
  if (!r || r.status !== 'connected') return null
  const summary = normalizeSummary(r.connection)
  return summary && summary.status === 'active' ? summary : null
}

/** ``POST /disconnect`` → the tombstoned summary. */
export function parseDisconnectResponse(raw: unknown): ShopifyConnectionSummary | null {
  const r = asRecord(raw)
  return r ? normalizeSummary(r.connection) : null
}

/** ``POST /start`` → the authorize URL string (validated separately). */
export function parseStartResponse(raw: unknown): string | null {
  const r = asRecord(raw)
  return r && typeof r.authorize_url === 'string' ? r.authorize_url : null
}

export type ConnectionView = 'authorized' | 'verifying' | 'reconnect_required' | 'uninstalled' | 'disconnected' | 'unknown'

export function connectionView(c: ShopifyConnectionSummary): ConnectionView {
  switch (c.status) {
    case 'active': return 'authorized'
    case 'quarantined': return 'verifying'
    case 'reauth_required': return 'reconnect_required'
    case 'uninstalled': return 'uninstalled'
    case 'disconnected': return 'disconnected'
    default: return 'unknown'
  }
}

/** Local disconnect erases Nahlah's credentials; the server is idempotent for tombstones. */
export function canDisconnect(c: ShopifyConnectionSummary): boolean {
  return c.status === 'active' || c.status === 'quarantined' || c.status === 'reauth_required'
}

/** A new authorization for the same store (same-tenant reinstall). */
export function canReconnect(c: ShopifyConnectionSummary): boolean {
  return c.status === 'reauth_required' || c.status === 'disconnected' || c.status === 'uninstalled'
}

export type AuthorizationStep = 'done' | 'attention' | 'todo'

/**
 * Onboarding status. Only the authorization step has server state. Product
 * import does not exist in this release, so it is always reported as not
 * started — never inferred from the authorization.
 */
export function onboardingSteps(status: ShopifyStatus | null): { authorization: AuthorizationStep; productImport: 'not_started' } {
  const list = status?.connections ?? []
  const authorization: AuthorizationStep = list.some((c) => c.status === 'active')
    ? 'done'
    : list.some((c) => c.status === 'quarantined' || c.status === 'reauth_required')
      ? 'attention'
      : 'todo'
  return { authorization, productImport: 'not_started' }
}

// ── Errors → fixed codes ──────────────────────────────────────────────────────

/** Every code the backend may return for these routes (lifecycle, router, actor). */
export const KNOWN_ERROR_CODES: ReadonlySet<string> = new Set([
  'shop_unavailable', 'too_many_pending_authorizations', 'invalid_state', 'state_replayed', 'state_expired',
  'shop_mismatch', 'callback_mismatch', 'actor_revoked', 'superseded', 'invalid_completion', 'session_mismatch',
  'state_unreadable', 'exchange_in_progress', 'token_exchange_failed', 'scope_invalid', 'identity_unverified',
  'identity_mismatch', 'identity_conflict', 'not_found', 'unavailable', 'error',
  'callback_invalid', 'shop_invalid', 'shopify_connection_unavailable',
  'platform_session_refused', 'support_session_refused', 'role_not_permitted', 'session_claims_invalid',
  'session_revoked', 'session_unverifiable',
])

export type SafeFailure =
  /** Route answered 404 without a lifecycle code: the backend flag is off. */
  | { kind: 'feature_off' }
  /** No response, a timeout, or a 5xx without a known code: the outcome is unknown. */
  | { kind: 'uncertain' }
  /** 401: the dashboard session itself is not accepted. */
  | { kind: 'session' }
  | { kind: 'refused'; code: string }

/**
 * Project any thrown API error to a closed shape. Reads only the numeric
 * ``status`` and a ``code`` from the known set — never ``message``, never
 * provider detail — so nothing server- or provider-authored reaches the UI.
 */
export function projectApiFailure(err: unknown): SafeFailure {
  const e = err && typeof err === 'object' ? (err as { status?: unknown; code?: unknown }) : {}
  const status = typeof e.status === 'number' ? e.status : null
  const code = typeof e.code === 'string' && KNOWN_ERROR_CODES.has(e.code) ? e.code : null
  if (status === null) return { kind: 'uncertain' }
  if (status === 401) return { kind: 'session' }
  if (status === 404 && (code === null || code === 'not_found')) {
    // The router's own 404 ({"error": "not_found"}) is the flag-off answer;
    // callers that can receive a lifecycle not_found (disconnect) re-read status.
    return { kind: 'feature_off' }
  }
  if (code === 'shopify_connection_unavailable') return { kind: 'refused', code: 'unavailable' }
  if (code) return { kind: 'refused', code }
  if (status >= 500) return { kind: 'uncertain' }
  return { kind: 'refused', code: 'error' }
}

/** Closed set of message keys; each maps to fixed copy in ``shopifyConnectionLabels``. */
export type MessageKey =
  | 'sessionChanged' | 'merchantOnly' | 'expired' | 'alreadyUsed' | 'shopUnavailable' | 'inProgress'
  | 'tooMany' | 'superseded' | 'shopInvalid' | 'notConfirmed' | 'scopeInvalid' | 'needsSupport'
  | 'unavailable' | 'uncertain' | 'unexpectedResponse' | 'generic'

const MESSAGE_FOR_CODE: Record<string, MessageKey> = {
  session_mismatch: 'sessionChanged', session_revoked: 'sessionChanged', session_claims_invalid: 'sessionChanged',
  session_unverifiable: 'sessionChanged', actor_revoked: 'sessionChanged',
  support_session_refused: 'merchantOnly', platform_session_refused: 'merchantOnly', role_not_permitted: 'merchantOnly',
  state_expired: 'expired', invalid_completion: 'expired',
  state_replayed: 'alreadyUsed',
  shop_unavailable: 'shopUnavailable',
  exchange_in_progress: 'inProgress',
  too_many_pending_authorizations: 'tooMany',
  superseded: 'superseded',
  shop_invalid: 'shopInvalid',
  token_exchange_failed: 'notConfirmed', identity_unverified: 'notConfirmed', state_unreadable: 'notConfirmed',
  callback_mismatch: 'notConfirmed', invalid_state: 'notConfirmed', shop_mismatch: 'notConfirmed',
  callback_invalid: 'notConfirmed',
  scope_invalid: 'scopeInvalid',
  identity_mismatch: 'needsSupport', identity_conflict: 'needsSupport',
  unavailable: 'unavailable', not_found: 'unavailable',
}

export function messageKeyForCode(code: string): MessageKey {
  return MESSAGE_FOR_CODE[code] ?? 'generic'
}

export function messageKeyForFailure(f: SafeFailure): MessageKey {
  switch (f.kind) {
    case 'feature_off': return 'unavailable'
    case 'uncertain': return 'uncertain'
    case 'session': return 'sessionChanged'
    default: return messageKeyForCode(f.code)
  }
}

// ── Telemetry / error-report URL redaction ────────────────────────────────────

/**
 * For error reporting: a URL on any Shopify connection path (any case,
 * percent-encoding or slash form, as early capture recognises it) or carrying
 * a Shopify return key anywhere loses its query and fragment. Other URLs are
 * returned unchanged.
 */
export function redactShopifyUrl(url: string): string {
  if (typeof url !== 'string') return url
  const cut = url.search(/[?#]/)
  if (cut === -1) return url
  const base = url.slice(0, cut)
  let decoded = base
  try {
    decoded = decodeURIComponent(base)
  } catch {
    /* malformed escape: inspect the raw form */
  }
  const onShopifyPath = decoded.toLowerCase().replace(/\/{2,}/g, '/').includes('/integrations/shopify')
  return onShopifyPath || RETURN_MARKER_RE.test(url) ? base : url
}
