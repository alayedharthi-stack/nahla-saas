import { apiCall } from './client'

// ── Types ─────────────────────────────────────────────────────────────────────

export type WaConnectionStatus =
  | 'not_connected'
  | 'pending'
  | 'connected'
  | 'error'
  | 'disconnected'
  | 'needs_reauth'
  | 'request_submitted'
  | 'pending_activation'
  | 'action_required'

export interface WaConnection {
  /** Unified flag — true only when status === 'connected' */
  connected: boolean
  status: WaConnectionStatus
  connection_status?: string | null
  phone_number: string | null
  display_phone_number: string | null
  business_display_name: string | null
  display_name: string | null
  whatsapp_business_account_id: string | null
  phone_number_id: string | null
  waba_id: string | null
  verification_status: string | null
  meta_business_account_id: string | null
  connected_at: string | null
  last_verified_at: string | null
  last_attempt_at: string | null
  last_error: string | null
  webhook_verified: boolean
  sending_enabled: boolean
  token_expires_at: string | null
  token_status?: string | null
  token_health?: string | null
  oauth_session_status?: string | null
  oauth_session_message?: string | null
  oauth_session_needs_reauth?: boolean
  active_graph_token_source?: string | null
  provider?: string | null
  provider_label?: string | null
  merchant_channel_label?: string | null
  connection_type?: string | null
  coexistence_status?: string | null
  action_required_message?: string | null
  request_submitted_at?: string | null
  coexistence_available?: boolean
  /** Per-URL Coexistence webhook block (channel / coexistence / status). */
  webhooks?: CoexistenceWebhookBlock | null
  coexistence_sync_state?: string | null
  pairing_state?: string | null
  mobile_app_connection_state?: string | null
  phone_app_handover_at?: string | null
  last_coexistence_event?: CoexistenceEvent | null
  last_coexistence_events_by_category?: Record<string, CoexistenceEvent>
  last_status_event?: CoexistenceEvent | null
}

export interface CoexistenceWebhookBlock {
  channel_url: string
  channel_status: string
  channel_last_received_at: string | null
  coexistence_url: string
  coexistence_status: string
  coexistence_last_received_at: string | null
  status_url: string
  status_status: string
  status_last_received_at: string | null
  internal_header_name: string
}

export interface CoexistenceEvent {
  event_type: string
  category?: string
  received_at: string
  payload_preview?: string
}

export interface WaStartResult {
  status:        string
  meta_app_id:   string
  graph_version: string
  scope:         string
  extras:        Record<string, unknown>
  config_id?:    string   // present only when META_WA_CONFIG_ID is set on server
}

export interface WaHealthResult {
  healthy: boolean
  status: WaConnectionStatus
  phone_number: string | null
  checks: {
    has_connection: boolean
    token_present: boolean
    token_valid: boolean
    webhook_verified: boolean
    sending_enabled: boolean
  }
  last_verified: string | null
  last_error: string | null
  provider?: string | null
}

export interface CoexistenceRequestPayload {
  phone_number: string
  display_name?: string
  has_whatsapp_business_app?: boolean
  understands_keep_app_installed?: boolean
  understands_open_every_13_days?: boolean
  notes?: string
}

// ── API ───────────────────────────────────────────────────────────────────────

export const whatsappConnectApi = {
  /**
   * Unified WhatsApp status — single source of truth for ALL pages.
   * Reads from GET /whatsapp/status (same data as /integrations/whatsapp/status).
   */
  getStatus: () =>
    apiCall<WaConnection>('/whatsapp/status'),

  start: () =>
    apiCall<WaStartResult>('/whatsapp/connection/start', { method: 'POST' }),

  callback: (data: { code: string; state?: string; waba_id?: string; phone_number_id?: string; business_id?: string }) =>
    apiCall<WaConnection & { status: string }>('/whatsapp/connection/callback', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  verify: () =>
    apiCall<{ verified: boolean; reason?: string; sending_enabled?: boolean }>(
      '/whatsapp/connection/verify',
      { method: 'POST' }
    ),

  disconnect: () =>
    apiCall<{ status: string }>('/whatsapp/connection/disconnect', { method: 'POST' }),

  reconnect: () =>
    apiCall<WaStartResult>('/whatsapp/connection/reconnect', { method: 'POST' }),

  health: () =>
    apiCall<WaHealthResult>('/whatsapp/connection/health'),

  requestCoexistence: (data: CoexistenceRequestPayload) =>
    apiCall<WaConnection & { status: string; message?: string }>('/whatsapp/coexistence/request', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  getCoexistenceStatus: () =>
    apiCall<WaConnection>('/whatsapp/coexistence/status'),
}
