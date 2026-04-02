export interface ComponentHealth {
  status: 'ok' | 'configured' | 'not_configured' | 'degraded' | 'unreachable' | 'error'
  error?: string
  model?: string
  platform?: string
  enabled?: boolean
  pipeline_stages?: number
}

export interface SystemHealth {
  status: 'ok' | 'degraded' | 'error'
  environment: string
  production: boolean
  components: {
    database:     ComponentHealth
    orchestrator: ComponentHealth
    moyasar:      ComponentHealth
    salla:        ComponentHealth
  }
  timestamp: string
}

export interface SystemEventEntry {
  id:           number
  category:     string
  event_type:   string
  severity:     'info' | 'warning' | 'error'
  summary:      string | null
  reference_id: string | null
  payload:      Record<string, unknown> | null
  created_at:   string | null
}

export interface SystemEventsResponse {
  events: SystemEventEntry[]
  total:  number
  offset: number
  limit:  number
}

export interface ConversationTurn {
  id:                  number
  session_id:          string | null
  turn:                number
  message:             string | null
  detected_intent:     string | null
  confidence:          number | null
  response_type:       string | null
  response_text:       string | null
  orchestrator_used:   boolean
  model_used:          string | null
  fact_guard_modified: boolean
  fact_guard_claims:   string[]
  actions_triggered:   Array<{ type: string; executable: boolean }>
  order_started:       boolean
  payment_link_sent:   boolean
  handoff_triggered:   boolean
  latency_ms:          number | null
  created_at:          string | null
}

import { apiCall } from './client'

export const systemApi = {
  health: () =>
    apiCall<SystemHealth>('/system/health'),

  events: (params?: { category?: string; severity?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.category) qs.set('category', params.category)
    if (params?.severity) qs.set('severity', params.severity)
    if (params?.limit    !== undefined) qs.set('limit',  String(params.limit))
    if (params?.offset   !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs}` : ''
    return apiCall<SystemEventsResponse>(`/system/events${query}`)
  },

  conversationTrace: (phone: string, limit = 50) =>
    apiCall<{ customer_phone: string; turns: ConversationTurn[] }>(
      `/conversations/trace/${encodeURIComponent(phone)}?limit=${limit}`
    ),
}
