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

export const customersApi = {
  list(search = '', page = 1, perPage = 50, segment = '') {
    const params = new URLSearchParams()
    if (search) params.set('search', search)
    if (segment && segment !== 'all') params.set('segment', segment)
    params.set('page', String(page))
    params.set('per_page', String(perPage))
    return apiCall<CustomersListResponse>(`/customers?${params}`)
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
