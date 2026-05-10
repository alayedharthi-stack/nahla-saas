import { apiCall } from './client'

export interface CustomerRfmScores {
  recency: number
  frequency: number
  monetary: number
  total: number
  code: string | null
}

export interface CustomerRecord {
  id: number
  name: string
  phone: string
  email: string
  source: string
  source_label: string
  status: string
  status_label: string
  customer_status: string
  customer_status_label: string
  segment: string
  segment_label: string
  rfm_segment: string
  rfm_segment_label: string
  rfm_scores: CustomerRfmScores
  rfm_recency_score: number
  rfm_frequency_score: number
  rfm_monetary_score: number
  rfm_total_score: number
  rfm_code: string | null
  orders_count: number
  total_orders: number
  total_spent: number
  total_spend: number
  avg_order_value: number
  average_order_value: number
  first_order_at: string | null
  first_order_date: string | null
  last_order_at: string | null
  last_order_date: string | null
  first_seen_at: string | null
  last_seen_at: string | null
  metrics_computed_at: string | null
  last_recomputed_reason: string | null
  days_since_last_order: number | null
  churn_risk_score: number
  lifetime_value_score: number
  is_returning: boolean
  is_unsubscribed: boolean
  unsubscribed_at: string | null
  resubscribed_at: string | null
  pending_unsubscribe: boolean
  pending_unsubscribe_at: string | null
  /** Merchant-driven exclusion from manual marketing campaigns.
   *  Distinct from `is_unsubscribed` (customer-driven). */
  marketing_opt_out_manual: boolean
  marketing_opt_out_manual_at: string | null
  /** Internal flag for the campaign test list — no merchant-visible
   *  tag, but the wizard can target it as a dry-run audience. */
  is_campaign_test_recipient: boolean
  /** Manual Nahla segment tags pinned by the merchant (e.g. ['vip',
   *  'unsubscribed']). Always a subset of the official Nahla
   *  registry — backend rejects unknown keys with 422.
   *  Note: this list reflects ONLY ``include`` rows. Excludes are
   *  surfaced via ``segment_sources`` instead. */
  manual_segments: string[]
  manual_segments_labels: string[]
  /** Per-segment source breakdown.
   *
   *  Shape: ``{ <segment_key>: { automatic, manual_include, manual_exclude } }``
   *
   *  Only segments where at least one of the three booleans is true
   *  appear here. Used by the drawer to render labels like:
   *    "VIP يدوي + تلقائي"  — automatic && manual_include
   *    "VIP يدوي"            — !automatic && manual_include
   *    "VIP تلقائي"          — automatic && !manual_include
   *    "مستبعد يدويًا من VIP" — manual_exclude
   *
   *  Filter formula (cemented backend-side too):
   *    member ⇔ (automatic ∨ manual_include) ∧ ¬ manual_exclude
   */
  segment_sources?: Record<string, {
    automatic: boolean
    manual_include: boolean
    manual_exclude: boolean
  }>
}

export interface CustomersListResponse {
  customers: CustomerRecord[]
  total: number
  page: number
  per_page: number
  pages: number
}

export interface CustomersMetricsResponse {
  totalCustomers: number
  activeCustomers: number
  vipCustomers: number
  newCustomers: number
  atRiskCustomers: number
  inactiveCustomers: number
  leads: number
  statusCounts: Record<string, number>
  rfmSegmentCounts: Record<string, number>
}

export interface CustomerCreatePayload {
  name: string
  phone: string
  email?: string
}

export interface CustomerSegmentMeta {
  key: string
  label_ar: string
  label_en: string
  description_ar: string
  /** Long, plain-Arabic explanation of which customers fall into this
   *  cohort. Returned by both /customers/segments and
   *  /campaigns/wizard/segments — surfaced in the info popover so the
   *  merchant knows exactly what each chip means. */
  criteria_ar: string
  icon: string
  natural_goals: string[]
  /** CRM `customer_status` values this cohort consumes (for docs only). */
  crm_statuses: string[]
  /** RFM bucket values this cohort consumes (for docs only). */
  rfm_buckets: string[]
  customer_count: number
}

export interface CustomersSegmentsResponse {
  segments: CustomerSegmentMeta[]
}

export interface CustomersListFilters {
  search?: string
  page?: number
  perPage?: number
  /** Auto Nahla segment key (e.g. 'vip'). */
  segment?: string
  /** Manual segment key, OR the special string 'none' to filter
   *  customers with NO manual tags. */
  manualSegment?: string
  marketingOptOut?: boolean
  testRecipient?: boolean
}

export interface CustomerSegmentMutationResponse {
  customer_id: number
  segment_key?: string
  /** "include" or "exclude" — only present on add (POST). */
  mode?: 'include' | 'exclude'
  label_ar?: string
  source?: string
  created_at?: string | null
  /** "deleted" | "excluded" | "noop" — only present on smart-remove (DELETE). */
  action?: 'deleted' | 'excluded' | 'noop'
  /** True when the auto classifier currently considers this customer
   *  to belong to the segment (used by the smart-remove decision). */
  auto_match?: boolean
  manual_segments: string[]
  /** Full {segment_key: mode} map after the mutation. */
  manual_sources?: Record<string, 'include' | 'exclude'>
}

export interface CustomerMarketingPreferences {
  customer_id: number
  marketing_opt_out_manual: boolean
  marketing_opt_out_manual_at: string | null
  is_campaign_test_recipient: boolean
  campaign_test_recipient_at: string | null
}

export const customersApi = {
  list(filters: CustomersListFilters | string = '', page = 1, perPage = 50, segment = '') {
    // Backwards-compat: old positional signature `list(search, page, perPage, segment)`
    // is preserved; the new object form is preferred for the new manual filters.
    const f: CustomersListFilters =
      typeof filters === 'string'
        ? { search: filters, page, perPage, segment }
        : filters
    const params = new URLSearchParams()
    if (f.search) params.set('search', f.search)
    if (f.segment && f.segment !== 'all') params.set('segment', f.segment)
    if (f.manualSegment) params.set('manual_segment', f.manualSegment)
    if (f.marketingOptOut !== undefined) params.set('marketing_opt_out', String(f.marketingOptOut))
    if (f.testRecipient !== undefined) params.set('test_recipient', String(f.testRecipient))
    params.set('page', String(f.page ?? 1))
    params.set('per_page', String(f.perPage ?? 50))
    return apiCall<CustomersListResponse>(`/customers?${params}`)
  },

  addManualSegment(id: number, segment_key: string) {
    return apiCall<CustomerSegmentMutationResponse>(`/customers/${id}/segments`, {
      method: 'POST',
      body: JSON.stringify({ segment_key }),
    })
  },

  removeManualSegment(id: number, segment_key: string) {
    return apiCall<CustomerSegmentMutationResponse>(
      `/customers/${id}/segments/${encodeURIComponent(segment_key)}`,
      { method: 'DELETE' },
    )
  },

  updateMarketingPreferences(
    id: number,
    prefs: Partial<{ marketing_opt_out_manual: boolean; is_campaign_test_recipient: boolean }>,
  ) {
    return apiCall<CustomerMarketingPreferences>(
      `/customers/${id}/marketing-preferences`,
      { method: 'PATCH', body: JSON.stringify(prefs) },
    )
  },

  segments() {
    return apiCall<CustomersSegmentsResponse>('/customers/segments')
  },

  metrics() {
    return apiCall<CustomersMetricsResponse>('/customers/metrics')
  },

  get(id: number) {
    return apiCall<CustomerRecord>(`/customers/${id}`)
  },

  create(data: CustomerCreatePayload) {
    return apiCall<{ id: number; message: string }>('/customers', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  },

  update(id: number, data: Partial<CustomerCreatePayload>) {
    return apiCall<{ updated: boolean }>(`/customers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    })
  },

  delete(id: number) {
    return apiCall<{ deleted: boolean }>(`/customers/${id}`, {
      method: 'DELETE',
    })
  },

  bulkDelete(ids: number[]) {
    return apiCall<{ deleted: number }>('/customers/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    })
  },

  deleteAll() {
    return apiCall<{ deleted: number }>('/customers/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ delete_all: true }),
    })
  },
}
