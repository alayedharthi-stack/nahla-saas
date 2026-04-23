// ── Types ─────────────────────────────────────────────────────────────────────

export interface OrderStatusUpdateConfig {
  enabled: boolean
  notify_statuses: string[]
  template_name: string
}

export interface PredictiveReorderConfig {
  enabled: boolean
  days_before: number
  consumption_days_default: number
  template_name: string
}

export interface AbandonedCartConfig {
  enabled: boolean
  // Stage 1 — 30 minute friendly nudge, no discount.
  reminder_30min: boolean
  // Stage 2 — 6 hour empathetic follow-up, no discount.
  reminder_6h: boolean
  // Stage 3 — 24 hour last-chance reminder. May include a coupon
  // depending on AI decision (cart value / customer value) when the
  // tenant is on OfferDecisionService advisory or enforce mode.
  coupon_24h: boolean
  coupon_code: string
  template_name: string
}

export interface InactiveRecoveryConfig {
  enabled: boolean
  inactive_days: number
  discount_pct: number
  template_name: string
}

export interface AutopilotSettings {
  enabled: boolean
  order_status_update: OrderStatusUpdateConfig
  predictive_reorder: PredictiveReorderConfig
  abandoned_cart: AbandonedCartConfig
  inactive_recovery: InactiveRecoveryConfig
}

export interface DailySummaryItem {
  key: string
  label: string
  count: number
  icon: string
}

export interface AutopilotStatus {
  settings: AutopilotSettings
  daily_summary: DailySummaryItem[]
  last_run_at: string | null
  is_running: boolean
  /** Temporary, env-gated switch (`AUTOPILOT_ENABLE_MANUAL_RETRY`) for the
   *  abandoned-cart manual retry button. Default false. When false the UI
   *  hides the button entirely. */
  manual_retry_enabled?: boolean
}

export interface AutopilotRunResult {
  ran: boolean
  total_actions: number
  breakdown: Record<string, number>
  ran_at: string
  message: string
}

// ── Queue item types ──────────────────────────────────────────────────────────

// Per-cart recovery progress surfaced on the autopilot queue. Mirrors
// ``services/cart_recovery_status.RECOVERY_STATUS_*`` (backend).
export type RecoveryStatus =
  | 'no_recovery'
  | 'pending'
  | 'in_progress'
  | 'completed'
  | 'converted'
  | 'failed'

export interface AbandonedCartRecoverySummary {
  status: RecoveryStatus
  steps_sent: number
  steps_failed: number
  last_sent_at: string | null
  last_status: string | null
  /** Raw error_message from AutomationExecution — kept for backward
   *  compat. Prefer ``last_failure_label`` for display. */
  last_error: string | null
  /** Internal failure code (``invalid_phone_number`` / ``template_not_approved``
   *  / …). Stable enum the dashboard can branch on. */
  last_failure_code: string | null
  /** Localised Arabic label for the latest failure — safe to render as-is. */
  last_failure_label: string | null
  next_pending_at: string | null
  converted_at: string | null
  cancel_reason: string | null
  recovery_event_id: number | null
}

export interface AbandonedCartRecoveryStep {
  step_idx: number
  event_id: number
  is_root: boolean
  status: 'sent' | 'pending' | 'skipped' | 'failed' | string
  scheduled_at: string | null
  sent_at: string | null
  /** Raw error_message — kept for engineering, hidden from the merchant
   *  in favour of ``failure_label`` when present. */
  error: string | null
  failure_code: string | null
  failure_label: string | null
  /** Raw Meta error envelope (code/subcode/message/trace_id). Only set
   *  for steps that actually called the provider. */
  meta_error: Record<string, unknown> | null
  skip_reason: string | null
  wa_message_id: string | null
  channel: string | null
  template_name: string | null
}

export interface AbandonedCartRecoveryTimeline extends AbandonedCartRecoverySummary {
  steps: AbandonedCartRecoveryStep[]
}

export interface AbandonedCartItem {
  order_id: number
  external_id: string | null
  customer_name: string
  customer_phone: string
  checkout_url: string
  total: number
  status: string
  created_at: string
  abandoned_at?: string
  recovery: AbandonedCartRecoverySummary
}

export interface PredictiveReorderItem {
  estimate_id: number
  customer_name: string
  customer_phone: string
  product_name: string
  predicted_date: string | null
  days_remaining: number
  notified: boolean
}

export interface OrderStatusUpdateItem {
  order_id: number
  external_id: string | null
  customer_name: string
  customer_phone: string
  status: string
  status_label: string
  previous_status: string | null
  previous_status_label: string | null
  created_at: string
}

export interface AutopilotQueues {
  abandoned_carts: AbandonedCartItem[]
  predictive_reorder: PredictiveReorderItem[]
  order_status_updates: OrderStatusUpdateItem[]
}

// ── API client ────────────────────────────────────────────────────────────────

import { apiCall } from './client'

export const autopilotApi = {
  /** Get autopilot settings + today's daily summary. */
  status: () =>
    apiCall<AutopilotStatus>('/autopilot/status'),

  /** Save autopilot settings (partial — only provided fields are updated). */
  save: (patch: Partial<AutopilotSettings> & { enabled?: boolean }) =>
    apiCall<{ settings: AutopilotSettings }>('/autopilot/settings', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  /** Manually trigger all enabled autopilot jobs (for testing). */
  runNow: () =>
    apiCall<AutopilotRunResult>('/autopilot/run', { method: 'POST' }),

  /** Get operational queues: abandoned carts, predictive reorder, order status updates. */
  queues: () =>
    apiCall<AutopilotQueues>('/autopilot/queues'),

  /** Per-cart recovery timeline (every reminder stage emitted, with delivery
   *  status and error). Returns ``status="no_recovery"`` + empty ``steps``
   *  for carts that never produced a cart_abandoned event. */
  abandonedCartRecovery: (orderId: number) =>
    apiCall<AbandonedCartRecoveryTimeline>(
      `/autopilot/abandoned-carts/${orderId}/recovery`,
    ),

  /** Manually re-enqueue the latest failed stage of a cart's recovery
   *  sequence. Feature-flagged on the backend
   *  (``AUTOPILOT_ENABLE_MANUAL_RETRY``); the dashboard hides its
   *  trigger button when ``AutopilotStatus.manual_retry_enabled`` is
   *  false, so calling this with the flag off returns 403.
   *
   *  Idempotent: if a retry was already enqueued in the last 60s for
   *  the same step, the response carries ``deduplicated: true`` and
   *  the existing event id. */
  retryAbandonedCart: (orderId: number) =>
    apiCall<{
      ok: boolean
      deduplicated: boolean
      retry_event_id: number
      step_idx: number
      queued_at: string
      message: string
    }>(`/autopilot/abandoned-carts/${orderId}/retry`, { method: 'POST' }),

  retryAllStaleCarts: () =>
    apiCall<{
      ok: boolean
      retried: number
      sent_immediately?: number
      engine_error?: string | null
      errors?: string[]
      message: string
    }>('/autopilot/abandoned-carts/retry-all-stale', { method: 'POST' }),
}

// ── Order status labels (Arabic) ──────────────────────────────────────────────

export const ORDER_STATUS_LABELS: Record<string, string> = {
  pending:           'قيد الانتظار',
  under_review:      'قيد المراجعة',
  in_progress:       'قيد المعالجة',
  processing:        'قيد المعالجة',
  shipped:           'تم الشحن',
  out_for_delivery:  'خرج للتوصيل',
  delivered:         'تم التوصيل',
  completed:         'مكتمل',
  cancelled:         'ملغي',
  refunded:          'مسترجع',
  payment_pending:   'في انتظار الدفع',
  ready_for_pickup:  'جاهز للاستلام',
  on_hold:           'في الانتظار',
  failed:            'فشل',
  draft:             'مسودة',
  cod:               'الدفع عند الاستلام',
  abandoned:         'سلة متروكة',
}

export const ORDER_STATUS_COLORS: Record<string, string> = {
  pending:          'amber',
  under_review:     'amber',
  in_progress:      'blue',
  processing:       'blue',
  shipped:          'purple',
  out_for_delivery: 'purple',
  delivered:        'green',
  completed:        'green',
  cancelled:        'red',
  refunded:         'orange',
  payment_pending:  'amber',
  ready_for_pickup: 'teal',
  on_hold:          'slate',
  failed:           'red',
  draft:            'slate',
  cod:              'amber',
  abandoned:        'orange',
}

// ── Display metadata ──────────────────────────────────────────────────────────

export const AUTOPILOT_SUB_META: Record<
  keyof Omit<AutopilotSettings, 'enabled'>,
  { label: string; desc: string; template: string; icon: string; triggerLabel: string }
> = {
  order_status_update: {
    label: 'إشعارات تحديثات الطلبات',
    desc:  'يُرسل إشعار واتساب فور تغيُّر حالة الطلب (قيد الانتظار، الشحن، التوصيل، الإلغاء...).',
    template: 'order_status_update_ar',
    icon: '📦',
    triggerLabel: 'order_status_changed',
  },
  predictive_reorder: {
    label: 'تذكير إعادة الطلب التنبؤي',
    desc:  'يحسب دورة استهلاك كل منتج ويُرسل تذكيراً قبل النفاد بـ 3 أيام.',
    template: 'predictive_reorder_reminder_ar',
    icon: '🔄',
    triggerLabel: 'predictive_reorder_due',
  },
  abandoned_cart: {
    label: 'استرداد السلة المتروكة',
    desc:  'تذكير ودود بعد 30 دقيقة، متابعة بعد 6 ساعات، ثم تذكير أخير بعد 24 ساعة (كوبون اختياري حسب قرار الذكاء الاصطناعي).',
    template: 'abandoned_cart_recovery_ar',
    icon: '🛒',
    triggerLabel: 'cart_abandoned',
  },
  inactive_recovery: {
    label: 'استرجاع العملاء غير النشطين',
    desc:  'يُرسل عرضاً للعملاء الذين لم يتسوقوا منذ X يوماً.',
    template: 'win_back',
    icon: '💙',
    triggerLabel: 'customer_inactive',
  },
}
