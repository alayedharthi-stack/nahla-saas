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
  status: 'APPROVED' | 'PENDING' | 'REJECTED' | 'DRAFT'
  components: WaTemplateComponent[]
  library?: {
    library_key: string
    label: string
    objective: string
    customer_statuses: string[]
    rfm_segments: string[]
  } | null
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
  template_status?: string
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
  /** When set, the backend auto-generates a unique coupon per customer at send time. */
  discount_percent?: number
  /** True = system generates coupons automatically per customer (no static code). */
  auto_coupon?: boolean
}

import { apiCall } from './client'

// ── Wizard types (new smart-campaign flow) ────────────────────────────────────

export interface CampaignGoal {
  key: string
  label_ar: string
  label_en: string
  description_ar: string
  icon: string
  allowed_meta_categories: string[]
  default_segment_key: string
}

export interface CustomerSegmentMeta {
  key: string
  label_ar: string
  label_en: string
  description_ar: string
  /** Long plain-Arabic description of this cohort, shown in the info
   *  popover on chips so the merchant understands what each segment
   *  means. Mirrors the field returned by /customers/segments. */
  criteria_ar: string
  icon: string
  natural_goals: string[]
  /** CRM customer_status values this cohort reads (docs / debugging). */
  crm_statuses: string[]
  /** RFM bucket values this cohort reads (docs / debugging). */
  rfm_buckets: string[]
  customer_count: number
}

export interface SegmentSampleRow {
  id: number
  name: string
  phone_masked: string
  email_masked: string
}

export interface RecommendedTemplate extends WaTemplate {
  display_name_ar?: string | null
  objective?: string | null
  score: number
  is_best: boolean
  badges: string[]
  reason_ar: string
}

export interface NextBestTemplate {
  id: number
  name: string
  language: string | null
  category: string | null
  status: string
  display_name_ar: string | null
}

export interface TemplateRecommendation {
  goal: { key: string; label_ar: string } | null
  segment: { key: string; label_ar: string } | null
  language: string
  templates: RecommendedTemplate[]
  best_template_id: number | null
  total: number
  /** Closest non-APPROVED template — populated only when `total === 0`. */
  next_best_template: NextBestTemplate | null
  /** Human-readable hint for the empty-state — Arabic, populated only
   *  when `total === 0`. */
  suggestion_ar: string | null
}

export interface WizardTestSendResult {
  sent: boolean
  simulated: boolean
  wa_message_id: string | null
  to: string
  error_code: string | null
  error_message: string | null
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

  // Legacy test-send (still used by older flows). The wizard below now
  // posts to /campaigns/wizard/test-send for richer error reporting.
  testSend: (phone: string, templateId: string, templateName: string, templateLanguage: string, variables: Record<string, string>) =>
    apiCall<{ success: boolean; simulated: boolean; message: string }>('/campaigns/test-send', {
      method: 'POST',
      body: JSON.stringify({ phone, template_id: templateId, template_name: templateName, template_language: templateLanguage, variables }),
    }),

  // ── Wizard endpoints ───────────────────────────────────────────────────────
  wizard: {
    goals: () =>
      apiCall<{ goals: CampaignGoal[] }>('/campaigns/wizard/goals'),

    segments: () =>
      apiCall<{ segments: CustomerSegmentMeta[] }>('/campaigns/wizard/segments'),

    segmentSample: (key: string, limit = 5) =>
      apiCall<{ segment_key: string; customer_count: number; sample: SegmentSampleRow[] }>(
        `/campaigns/wizard/segments/${encodeURIComponent(key)}/sample?limit=${limit}`,
      ),

    templates: (goal?: string, segment?: string, language = 'ar') => {
      const qs = new URLSearchParams()
      if (goal) qs.set('goal', goal)
      if (segment) qs.set('segment', segment)
      qs.set('language', language)
      return apiCall<TemplateRecommendation>(`/campaigns/wizard/templates?${qs.toString()}`)
    },

    testSend: (templateId: number, toPhone: string, variables: Record<string, string>) =>
      apiCall<WizardTestSendResult>('/campaigns/wizard/test-send', {
        method: 'POST',
        body: JSON.stringify({ template_id: templateId, to_phone: toPhone, variables }),
      }),
  },
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
