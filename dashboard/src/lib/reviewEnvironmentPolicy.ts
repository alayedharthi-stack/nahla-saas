/**
 * reviewEnvironmentPolicy.ts
 * ──────────────────────────
 * Pure policy for the **catalog_management review environment** build of the
 * dashboard (`nahla-catalog-review-web`). No `import.meta`, no DOM — it is
 * shared by the browser bootstrap (`reviewEnvironment.ts` / `auth.ts`), the
 * Vite build step (`vite.config.ts`) and the CI check script, so the same
 * rule is enforced at build time and at runtime.
 *
 * Rule: when `VITE_NAHLA_CATALOG_REVIEW_ENV` is truthy the API base MUST be
 * set explicitly (`VITE_API_BASE`, or the legacy aliases) and MUST NOT point
 * at the production API or at a localhost default. There is no silent
 * fallback to production: a missing or production API base is a hard,
 * visible failure.
 *
 * Outside review mode this module is inert and the historic resolution
 * (env → default) is unchanged.
 */

export const REVIEW_ENV_FLAG = 'VITE_NAHLA_CATALOG_REVIEW_ENV'

/** Env keys consulted for the API base, in priority order (mirrors auth.ts). */
export const API_BASE_ENV_KEYS = [
  'VITE_API_BASE',
  'VITE_API_BASE_URL',
  'VITE_API_URL',
  'NEXT_PUBLIC_API_URL',
  'REACT_APP_API_URL',
] as const

/** Hosts that are production (or the production fallback) and therefore forbidden in review mode. */
export const PRODUCTION_API_HOSTS = [
  'api.nahlah.ai',
  'nahla-saas-production.up.railway.app',
] as const

export type ReviewApiBaseFailure =
  | 'api_base_missing'
  | 'api_base_malformed'
  | 'api_base_is_production'
  | 'api_base_is_localhost_default'
  | 'api_base_not_https'

export interface ReviewApiBaseCheck {
  /** True when the build/runtime is a review environment. */
  enabled: boolean
  ok: boolean
  failure: ReviewApiBaseFailure | null
  /** Host of the configured API base (never contains credentials). */
  host: string | null
}

export function isTruthyFlag(value: string | undefined | null): boolean {
  return ['1', 'true', 'yes', 'on'].includes(String(value ?? '').trim().toLowerCase())
}

/** First non-empty API base from the given env map, trailing slashes removed. */
export function pickApiBase(env: Record<string, string | undefined>): string {
  for (const key of API_BASE_ENV_KEYS) {
    const v = env[key]
    if (v && String(v).trim()) return String(v).trim().replace(/\/+$/, '')
  }
  return ''
}

function hostOf(url: string): string | null {
  try {
    return new URL(url).host.toLowerCase()
  } catch {
    return null
  }
}

/**
 * Validate the API base for a review build/runtime.
 * `env` is the (build-time or baked) environment map; `reviewFlag` the raw flag value.
 */
export function checkReviewApiBase(
  env: Record<string, string | undefined>,
  reviewFlag: string | undefined = env[REVIEW_ENV_FLAG],
): ReviewApiBaseCheck {
  const enabled = isTruthyFlag(reviewFlag)
  const base = pickApiBase(env)
  if (!enabled) {
    return { enabled: false, ok: true, failure: null, host: base ? hostOf(base) : null }
  }
  if (!base) return { enabled, ok: false, failure: 'api_base_missing', host: null }
  const host = hostOf(base)
  if (!host) return { enabled, ok: false, failure: 'api_base_malformed', host: null }
  if ((PRODUCTION_API_HOSTS as readonly string[]).includes(host)) {
    return { enabled, ok: false, failure: 'api_base_is_production', host }
  }
  if (/^(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$/.test(host)) {
    return { enabled, ok: false, failure: 'api_base_is_localhost_default', host }
  }
  if (!/^https:\/\//i.test(base)) {
    return { enabled, ok: false, failure: 'api_base_not_https', host }
  }
  return { enabled, ok: true, failure: null, host }
}

/** Operator-facing explanation (no secrets; English, shown in logs and on the boot screen). */
export function describeReviewApiBaseFailure(check: ReviewApiBaseCheck): string {
  switch (check.failure) {
    case 'api_base_missing':
      return `Review environment build requires an explicit API base: set ${API_BASE_ENV_KEYS[0]} to the review API (no fallback to production).`
    case 'api_base_malformed':
      return `Review environment API base is not a valid URL (${API_BASE_ENV_KEYS[0]}).`
    case 'api_base_is_production':
      return `Review environment must not use the production API (${check.host}). Set ${API_BASE_ENV_KEYS[0]} to the review API host.`
    case 'api_base_is_localhost_default':
      return `Review environment API base resolved to a localhost default (${check.host}); set ${API_BASE_ENV_KEYS[0]} explicitly.`
    case 'api_base_not_https':
      return `Review environment API base must use https (${check.host}).`
    default:
      return ''
  }
}
