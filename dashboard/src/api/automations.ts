// ── Types ─────────────────────────────────────────────────────────────────────

export type AutomationType =
  | 'abandoned_cart'
  | 'predictive_reorder'
  | 'customer_winback'
  | 'vip_upgrade'
  | 'new_product_alert'
  | 'back_in_stock'
  | 'unpaid_order_reminder'
  | 'cod_confirmation'
  | 'seasonal_offer'
  | 'salary_payday_offer'
  | 'order_notifications'

export type EngineKey = 'recovery' | 'growth' | 'experience' | 'intelligence'

export interface AutomationRecord {
  id: number
  automation_type: AutomationType
  name: string
  enabled: boolean
  engine: EngineKey
  config: Record<string, unknown>
  template_id: number | null
  template_name: string | null
  stats_triggered: number
  stats_sent: number
  stats_converted: number
  updated_at: string | null
}

export interface EngineKpis {
  messages_sent_30d: number
  orders_attributed_30d: number
  revenue_sar_30d: number
}

export interface EngineSummary {
  engine: EngineKey
  name: string
  description: string
  available: boolean
  enabled: boolean
  automations_count: number
  active_automations: number
  automation_ids: number[]
  kpis: EngineKpis
}

export interface EnginesSummaryResponse {
  engines: EngineSummary[]
  autopilot_enabled: boolean
  window_days: number
}

export interface IntelligenceSummary {
  reorder_soon_count: number
  churn_risk_count: number
  vip_count: number
  active_automations: number
  leads_count?: number
  inactive_count?: number
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
  rfm_segments?: CustomerSegment[]
}

// ── Response Quality / Brain Analytics types ──────────────────────────────────

export interface ResponseQualityMetrics {
  turns_total: number
  sessions_total: number
  avg_turns_per_session: number
  order_started_count: number
  order_confirmed_count: number
  coupon_redeemed_count: number
  handoff_count: number
  payment_link_count: number
  conversion_rate: number
  handoff_rate: number
  payment_link_rate: number
  avg_latency_ms: number | null
  p95_latency_ms: number | null
}

export interface ResponseQualityData {
  last_7_days: ResponseQualityMetrics
  last_30_days: ResponseQualityMetrics
  top_intents: { intent: string; count: number }[]
  top_actions: { action: string; count: number }[]
  daily: { date: string; turns: number; orders_started: number; orders_confirmed: number }[]
}

// ── Merchant Brain Knowledge types ────────────────────────────────────────────

export interface MerchantKnowledgeSyncStatus {
  last_sync_at: string | null
  is_fresh: boolean
  platform: string
  store_name: string
  store_url: string
  spl_enabled?: boolean
  address_autofill?: boolean
}

export interface MerchantKnowledgeProduct {
  id: number
  title: string
  sku: string
  price: number | null
  sale_price?: number | null
  category?: string
  in_stock: boolean
  stock_qty: number | null
  orderable: boolean
  external_id?: string | null
}

export interface MerchantKnowledgeExcludedProduct {
  id: number
  title: string
  sku: string
  price: number | null
  in_stock: boolean | null
  stock_qty: number | null
  status: string
  has_salla_id: boolean
  reason: string
}

export interface MerchantKnowledgeShippingMethod {
  name: string
  cost: string
  eta: string
}

export interface MerchantKnowledgePolicies {
  return_policy: string
  shipping_policy: string
  payment_policy: string
  warranty_policy: string
  delivery_areas: string
  working_hours: string
}

export interface MerchantKnowledgeFaqs {
  approved: string[]
  suggested: string[]
}

export interface MerchantKnowledgePage {
  title: string
  url?: string
  content?: string
  type?: string
}

export interface MerchantKnowledgeBrainProfile {
  tone: string
  reply_length: string
  coupon_strategy: string
  upsell_enabled: boolean
  owner_instructions: string
}

export interface MerchantKnowledge {
  sync_status: MerchantKnowledgeSyncStatus
  quality: { score: number; label: string }
  products: {
    orderable_count: number
    excluded_count: number
    total_count: number
    without_description_count: number
    orderable: MerchantKnowledgeProduct[]
    excluded: MerchantKnowledgeExcludedProduct[]
  }
  policies: MerchantKnowledgePolicies
  payment_methods: string[]
  shipping_methods: MerchantKnowledgeShippingMethod[]
  faqs: MerchantKnowledgeFaqs
  pages: MerchantKnowledgePage[]
  missing: string[]
  warnings: string[]
  brain_profile: MerchantKnowledgeBrainProfile
}

// ── API client ────────────────────────────────────────────────────────────────

import { apiCall } from './client'

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

  getMerchantKnowledge: () =>
    apiCall<MerchantKnowledge>('/intelligence/merchant-brain/knowledge'),

  updateMerchantKnowledge: (payload: {
    faqs?: { approved?: string[]; suggested?: string[] }
    policies?: Partial<MerchantKnowledgePolicies>
  }) =>
    apiCall<{ ok: boolean; changed: boolean }>('/intelligence/merchant-brain/knowledge', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  getResponseQuality: () =>
    apiCall<ResponseQualityData>('/intelligence/response-quality'),

  enginesSummary: (windowDays?: number) =>
    apiCall<EnginesSummaryResponse>(
      `/automations/engines/summary${windowDays ? `?days=${windowDays}` : ''}`,
    ),

  toggleEngine: (engine: EngineKey, enabled: boolean) =>
    apiCall<{
      engine: EngineKey
      enabled: boolean
      automations_count: number
      automations_changed: number
    }>(`/automations/engines/${engine}/toggle`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
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
  unpaid_order_reminder: {
    label: 'تذكير الطلبات غير المدفوعة',
    desc: 'يُرسل تذكيرات تلقائية للعملاء الذين أنشأوا طلباً ولم يدفعوا',
    trigger: 'order_payment_pending',
    icon: '💳',
    color: 'red',
  },
  cod_confirmation: {
    label: 'تأكيد طلبات الدفع عند الاستلام',
    desc: 'يطلب من العميل تأكيد طلب COD، يُذكّره بعد 6 ساعات، ويُلغي الطلب تلقائياً بعد 24 ساعة بدون تأكيد. منفصل تماماً عن تذكير الطلبات غير المدفوعة.',
    trigger: 'order_cod_pending',
    icon: '📞',
    color: 'amber',
  },
  seasonal_offer: {
    label: 'عروض المناسبات الذكية',
    desc: 'حملة تلقائية قبل المناسبات السعودية (اليوم الوطني، رمضان، …)',
    trigger: 'seasonal_event_due',
    icon: '🎉',
    color: 'amber',
  },
  salary_payday_offer: {
    label: 'عروض الرواتب',
    desc: 'حملة تلقائية شهرياً قبل موعد نزول الرواتب',
    trigger: 'salary_payday_due',
    icon: '💰',
    color: 'emerald',
  },
  order_notifications: {
    label: 'إشعارات الطلبات',
    desc: 'تنبيهات واتساب لمراحل الطلب — تأكيد، شحن، تسليم، وتأكيد الدفع عند الاستلام (قوالب مكتبة نحلة)',
    trigger: 'order_notifications',
    icon: '📣',
    color: 'blue',
  },
}
