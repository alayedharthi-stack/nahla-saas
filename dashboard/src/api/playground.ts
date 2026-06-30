import { apiCall } from './client'

export interface PlaygroundOrderContext {
  order_status?: string
  order_reference?: string
  tracking_number?: string
  shipping_provider?: string
}

export interface PlaygroundDryRunRequest {
  message: string
  mode?: 'stateless'
  context?: PlaygroundOrderContext
}

export interface PlaygroundSideEffects {
  whatsapp_sent: boolean
  order_created: boolean
  customer_updated: boolean
  automation_triggered: boolean
}

export interface PlaygroundDryRunResponse {
  ok: boolean
  dry_run: boolean
  would_send: boolean
  outbound_kind: 'session_text' | 'template' | 'catalog' | 'none' | string
  reply_text: string | null
  blocked_reason: string | null
  used_llm: boolean
  decision_topic: string | null
  decision_action: string | null
  owner: string | null
  facts: Record<string, unknown>
  warnings: string[]
  needs_context: boolean
  side_effects: PlaygroundSideEffects
}

export const playgroundApi = {
  dryRun: (payload: PlaygroundDryRunRequest) =>
    apiCall<PlaygroundDryRunResponse>('/intelligence/playground/dry-run', {
      method: 'POST',
      body: JSON.stringify({
        mode: 'stateless',
        ...payload,
      }),
    }),
}
