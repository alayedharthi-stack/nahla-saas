/**
 * reviewEnvironmentPolicy.js
 * ──────────────────────────
 * Pure policy for the **catalog_management review environment** build of the
 * dashboard (`nahla-catalog-review-web`). No `import.meta`, no DOM, no Node
 * globals — it is shared by the browser bootstrap (`reviewEnvironment.ts` /
 * `auth.ts`), the Vite build step (`vite.config.ts`) and the CI check script,
 * so the same rule is enforced at build time and at runtime.
 *
 * It is plain ESM JavaScript (types in `reviewEnvironmentPolicy.d.ts`) on
 * purpose: `vite.config.ts` belongs to the composite `tsconfig.node.json`
 * project and the browser code to `tsconfig.json`; a `.ts` source imported by
 * both projects makes `tsc` fail with TS6305, a declaration file does not.
 *
 * Rule: when `VITE_NAHLA_CATALOG_REVIEW_ENV` is truthy the API base MUST be
 * set explicitly (`VITE_API_BASE`, or the legacy aliases) and MUST NOT point
 * at the production API or at a localhost default. There is no silent
 * fallback to production: a missing or production API base is a hard,
 * visible failure. Outside review mode this module is inert.
 */

export const REVIEW_ENV_FLAG = 'VITE_NAHLA_CATALOG_REVIEW_ENV'

/** Env keys consulted for the API base, in priority order (mirrors auth.ts). */
export const API_BASE_ENV_KEYS = [
  'VITE_API_BASE',
  'VITE_API_BASE_URL',
  'VITE_API_URL',
  'NEXT_PUBLIC_API_URL',
  'REACT_APP_API_URL',
]

/** Hosts that are production (or the production fallback) and therefore forbidden in review mode. */
export const PRODUCTION_API_HOSTS = [
  'api.nahlah.ai',
  'nahla-saas-production.up.railway.app',
]

/** @param {string | undefined | null} value */
export function isTruthyFlag(value) {
  return ['1', 'true', 'yes', 'on'].includes(String(value ?? '').trim().toLowerCase())
}

/**
 * First non-empty API base from the given env map, trailing slashes removed.
 * @param {Record<string, string | undefined>} env
 * @returns {string}
 */
export function pickApiBase(env) {
  for (const key of API_BASE_ENV_KEYS) {
    const v = env[key]
    if (v && String(v).trim()) return String(v).trim().replace(/\/+$/, '')
  }
  return ''
}

/**
 * Host (with optional :port) of an http(s) URL, or null. Regex-based so the
 * module needs no `URL` global.
 * @param {string} url
 * @returns {string | null}
 */
function hostOf(url) {
  const m = /^https?:\/\/(?:[^@/?#]*@)?([^/?#:]+)(:\d+)?(?:[/?#]|$)/i.exec(url.trim())
  if (!m) return null
  const host = m[1].toLowerCase().replace(/\.$/, '')
  return m[2] ? `${host}${m[2]}` : host
}

/**
 * Validate the API base for a review build/runtime.
 * @param {Record<string, string | undefined>} env build-time or baked environment map
 * @param {string | undefined} [reviewFlag] raw flag value (defaults to env[REVIEW_ENV_FLAG])
 * @returns {import('./reviewEnvironmentPolicy').ReviewApiBaseCheck}
 */
export function checkReviewApiBase(env, reviewFlag = env[REVIEW_ENV_FLAG]) {
  const enabled = isTruthyFlag(reviewFlag)
  const base = pickApiBase(env)
  if (!enabled) {
    return { enabled: false, ok: true, failure: null, host: base ? hostOf(base) : null }
  }
  if (!base) return { enabled, ok: false, failure: 'api_base_missing', host: null }
  const host = hostOf(base)
  if (!host) return { enabled, ok: false, failure: 'api_base_malformed', host: null }
  if (PRODUCTION_API_HOSTS.includes(host)) {
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

/**
 * Operator-facing explanation (no secrets; English, shown in logs and on the boot screen).
 * @param {import('./reviewEnvironmentPolicy').ReviewApiBaseCheck} check
 * @returns {string}
 */
export function describeReviewApiBaseFailure(check) {
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
