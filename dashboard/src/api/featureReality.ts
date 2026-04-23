import { apiCall } from './client'

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
}

export type MessageEventType = 'customer' | 'ai' | 'campaign' | 'automation' | 'cod' | 'manual' | 'system'

export interface DashboardMessage {
  id: string
  direction: 'in' | 'out'
  body: string
  time: string
  isAI?: boolean
  eventType?: MessageEventType
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
  conversations(): Promise<{ conversations: DashboardConversation[] }> {
    return apiCall('/conversations')
  },
  conversationMessages(phone: string): Promise<{ messages: DashboardMessage[] }> {
    return apiCall(`/conversations/messages/${encodeURIComponent(phone)}`)
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
}
