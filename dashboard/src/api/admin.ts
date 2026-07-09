import { apiCall } from './client'

export interface CoexistenceIntegrationCompleteness {
  truly_connected: boolean
  reason_code: string | null
  missing_fields: string[]
  db_status?: string | null
}

export type WhatsAppConnectRequestKind = 'assisted_connect' | 'coexistence' | 'manual_help'

export interface CoexistenceRequest {
  request_id?: number | null
  request_kind?: WhatsAppConnectRequestKind | null
  tenant_id: number
  tenant_name: string | null
  merchant_email: string | null
  merchant_phone: string | null
  wa_status: string
  connection_type: string | null
  provider: string | null
  requested_phone: string | null
  display_name: string | null
  notes: string | null
  submitted_at: string | null
  has_whatsapp_business_app: boolean | null
  phone_number_id: string | null
  waba_id: string | null
  channel_id: string | null
  client_id: string | null
  has_api_key: boolean
  last_attempt_at: string | null
  last_error: string | null
  sending_enabled: boolean
  webhook_verified: boolean
  connected_at: string | null
  integration_complete: CoexistenceIntegrationCompleteness
  webhooks: CoexistenceWebhookBlock
}

export interface CoexistenceActivatePayload {
  tenant_id: number
  phone_number_id: string
  phone_number: string
  display_name?: string
  waba_id?: string
  api_key: string
  channel_id?: string
  client_id?: string
  configure_webhook?: boolean
  action_required_message?: string
}

/** Per-URL block describing each of the three 360dialog webhooks Nahla supports. */
export interface CoexistenceWebhookBlock {
  channel_url: string
  channel_status: string
  channel_last_received_at: string | null
  coexistence_url: string
  coexistence_status: string
  coexistence_last_received_at: string | null
  status_url: string
  status_status: string
  status_last_received_at: string | null
  internal_header_name: string
}

export interface CoexistenceTestWebhookResult {
  tenant_id: number
  all_ok: boolean
  results: Record<string, {
    ok: boolean
    url: string
    status_code?: number
    body?: string
    error?: string
  }>
}

export interface CoexistenceVerifyWebhookResult {
  tenant_id: number
  expected_url: string
  remote_url: string
  matches: boolean
  verify_error?: string | null
  raw: unknown
  webhooks: CoexistenceWebhookBlock
}

export interface CoexistenceAutoConfigureResult {
  tenant_id: number
  ok: boolean
  result: unknown
  webhooks: CoexistenceWebhookBlock
}

/**
 * Comprehensive read-only diagnostic snapshot returned by
 * `GET /whatsapp/admin/coexistence/diagnose`. Decouples the three
 * independent signals support needs to distinguish "Invalid api token"
 * from "URL mismatch" from "messages never arrived":
 *
 *   1. token_check       — does the api_key on the row authenticate
 *                          against 360dialog's REST API right now?
 *   2. registration      — what URL does 360dialog have on file?
 *   3. inbound_evidence  — has Nahla actually received any webhooks?
 *
 * Plus duplicates: every other WhatsAppConnection row that shares the
 * tenant's phone_number_id / channel_id / display phone — the #1 cause
 * of "Auto Configure failed but Test Webhook works" symptoms.
 */
export interface CoexistenceDiagnoseResult {
  tenant_id: number
  request_id: string
  connection: {
    found: boolean
    connection_id: number | null
    provider: string
    connection_type: string | null
    status: string | null
    phone_number_id: string | null
    waba_id: string | null
    phone_number: string | null
    channel_id: string | null
    api_key_present: boolean
    api_key_tail: string
    webhook_verified: boolean
    sending_enabled: boolean
    last_webhook_received_at: string | null
    last_coexistence_received_at: string | null
    last_status_received_at: string | null
    last_error: string | null
  }
  token_check: {
    channel_endpoint_ok: boolean
    channel_status_code: number | null
    channel_body_preview: string
    waba_endpoint_ok: boolean
    waba_status_code: number | null
    waba_body_preview: string
    verdict: 'valid' | 'rejected' | 'transport_error' | 'no_token'
  }
  registration: {
    expected_url: string
    channel_remote_url: string | null
    channel_matches: boolean
    waba_remote_url: string | null
    waba_matches: boolean
    waba_id_remote: string | null
    numbers_on_this_waba: string[]
    phone_id_drift: boolean
  }
  inbound_evidence: {
    channel_received_recently: boolean
    coexistence_received_recently: boolean
    status_received_recently: boolean
    any_inbound_ever: boolean
    freshness_seconds: {
      channel: number | null
      coexistence: number | null
      status: number | null
    }
  }
  duplicates: {
    by_phone_number_id: CoexistenceDuplicateRow[]
    by_channel_id: CoexistenceDuplicateRow[]
    by_display_phone: CoexistenceDuplicateRow[]
    has_duplicates: boolean
  }
  webhooks: CoexistenceWebhookBlock
}

export interface CoexistenceDuplicateRow {
  tenant_id: number
  connection_id: number
  status: string | null
  provider: string
  connection_type: string | null
  phone_number_id: string | null
  phone_number: string | null
  channel_id: string | null
  is_this_tenant: boolean
}

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

export interface AdminTenantSummary {
  id: number
  name: string
  domain: string | null
  is_active: boolean
  created_at: string | null
  /** null = visible by default; non-null = would be hidden in filtered view */
  visibility_tag: VisibilityTag | null
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

  previewManualGiftGrant: (tenantId: number, body: { days?: number; reason: string }) =>
    apiCall<{ preview: Record<string, unknown> }>(
      `/admin/tenants/${tenantId}/manual-gift-grant/preview`,
      {
        method: 'POST',
        body: JSON.stringify({ days: body.days ?? 30, reason: body.reason }),
      },
    ),

  applyManualGiftGrant: (tenantId: number, body: { days?: number; reason: string }) =>
    apiCall<{ grant: Record<string, unknown>; snapshot: AdminManualGiftGrantSnapshot }>(
      `/admin/tenants/${tenantId}/manual-gift-grant`,
      {
        method: 'POST',
        body: JSON.stringify({ days: body.days ?? 30, reason: body.reason }),
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

  coexistenceRequests: (statusFilter = 'request_submitted') =>
    apiCall<{ requests: CoexistenceRequest[]; total: number }>(
      `/admin/coexistence/requests?status_filter=${encodeURIComponent(statusFilter)}`,
      { signal: AbortSignal.timeout(25_000) },
    ),

  activateCoexistence: (payload: CoexistenceActivatePayload) =>
    apiCall<Record<string, unknown>>('/whatsapp/admin/coexistence/activate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** Self-test: probe Nahla's three Coexistence URLs from the dashboard side. */
  testCoexistenceWebhook: (tenantId: number) =>
    apiCall<CoexistenceTestWebhookResult>('/whatsapp/admin/coexistence/test-webhook', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),

  /** Read the channel webhook 360dialog has on file and compare with Nahla's. */
  verifyCoexistenceWebhook: (tenantId: number) =>
    apiCall<CoexistenceVerifyWebhookResult>('/whatsapp/admin/coexistence/verify-webhook', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),

  /** One-click: push Nahla's URL + secret header to 360dialog. */
  autoConfigureCoexistenceWebhook: (tenantId: number) =>
    apiCall<CoexistenceAutoConfigureResult>('/whatsapp/admin/coexistence/auto-configure', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),

  /**
   * Comprehensive read-only diagnostic snapshot for the tenant. Surfaces
   * the three independent signals (token / registration / inbound) plus
   * duplicate detection across tenants. Use this BEFORE running
   * Verify / Auto Configure when troubleshooting "Invalid api token" or
   * "webhook never arrives" reports — the answer is almost always in
   * one of those three columns, and the existing Verify endpoint only
   * answers the middle one.
   */
  diagnoseCoexistence: (tenantId: number) =>
    apiCall<CoexistenceDiagnoseResult>(
      `/whatsapp/admin/coexistence/diagnose?tenant_id=${tenantId}`,
    ),

  /**
   * Force a status-reconciliation pass. When operational health is green
   * (token + phone_id + waba_id + recent webhook traffic) but the row's
   * ``status`` field is stale, this promotes it to ``connected`` so the
   * owner panel and merchant page agree with reality. A no-op when the
   * row is already healthy OR when the merchant explicitly disconnected.
   */
  reconcileCoexistenceStatus: (tenantId: number) =>
    apiCall<{
      tenant_id: number
      reconciled: boolean
      before: {
        status: string | null
        sending_enabled: boolean
        integration_complete: CoexistenceIntegrationCompleteness
      }
      after: {
        status: string | null
        sending_enabled: boolean
        integration_complete: CoexistenceIntegrationCompleteness
      }
      webhooks: CoexistenceWebhookBlock
    }>('/whatsapp/admin/coexistence/reconcile-status', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),

  /**
   * Sync / Repair Integration Record.
   * Re-reads channel metadata from 360dialog (Partner API + per-tenant API
   * key) and fills any missing fields on the WhatsApp connection record so
   * the merchant page stops reporting `missing_waba_id`.
   */
  syncCoexistenceRecord: (tenantId: number) =>
    apiCall<{
      tenant_id: number
      request_id: string
      before: CoexistenceIntegrationCompleteness
      after: CoexistenceIntegrationCompleteness
      resolved: {
        waba_id: string | null
        phone_number_id: string | null
        phone_number: string | null
        display_name: string | null
        channel_status: string | null
        sources: string[]
        errors: Record<string, string>
      }
      integration_complete: CoexistenceIntegrationCompleteness
    }>('/whatsapp/admin/coexistence/sync-record', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenantId }),
    }),

  /**
   * Manual edit of any field on the integration record (WABA ID, channel ID,
   * phone number id, API key, …). Only fields explicitly provided are
   * touched.
   */
  editCoexistenceRecord: (payload: {
    tenant_id: number
    waba_id?: string | null
    phone_number_id?: string | null
    phone_number?: string | null
    channel_id?: string | null
    client_id?: string | null
    api_key?: string | null
    display_name?: string | null
    promote_to_connected?: boolean
  }) =>
    apiCall<{
      tenant_id: number
      request_id: string
      changed: string[]
      integration_complete: CoexistenceIntegrationCompleteness
    }>('/whatsapp/admin/coexistence/edit-record', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // ── Salla token health & diagnosis ─────────────────────────────────────
  /** Aggregate token health across all tenants (or one). */
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
