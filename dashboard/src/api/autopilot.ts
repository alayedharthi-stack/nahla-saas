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
  total_stages?: number
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
  status: 'sent' | 'pending' | 'skipped' | 'failed' | 'upcoming' | string
  label?: string
  delay_minutes?: number
  delay_label?: string
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

export interface PendingPaymentOrderItem {
  order_id: number
  external_id: string | null
  order_number: string
  customer_name: string
  customer_phone: string
  checkout_url: string
  total: number
  status: string
  created_at: string
  reminders_sent: number
  last_reminder_at: string | null
  /** 0 = no reminder emitted yet, 1 = stage-1 sent, 2 = stage-2 sent, etc. */
  current_stage: number
}

export interface CodPendingOrderItem {
  order_id: number
  external_id: string | null
  order_number: string
  customer_name: string
  customer_phone: string
  total: number
  status: string
  created_at: string
  reminders_sent: number
  last_reminder_at: string | null
  /** ISO timestamp when the order was auto-cancelled (null = not cancelled). */
  auto_cancel_at: string | null
}

export interface AutopilotQueues {
  abandoned_carts: AbandonedCartItem[]
  predictive_reorder: PredictiveReorderItem[]
  order_status_updates: OrderStatusUpdateItem[]
  pending_payment_orders: PendingPaymentOrderItem[]
  cod_pending_orders: CodPendingOrderItem[]
}

export type OrderReminderStepStatus = 'sent' | 'failed' | 'skipped' | 'pending' | 'emitted'

export interface OrderReminderStep {
  step_idx: number               // 1-based display index
  emitted_at: string | null      // when the event was queued
  executed_at: string | null     // when the engine actually ran it
  status: OrderReminderStepStatus
  status_label: string           // Arabic label from backend
  skip_reason: string | null
  error_message: string | null
  template_name: string | null
}

export interface OrderReminderTimeline {
  order_id: number
  order_number: string
  customer_name: string
  reminder_type: 'pending_payment' | 'cod' | 'unknown'
  total_emitted: number
  steps_sent: number
  steps: OrderReminderStep[]
  order_status: string
}

// ── API client ────────────────────────────────────────────────────────────────

import { apiCall } from './client'

export interface CartRecoveryStepReadiness {
  step: number
  label: string
  ready: boolean
  template_id: number | null
  template_name: string | null
  status: string
  reason?: string
}

export interface CartRecoveryReadiness {
  all_ready: boolean
  steps: CartRecoveryStepReadiness[]
}

export interface AutomationStepReadiness {
  label: string
  template_name: string | null
  ready: boolean
  status: string
  reason?: string
  step?: number
}

export interface AutomationReadiness {
  all_ready: boolean
  steps: AutomationStepReadiness[]
}

/** Map from automation_type → readiness */
export type AllAutomationsReadiness = Record<string, AutomationReadiness>

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

  /** Reminder timeline for a pending-payment or COD-pending order.
   *  Returns actual delivery status (sent / failed / skipped / pending)
   *  for every emitted stage — more honest than the queue-level current_stage. */
  orderReminderTimeline: (orderId: number) =>
    apiCall<OrderReminderTimeline>(
      `/autopilot/orders/${orderId}/reminder-timeline`,
    ),

  /** Clear failed/skipped stage markers so the next emitter sweep re-queues them.
   *  Successfully-sent stages and permanently-blocked ones are preserved.
   *  Returns has_template_error=true when Meta template approval is required. */
  rescheduleOrderReminders: (orderId: number) =>
    apiCall<{
      ok: boolean
      steps_cleared: number
      has_template_error: boolean
      has_permanent_block: boolean
      message: string
    }>(`/autopilot/orders/${orderId}/reschedule-reminders`, { method: 'POST' }),

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
      engine_error?: string | null
      errors?: string[]
      message: string
    }>('/autopilot/abandoned-carts/retry-all-stale', { method: 'POST' }),

  /** Check whether all 3 cart_recovery templates are APPROVED. */
  cartRecoveryReadiness: () =>
    apiCall<CartRecoveryReadiness>('/autopilot/cart-recovery/readiness'),

  /** Get template readiness for ALL automation types in one call. */
  allReadiness: () =>
    apiCall<AllAutomationsReadiness>('/autopilot/readiness'),

  /** Governor log — كل حالات المنع والتأجيل مع السبب بالعربي */
  governorLog: (params?: { customer_id?: number; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.customer_id) q.set('customer_id', String(params.customer_id))
    if (params?.limit)       q.set('limit', String(params.limit))
    const qs = q.toString()
    return apiCall<GovernorLogResponse>(`/automations/governor/log${qs ? `?${qs}` : ''}`)
  },
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

// ── Governor types ─────────────────────────────────────────────────────────

export type GovernorReasonCode =
  | 'blocked_by_priority'
  | 'blocked_by_unsubscribe'
  | 'blocked_by_daily_limit'
  | 'blocked_by_weekly_limit'
  | 'blocked_by_6h_limit'
  | 'blocked_by_cooldown'
  | 'allowed'

export interface GovernorLogItem {
  id: number
  executed_at: string | null
  automation_type: string | null
  automation_name: string | null
  customer_id: number | null
  customer_name: string | null
  customer_phone: string | null
  reason_code: GovernorReasonCode
  label_ar: string
  suggestion_ar: string
}

export interface GovernorLogResponse {
  items: GovernorLogItem[]
  count: number
}

export const GOVERNOR_REASON_META: Record<
  GovernorReasonCode,
  { icon: string; color: 'red' | 'amber' | 'slate' | 'emerald' }
> = {
  blocked_by_priority:     { icon: '🚫', color: 'red'     },
  blocked_by_unsubscribe:  { icon: '🔕', color: 'slate'   },
  blocked_by_daily_limit:  { icon: '⏳', color: 'amber'   },
  blocked_by_weekly_limit: { icon: '🚫', color: 'red'     },
  blocked_by_6h_limit:     { icon: '⏰', color: 'amber'   },
  blocked_by_cooldown:     { icon: '🕐', color: 'amber'   },
  allowed:                 { icon: '✅', color: 'emerald'  },
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
