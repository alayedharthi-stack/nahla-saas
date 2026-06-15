import { apiCall } from './client'

const CONVERSATIONS_LIST_TIMEOUT_MS = 52_000
const CONVERSATIONS_MESSAGES_TIMEOUT_MS = 48_000

export interface AnalyticsDashboard {
  summary: {
    current_month_revenue_sar: number
    conversion_rate_pct: number
    current_month_orders: number
    current_month_conversations: number
    today_revenue_sar: number
    pending_orders: number
    completed_today: number
  }
  revenue_trend: Array<{ month: string; revenue: number }>
  conversion_trend: Array<{ day: string; conversations: number; conversions: number }>
  source_breakdown: Array<{ name: string; value: number; color: string }>
  top_products: Array<{ name: string; revenue: number; orders: number; trend: string }>
}

export type OrderSourceKey =
  | 'salla'
  | 'zid'
  | 'shopify'
  | 'whatsapp'
  | 'manual'

export type NeedsActionLevel = 'amber' | 'red' | 'blue' | 'purple'

export interface OrderNeedsAction {
  key: string
  label: string
  level: NeedsActionLevel
}

export interface DashboardOrder {
  // The platform's human-visible order number (e.g. "#1585297702"). The
  // backend now sets `id` to this value so existing key/search code
  // continues to work and the merchant sees the same number their store
  // dashboard shows.
  id: string
  order_number: string
  internal_id?: string
  external_id?: string | null
  customer: string
  customer_name: string
  phone: string
  items: string
  amount: string
  amount_sar: number
  status: 'paid' | 'pending' | 'failed' | 'cancelled'
  status_label?: string
  source: OrderSourceKey
  source_label: string
  paymentLink?: string
  createdAt: string
  is_ai_created?: boolean
  is_vip?: boolean
  has_open_conversation?: boolean
  needs_action?: OrderNeedsAction[]
}

export interface OrderDetailLineItem {
  product_id: string
  name: string
  quantity: number
  variant_id?: string | null
  unit_price?: number | null
  image_url?: string | null
}

export interface OrderDetailLinks {
  store?: string | null
  store_label?: string | null
  whatsapp?: string | null
  conversation?: string | null
}

export interface OrderDetailAddress {
  city?: string | null
  district?: string | null
  street?: string | null
  building_number?: string | null
  postal_code?: string | null
  address?: string | null
}

export interface OrderTimelineEvent {
  key:   string
  label: string
  at:    string
  icon:  string
}

export interface OrderDetail extends DashboardOrder {
  line_items: OrderDetailLineItem[]
  customer_address: OrderDetailAddress
  links: OrderDetailLinks
  payment_method?: string | null
  notes?: string | null
  timeline: OrderTimelineEvent[]
  payment_reminder_draft?: string | null
}

export interface PaymentReminderResult {
  sent: boolean
  reason?: string
  error?: string
  message: string
  conversation_url: string
  sent_at?: string
}

export interface OrdersDashboard {
  summary: {
    total_orders: number
    today_revenue_sar: number
    pending_orders: number
    completed_today: number
    whatsapp_orders_today: number
    whatsapp_revenue_today: number
    orders_needing_action: number
  }
  orders: DashboardOrder[]
}

export type CouponOrigin = 'manual' | 'automation' | 'promotion' | 'vip' | 'widget'

export interface DashboardCoupon {
  id: string
  code: string
  type: 'percentage' | 'fixed'
  value: number | string
  usages: number
  limit: number
  expires: string
  /** Legacy field kept for backward compat. Prefer `origin`. */
  category: 'standard' | 'vip' | 'auto'
  /** What generated this code — drives the "🤖 AI" vs "✋ Manual" badge. */
  origin?: CouponOrigin
  /** When `origin === 'automation'` — which automation slug created it. */
  automation_type?: string | null
  /** When `origin === 'promotion'` — which promotion materialised it. */
  promotion_id?: number | null
  active: boolean
  /** Who created this code — drives the "نظام / يدوي / مستورد" chip. */
  source_type?: 'manual' | 'system' | 'imported'
  /** Bronze / silver / gold / vip — drives the level chip. */
  coupon_level?: CouponLevelId | null
  /** Where the code is allowed to be issued from. */
  allocation_channel?: CouponChannel | null
  /** Seconds remaining until expiry (null when no expiry). */
  remaining_seconds?: number | null
}

export type CouponLevelId = 'bronze' | 'silver' | 'gold' | 'vip'
export type CouponChannel = 'ai' | 'campaign' | 'autopilot' | 'shared'
export type CouponValidityPreset = '3h' | '6h' | '24h' | 'custom'
export type CouponDiscountType = 'percentage' | 'fixed'
export type CouponPoolMode = 'pool_first' | 'pool_only' | 'on_demand_only'

export interface CouponLevel {
  id: CouponLevelId
  label: string
  threshold: string
  discount_default: number
  discount_min: number
  discount_max: number
  validity_hours: number
  max_uses: number
  per_customer_usage: number
  allowed_channels: CouponChannel[]
  enabled: boolean
}

export interface CouponGlobalDefaults {
  discount_type: CouponDiscountType
  default_discount_value: number
  total_usage_limit: number | null
  customer_limit: number | null
  per_customer_usage: number
  min_order_amount: number
  default_validity: CouponValidityPreset
  custom_validity_hours: number | null
  allowed_channels: CouponChannel[]
  combinable_with_offers: boolean
}

export interface CouponAiPolicy {
  enabled: boolean
  allowed_levels: CouponLevelId[]
  min_remaining_hours: number
  pool_mode: CouponPoolMode
}

export interface CouponRule {
  id:               string
  label:            string
  enabled:          boolean
  description?:     string | null
  /** 'percentage' or 'fixed'. */
  discount_type?:   'percentage' | 'fixed'
  discount_value?:  number
  /** How many days the auto-generated coupon stays valid. */
  validity_days?:   number
  /** Minimum cart subtotal that triggers the rule. */
  min_order_amount?: number | null
  /** How many times each generated coupon can be redeemed (null = unlimited). */
  max_uses?:        number | null
}

export interface CouponsDashboard {
  rules: CouponRule[]
  vip_tiers: Array<{ tier: string; threshold: string; discount: string }>
  levels?: CouponLevel[]
  global_defaults?: CouponGlobalDefaults
  ai_policy?: CouponAiPolicy
  coupons: DashboardCoupon[]
}

export interface CouponDashboardSettings {
  rules: CouponRule[]
  vip_tiers?: Array<{ tier: string; threshold: string; discount: string }>
  levels?: CouponLevel[]
  global_defaults?: CouponGlobalDefaults
  ai_policy?: CouponAiPolicy
}

export type AIPauseReason =
  | 'manual'
  | 'manual_pause'
  | 'human_handoff'
  | 'bot_loop_detected'
  | 'rate_limit'
  | 'internal_number'
  | 'manual_takeover'
  | 'support_escalation'

export interface AIPauseStateResponse {
  ok: boolean
  customerPhone?: string
  aiPaused: boolean
  aiPausedReason: AIPauseReason | null
  aiPausedAt: string | null
  aiPausedBy?: string | null
  // Backwards-compatible snake_case mirrors (still emitted by backend).
  ai_paused?: boolean
  ai_paused_reason?: AIPauseReason | null
  ai_paused_at?: string | null
  ai_paused_by?: string | null
}

export interface DashboardConversation {
  id: string
  customer: string
  phone: string
  lastMsg: string
  time: string
  isAI: boolean
  status: 'active' | 'human' | 'closed'
  unread: number
  lastMsgType?: MessageEventType | ''
  windowOpen?: boolean
  handoffReason?: string | null
  isUnsubscribed?: boolean
  pendingUnsubscribe?: boolean
  aiPaused?: boolean
  aiPausedReason?: AIPauseReason | null
  aiPausedAt?: string | null
  // Unified human-takeover signals (mirrors the backend list payload).
  needsHuman?: boolean
  handoffActive?: boolean
  takenOverAt?: string | null
  takenOverBy?: string | null
  // Blocked-number signal — the phone is on the tenant's blocklist
  // (or paused with reason='internal_number'). Mutually exclusive
  // with the human and "paused only" filters at the UI level.
  isBlocked?: boolean
  // ISO timestamp of the most recent payment receipt the platform
  // confirmed for this conversation (``payment_evidence_status='confirmed'``
  // / ``payment_receipt_received=True``). Drives the "طلبات مدفوعة"
  // filter + the green CheckCheck-style badge. ``null`` when the
  // customer has never uploaded an accepted receipt.
  lastPaymentConfirmedAt?: string | null
}

export type MessageEventType = 'customer' | 'ai' | 'campaign' | 'automation' | 'cod' | 'manual' | 'system'

// ── Inbound media (voice notes, images) ─────────────────────────────
// The conversation-drawer renders an audio player / image preview for
// any inbound message whose backend row carried a ``normalized_inbound``
// payload (audio or image source_type). ``ai_used`` tells the merchant
// whether AI consumed the transcript/description for the reply, which
// matters for trust + debugging.
export interface DashboardMessageMediaAudio {
  kind: 'audio'
  message_event_id: number
  storage_url: string | null
  mime_type: string | null
  duration: number | null
  voice: boolean
  transcript: string | null
  transcript_status: string | null
  /** 'pending' | 'ok' | 'failed' | null — distinct from transcript_status */
  download_status: string | null
  ai_used: boolean
  caption: string | null
  error: string | null
}

export interface DashboardMessageMediaImage {
  kind: 'image'
  message_event_id: number
  storage_url: string | null
  mime_type: string | null
  description: string | null
  vision_status: string | null
  download_status: string | null
  ai_used: boolean
  caption: string | null
  error: string | null
}

export interface DashboardMessageMediaVideo {
  kind: 'video'
  message_event_id: number
  storage_url: string | null
  mime_type: string | null
  duration: number | null
  download_status: string | null
  caption: string | null
  filename: string | null
  forwarded: boolean
  frequently_forwarded: boolean
  error: string | null
}

export interface DashboardMessageMediaDocument {
  kind: 'document'
  message_event_id: number
  storage_url: string | null
  mime_type: string | null
  filename: string | null
  byte_size: number | null
  download_status: string | null
  pdf_kind: string | null
  pdf_text_status: string | null
  /** Short internal preview (≤280 chars) — not raw extraction */
  summary: string | null
  caption: string | null
  error: string | null
}

export type DashboardMessageMedia =
  | DashboardMessageMediaAudio
  | DashboardMessageMediaImage
  | DashboardMessageMediaVideo
  | DashboardMessageMediaDocument

// ── Outbound send status (Meta / 360dialog wire-layer outcome) ──────
// Surfaced per outbound MessageEvent so the UI can tell the merchant
// whether the AI / manual reply ACTUALLY reached the customer. See
// ``backend/core/outbound_send_status.py`` for how this is stamped
// onto the row's ``extra_metadata.provider_send`` block.
//
// * 'queued'   → row persisted, provider POST hasn't returned yet.
//                Render with a clock icon. Should flip to 'sent'
//                or 'failed' within ~1s in steady state.
// * 'sent'     → Meta / 360dialog returned 2xx + wamid. Render ✔✔.
// * 'failed'   → non-2xx / provider error envelope / missing wamid
//                / transport exception / Nahla burst throttle. The
//                ``sendError`` block carries the Arabic merchant
//                label + advice + Meta code/subcode. Render a red
//                × with a tooltip on top of the bubble.
// * null/absent → historical row from before the stamping fix, or
//                 a row written from a path that doesn't go through
//                 ``_post_wa`` (campaign dispatcher has its own
//                 status surface). Render the previous unconditional
//                 double-check so old conversations don't suddenly
//                 show red ×s.
export type OutboundSendStatus = 'queued' | 'sent' | 'failed' | null

export interface OutboundSendError {
  labelAr: string
  adviceAr?: string | null
  code?: number | string | null
  subcode?: number | string | null
  key?: string | null
  isRecoverable?: boolean
}

export interface DashboardMessage {
  id: string
  direction: 'in' | 'out'
  body: string
  time: string
  isAI?: boolean
  eventType?: MessageEventType
  media?: DashboardMessageMedia | null
  /** Wire-layer outcome of the Meta/360dialog POST. Outbound rows only. */
  sendStatus?: OutboundSendStatus
  /** Arabic error label + Meta code metadata when sendStatus === 'failed'. */
  sendError?: OutboundSendError | null
  /** Meta-issued message id (wamid) when the POST succeeded. */
  wamid?: string | null
}

export const featureRealityApi = {
  analytics(): Promise<AnalyticsDashboard> {
    return apiCall('/analytics/dashboard')
  },
  orders(): Promise<OrdersDashboard> {
    return apiCall('/orders')
  },
  orderDetail(orderId: string | number): Promise<{ order: OrderDetail }> {
    return apiCall(`/orders/${encodeURIComponent(String(orderId))}`)
  },
  sendOrderPaymentReminder(
    orderId: string | number,
    body: { message?: string } = {},
  ): Promise<PaymentReminderResult> {
    return apiCall(`/orders/${encodeURIComponent(String(orderId))}/send-payment-reminder`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  coupons(): Promise<CouponsDashboard> {
    return apiCall('/coupons')
  },
  saveCouponSettings(settings: CouponDashboardSettings): Promise<CouponDashboardSettings> {
    return apiCall('/coupons/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    })
  },
  createCoupon(body: {
    code: string
    type: 'percentage' | 'fixed'
    value: string
    description?: string
    limit?: number
    expires?: string
    category?: 'standard' | 'vip' | 'auto'
    active?: boolean
  }): Promise<{ id: number }> {
    return apiCall('/coupons', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  updateCoupon(couponId: string, patch: Record<string, unknown>): Promise<{ updated: boolean }> {
    return apiCall(`/coupons/${couponId}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  },
  deleteCoupon(couponId: string): Promise<{ deleted: boolean }> {
    return apiCall(`/coupons/${couponId}`, {
      method: 'DELETE',
    })
  },
  conversations(opts?: {
    signal?: AbortSignal
    limit?: number
    offset?: number
    /**
     * Server-side filter slug. Matches the tab the merchant has
     * selected so the SQL ``LIMIT`` window narrows BEFORE pagination,
     * not after — critical for large inboxes where the human / closed
     * tail can sit beyond the first 200-1500 SQL rows. Backend
     * accepts: ``all`` | ``active`` | ``human`` | ``agent_req`` |
     * ``paused`` | ``blocked`` | ``unsubscribed`` | ``closed``.
     * Unknown values fall back to ``all`` server-side.
     */
    filter?: string
  }): Promise<{
    conversations: DashboardConversation[]
    total_count?: number
    has_more?: boolean
  }> {
    const q = new URLSearchParams()
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    if (opts?.offset != null) q.set('offset', String(opts.offset))
    if (opts?.filter && opts.filter !== 'all') q.set('filter', opts.filter)
    const qs = q.toString()
    return apiCall(`/conversations${qs ? `?${qs}` : ''}`, {
      signal: opts?.signal,
      timeoutMs: CONVERSATIONS_LIST_TIMEOUT_MS,
    })
  },
  conversationMessages(
    phone: string,
    opts?: { signal?: AbortSignal; limit?: number },
  ): Promise<{ messages: DashboardMessage[] }> {
    const q = new URLSearchParams()
    if (opts?.limit != null) q.set('limit', String(opts.limit))
    const qs = q.toString()
    const suffix = qs ? `?${qs}` : ''
    return apiCall(
      `/conversations/messages/${encodeURIComponent(phone)}${suffix}`,
      { signal: opts?.signal, timeoutMs: CONVERSATIONS_MESSAGES_TIMEOUT_MS },
    )
  },
  /** Re-run the inbound-media pipeline (download + AI) for a single
   * `MessageEvent` row. Used by the "إعادة معالجة" button in the
   * conversation drawer when a recording's storage url 404s or
   * transcription was skipped because OPENAI_API_KEY was missing at
   * intake time. Backend never calls the brain — it only refreshes
   * `extra_metadata.normalized_inbound`. */
  reprocessInboundMedia(messageEventId: number): Promise<{
    ok: boolean
    message_event_id: number
    source_type: string
    normalized_inbound: Record<string, unknown>
    should_process: boolean
    fallback_reply_ar: string | null
  }> {
    return apiCall(`/conversations/media/${messageEventId}/reprocess`, {
      method: 'POST',
    })
  },
  replyToConversation(body: { customer_phone: string; message: string }): Promise<{ sent: boolean }> {
    return apiCall('/conversations/reply', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  handoffConversation(body: { customer_phone: string; customer_name?: string; last_message?: string; reason?: string }): Promise<{ handoff: boolean; session_id: number }> {
    return apiCall('/conversations/handoff', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  closeConversation(body: { customer_phone: string }): Promise<{ closed: boolean }> {
    return apiCall('/conversations/close', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  pauseConversationAI(body: { customer_phone: string; reason?: AIPauseReason }): Promise<AIPauseStateResponse> {
    return apiCall('/conversations/ai-pause', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  resumeConversationAI(body: { customer_phone: string }): Promise<AIPauseStateResponse> {
    return apiCall('/conversations/ai-resume', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  returnHandoffToAI(body: { customer_phone: string }): Promise<AIPauseStateResponse & {
    needsHuman: boolean
    handoffActive: boolean
    takenOverAt: string | null
    takenOverBy: string | null
  }> {
    return apiCall('/conversations/handoff/return-to-ai', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  markConversationRead(body: { customer_phone: string }): Promise<{
    ok: boolean
    customerPhone: string
    updated: number
    lastReadAt?: string
  }> {
    return apiCall('/conversations/mark-read', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  getBlocklist(): Promise<{ numbers: string[] }> {
    return apiCall('/conversations/blocklist')
  },
  addToBlocklist(body: { phone: string; customer_phone?: string }): Promise<{ ok: boolean; numbers: string[] }> {
    return apiCall('/conversations/blocklist/add', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
  removeFromBlocklist(body: { phone: string }): Promise<{ ok: boolean; numbers: string[] }> {
    return apiCall('/conversations/blocklist/remove', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
}
