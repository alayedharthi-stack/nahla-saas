import { apiCall } from './client'

export interface CustomerRecord {
  id: number
  name: string
  phone: string
  email: string
  source: string
  source_label: string
  segment: string
  segment_label: string
  total_orders: number
  total_spend: number
  average_order_value: number
  last_order_at: string | null
  first_seen_at: string | null
  churn_risk_score: number
  is_returning: boolean
}

export interface CustomersListResponse {
  customers: CustomerRecord[]
  total: number
  page: number
  per_page: number
  pages: number
}

export interface CustomerCreatePayload {
  name: string
  phone: string
  email?: string
}

export const customersApi = {
  list(search = '', page = 1, perPage = 50) {
    const params = new URLSearchParams()
    if (search) params.set('search', search)
    params.set('page', String(page))
    params.set('per_page', String(perPage))
    return apiCall<CustomersListResponse>(`/customers?${params}`)
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
}
