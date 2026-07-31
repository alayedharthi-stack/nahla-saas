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

/** Overview command-center layout experiment (future). */
export function isOverviewCommandCenterEnabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_OVERVIEW_COMMAND_CENTER)
}
