/**
 * Platform UX feature flags (env-driven kill switches).
 *
 * Default is OFF — the current sidebar and overview remain the default path.
 */

const DEV_NAV_SIMPLIFIED_LS_KEY = 'nahla_platform_nav_simplified_8'

function isTruthyEnv(value: string | undefined): boolean {
  if (value === undefined) return false
  const normalized = value.trim().toLowerCase()
  return normalized === 'true' || normalized === '1'
}

function isFalsyEnv(value: string | undefined): boolean {
  if (value === undefined) return false
  const normalized = value.trim().toLowerCase()
  return normalized === 'false' || normalized === '0'
}

/** Simplified 8-destination navigation shell (P3.1). Default OFF. */
export function isNavSimplified8Enabled(): boolean {
  if (import.meta.env.DEV && typeof localStorage !== 'undefined') {
    const override = localStorage.getItem(DEV_NAV_SIMPLIFIED_LS_KEY)
    if (isTruthyEnv(override ?? undefined)) return true
    if (isFalsyEnv(override ?? undefined)) return false
  }
  return isTruthyEnv(import.meta.env.VITE_PLATFORM_NAV_SIMPLIFIED_8)
}

/**
 * Shopify connection merchant UI (card on /integrations + completion page).
 * Default OFF. The backend has its own default-off flag
 * (NAHLA_SHOPIFY_CONNECTION_ENABLED); both must be on. No local override.
 */
export function isShopifyConnectionUiEnabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_SHOPIFY_CONNECTION_UI)
}

/**
 * Shopify catalog import step on the Shopify card (status + start). Default
 * OFF. The backend has its own default-off flags
 * (NAHLA_SHOPIFY_CONNECTION_ENABLED and NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED);
 * all must be on. No local override. Off: the card keeps its static
 * "not started" import step.
 */
export function isShopifyCatalogImportUiEnabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_SHOPIFY_CATALOG_IMPORT_UI)
}

/** Overview command-center layout experiment (future). */
export function isOverviewCommandCenterEnabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_OVERVIEW_COMMAND_CENTER)
}
