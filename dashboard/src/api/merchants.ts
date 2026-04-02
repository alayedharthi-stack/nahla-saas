import { apiCall } from './client'

export interface Merchant {
  id:         number
  email:      string
  role:       string
  is_active:  boolean
  tenant_id:  number
  store_name: string
  created_at: string | null
}

export interface CreateMerchantPayload {
  email:      string
  password:   string
  store_name: string
  phone?:     string
}

export const merchantsApi = {
  list: () =>
    apiCall<{ merchants: Merchant[] }>('/admin/merchants'),

  create: (payload: CreateMerchantPayload) =>
    apiCall<Merchant>('/admin/merchants', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  toggle: (id: number) =>
    apiCall<Merchant>(`/admin/merchants/${id}/toggle`, { method: 'PUT' }),

  remove: (id: number) =>
    apiCall<{ deleted: boolean }>(`/admin/merchants/${id}`, { method: 'DELETE' }),
}
