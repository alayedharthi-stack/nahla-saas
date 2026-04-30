export interface SupersededIntegration {
  id: number
  enabled: boolean
  store_id: string
  api_key_hint: string
  easy_mode: boolean
  api_key_source: string
  superseded_at?: string | null
  superseded_reason?: string | null
}

export interface StoreIntegrationStatus {
  configured: boolean
  platform: string | null
  integration_id?: number
  store_id: string
  store_name?: string
  api_key_hint: string
  enabled: boolean
  easy_mode?: boolean
  api_key_source?: string
  app_type?: string
  connected_at?: string | null
  sync_error?: string | null
  no_auto_refresh?: boolean
  needs_reauth?: boolean
  superseded_integrations?: SupersededIntegration[]
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
