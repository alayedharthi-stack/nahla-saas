export interface StoreIntegrationStatus {
  configured: boolean
  platform: string | null
  store_id: string
  api_key_hint: string
  enabled: boolean
}

export interface StoreIntegrationInput {
  platform: string
  api_key: string
  store_id: string
  webhook_secret?: string
  enabled: boolean
}

export interface StoreIntegrationTestResult {
  status: 'ok' | 'error' | 'not_configured'
  platform?: string
  products_found?: number
  error?: string
  sample?: Record<string, unknown>
}

import { apiCall } from './client'

export const storeIntegrationApi = {
  getSettings: () =>
    apiCall<StoreIntegrationStatus>('/store-integration/settings'),

  saveSettings: (input: StoreIntegrationInput) =>
    apiCall<{ status: string; platform: string; enabled: boolean }>(
      '/store-integration/settings',
      { method: 'PUT', body: JSON.stringify(input) }
    ),

  disable: () =>
    apiCall<{ status: string }>('/store-integration/settings', { method: 'DELETE' }),

  test: () =>
    apiCall<StoreIntegrationTestResult>('/store-integration/test'),
}
