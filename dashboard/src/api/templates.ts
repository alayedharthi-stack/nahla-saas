// ── Types ─────────────────────────────────────────────────────────────────────

export type TemplateStatus = 'APPROVED' | 'PENDING' | 'REJECTED' | 'DISABLED'
export type TemplateCategory = 'MARKETING' | 'UTILITY' | 'AUTHENTICATION'

export interface TemplateButton {
  type: 'URL' | 'PHONE_NUMBER' | 'COPY_CODE' | 'QUICK_REPLY'
  text: string
  url?: string
  phone_number?: string
  example?: string[]
}

export interface TemplateComponent {
  type: 'HEADER' | 'BODY' | 'FOOTER' | 'BUTTONS'
  format?: 'TEXT' | 'IMAGE' | 'DOCUMENT' | 'VIDEO'
  text?: string
  buttons?: TemplateButton[]
}

export interface WhatsAppTemplateRecord {
  id: number
  meta_template_id: string | null
  name: string
  language: string
  category: TemplateCategory
  status: TemplateStatus
  rejection_reason: string | null
  components: TemplateComponent[]
  created_at: string | null
  updated_at: string | null
  synced_at: string | null
}

export interface CreateTemplatePayload {
  name: string
  language: string
  category: TemplateCategory
  components: TemplateComponent[]
}

// ── API client ────────────────────────────────────────────────────────────────

async function apiCall<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
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

export interface VarMapAnnotated {
  field: string
  label: string
}

export interface TemplateVarMapRecord {
  template_id: number
  template_name: string
  category: string
  var_map: Record<string, string>            // {"{{1}}": "customer_name", ...}
  var_map_annotated: Record<string, VarMapAnnotated>
  is_default: boolean
}

export interface ResolvedTemplate {
  template_name: string
  resolved_components: TemplateComponent[]
  rendered_body: string
  wa_parameters: { type: 'text'; text: string }[]
}

export const templatesApi = {
  list: (status?: TemplateStatus) =>
    apiCall<{ templates: WhatsAppTemplateRecord[] }>(
      `/templates${status ? `?status=${status}` : ''}`
    ),

  create: (payload: CreateTemplatePayload) =>
    apiCall<WhatsAppTemplateRecord>('/templates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  updateStatus: (id: number, status: TemplateStatus, rejectionReason?: string) =>
    apiCall<WhatsAppTemplateRecord>(`/templates/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ status, rejection_reason: rejectionReason }),
    }),

  delete: (id: number) =>
    apiCall<{ deleted: boolean }>(`/templates/${id}`, { method: 'DELETE' }),

  sync: () =>
    apiCall<{ synced: number; message: string }>('/templates/sync', { method: 'POST' }),

  /** Fetch the variable → customer-field mapping for a template. */
  getVarMap: (id: number) =>
    apiCall<TemplateVarMapRecord>(`/templates/${id}/var-map`),

  /** Resolve template variables for a specific customer and return the rendered body. */
  resolve: (id: number, customerId: number, extra: Record<string, string> = {}) =>
    apiCall<ResolvedTemplate>(`/templates/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ customer_id: customerId, extra }),
    }),
}

// ── Helpers ───────────────────────────────────────────────────────────────────

export function getBody(tpl: WhatsAppTemplateRecord | { components: TemplateComponent[] }): string {
  return tpl.components.find(c => c.type === 'BODY')?.text ?? ''
}

export function getHeader(tpl: WhatsAppTemplateRecord | { components: TemplateComponent[] }): string {
  const h = tpl.components.find(c => c.type === 'HEADER')
  return h?.text ?? ''
}

export function getFooter(tpl: WhatsAppTemplateRecord | { components: TemplateComponent[] }): string {
  return tpl.components.find(c => c.type === 'FOOTER')?.text ?? ''
}

export function getButtons(tpl: WhatsAppTemplateRecord | { components: TemplateComponent[] }): TemplateButton[] {
  return tpl.components.find(c => c.type === 'BUTTONS')?.buttons ?? []
}

export function extractVars(text: string): string[] {
  return [...new Set((text.match(/\{\{\d+\}\}/g) ?? []))].sort()
}

export function renderBody(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{(\d+)\}\}/g, (_, n) => vars[`{{${n}}}`] ?? vars[n] ?? `{{${n}}}`)
}

export function countVars(tpl: WhatsAppTemplateRecord): number {
  const body = getBody(tpl)
  return extractVars(body).length
}

export const STATUS_COLORS: Record<TemplateStatus, string> = {
  APPROVED: 'green',
  PENDING:  'amber',
  REJECTED: 'red',
  DISABLED: 'slate',
}

export const STATUS_LABELS: Record<TemplateStatus, string> = {
  APPROVED: 'معتمد',
  PENDING:  'قيد المراجعة',
  REJECTED: 'مرفوض',
  DISABLED: 'معطّل',
}

export const CATEGORY_LABELS: Record<TemplateCategory, string> = {
  MARKETING:      'تسويق',
  UTILITY:        'خدمة',
  AUTHENTICATION: 'مصادقة',
}

export const LANGUAGE_LABELS: Record<string, string> = {
  ar: 'العربية',
  en: 'English',
  en_US: 'English (US)',
}
