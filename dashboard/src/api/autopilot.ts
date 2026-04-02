// ── Types ─────────────────────────────────────────────────────────────────────

export interface CodConfirmationConfig {
  enabled: boolean
  reminder_hours: number
  auto_cancel_hours: number
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
  cod_confirmation: CodConfirmationConfig
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

// ── API client ────────────────────────────────────────────────────────────────

const API_BASE = (import.meta.env.VITE_API_BASE ?? '') + '/api'

async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': '1',
      ...(options?.headers ?? {}),
    },
    ...options,
  })
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}

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
}

// ── Display metadata ──────────────────────────────────────────────────────────

export const AUTOPILOT_SUB_META: Record<
  keyof Omit<AutopilotSettings, 'enabled'>,
  { label: string; desc: string; template: string; icon: string; triggerLabel: string }
> = {
  cod_confirmation: {
    label: 'تأكيد الطلب النقدي (COD)',
    desc:  'يُرسل رسالة تأكيد لكل طلب بالدفع عند الاستلام ويتابع الرد تلقائياً.',
    template: 'cod_order_confirmation_ar',
    icon: '🍯',
    triggerLabel: 'order_created (COD)',
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
