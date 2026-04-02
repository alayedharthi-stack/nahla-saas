// WhatsApp template component types
export interface WaTemplateComponent {
  type: 'HEADER' | 'BODY' | 'FOOTER' | 'BUTTONS'
  format?: 'TEXT' | 'IMAGE' | 'DOCUMENT' | 'VIDEO'
  text?: string
  buttons?: { type: string; text: string; url?: string; phone_number?: string }[]
}

export interface WaTemplate {
  id: string
  name: string
  language: string
  category: 'MARKETING' | 'UTILITY' | 'AUTHENTICATION'
  status: 'APPROVED' | 'PENDING' | 'REJECTED'
  components: WaTemplateComponent[]
}

export interface CampaignRecord {
  id: number
  name: string
  campaign_type: string
  status: 'draft' | 'scheduled' | 'active' | 'completed' | 'paused'
  template_id: string
  template_name: string
  template_language: string
  template_category: string
  template_body: string
  template_variables: Record<string, string>
  audience_type: string
  audience_count: number
  schedule_type: string
  schedule_time: string | null
  delay_minutes: number | null
  coupon_code: string
  sent_count: number
  delivered_count: number
  read_count: number
  clicked_count: number
  converted_count: number
  created_at: string | null
  launched_at: string | null
}

export interface CreateCampaignPayload {
  name: string
  campaign_type: string
  template_id: string
  template_name: string
  template_language: string
  template_category: string
  template_body: string
  template_variables: Record<string, string>
  audience_type: string
  audience_count: number
  schedule_type: 'immediate' | 'scheduled' | 'delayed'
  schedule_time?: string
  delay_minutes?: number
  coupon_code: string
}

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

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
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}

export const campaignsApi = {
  getTemplates: () =>
    apiCall<{ templates: WaTemplate[]; source: 'meta' | 'mock' }>('/campaigns/templates'),

  list: () =>
    apiCall<{ campaigns: CampaignRecord[] }>('/campaigns'),

  create: (payload: CreateCampaignPayload) =>
    apiCall<CampaignRecord>('/campaigns', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateStatus: (id: number, status: string) =>
    apiCall<CampaignRecord>(`/campaigns/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ status }),
    }),

  testSend: (phone: string, templateId: string, templateName: string, templateLanguage: string, variables: Record<string, string>) =>
    apiCall<{ success: boolean; simulated: boolean; message: string }>('/campaigns/test-send', {
      method: 'POST',
      body: JSON.stringify({ phone, template_id: templateId, template_name: templateName, template_language: templateLanguage, variables }),
    }),
}

/** Extract variable placeholders {{1}}, {{2}}, … from a template body string */
export function extractVariables(text: string): string[] {
  const matches = text.match(/\{\{(\d+)\}\}/g) ?? []
  return [...new Set(matches)].sort()
}

/** Render a template body by substituting {{N}} with provided values */
export function renderTemplate(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{(\d+)\}\}/g, (_, n) => vars[`{{${n}}}`] ?? vars[n] ?? `{{${n}}}`)
}

/** Get the BODY component text from a template */
export function getTemplateBody(template: WaTemplate): string {
  return template.components.find(c => c.type === 'BODY')?.text ?? ''
}

/** Get the HEADER component text from a template */
export function getTemplateHeader(template: WaTemplate): string {
  const h = template.components.find(c => c.type === 'HEADER')
  return h?.text ?? ''
}

/** Get the FOOTER text from a template */
export function getTemplateFooter(template: WaTemplate): string {
  return template.components.find(c => c.type === 'FOOTER')?.text ?? ''
}
