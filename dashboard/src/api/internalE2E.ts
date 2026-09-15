import { apiCall } from './client'

export type InternalE2EAlias = 'A' | 'B' | 'C'
export const INTERNAL_E2E_ALIASES: readonly InternalE2EAlias[] = ['A', 'B', 'C']

export interface InternalE2EFixtureStatus {
  provisioned: boolean
  conversation_id: number | null
}

export interface InternalE2EStatus {
  enabled: boolean
  operator_tenant_id: number
  approved_aliases: InternalE2EAlias[]
  configured_allowlist: string
  external_egress_allowed: false
  fixtures: Partial<Record<InternalE2EAlias, InternalE2EFixtureStatus>>
}

export interface InternalE2ESafetyProof {
  value: number | null
  proven: boolean
  violations?: string[]
  evidence?: unknown
}

export interface InternalE2EResult {
  status?: string
  failure_reason?: string | null
  trace_id?: string | null
  case_id?: string
  account_alias?: InternalE2EAlias
  owner?: string
  tool_calls?: string[]
  total_runner_latency_ms?: number | null
  fallback_type?: string
  internal_inbound_message_id?: string
  internal_outbound_message_id?: string
  guardrail_passed?: boolean
  guardrail_result?: unknown
  safety_proofs?: Record<string, InternalE2ESafetyProof>
  external_egress_count?: number | null
  cross_tenant_leakage?: number | null
  cross_customer_leakage?: number | null
  write_mutations?: number | null
  salla_mutations?: number | null
  unsupported_commercial_claims?: number | null
  duplicate_replies?: number | null
  silent_v1_fallback?: number | null
}

export const internalE2EApi = {
  status: () => apiCall<InternalE2EStatus>('/admin/internal-e2e/status', {
    authScope: 'platform-admin',
  }),
  provision: () => apiCall('/admin/internal-e2e/fixtures/provision', {
    method: 'POST',
    authScope: 'platform-admin',
    body: JSON.stringify({ reset_aliases: [] }),
    timeoutMs: 60_000,
  }),
  reset: (alias: InternalE2EAlias) =>
    apiCall(`/admin/internal-e2e/fixtures/${alias}/reset`, {
      method: 'POST',
      authScope: 'platform-admin',
    }),
  submitTurn: (input: { alias: InternalE2EAlias; text: string; caseId: string }) =>
    apiCall<InternalE2EResult>('/admin/internal-e2e/turns', {
      method: 'POST',
      authScope: 'platform-admin',
      body: JSON.stringify({
        alias: input.alias,
        text: input.text,
        case_id: input.caseId,
        service_tier: 'auto',
      }),
      timeoutMs: 120_000,
    }),
  result: (lookup: { internalMessageId?: string; traceId?: string }) => {
    const query = new URLSearchParams()
    if (lookup.internalMessageId) query.set('internal_message_id', lookup.internalMessageId)
    if (lookup.traceId) query.set('trace_id', lookup.traceId)
    return apiCall<InternalE2EResult>(`/admin/internal-e2e/results?${query.toString()}`, {
      authScope: 'platform-admin',
    })
  },
}
