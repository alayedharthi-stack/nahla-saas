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

export interface DashboardOrder {
  id: string
  customer: string
  phone: string
  items: string
  amount: string
  amount_sar: number
  status: 'paid' | 'pending' | 'failed' | 'cancelled'
  source: 'AI' | 'manual'
  paymentLink?: string
  createdAt: string
}

export interface OrdersDashboard {
  summary: {
    total_orders: number
    today_revenue_sar: number
    pending_orders: number
    completed_today: number
  }
  orders: DashboardOrder[]
}

export interface DashboardCoupon {
  id: string
  code: string
  type: 'percentage' | 'fixed'
  value: number | string
  usages: number
  limit: number
  expires: string
  category: 'standard' | 'vip' | 'auto'
  active: boolean
}

export interface CouponsDashboard {
  rules: Array<{ id: string; label: string; enabled: boolean }>
  vip_tiers: Array<{ tier: string; threshold: string; discount: string }>
  coupons: DashboardCoupon[]
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
}

export interface DashboardMessage {
  id: string
  direction: 'in' | 'out'
  body: string
  time: string
  isAI?: boolean
}

export interface CouponDashboardSettings {
  rules: Array<{ id: string; label: string; enabled: boolean }>
  vip_tiers: Array<{ tier: string; threshold: string; discount: string }>
}

export const featureRealityApi = {
  analytics(): Promise<AnalyticsDashboard> {
    return apiCall('/analytics/dashboard')
  },
  orders(): Promise<OrdersDashboard> {
    return apiCall('/orders')
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
