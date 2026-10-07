/**
 * Decide what the /catalog page may say after the Meta catalog-consent
 * callback returned with ``#meta_catalog_consent=<code>``.
 *
 * The fragment is user-controlled, so it is only a hint. A success
 * ("connected") is claimed only when a fresh, authenticated status read shows
 * an active authorization for exactly the approved catalog and business.
 * Anything else that hints success is shown as "not confirmed". Failure codes
 * claim no success and are shown as warnings.
 */
import type { MetaCatalogConsentStatus } from '../api/catalog'

export type ConsentBanner =
  | { tone: 'ok'; key: 'connected' }
  | { tone: 'warn'; key: string }

export function confirmedActiveAuthorization(status: MetaCatalogConsentStatus | null | undefined): boolean {
  // Current availability must hold too: an active row alone never overrides
  // a failed environment, entitlement, scope or schema check.
  if (!status || status.available !== true || !status.approved) return false
  const auth = status.authorization
  return (
    !!auth
    && auth.state === 'active'
    && !!auth.catalog_id
    && !!auth.business_id
    && auth.catalog_id === status.approved.catalog_id
    && auth.business_id === status.approved.business_id
  )
}

/**
 * ``hint``: the code read from the fragment (already format-checked), or null.
 * ``status``: the fresh server status, or null while it is still loading.
 * ``statusFailed``: the fresh status could not be read.
 * Returns null when nothing should be shown (no hint, or success hint while
 * the fresh status is still loading).
 */
export function resolveConsentBanner(
  hint: string | null,
  status: MetaCatalogConsentStatus | null,
  statusFailed: boolean,
  knownCodes: ReadonlySet<string>,
): ConsentBanner | null {
  if (!hint) return null
  if (hint === 'connected') {
    if (!statusFailed && confirmedActiveAuthorization(status)) return { tone: 'ok', key: 'connected' }
    if (!status && !statusFailed) return null
    return { tone: 'warn', key: 'notConfirmed' }
  }
  return { tone: 'warn', key: knownCodes.has(hint) ? hint : 'error' }
}
