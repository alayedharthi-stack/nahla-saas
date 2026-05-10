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

/**
 * Merchant-facing lifecycle verb, derived from ``status`` plus the
 * per-recipient counters that the dispatcher writes back to the
 * campaign row. The UI should prefer this over ``status`` for the
 * badge label — "active" on its own is ambiguous (queued vs sending
 * vs no-recipients-yet), but lifecycle disambiguates.
 */
export type CampaignLifecycle =
  | 'draft'
  | 'waiting_scheduler'
  | 'pending_dispatch'
  | 'sending'
  | 'sent'
  | 'partial'
  | 'failed'
  | 'failed_all'
  | 'completed_empty'
  | 'unknown'

export interface CampaignRecord {
  id: number
  name: string
  campaign_type: string
  status: 'draft' | 'scheduled' | 'active' | 'completed' | 'paused' | 'failed'
  /** Granular merchant-facing status. Always present from /campaigns. */
  lifecycle?: CampaignLifecycle
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
  failed_count: number
  skipped_count: number
  dispatch_errors: string[]
  /** First entry from dispatch_errors, surfaced under the status pill. */
  last_error?: string | null
  delivered_count: number
  read_count: number
  clicked_count: number
  converted_count: number
  created_at: string | null
  launched_at: string | null
}

export interface CampaignDebugSnapshot {
  campaign: {
    id: number
    name: string
    status: string
    lifecycle: CampaignLifecycle
    campaign_type: string
    audience_type: string
    audience_count: number
    schedule_type: string
    schedule_time: string | null
    delay_minutes: number | null
    template_name: string
    template_language: string
    launched_at: string | null
    created_at: string | null
    dispatch_errors: string[]
  }
  recipients: {
    total: number
    queued: number
    sending: number
    sent: number
    failed: number
    skipped_duplicate: number
    skipped_invalid: number
    skipped_unsubscribed: number
    skipped_unreachable: number
    skipped_manual_exclusion: number
  }
  sample_failed: Array<{
    phone: string
    error_code: string | null
    error_message: string
    attempt_count: number
    updated_at: string | null
  }>
  sample_sent: Array<{
    phone: string
    provider_message_id: string | null
    sent_at: string | null
  }>
  template: {
    id: number
    name: string
    language: string
    category: string
    status: string
    approved: boolean
  } | null
  wa_connection: {
    phone_number_id: string | null
    status: string | null
    provider: string | null
    last_error: string | null
  } | null
  scheduler: {
    campaign_dispatcher_enabled: boolean
    kill_switch_set: boolean
    poll_seconds: number
    note: string
  }
  hints: string[]
  errors: string[]
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
  /** Whether this template is a fully-manual template (merchant types
   *  every dynamic value) or an auto template (Nahla resolves
   *  customer name / coupon / cart URL from system data). Drives the
   *  grouping + badge in Step 3, and forces manual-mode behaviour in
   *  Steps 4 / 7 regardless of the chosen goal. */
  mode?: 'manual' | 'auto'
  /** Library-suggested display label, e.g. "عرض خاص — يدوي". The
   *  wizard prefers ``display_name_ar`` (merchant override) and falls
   *  back to this. */
  library_label_ar?: string | null
  /** True when the auto template can bind to Nahla's coupon
   *  generator. Only valid for ``mode === 'auto'`` templates. */
  auto_coupon_capable?: boolean
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

/** Anti-spam protection settings for manual marketing campaigns. */
export interface CampaignProtectionInfo {
  frequency_cap_days: number
  idempotent_resend_protected: boolean
}

/** Per-recipient counters for the campaign report. */
export interface CampaignReport {
  campaign_id: number
  campaign_status: string
  frequency_cap_days: number
  total_recipients: number
  queued: number
  sending: number
  sent: number
  failed: number
  skipped_duplicate: number
  invalid_phone: number
  skipped_unsubscribed: number
  skipped_unreachable: number
  stopped_by_limit: number
  last_error_code: string | null
  last_error_message: string | null
}

export const campaignsApi = {
  getTemplates: () =>
    apiCall<{ templates: WaTemplate[]; source: 'meta' | 'mock' }>('/campaigns/templates'),

  list: () =>
    apiCall<{ campaigns: CampaignRecord[] }>('/campaigns'),

  /** Read-only metadata for the "🛡️ حماية ذكية من التكرار" trust card.
   *  Cheap (one fast SELECT) so it's safe to call once per wizard
   *  open. */
  protectionInfo: () =>
    apiCall<CampaignProtectionInfo>('/campaigns/protection-info'),

  /** Per-recipient counters. Used both by the post-launch report and
   *  by the wizard's pre-launch trust card to surface the historical
   *  skipped-duplicate count for the merchant's account. */
  report: (id: number) =>
    apiCall<CampaignReport>(`/campaigns/${id}/report`),

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

  debugTemplate: (templateId: string) =>
    apiCall<Record<string, unknown>>(`/campaigns/debug-template/${templateId}`),

  /** Full diagnostic snapshot for a single campaign.
   *
   *  Returns recipient counts, sample failed/sent rows, template
   *  approval state, WhatsApp connection state, scheduler health
   *  and merchant-facing ``hints`` (e.g. "ASYNCIO task died" or
   *  "NAHLA_DISABLE_SCHEDULERS=1 is set"). The endpoint never raises
   *  500 — any internal failure is captured in ``errors``. */
  debug: (id: number) =>
    apiCall<CampaignDebugSnapshot>(`/campaigns/${id}/debug`),

  /** Force the dispatcher to run synchronously for a campaign that's
   *  stuck in ``pending_dispatch`` or ``failed``. Idempotent — rows
   *  already in ``status='sent'`` are NOT re-sent. */
  dispatchNow: (id: number) =>
    apiCall<{
      campaign_id: number
      ok?: boolean
      skipped?: boolean
      reason?: string
      message?: string
      status?: string
      sent?: number
      failed?: number
      queued?: number
      errors?: string[]
    }>(`/campaigns/${id}/dispatch-now`, { method: 'POST' }),

  deleteCampaign: (id: number) =>
    apiCall<{ deleted: boolean; id: number }>(`/campaigns/${id}`, { method: 'DELETE' }),

  bulkDelete: (ids: number[]) =>
    apiCall<{ deleted: number; ids: number[] }>('/campaigns/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ ids }),
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
