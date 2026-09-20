import { apiCall } from './client'


export type WhatsAppConnectRequestKind = 'assisted_connect' | 'coexistence' | 'manual_help'









export interface AdminPlatformStats {
  merchants: { total: number; active: number; trial: number; paid: number; suspended: number }
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
  // SaaS owner fields
  new_this_week: number
  wa_connected:  number
  onboarding: {
    registered_only: number
    salla_only:      number
    whatsapp_only:   number
    both_connected:  number
  }
  at_risk: {
    trials_expiring_7d: number
    salla_needs_reauth: number
    suspended:          number
  }
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

export type VisibilityTag = 'archived' | 'disconnected' | 'test' | 'pending_payment'

export type BillingAccessKind =
  | 'paid'
  | 'gift'
  | 'trial'
  | 'pending_payment'
  | 'none'
  | 'store_disabled'

export interface AdminBillingDisplay {
  billing_access_kind: BillingAccessKind
  billing_access_label_ar: string
  billing_plan_slug: string | null
  billing_ends_at: string | null
  gift_active: boolean
  gift_ends_at: string | null
}

export interface AdminTenantSummary {
  id: number
  name: string
  domain: string | null
  is_active: boolean
  created_at: string | null
  /** null = visible by default; non-null = would be hidden in filtered view */
  visibility_tag: VisibilityTag | null
  billing_display: AdminBillingDisplay
  subscription: {
    status: string
    plan: string
    trial_ends_at: string | null
    ends_at: string | null
  }
  whatsapp: {
    status: string
    phone_number: string | null
    phone_number_id: string | null
    whatsapp_business_account_id: string | null
    business_display_name: string | null
    sending_enabled: boolean
    webhook_verified: boolean
    connection_type: string | null
    provider: string | null
    connected_at: string | null
    disconnect_reason: string | null
    disconnected_at: string | null
  }
  stats: {
    orders: number
    conversations: number
    revenue_sar: number
  }
  integration: {
    integration_id: number | null
    external_store_id: string | null
    enabled: boolean | null
    provider: string | null
  }
}

export interface AdminManualGiftGrantSnapshot {
  tenant_id: number
  store_name: string
  domain: string | null
  owner_email: string | null
  owner_phone: string | null
  entitlements: {
    plan_slug: string
    billing_status: string
    is_active: boolean
  }
  trial: {
    is_trial: boolean
    trial_expired: boolean
    trial_ends_at: string | null
    trial_days_remaining: number
  }
  salla: {
    billing_status: string | null
    plan_slug: string | null
  }
  nahla_subscription: {
    active: boolean
    status: string | null
    plan_slug: string | null
    ends_at: string | null
  }
  gift: {
    active: boolean
    blob: Record<string, unknown> | null
    manual_gift_grant_active?: boolean
    manual_gift_grant_reason?: string | null
    manual_gift_grant_plan_slug?: string | null
    manual_gift_grant_ends_at?: string | null
    manual_gift_grant_permanent?: boolean
    manual_gift_grant_billing_status?: string | null
  }
  can_grant: boolean
  grant_blocked_reason: string | null
  grant_blocked_message_ar: string | null
}

export interface AdminTenantsResponse {
  total: number
  total_active: number
  total_hidden: number
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
  period?: string
  turns_total: number
  turns_orchestrated: number
  ai_actions_logged: number
  avg_latency_ms: number
  calls_total: number
  actual_total_tokens: number
  estimated_total_tokens: number
  actual_total_cost_usd: number
  estimated_total_cost_usd: number
  unattributed_total_cost_usd: number
  models: Array<{ model: string; count: number }>
  providers: Array<{ provider: string; count: number }>
  reasons: Array<{ reason: string; count: number }>
}

export interface AdminAICostsSummary {
  period: string
  actual_total_cost_usd: number
  estimated_total_cost_usd: number
  unattributed_total_cost_usd: number
  actual_total_tokens: number
  estimated_total_tokens: number
  calls_total: number
  providers: Array<{ provider: string; count: number }>
  models: Array<{ model: string; count: number }>
  reasons: Array<{ reason: string; count: number }>
  tenants: Array<{
    tenant_id: number
    tenant_name: string
    store_id: number
    actual_total_cost_usd: number
    estimated_total_cost_usd: number
    total_cost_usd: number
    actual_total_tokens: number
    estimated_total_tokens: number
    calls_total: number
  }>
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

  tenants: (params?: {
    search?: string
    status?: '' | 'active' | 'inactive'
    show_all?: boolean
    limit?: number
    offset?: number
  }) => {
    const qs = new URLSearchParams()
    if (params?.search) qs.set('search', params.search)
    if (params?.status) qs.set('status', params.status)
    if (params?.show_all) qs.set('show_all', 'true')
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    const query = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<AdminTenantsResponse>(`/admin/tenants${query}`)
  },

  tenantSummary: (tenantId: number) =>
    apiCall<AdminTenantSummary>(`/admin/tenants/${tenantId}/summary`),

  manualGiftGrantSnapshot: (tenantId: number) =>
    apiCall<AdminManualGiftGrantSnapshot>(`/admin/tenants/${tenantId}/manual-gift-grant`),

  previewManualGiftGrant: (tenantId: number, body: { days?: number; permanent?: boolean; force?: boolean; reason: string }) =>
    apiCall<{ preview: Record<string, unknown> }>(
      `/admin/tenants/${tenantId}/manual-gift-grant/preview`,
      {
        method: 'POST',
        body: JSON.stringify({
          days: body.permanent ? undefined : (body.days ?? 30),
          permanent: Boolean(body.permanent),
          force: Boolean(body.force),
          reason: body.reason,
        }),
      },
    ),

  applyManualGiftGrant: (tenantId: number, body: { days?: number; permanent?: boolean; force?: boolean; reason: string }) =>
    apiCall<{ grant: Record<string, unknown>; snapshot: AdminManualGiftGrantSnapshot }>(
      `/admin/tenants/${tenantId}/manual-gift-grant`,
      {
        method: 'POST',
        body: JSON.stringify({
          days: body.permanent ? undefined : (body.days ?? 30),
          permanent: Boolean(body.permanent),
          force: Boolean(body.force),
          reason: body.reason,
        }),
      },
    ),

  revokeManualGiftGrant: (tenantId: number) =>
    apiCall<{ revoke: Record<string, unknown>; snapshot: AdminManualGiftGrantSnapshot }>(
      `/admin/tenants/${tenantId}/manual-gift-grant/revoke`,
      { method: 'POST' },
    ),

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

  aiUsage: (period = '7d') =>
    apiCall<{ tenants: AdminAIUsageTenant[]; period: string }>(`/admin/ai/usage?period=${period}`),

  aiUsageTenant: (tenantId: number, period = '7d') =>
    apiCall<AdminAIUsageTenant>(`/admin/ai/usage/${tenantId}?period=${period}`),

  aiCosts: (period = '7d') =>
    apiCall<AdminAICostsSummary>(`/admin/ai/costs?period=${period}`),

  aiProviders: (period = '7d') =>
    apiCall<{ period: string; providers: Array<{ provider: string; count: number }>; models: Array<{ model: string; count: number }> }>(
      `/admin/ai/providers?period=${period}`,
    ),

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


  sallaTokenStatus: (params?: { tenant_id?: number; enabled_only?: boolean; secret?: string }) => {
    const qs = new URLSearchParams()
    if (params?.tenant_id !== undefined) qs.set('tenant_id', String(params.tenant_id))
    if (params?.enabled_only) qs.set('enabled_only', 'true')
    if (params?.secret) qs.set('secret', params.secret)
    const query = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<SallaTokenStatusResponse>(`/admin/salla/integrations/token-status${query}`)
  },

  /** Deep diagnosis: every Salla integration row + sibling/duplicate grouping. */
  sallaDiagnose: (tenantId: number, secret?: string) => {
    const qs = secret ? `?secret=${encodeURIComponent(secret)}` : ''
    return apiCall<SallaDiagnoseResponse>(`/admin/salla/diagnose/${tenantId}${qs}`)
  },

  /** Manually trigger a Salla token refresh for one integration. */
  sallaForceRefresh: (integrationId: number, opts?: { dry_run?: boolean; secret?: string }) => {
    const qs = new URLSearchParams()
    if (opts?.dry_run) qs.set('dry_run', 'true')
    if (opts?.secret) qs.set('secret', opts.secret)
    const query = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<SallaForceRefreshResponse>(
      `/admin/salla/integrations/${integrationId}/refresh${query}`,
      { method: 'POST' },
    )
  },
}

// ── Salla token-health response shapes ─────────────────────────────────────

export interface SallaTokenRow {
  tenant_id: number
  integration_id: number
  store_id: string | null
  store_name: string | null
  enabled: boolean
  easy_mode: boolean
  app_type: string | null
  token_source: string | null
  has_access_token: boolean
  has_refresh_token: boolean
  created_at: string | null
  updated_at: string | null
  expires_at: string | null
  token_expires_at: string | null
  days_until_expiry: number | null
  expiry_health: 'ok' | 'warning' | 'critical' | 'expired' | 'unknown'
  refresh_token_received_at: string | null
  last_successful_refresh: string | null
  last_token_refresh_at: string | null
  last_failed_refresh: string | null
  token_refresh_status: string | null
  token_refresh_error: string | null
  token_refresh_failed_at: string | null
  first_failure_at: string | null
  refresh_attempts: number
  token_refresh_attempts: number
  needs_reauth: boolean
  needs_reauth_reason: string | null
  needs_reauth_at: string | null
  reauth_reason: string | null
  token_reauth_alert_sent_at: string | null
  alert_suppressed: boolean
  alert_suppressed_reason: string | null
  alert_suppressed_by: number | null
  superseded: boolean
  superseded_by_integration_id: number | null
  superseded_at: string | null
  no_auto_refresh: boolean
  no_auto_refresh_reason: string | null
  connected_at: string | null
  shadow?: boolean
  newest_healthy_sibling_id?: number | null
}

export interface SallaTokenStatusResponse {
  ok: boolean
  summary: {
    total: number
    expiry_ok: number
    expiry_warning: number
    expiry_critical: number
    expiry_expired: number
    expiry_unknown: number
    needs_reauth: number
    failed_last_refresh: number
    no_refresh_token: number
  }
  integrations: SallaTokenRow[]
  hint?: string
}

export interface SallaDiagnoseResponse {
  ok: boolean
  tenant_id: number
  selected: SallaTokenRow | null
  all: SallaTokenRow[]
  store_groups: Record<string, SallaTokenRow[]>
  summary: {
    total: number
    stores: number
    duplicate_stores: number
    needs_reauth: number
    superseded: number
    alert_suppressed: number
  }
}

export interface SallaForceRefreshResponse {
  ok: boolean
  outcome?: 'refreshed' | 'invalid_grant_needs_reauth' | 'superseded_invalid_grant' | 'transient_failure'
  reason?: string
  error?: string
  dry_run?: boolean
  superseded_by?: number
  salla_response?: {
    status: number | null
    body: unknown
  }
  before?: SallaTokenRow
  after?: SallaTokenRow
  note?: string
}
