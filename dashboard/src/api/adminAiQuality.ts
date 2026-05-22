// ── /admin/ai-quality typed API client ───────────────────────────────────────
// Backed by backend/routers/admin_ai_quality.py.
//
// Privacy contract: ``customer_phone_masked`` is the ONLY phone-shaped
// field the dashboard ever sees — full E.164 numbers stay in the inbox.
import { apiCall } from './client'

export type ResolvedStatus = 'open' | 'reviewed' | 'ignored' | 'fixed'

// May 2026 #22 — event categories. The original use case was
// ``ai_mismatch`` (brain alignment), now extended with three pre-brain
// silent-drop families surfaced in the owner dashboard tabs.
export type AiQualityCategory =
  | 'ai_mismatch'
  | 'inbound_drop'
  | 'webhook_routing'
  | 'media_failure'

export interface AiQualityEvent {
  id:                       number
  tenant_id:                number
  conversation_id:          number | null
  customer_phone_masked:    string
  mismatch_type:            string
  mismatch_reason:          string | null
  detected_intent:          string | null
  social_category:          string | null
  action_taken:             string | null
  chosen_path:              string | null
  fallback_used:            boolean | null
  order_status:             string | null
  awaiting_payment_receipt: boolean | null
  model_used:               string | null
  turn:                     number | null
  inbound_preview:          string | null
  reply_preview:            string | null
  alignment_passed:         boolean
  regen_fired:              boolean
  resolved_status:          ResolvedStatus
  resolved_by:              string | null
  resolved_at:              string | null
  resolved_note:            string | null
  // Optional for backward-compat: older API builds (pre-0070) won't
  // return this field. Treat ``undefined`` as ``ai_mismatch``.
  category?:                AiQualityCategory
  created_at:               string
}

export interface AiQualityEventListResponse {
  items:  AiQualityEvent[]
  total:  number
  limit:  number
  offset: number
}

export interface AiQualityCountByType {
  mismatch_type: string
  count:         number
}

export interface AiQualityCountByCategory {
  category: AiQualityCategory | string
  count:    number
}

export interface AiQualityTopConversation {
  conversation_id: number
  count:           number
  last_seen:       string
}

export interface AiQualitySummaryResponse {
  window_start:        string
  window_hours:        number
  total_open:          number
  total_in_window:     number
  counts_by_type:      AiQualityCountByType[]
  // May 2026 #22 — per-category roll-up for the tab badges. Defaults
  // to an empty array if the API build pre-dates the schema change.
  counts_by_category?: AiQualityCountByCategory[]
  top_conversations:   AiQualityTopConversation[]
  latest_events:       AiQualityEvent[]
}

export interface ListEventsParams {
  tenant_id?:        number
  category?:         AiQualityCategory
  mismatch_type?:    string
  resolved_status?:  ResolvedStatus
  since?:            string
  until?:            string
  limit?:            number
  offset?:           number
}

function toQuery(params: Record<string, unknown>): string {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    u.append(k, String(v))
  }
  const s = u.toString()
  return s ? `?${s}` : ''
}

export async function listAiQualityEvents(
  params: ListEventsParams = {},
): Promise<AiQualityEventListResponse> {
  return apiCall<AiQualityEventListResponse>(
    `/admin/ai-quality/events${toQuery(params as Record<string, unknown>)}`,
  )
}

export async function getAiQualitySummary(
  params: {
    tenant_id?:    number
    category?:     AiQualityCategory
    window_hours?: number
  } = {},
): Promise<AiQualitySummaryResponse> {
  return apiCall<AiQualitySummaryResponse>(
    `/admin/ai-quality/summary${toQuery(params as Record<string, unknown>)}`,
  )
}

export async function resolveAiQualityEvent(
  eventId: number,
  payload: { resolved_status: ResolvedStatus; resolved_note?: string | null },
): Promise<AiQualityEvent> {
  return apiCall<AiQualityEvent>(`/admin/ai-quality/events/${eventId}`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  })
}
