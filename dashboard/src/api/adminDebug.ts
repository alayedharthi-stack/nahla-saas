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

/** Identity of the backend process that answered the media-env
 *  request. The pipeline runs across multiple Railway services
 *  (web + worker + scheduler) and each one captures its own env
 *  snapshot when it starts. Surfacing the answering process lets
 *  support spot env-var drift between services — e.g. web sees
 *  the OpenAI key but worker doesn't, which manifests as
 *  "OPENAI_API_KEY مفقود" inside actual conversations even though
 *  this diagnostic reads green. */
export interface AdminMediaEnvProcessIdentity {
  /** OS process id answering this request. */
  pid: number
  /** Railway service name (or NAHLA_SERVICE_ROLE override). */
  service: string
  /** pid that loaded `modules.ai.media.normalizer`. Same as `pid`
   *  unless the request hopped across workers in the same dyno. */
  boot_pid: number
  /** False when the normalizer module was imported by a different
   *  process — surfaces request-routing surprises. */
  normalizer_loaded_in_this_process: boolean
  /** Live re-read of OPENAI_API_KEY from os.environ. */
  openai_key_present_now: boolean
  /** Snapshot of OPENAI_API_KEY presence at module-load time.
   *  ``null`` if the normalizer module hasn't been imported yet. */
  openai_key_present_at_boot: boolean | null
  /** True when key is present NOW but was missing at boot —
   *  this process needs a restart for stale callers to pick it
   *  up. Sibling services may also need the same treatment. */
  needs_restart_to_pick_up_env: boolean
  railway_service_name:    string | null
  railway_replica_id:      string | null
  railway_deployment_id:   string | null
  /** Server epoch seconds — sanity check for clock skew. */
  epoch: number
}

export interface AdminMediaEnvSnapshot {
  process: AdminMediaEnvProcessIdentity
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
  ffmpeg: {
    found:   boolean
    path:    string | null
    version: string | null
  }
  // Flat aliases (public contract — mirrors the nested groups 1:1).
  // Documented so dashboards/runbooks don't need to walk the structure.
  openai_key_present:  boolean
  openai_key_tail:     string | null
  vision_enabled:      boolean
  stt_enabled:         boolean
  media_dir_writable:  boolean
  inbound_media_dir:   string
  ffmpeg_found:        boolean
  ffmpeg_version:      string | null
  issues:              string[]
  hints:               string[]
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
