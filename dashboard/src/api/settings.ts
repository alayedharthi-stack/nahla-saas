export interface WhatsAppSettings {
  business_display_name: string
  phone_number: string
  phone_number_id: string
  access_token: string
  verify_token: string
  webhook_url: string
  store_button_label: string
  store_button_url: string
  owner_contact_label: string
  owner_whatsapp_number: string
  auto_reply_enabled: boolean
  transfer_to_owner_enabled: boolean
}

export interface AISettings {
  assistant_name: string
  assistant_role: string
  reply_tone: 'friendly' | 'professional' | 'sales'
  reply_length: 'short' | 'medium' | 'detailed'
  default_language: 'arabic' | 'english' | 'bilingual'
  owner_instructions: string
  coupon_rules: string
  escalation_rules: string
  allowed_discount_levels: string
  recommendations_enabled: boolean
}

export interface StoreSettings {
  store_name: string
  store_logo_url: string
  store_url: string
  platform_type: 'salla' | 'zid' | 'shopify' | 'custom'
  salla_client_id: string
  salla_client_secret: string
  salla_access_token: string
  zid_client_id: string
  zid_client_secret: string
  shopify_shop_domain: string
  shopify_access_token: string
  shipping_provider: string
  google_maps_location: string
  instagram_url: string
  twitter_url: string
  snapchat_url: string
  tiktok_url: string
}

export interface NotificationSettings {
  whatsapp_alerts: boolean
  email_alerts: boolean
  system_alerts: boolean
  failed_webhook_alerts: boolean
  low_balance_alerts: boolean
}

export interface AllSettings {
  whatsapp: WhatsAppSettings
  ai: AISettings
  store: StoreSettings
  notifications: NotificationSettings
}

const API_BASE = (import.meta.env.VITE_API_BASE ?? '') + '/api'

async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-Tenant-ID': '1',
      ...(options?.headers ?? {}),
    },
    ...options,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

export const settingsApi = {
  getAll: () => apiCall<AllSettings>('/settings'),

  update: (data: Partial<AllSettings>) =>
    apiCall<AllSettings>('/settings', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  testWhatsApp: () =>
    apiCall<{ success: boolean; message: string }>('/settings/test-whatsapp', {
      method: 'POST',
    }),
}
