/**
 * Shopify catalog import — pure client model (no package imports, CI-checkable).
 *
 * "Imported" means one committed, complete snapshot in Nahlah's read-only
 * Shopify copy (the mirror). It is never inferred: only the server's
 * ``state === 'imported'`` with complete evidence shows it. Inventory is
 * reported separately — a products-only import (``scope_missing``) never
 * implies that inventory was read. Server and provider text is never shown:
 * every reason maps to a fixed message key.
 */

export type ImportState = 'not_started' | 'importing' | 'imported' | 'error' | 'unknown'

export interface ImportEvidence {
  completedAt: string
  apiVersion: string
  products: number
  variants: number
  currentInstallation: boolean
  inventory:
    | { mode: 'per_location'; locations: number; levels: number }
    | { mode: 'scope_missing'; locations: null; levels: null }
}

export interface ImportEntry {
  shop: string
  connectionStatus: string
  state: ImportState
  reasonKey: ImportReasonKey | null
  canStart: boolean
  lastSuccess: ImportEvidence | null
}

export interface ImportStatus {
  available: boolean
  entries: ImportEntry[]
}

/** Every failure / refusal code the backend may report for the import. */
export const IMPORT_REASON_CODES: ReadonlySet<string> = new Set([
  'connection_unavailable', 'connection_changed', 'interrupted', 'credential_unavailable', 'credentials_rejected',
  'credential_churn', 'scope_missing', 'scope_denied', 'identity_mismatch', 'shop_unavailable',
  'api_version_mismatch', 'throttled', 'upstream_unavailable', 'query_cost_exceeded', 'graphql_error',
  'response_too_large', 'invalid_response', 'deadline_exceeded', 'invalid_identity', 'duplicate_identity',
  'identity_conflict', 'pagination_loop', 'catalog_too_large', 'source_changed_during_import',
  'traversal_inconsistent', 'storage_unavailable', 'internal_error',
  // start refusals
  'not_found', 'import_in_progress', 'actor_revoked',
])

export type ImportReasonKey =
  | 'connection' | 'permission' | 'tooLarge' | 'changed' | 'interrupted' | 'unavailable' | 'inProgress'
  | 'sessionChanged' | 'generic'

const REASON_KEY: Record<string, ImportReasonKey> = {
  connection_unavailable: 'connection', connection_changed: 'connection', not_found: 'connection',
  identity_mismatch: 'connection', shop_unavailable: 'connection',
  credential_unavailable: 'permission', credentials_rejected: 'permission', credential_churn: 'permission',
  scope_missing: 'permission', scope_denied: 'permission',
  catalog_too_large: 'tooLarge',
  source_changed_during_import: 'changed', traversal_inconsistent: 'changed',
  interrupted: 'interrupted',
  throttled: 'unavailable', upstream_unavailable: 'unavailable', deadline_exceeded: 'unavailable',
  storage_unavailable: 'unavailable',
  import_in_progress: 'inProgress',
  actor_revoked: 'sessionChanged',
}

/** A known code → its message key; anything else → ``generic``. */
export function reasonKey(code: unknown): ImportReasonKey | null {
  if (code === null || code === undefined) return null
  if (typeof code !== 'string' || !IMPORT_REASON_CODES.has(code)) return 'generic'
  return REASON_KEY[code] ?? 'generic'
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function count(v: unknown): number | null {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null
}

function parseEvidence(raw: unknown): ImportEvidence | null {
  const r = asRecord(raw)
  const inv = asRecord(r?.inventory)
  if (!r || !inv) return null
  const products = count(r.products)
  const variants = count(r.variants)
  if (typeof r.completed_at !== 'string' || typeof r.api_version !== 'string' || products === null
      || variants === null || typeof r.current_installation !== 'boolean') return null
  let inventory: ImportEvidence['inventory']
  if (inv.mode === 'per_location') {
    const locations = count(inv.locations)
    const levels = count(inv.levels)
    if (locations === null || levels === null) return null
    inventory = { mode: 'per_location', locations, levels }
  } else if (inv.mode === 'scope_missing' && inv.locations === null && inv.levels === null) {
    inventory = { mode: 'scope_missing', locations: null, levels: null }
  } else {
    return null
  }
  return {
    completedAt: r.completed_at, apiVersion: r.api_version, products, variants,
    currentInstallation: r.current_installation, inventory,
  }
}

const STATES: ReadonlySet<string> = new Set(['not_started', 'importing', 'imported', 'error'])

function parseEntry(raw: unknown): ImportEntry | null {
  const r = asRecord(raw)
  const imp = asRecord(r?.import)
  if (!r || !imp || typeof r.shop_domain !== 'string' || typeof r.connection_status !== 'string') return null
  const lastSuccess = imp.last_success === null ? null : parseEvidence(imp.last_success)
  let state: ImportState = typeof imp.state === 'string' && STATES.has(imp.state) ? (imp.state as ImportState) : 'unknown'
  // "Imported" needs its own committed evidence from the current installation.
  if (state === 'imported' && !(lastSuccess && lastSuccess.currentInstallation)) state = 'unknown'
  return {
    shop: r.shop_domain,
    connectionStatus: r.connection_status,
    state,
    reasonKey: state === 'error' ? (reasonKey(imp.reason) ?? 'generic') : null,
    canStart: imp.can_start === true && r.connection_status === 'active' && state !== 'importing',
    lastSuccess,
  }
}

/** ``GET /catalog-import/status`` → normalized view, or null when malformed. */
export function parseImportStatus(raw: unknown): ImportStatus | null {
  const r = asRecord(raw)
  if (!r || typeof r.available !== 'boolean' || !Array.isArray(r.connections)) return null
  const entries: ImportEntry[] = []
  for (const c of r.connections) {
    const entry = parseEntry(c)
    if (entry) entries.push(entry)
  }
  return { available: r.available, entries }
}

export type ImportFailure = { kind: 'feature_off' } | { kind: 'uncertain' } | { kind: 'session' } | { kind: 'refused'; key: ImportReasonKey }

/** Reads only the numeric ``status`` and a known ``code`` — never a message. */
export function projectImportFailure(err: unknown): ImportFailure {
  const e = err && typeof err === 'object' ? (err as { status?: unknown; code?: unknown }) : {}
  const status = typeof e.status === 'number' ? e.status : null
  const code = typeof e.code === 'string' ? e.code : null
  if (status === null) return { kind: 'uncertain' }
  if (status === 401) return { kind: 'session' }
  // The router answers 404 while either backend flag is off; start is only
  // offered for shops the status route listed, so a 404 is treated the same.
  if (status === 404) return { kind: 'feature_off' }
  if (status === 403) return { kind: 'refused', key: 'sessionChanged' }
  if (code && IMPORT_REASON_CODES.has(code)) return { kind: 'refused', key: reasonKey(code) ?? 'generic' }
  if (status >= 500) return { kind: 'uncertain' }
  return { kind: 'refused', key: 'generic' }
}

/** The ``data-step`` of the import step for one shop (never inferred from authorization). */
export function importStepValue(entry: ImportEntry | null): ImportState {
  return entry ? entry.state : 'not_started'
}
