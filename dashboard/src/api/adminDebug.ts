/**
 * adminDebug.ts
 * ─────────────
 * Thin typed client for the platform-admin "/admin/debug/..." surface.
 * Everything in this file is gated by the dashboard's `isAdmin()` and
 * by the backend's `require_admin` dependency — never expose these
 * methods to merchant-role UI components.
 */
import { apiCall } from './client'

// ── /admin/debug/whatsapp/send-template ─────────────────────────────
// Fires a single template message at a real WhatsApp number, bypassing
// the entire campaign engine. Returns the raw provider response so
// support can read the exact Meta/360dialog error envelope.

export interface AdminDirectSendRequest {
  phone_number_id: string
  to:              string                          // E.164
  template:        string
  language?:       string                          // default 'ar'
  merchant_vars?:  Record<string, string>          // body placeholders
}

export interface AdminDirectSendResponse {
  ok:                  boolean
  http_status:         number
  provider:            'meta_cloud' | '360dialog' | string
  phone_number_id:     string
  tenant_id:           number
  template:            string
  language:            string
  to_masked:           string
  raw_request_masked:  Record<string, unknown>
  raw_response:        Record<string, unknown> | null
  provider_message_id: string | null
  duration_ms:         number
  error_message:       string | null
}

// ── /admin/debug/media-env ──────────────────────────────────────────
// Diagnostic snapshot of the inbound-media pipeline configuration —
// without ever exposing the OPENAI_API_KEY value. Used by the
// "تشخيص الوسائط" panel when the conversation drawer reports
// "ميزة التفريغ الصوتي غير مفعّلة" so support can confirm whether
// the issue is env (key missing) or volume (NAHLA_INBOUND_MEDIA_DIR
// not writable) without grepping Railway logs.

export interface AdminMediaEnvSnapshot {
  openai: {
    api_key_present: boolean
    api_key_tail:    string | null
    api_base:        string
    chat_model:      string
    audio_model:     string
    vision_model:    string
    stt_language:    string
  }
  storage: {
    root:              string
    exists:            boolean
    writable:          boolean
    write_probe_error: string | null
    free_bytes:        number | null
    max_inbound_bytes: number
  }
  ready: {
    audio:  boolean
    vision: boolean
  }
  issues: string[]
  hints:  string[]
}

export const adminDebugApi = {
  /** Fire a single template send directly through the live provider. */
  sendTemplate: (body: AdminDirectSendRequest) =>
    apiCall<AdminDirectSendResponse>('/admin/debug/whatsapp/send-template', {
      method: 'POST',
      body:   JSON.stringify(body),
    }),
  /** Inspect the inbound-media pipeline configuration on the server. */
  mediaEnv: () =>
    apiCall<AdminMediaEnvSnapshot>('/admin/debug/media-env'),
}
