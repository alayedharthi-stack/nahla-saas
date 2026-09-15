import type { InternalE2EResult, InternalE2ESafetyProof } from '../api/internalE2E'

const STOP_METRICS = [
  'external_egress_count',
  'cross_tenant_leakage',
  'cross_customer_leakage',
  'write_mutations',
  'salla_mutations',
  'unsupported_commercial_claims',
  'duplicate_replies',
  'silent_v1_fallback',
] as const

const REQUIRED_PROOFS = [
  'cross_tenant_leakage',
  'cross_customer_leakage',
  'write_mutations',
  'salla_mutations',
  'unsupported_commercial_claims',
  'duplicate_replies',
  'silent_v1_fallback',
] as const

function proof(result: InternalE2EResult, key: string): InternalE2ESafetyProof | undefined {
  return result.safety_proofs?.[key]
}

/** Return fail-closed reasons that lock further turn submission. */
export function internalE2EStopReasons(result: InternalE2EResult | null): string[] {
  if (!result) return []
  const reasons: string[] = []
  for (const key of STOP_METRICS) {
    const value = result[key]
    if (value === null || value === undefined || Number(value) !== 0) reasons.push(key)
  }
  for (const key of REQUIRED_PROOFS) {
    if (proof(result, key)?.proven !== true) reasons.push(`${key}_unproven`)
  }
  if (result.guardrail_passed !== true) reasons.push('guardrail_failed')
  if (result.status !== 'completed') reasons.push('turn_not_completed')
  return [...new Set(reasons)]
}
