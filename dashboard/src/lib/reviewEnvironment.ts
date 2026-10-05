/**
 * reviewEnvironment.ts
 * ────────────────────
 * Browser-side wiring of the review-environment policy. Reads the values Vite
 * baked at build time (`import.meta.env`) and exposes:
 *
 *   - `isReviewEnvironment()`       — review build or not
 *   - `reviewApiBaseCheck()`        — the policy result for this bundle
 *   - `ReviewEnvironmentConfigError` — thrown by `getApiBase()` in a misconfigured review build
 *
 * In a review build a misconfigured API base is never patched over: `auth.ts`
 * refuses to resolve a base (no localStorage override, no production default)
 * and `main.tsx` renders a static failure screen instead of the app.
 */
import {
  checkReviewApiBase,
  describeReviewApiBaseFailure,
  REVIEW_ENV_FLAG,
  type ReviewApiBaseCheck,
} from './reviewEnvironmentPolicy'

export class ReviewEnvironmentConfigError extends Error {
  readonly check: ReviewApiBaseCheck
  constructor(check: ReviewApiBaseCheck) {
    super(`[review-env] ${describeReviewApiBaseFailure(check)}`)
    this.name = 'ReviewEnvironmentConfigError'
    this.check = check
  }
}

function bakedEnv(): Record<string, string | undefined> {
  // Vite only exposes statically referenced keys; list them explicitly.
  const e = import.meta.env as Record<string, string | undefined>
  return {
    [REVIEW_ENV_FLAG]: e[REVIEW_ENV_FLAG],
    VITE_API_BASE: e.VITE_API_BASE,
    VITE_API_BASE_URL: e.VITE_API_BASE_URL,
    VITE_API_URL: e.VITE_API_URL,
    NEXT_PUBLIC_API_URL: e.NEXT_PUBLIC_API_URL,
    REACT_APP_API_URL: e.REACT_APP_API_URL,
  }
}

let _cached: ReviewApiBaseCheck | null = null

export function reviewApiBaseCheck(): ReviewApiBaseCheck {
  if (!_cached) _cached = checkReviewApiBase(bakedEnv())
  return _cached
}

export function isReviewEnvironment(): boolean {
  return reviewApiBaseCheck().enabled
}

/** Throws when this is a review build whose API base is missing or points at production. */
export function assertReviewApiBase(): void {
  const check = reviewApiBaseCheck()
  if (check.enabled && !check.ok) throw new ReviewEnvironmentConfigError(check)
}

export { describeReviewApiBaseFailure }
