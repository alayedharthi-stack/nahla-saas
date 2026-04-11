import { apiCall } from './client'

export interface AdminPlatformStats {
  merchants: { total: number; active: number; trial: number }
  tenants: { total: number }
  subscriptions: {
    active: number
    trial: number
    total: number
    by_plan: Record<string, { name_ar: string; count: number; price: number }>
  }
  revenue: { total_sar: number; today_sar: number; mrr_sar: number }
  recent_payments: Array<{
    id: number
    tenant_id: number
    amount: number
    currency: string
    status: string
    gateway: string
    created_at: string | null
  }>
  recent_merchants: AdminMerchantSummary[]
  all_merchants: AdminMerchantSummary[]
}

export interface AdminMerchantSummary {
  id: number
  tenant_id: number | null
  email: string
  store_name: string
  phone: string
  is_active: boolean
  plan: string
  sub_status: string
  wa_status: string
  created_at: string | null
}

export interface AdminTenantSummary {
  id: number
  name: string
  domain: string | null
  is_active: boolean
  created_at: string | null
  subscription: {
    status: string
    plan: string
    trial_ends_at: string | null
    ends_at: string | null
  }
  whatsapp: {
    status: string
    phone_number: string | null
    business_display_name: string | null
    sending_enabled: boolean
    webhook_verified: boolean
  }
  stats: {
    orders: number
    conversations: number
    revenue_sar: number
  }
}

export interface AdminTenantsResponse {
  total: number
  offset: number
  limit: number
  tenants: AdminTenantSummary[]
}

export interface AdminBillingOverview {
  subscriptions: { total: number; active: number }
  revenue: { total_sar: number; today_sar: number }
  invoices_due: number
  by_plan: Record<string, { name: string; name_ar: string; price_sar: number; active_count: number }>
}

export interface AdminPayment {
  id: number
  tenant_id: number
  tenant_name: string
  amount_sar: number
  currency: string
  gateway: string
  status: string
  paid_at: string | null
  created_at: string | null
}

export interface AdminSubscription {
  id: number
  tenant_id: number
  tenant_name: string
  plan: string
  status: string
  started_at: string | null
  trial_ends_at: string | null
  ends_at: string | null
  auto_renew: boolean
}

export interface AdminAIUsageTenant {
  tenant_id: number
  tenant_name?: string
  turns_total: number
  turns_orchestrated: number
  ai_actions_logged: number
  avg_latency_ms: number
  estimated_total_tokens: number
  estimated_total_cost_usd: number
  models: Array<{ model: string; count: number }>
  providers: Array<{ provider: string; count: number }>
}

export interface AdminSystemEvent {
  id: number
  tenant_id: number
  tenant_name: string
  category: string
  event_type: string
  severity: 'info' | 'warning' | 'error'
  summary: string | null
  payload: Record<string, unknown> | null
  reference_id: string | null
  created_at: string | null
}

export interface AdminFeatureFlags {
  features: Record<string, boolean>
}

export interface AdminTroubleshootingSummary {
  tenant: AdminTenantSummary
  support_access: {
    enabled: boolean
    expires_at: string | null
  }
  latest_sync: {
    status: string
    sync_type: string | null
    created_at: string | null
    error_message: string | null
  }
  recent_events: Array<{
    id: number
    category: string
    event_type: string
    severity: string
    summary: string | null
    created_at: string | null
  }>
}

export interface AdminSystemHealth {
  status: 'ok' | 'degraded' | 'error'
  timestamp: string
  components: Record<string, Record<string, unknown>>
}

export const adminApi = {
  stats: () => apiCall<AdminPlatformStats>('/admin/stats'),

  tenants: (params?: { search?: string; status?: '' | 'active' | 'inactive'; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.search) qs.set('search', params.search)
    if (params?.status) qs.set('status', params.status)
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<AdminTenantsResponse>(`/admin/tenants${query}`)
  },

  tenantSummary: (tenantId: number) =>
    apiCall<AdminTenantSummary>(`/admin/tenants/${tenantId}/summary`),

  tenantUsers: (tenantId: number) =>
    apiCall<{ tenant_id: number; users: Array<{ id: number; email: string; role: string; is_active: boolean; created_at: string | null }> }>(
      `/admin/tenants/${tenantId}/users`,
    ),

  updateTenantStatus: (tenantId: number, is_active: boolean) =>
    apiCall<AdminTenantSummary>(`/admin/tenants/${tenantId}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active }),
    }),

  billingOverview: () =>
    apiCall<AdminBillingOverview>('/admin/billing/overview'),

  billingSubscriptions: () =>
    apiCall<{ subscriptions: AdminSubscription[] }>('/admin/billing/subscriptions'),

  billingPayments: (status = '') =>
    apiCall<{ payments: AdminPayment[] }>(`/admin/billing/payments${status ? `?status=${encodeURIComponent(status)}` : ''}`),

  revenueSummary: () =>
    apiCall<{ total_sar: number; today_sar: number; mrr_sar: number; paid_count: number; failed_count: number; avg_payment_sar: number }>(
      '/admin/revenue/summary',
    ),

  revenueTimeseries: (days = 30) =>
    apiCall<{ days: number; points: Array<{ date: string; revenue_sar: number }> }>(`/admin/revenue/timeseries?days=${days}`),

  aiUsage: () =>
    apiCall<{ tenants: AdminAIUsageTenant[] }>('/admin/ai/usage'),

  aiUsageTenant: (tenantId: number) =>
    apiCall<AdminAIUsageTenant>(`/admin/ai/usage/${tenantId}`),

  aiCosts: () =>
    apiCall<{ estimated_total_cost_usd: number; estimated_total_tokens: number; tenants: Array<{ tenant_id: number; tenant_name: string; estimated_total_cost_usd: number; estimated_total_tokens: number }> }>(
      '/admin/ai/costs',
    ),

  aiProviders: () =>
    apiCall<{ providers: Array<{ provider: string; count: number }>; models: Array<{ model: string; count: number }> }>('/admin/ai/providers'),

  systemHealth: () =>
    apiCall<AdminSystemHealth>('/admin/system/health'),

  systemDependencies: () =>
    apiCall<Record<string, unknown>>('/admin/system/dependencies'),

  tenantIsolation: () =>
    apiCall<{ all_checks_passed: boolean; issues: string[]; checked_at: string }>('/admin/system/tenant-isolation'),

  systemEvents: (params?: { category?: string; severity?: string; tenant_id?: number; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (params?.category) qs.set('category', params.category)
    if (params?.severity) qs.set('severity', params.severity)
    if (params?.tenant_id !== undefined) qs.set('tenant_id', String(params.tenant_id))
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<{ total: number; offset: number; limit: number; events: AdminSystemEvent[] }>(`/admin/system/events${query}`)
  },

  globalFeatures: () =>
    apiCall<AdminFeatureFlags>('/admin/features'),

  updateGlobalFeature: (featureKey: string, enabled: boolean) =>
    apiCall<{ feature_key: string; enabled: boolean; features: Record<string, boolean> }>(`/admin/features/${featureKey}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),

  tenantFeatures: (tenantId: number) =>
    apiCall<{ tenant_id: number; features: Record<string, boolean>; global_defaults: Record<string, boolean> }>(
      `/admin/tenants/${tenantId}/features`,
    ),

  updateTenantFeature: (tenantId: number, featureKey: string, enabled: boolean) =>
    apiCall<{ tenant_id: number; feature_key: string; enabled: boolean; features: Record<string, boolean> }>(
      `/admin/tenants/${tenantId}/features/${featureKey}`,
      {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
      },
    ),

  troubleshootTenant: (tenantId: number) =>
    apiCall<AdminTroubleshootingSummary>(`/admin/troubleshooting/tenants/${tenantId}`),

  troubleshootTenantWhatsApp: (tenantId: number) =>
    apiCall<{
      tenant_id: number
      tenant_name: string
      connection: Record<string, unknown>
      usage: Array<Record<string, unknown>>
    }>(`/admin/troubleshooting/tenants/${tenantId}/whatsapp`),

  troubleshootTenantIntegrations: (tenantId: number) =>
    apiCall<{
      tenant_id: number
      tenant_name: string
      integrations: Array<Record<string, unknown>>
      sync_jobs: Array<Record<string, unknown>>
    }>(`/admin/troubleshooting/tenants/${tenantId}/integrations`),
}
