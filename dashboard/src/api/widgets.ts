import { apiCall } from './client'
import { getApiBase, getTenantId, getToken } from '../auth'

export type WidgetBadge    = 'free' | 'paid' | 'coming_soon'
export type WidgetCategory = 'conversion' | 'communication' | 'general'

export interface DisplayRules {
  trigger:            'entry' | 'scroll' | 'exit_intent' | 'click_tab'
  show_after_seconds: number
  show_on_pages:      string[]
  show_once_per_user: boolean
  scroll_percent?:    number
}

export interface WidgetItem {
  key:           string
  name:          string
  description:   string
  category:      WidgetCategory
  badge:         WidgetBadge
  icon:          string
  has_settings:  boolean
  is_enabled:    boolean
  settings:      Record<string, unknown>
  display_rules: DisplayRules
}

export interface WidgetsListResponse {
  widgets: WidgetItem[]
}

export interface SallaInstallResult {
  success:          boolean
  method?:          string
  reason?:          string
  message:          string
  script_tag?:      string
  embed_url?:       string
  salla_admin_url?: string
  salla_store_id?:  string
}

export const widgetsApi = {
  list: () =>
    apiCall<WidgetsListResponse>('/merchant/widgets'),

  toggle: (key: string, enabled: boolean) =>
    apiCall<WidgetItem>(`/merchant/widgets/${key}/toggle`, {
      method: 'POST',
      body:   JSON.stringify({ enabled }),
    }),

  updateSettings: (key: string, settings: Record<string, unknown>) =>
    apiCall<WidgetItem>(`/merchant/widgets/${key}/settings`, {
      method: 'PUT',
      body:   JSON.stringify({ settings }),
    }),

  updateRules: (key: string, rules: Partial<DisplayRules>) =>
    apiCall<WidgetItem>(`/merchant/widgets/${key}/rules`, {
      method: 'PUT',
      body:   JSON.stringify({ rules }),
    }),

  async uploadWhatsAppLogo(file: File): Promise<{ image_url: string }> {
    const form = new FormData()
    form.append('file', file)
    const token = getToken()
    const tenantId = getTenantId()
    const res = await fetch(`${getApiBase()}/merchant/widgets/whatsapp_widget/logo`, {
      method: 'POST',
      cache: 'no-store',
      headers: {
        ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: form,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok || !data.image_url) {
      throw new Error(typeof data.detail === 'string' ? data.detail : 'widget_logo_upload_failed')
    }
    return data as { image_url: string }
  },

  sallaInstall: () =>
    apiCall<SallaInstallResult>('/merchant/widgets/salla-install', {
      method: 'POST',
    }),
}
