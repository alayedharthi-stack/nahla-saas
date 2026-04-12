import { apiCall } from './client'

// ── Types ─────────────────────────────────────────────────────────────────────

export type WaConnectionStatus =
  | 'not_connected'
  | 'pending'
  | 'connected'
  | 'error'
  | 'disconnected'
  | 'needs_reauth'

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
  oauth_session_status?: string | null
  oauth_session_message?: string | null
  oauth_session_needs_reauth?: boolean
  active_graph_token_source?: string | null
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
}
