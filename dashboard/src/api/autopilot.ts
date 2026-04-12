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
  reminder_30min: boolean
  reminder_24h: boolean
  coupon_48h: boolean
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
}

export interface AutopilotRunResult {
  ran: boolean
  total_actions: number
  breakdown: Record<string, number>
  ran_at: string
  message: string
}

// ── Queue item types ──────────────────────────────────────────────────────────

export interface AbandonedCartItem {
  order_id: number
  external_id: string | null
  customer_name: string
  customer_phone: string
  checkout_url: string
  total: number
  status: string
  created_at: string
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
    desc:  'بعد 30 دقيقة، 24 ساعة، وخيارياً 48 ساعة مع كوبون.',
    template: 'abandoned_cart_reminder',
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
