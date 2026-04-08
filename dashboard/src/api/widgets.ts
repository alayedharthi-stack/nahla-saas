import { apiCall } from './client'

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

  sallaInstall: () =>
    apiCall<SallaInstallResult>('/merchant/widgets/salla-install', {
      method: 'POST',
    }),
}
