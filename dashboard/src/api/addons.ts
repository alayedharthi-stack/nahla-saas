import { apiCall } from './client'

export type AddonBadge = 'free' | 'paid' | 'coming_soon'

export interface AddonItem {
  key:          string
  name:         string
  description:  string
  badge:        AddonBadge
  has_settings: boolean
  is_enabled:   boolean
  settings:     Record<string, unknown>
}

export interface AddonsListResponse {
  addons: AddonItem[]
}

export const addonsApi = {
  list: () =>
    apiCall<AddonsListResponse>('/merchant/addons'),

  toggle: (key: string, enabled: boolean) =>
    apiCall<AddonItem>(`/merchant/addons/${key}/toggle`, {
      method: 'POST',
      body:   JSON.stringify({ enabled }),
    }),

  updateSettings: (key: string, settings: Record<string, unknown>) =>
    apiCall<AddonItem>(`/merchant/addons/${key}/settings`, {
      method: 'PUT',
      body:   JSON.stringify({ settings }),
    }),
}
