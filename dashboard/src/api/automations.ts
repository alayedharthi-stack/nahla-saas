// ── Types ─────────────────────────────────────────────────────────────────────

export type AutomationType =
  | 'abandoned_cart'
  | 'predictive_reorder'
  | 'customer_winback'
  | 'vip_upgrade'
  | 'new_product_alert'
  | 'back_in_stock'

export interface AutomationRecord {
  id: number
  automation_type: AutomationType
  name: string
  enabled: boolean
  config: Record<string, unknown>
  template_id: number | null
  template_name: string | null
  stats_triggered: number
  stats_sent: number
  stats_converted: number
  updated_at: string | null
}

export interface IntelligenceSummary {
  reorder_soon_count: number
  churn_risk_count: number
  vip_count: number
  active_automations: number
}

export interface ReorderPrediction {
  customer_name: string
  phone: string
  product_name: string
  predicted_date: string
  confidence: number
}

export interface ChurnRiskCustomer {
  customer_name: string
  phone: string
  last_purchase: string
  days_inactive: number
  risk_score: number
}

export interface VipCustomer {
  customer_name: string
  total_spent: number
  orders: number
  segment: string
}

export interface IntelligenceSuggestion {
  id: string
  type: string
  priority: 'high' | 'medium' | 'low'
  title: string
  desc: string
  action: string
  automation_type: AutomationType
}

export interface CustomerSegment {
  key: string
  label: string
  count: number
  color: string
}

export interface IntelligenceDashboard {
  summary: IntelligenceSummary
  reorder_predictions: ReorderPrediction[]
  churn_risk: ChurnRiskCustomer[]
  vip_customers: VipCustomer[]
  suggestions: IntelligenceSuggestion[]
  segments: CustomerSegment[]
}

// ── API client ────────────────────────────────────────────────────────────────

async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
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

export const automationsApi = {
  list: () =>
    apiCall<{ automations: AutomationRecord[]; autopilot_enabled: boolean }>('/automations'),

  toggle: (id: number, enabled: boolean) =>
    apiCall<AutomationRecord>(`/automations/${id}/toggle`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  updateConfig: (id: number, config: Record<string, unknown>, templateId?: number) =>
    apiCall<AutomationRecord>(`/automations/${id}/config`, {
      method: 'PUT',
      body: JSON.stringify({ config, template_id: templateId }),
    }),

  setAutopilot: (enabled: boolean) =>
    apiCall<{ autopilot_enabled: boolean }>('/automations/autopilot', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  emitEvent: (eventType: string, customerId?: number, payload?: Record<string, unknown>) =>
    apiCall<{ event_id: number; event_type: string }>('/automations/events', {
      method: 'POST',
      body: JSON.stringify({ event_type: eventType, customer_id: customerId, payload }),
    }),

  getIntelligence: () =>
    apiCall<IntelligenceDashboard>('/intelligence/dashboard'),
}

// ── Display metadata ──────────────────────────────────────────────────────────

export const AUTOMATION_META: Record<AutomationType, {
  label: string
  desc: string
  trigger: string
  icon: string
  color: string
}> = {
  abandoned_cart: {
    label: 'استرداد العربة المتروكة',
    desc: 'يُرسل تذكيرات تلقائية للعملاء الذين أضافوا منتجات ولم يكملوا الشراء',
    trigger: 'cart_abandoned',
    icon: '🛒',
    color: 'amber',
  },
  predictive_reorder: {
    label: 'تذكير إعادة الطلب التنبؤي',
    desc: 'يتنبأ بموعد نفاد المنتج ويُرسل تذكيراً قبل 3 أيام',
    trigger: 'predictive_reorder_due',
    icon: '🐝',
    color: 'brand',
  },
  customer_winback: {
    label: 'استرجاع العملاء غير النشطين',
    desc: 'يُرسل عروضاً للعملاء الذين لم يتسوقوا منذ 60 أو 90 يوماً',
    trigger: 'customer_inactive',
    icon: '💙',
    color: 'blue',
  },
  vip_upgrade: {
    label: 'مكافأة عملاء VIP',
    desc: 'يُرسل عروضاً حصرية للعملاء الذين أنفقوا أكثر من 2000 ر.س',
    trigger: 'vip_customer_upgrade',
    icon: '👑',
    color: 'purple',
  },
  new_product_alert: {
    label: 'تنبيه المنتجات الجديدة',
    desc: 'يُبلغ العملاء المهتمين عند إضافة منتج جديد',
    trigger: 'product_created',
    icon: '✨',
    color: 'emerald',
  },
  back_in_stock: {
    label: 'تنبيه عودة المنتج للمخزون',
    desc: 'يُبلغ العملاء السابقين عند عودة منتج للمخزون',
    trigger: 'product_back_in_stock',
    icon: '📦',
    color: 'green',
  },
}
