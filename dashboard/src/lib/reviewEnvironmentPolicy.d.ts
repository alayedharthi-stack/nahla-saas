/** Types for reviewEnvironmentPolicy.js (shared by the app project and the Vite config project). */

export declare const REVIEW_ENV_FLAG: 'VITE_NAHLA_CATALOG_REVIEW_ENV'
export declare const API_BASE_ENV_KEYS: readonly string[]
export declare const PRODUCTION_API_HOSTS: readonly string[]

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

export declare function isTruthyFlag(value: string | undefined | null): boolean
export declare function pickApiBase(env: Record<string, string | undefined>): string
export declare function checkReviewApiBase(
  env: Record<string, string | undefined>,
  reviewFlag?: string | undefined,
): ReviewApiBaseCheck
export declare function describeReviewApiBaseFailure(check: ReviewApiBaseCheck): string
