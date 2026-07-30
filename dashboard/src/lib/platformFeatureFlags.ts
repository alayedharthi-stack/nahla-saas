/**
 * Platform UX feature flags (env-driven kill switches).
 *
 * These flags enable or disable future navigation / overview experiments.
 * Default is OFF — the current sidebar and overview remain the default path.
 * Do not wire into Sidebar or Overview until the gated UX PR lands.
 */

function isTruthyEnv(value: string | undefined): boolean {
  if (value === undefined) return false
  const normalized = value.trim().toLowerCase()
  return normalized === 'true' || normalized === '1'
}

/** Simplified 8-destination navigation experiment (future). */
export function isNavSimplified8Enabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_PLATFORM_NAV_SIMPLIFIED_8)
}

/** Overview command-center layout experiment (future). */
export function isOverviewCommandCenterEnabled(): boolean {
  return isTruthyEnv(import.meta.env.VITE_OVERVIEW_COMMAND_CENTER)
}
