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

export type StoreAIMode = 'off' | 'test' | 'on'

export interface AISettings {
  /** Store-wide master switch — legacy mirror of store_ai_mode=on. */
  store_ai_enabled: boolean
  /** off | test | on — test replies only to ai_test_allowed_numbers. */
  store_ai_mode?: StoreAIMode
  ai_test_allowed_numbers?: string[]
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
  /**
   * Free-form merchant-supplied store knowledge.  Edited on the dedicated
   * "قاعدة المعرفة" page (KnowledgeBase.tsx), kept separate from
   * `owner_instructions` because behaviour ≠ facts.  Empty string by
   * default.  See `backend/modules/ai/prompts/tenant_overlay.py` for the
   * Salla-precedence rules baked into the prompt overlay.
   */
  manual_knowledge_base: string
  // ── Merchant-configurable policy rules (Phase 11) ────────────────────
  coupon_cap_hours: number
  auto_escalate_after_n: number
  max_order_value: number
  context_verbosity: 'full' | 'compact'
}

export interface StoreSettings {
  store_name: string
  store_name_ar?: string
  store_name_en?: string
  store_name_ar_source?: string
  store_name_en_source?: string
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
  sales_channels?: {
    online_store?: { enabled?: boolean }
    whatsapp_quick_order?: { enabled?: boolean }
    showroom_visit?: { enabled?: boolean }
  }
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

export interface StoreAIPatchPayload {
  store_ai_enabled?: boolean
  store_ai_mode?: StoreAIMode
  ai_test_allowed_numbers?: string[]
}

export interface StoreAIPatchResponse {
  ok: boolean
  store_ai_enabled: boolean
  store_ai_mode: StoreAIMode
  ai_test_allowed_numbers: string[]
  ai: AISettings
}

export interface SalesChannelAvailabilitySlot {
  enabled?: boolean
  available?: boolean
  evidence?: string
}

export interface SalesChannelAvailability {
  online_store?: SalesChannelAvailabilitySlot
  whatsapp_quick_order?: SalesChannelAvailabilitySlot
  showroom_visit?: SalesChannelAvailabilitySlot
  maps_url?: string
  store_url?: string
}

import { apiCall } from './client'

export interface AllSettings {
  whatsapp: WhatsAppSettings
  ai: AISettings
  store: StoreSettings
  notifications: NotificationSettings
  sales_channel_availability?: SalesChannelAvailability
}

export const settingsApi = {
  getAll: () => apiCall<AllSettings>('/settings'),

  update: (data: Partial<AllSettings>) =>
    apiCall<AllSettings>('/settings', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  patchStoreAI: (payload: StoreAIPatchPayload) =>
    apiCall<StoreAIPatchResponse>('/settings/ai', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  testWhatsApp: () =>
    apiCall<{ success: boolean; message: string }>('/settings/test-whatsapp', {
      method: 'POST',
    }),
}
