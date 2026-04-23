// ── Types ─────────────────────────────────────────────────────────────────────

export type TemplateStatus =
  | 'DRAFT'
  | 'APPROVED'
  | 'PENDING'
  | 'REJECTED'
  | 'DISABLED'
  | 'PAUSED'
  | 'ARCHIVED'
  | 'LIMIT_EXCEEDED'
  | string
export type TemplateCategory = 'MARKETING' | 'UTILITY' | 'AUTHENTICATION' | string

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
  workflow_status?: string
  status_raw?: string | null
  rejection_reason: string | null
  components: TemplateComponent[]
  created_at: string | null
  updated_at: string | null
  synced_at: string | null
  editable?: boolean
  submittable?: boolean
  library?: {
    library_key: string
    label: string
    objective: string
    customer_statuses: string[]
    rfm_segments: string[]
  } | null
  compatibility?: TemplateCompatibility
  // Nahla display & management
  display_name_ar?: string | null
  service_key?: string | null
  service_name_ar?: string
  service_icon?: string
  service_color?: string
  nahla_source_key?: string | null
  is_active?: boolean
  is_hidden?: boolean
  step_number?: number | null
  has_coupon?: boolean
  trigger_delay_hours?: number | null
}

export interface CreateTemplatePayload {
  name: string
  language: string
  category: TemplateCategory
  components: TemplateComponent[]
  auto_submit?: boolean
}

export interface UpdateTemplatePayload {
  name?: string
  language?: string
  category?: TemplateCategory
  components?: TemplateComponent[]
}

// ── Template sync result + status ─────────────────────────────────────────────

/** Result returned by POST /templates/sync */
export interface TemplateSyncResult {
  synced: number
  auto_bound?: number
  failed?: number
  deleted_seeds?: number
  message: string
  error?: string | null
  detail?: string | null
}

/** Last cycle stats (background scheduler), surfaced for ops/debug. */
export interface TemplateSyncCycleStats {
  at: string | null
  duration_ms: number | null
  tenants_total: number
  tenants_synced: number
  tenants_failed: number
  tenants_skipped: number
  total_templates: number
  auto_bound: number
}

/** Result returned by GET /templates/sync/status */
export interface TemplateSyncStatus {
  recorded: boolean
  next_estimate?: string
  /** Present only when `recorded === true`. */
  at?: string
  source?: 'manual' | 'scheduled' | string
  synced?: number
  auto_bound?: number
  failed?: number
  deleted_seeds?: number
  error?: string | null
  message?: string
  /** Read-only: last full background cycle across all tenants. */
  last_cycle?: TemplateSyncCycleStats
}

/**
 * Map a stable backend `error` code (returned by /templates/sync or
 * /templates/sync/status) to a clear Arabic merchant-facing message.
 *
 * Keep these in sync with `_sync_templates_for_tenant` in
 * backend/routers/templates.py.
 */
export const TEMPLATE_SYNC_ERROR_MESSAGES: Record<string, string> = {
  no_waba_id:
    'لم يتم العثور على رقم WhatsApp Business مرتبط. يجب إكمال ربط واتساب أولاً من صفحة الإعدادات.',
  no_valid_token:
    'لا يوجد توكن صالح لمزامنة القوالب. أعد ربط حساب واتساب من إعدادات المنصة لتجديد الصلاحيات.',
  bad_provider_payload:
    'استجابة غير متوقعة من Meta. سنعيد المحاولة تلقائياً خلال دقائق دون تدخل منك.',
  no_provider_data:
    'تعذّر الاتصال بـ Meta حالياً. تحقّق من بيانات الاعتماد أو حاول لاحقاً.',
  db_lookup_failed:
    'تعذّرت قراءة بيانات الاتصال. سيُعاد المحاولة تلقائياً.',
  db_commit_failed:
    'تعذّر حفظ القوالب في قاعدة البيانات. ستتم إعادة المحاولة تلقائياً.',
  unexpected_failure:
    'حدث خطأ غير متوقّع. ستتم المزامنة تلقائياً خلال دقائق.',
  read_failed:
    'تعذّر قراءة سجل آخر مزامنة من قاعدة البيانات.',
}

export function getTemplateSyncErrorMessage(
  code: string | null | undefined,
  fallback?: string,
): string {
  if (!code) return fallback || ''
  return TEMPLATE_SYNC_ERROR_MESSAGES[code] || fallback || code
}

// ── API client ────────────────────────────────────────────────────────────────

import { apiCall } from './client'

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
  compatibility?: TemplateCompatibility
}

export interface ResolvedTemplate {
  template_name: string
  resolved_components: TemplateComponent[]
  rendered_body: string
  wa_parameters: { type: 'text'; text: string }[]
  compatibility?: TemplateCompatibility
}

export interface TemplateCompatibility {
  compatibility: 'compatible' | 'review_needed' | 'pending_meta' | string
  placeholder_count: number
  placeholders: string[]
  var_map: Record<string, string>
  supported_features: string[]
  issues: string[]
  has_body_text: boolean
  language_normalized: string
  category_normalized: string
  status_normalized: string
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

  update: (id: number, payload: UpdateTemplatePayload) =>
    apiCall<WhatsAppTemplateRecord>(`/templates/${id}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  updateStatus: (id: number, status: TemplateStatus, rejectionReason?: string) =>
    apiCall<WhatsAppTemplateRecord>(`/templates/${id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ status, rejection_reason: rejectionReason }),
    }),

  submit: (id: number) =>
    apiCall<{ submitted: boolean; template: WhatsAppTemplateRecord }>(`/templates/${id}/submit`, {
      method: 'POST',
    }),

  delete: (id: number, nahlaOnly = false) =>
    apiCall<{ deleted: boolean; soft_removed?: boolean; meta_deleted?: boolean; meta_error?: string; message?: string }>(
      `/templates/${id}${nahlaOnly ? '?nahla_only=true' : ''}`,
      { method: 'DELETE' },
    ),

  sync: () =>
    apiCall<TemplateSyncResult>('/templates/sync', { method: 'POST' }),

  /** Read the most recent template sync attempt (background or manual). */
  syncStatus: () =>
    apiCall<TemplateSyncStatus>('/templates/sync/status'),

  /** Fetch the variable → customer-field mapping for a template. */
  getVarMap: (id: number) =>
    apiCall<TemplateVarMapRecord>(`/templates/${id}/var-map`),

  /** Resolve template variables for a specific customer and return the rendered body. */
  resolve: (id: number, customerId: number, extra: Record<string, string> = {}) =>
    apiCall<ResolvedTemplate>(`/templates/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ customer_id: customerId, extra }),
    }),

  library: () =>
    apiCall<{ templates: Array<{ template_name: string; library_key: string; label: string; objective: string; customer_statuses: string[]; rfm_segments: string[] }> }>('/templates/library'),

  /** Nahla built-in template library */
  nahlaLibrary: (params?: { category?: string; tag?: string; search?: string }) => {
    const q = new URLSearchParams()
    if (params?.category) q.set('category', params.category)
    if (params?.tag)      q.set('tag',      params.tag)
    if (params?.search)   q.set('search',   params.search)
    const qs = q.toString() ? `?${q.toString()}` : ''
    return apiCall<{
      templates: NahlaLibraryTemplate[]
      total: number
      filter_tags: Record<string, string>
      smart_trigger_map: Record<string, string[]>
    }>(`/templates/nahla-library${qs}`)
  },

  importNahlaTemplate: (template_key: string, language = 'ar', custom_name?: string) =>
    apiCall<{ success: boolean; message: string; template: WhatsAppTemplateRecord }>(
      '/templates/import-nahla-template',
      { method: 'POST', body: JSON.stringify({ template_key, language, custom_name }) },
    ),

  updateNahlaSettings: (id: number, payload: NahlaSettingsPayload) =>
    apiCall<WhatsAppTemplateRecord>(`/templates/${id}/nahla-settings`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  unlinkService: (id: number) =>
    apiCall<{ message: string; template: WhatsAppTemplateRecord }>(`/templates/${id}/unlink-service`, {
      method: 'POST',
    }),

  restore: (id: number) =>
    apiCall<{ message: string; template: WhatsAppTemplateRecord }>(`/templates/${id}/restore`, {
      method: 'POST',
    }),

  setActive: (id: number) =>
    apiCall<{ message: string; template: WhatsAppTemplateRecord; deactivated_template_name?: string }>(`/templates/${id}/set-active`, {
      method: 'POST',
    }),
}

// ── Nahla Library Types ───────────────────────────────────────────────────────

export interface NahlaLibraryTemplate {
  key:           string
  name_ar:       string
  description_ar: string
  category:      TemplateCategory
  filter_tags:   string[]
  smart_trigger: string | null
  smart_label:   string | null
  preview_body:  string
  preview_footer: string
  buttons:       TemplateButton[]
  slot_count:    number
  slots:         string[]
  service_key:            string
  service_name_ar:        string
  service_description_ar: string
  service_icon:           string
  service_color:          string
  step_number?:           number | null
  has_coupon?:            boolean
  trigger_delay_hours?:   number | null
}

export interface NahlaSettingsPayload {
  display_name_ar?: string
  service_key?: string
  is_active?: boolean
  step_number?: number
  has_coupon?: boolean
  trigger_delay_hours?: number
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

export const STATUS_COLORS: Record<string, string> = {
  DRAFT: 'slate',
  APPROVED: 'green',
  PENDING:  'amber',
  REJECTED: 'red',
  DISABLED: 'slate',
  PAUSED: 'purple',
  ARCHIVED: 'slate',
  LIMIT_EXCEEDED: 'red',
}

export const STATUS_LABELS: Record<string, string> = {
  DRAFT: 'مسودة',
  APPROVED: 'معتمد',
  PENDING:  'قيد المراجعة',
  REJECTED: 'مرفوض',
  DISABLED: 'معطّل',
  PAUSED: 'موقوف مؤقتًا',
  ARCHIVED: 'مؤرشف',
  LIMIT_EXCEEDED: 'تجاوز الحد',
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
