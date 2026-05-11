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
  /** Sent>0 with failures, but EVERY failure was minor (e.g. recipient
   *  not on WhatsApp). Treated as success in the UI. */
  | 'partial_minor'
  | 'failed'
  | 'failed_all'
  /** Sent==0 with failures, but every failure was minor — the campaign
   *  itself didn't fail, the recipient list was just unreachable. */
  | 'no_whatsapp_recipients'
  /** Audience matched > 0 customers but every one of them was filtered
   *  out (no phone, opted-out, manual-exclude) BEFORE any send-log row
   *  was written. Distinct from completed_empty (zero audience). */
  | 'excluded_before_send'
  /** Funnel says rows were materialized but campaign_send_logs is
   *  empty now — usually means rows were deleted or never committed. */
  | 'orphaned_materialized_rows'
  /** Rows exist but their status values aren't recognised by the
   *  current dispatcher (legacy / hand-edited data). */
  | 'unknown_status'
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
  /** Arabic-translated equivalent of last_error. UI prefers this so
   *  the merchant never sees raw English Meta jargon. */
  last_error_ar?: string | null
  /** Canonical Meta error key (e.g. "not_on_whatsapp", "rate_limit"). */
  last_error_key?: string | null
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
  /** Verbatim per-status counters — every canonical status plus an
   *  ``unknown_status`` bucket aggregating non-canonical values. */
  status_breakdown: {
    queued: number
    sending: number
    sent: number
    failed: number
    skipped_duplicate: number
    skipped_invalid: number
    skipped_unsubscribed: number
    skipped_unreachable: number
    skipped_manual_exclusion: number
    unknown_status: number
  }
  /** Raw status → count mapping straight from the GROUP BY query.
   *  Includes any exotic legacy keys (``pending``, ``processing``, …)
   *  so support can spot data-migration gaps. */
  status_breakdown_raw: Record<string, number>
  /** First 10 send-log rows for the campaign so support can drill
   *  down when counters disagree with the funnel. */
  sample_rows: Array<{
    id: number
    phone_masked: string
    status: string
    skip_reason: string | null
    error_code: string | null
    error_message: string | null
    attempt_count: number | null
    created_at: string | null
    updated_at: string | null
  }>
  /** Retry-health snapshot — surfaces the circuit-breaker signals.
   *  ``retry_storm_detected`` flips to true whenever a single row's
   *  ``attempt_count`` crossed ``attempt_circuit_breaker``. Always
   *  present (counters default to 0). */
  retry_health: {
    max_send_attempts: number
    attempt_circuit_breaker: number
    sending_timeout_seconds: number
    max_attempt_count: number
    rows_at_attempt_ceiling: number
    zombie_sending_count: number
    retry_storm_detected: boolean
  }
  sample_failed: Array<{
    phone: string
    /** Canonical Meta error key. */
    error_code: string
    /** Human-readable Arabic label. */
    error_label_ar: string
    /** "minor" | "major" | "blocking" — drives whether the
     *  campaign-level lifecycle treats this as a real failure. */
    severity: 'minor' | 'major' | 'blocking'
    is_recoverable: boolean
    /** Whether the dispatcher should auto-retry this row. Distinct from
     *  ``is_recoverable``: a row CAN be merchant-recoverable yet not
     *  worth auto-retrying (e.g. ``spam_rate_limit`` recovers after
     *  24h but blind retries make it worse). The UI hides "أعد
     *  المحاولة" on rows where ``retryable === false``. */
    retryable: boolean
    /** Provider-side billing/account restriction (client_payment_blocked,
     *  account_locked, auth_error, …). The merchant cannot fix this
     *  from the dashboard — the workflow is to contact 360dialog
     *  with the support bundle attached. */
    provider_billing_block: boolean
    /** One-line action hint in Arabic ("ask for opt-in", etc.). */
    advice_ar: string | null
    /** Raw technical message kept verbatim so support can copy it. */
    error_technical: string
    /** Parsed-out Meta error fields. Present even when ``error_code``
     *  is "unknown" so the UI can always show the merchant the raw
     *  Meta payload instead of a generic "خطأ غير معروف". */
    meta_error_code: string | null
    meta_error_subcode: string | null
    meta_error_type: string | null
    meta_error_message: string | null
    attempt_count: number
    updated_at: string | null
  }>
  /** Aggregated breakdown of failures by canonical error key. */
  failure_summary: Array<{
    error_code: string
    error_label_ar: string
    severity: 'minor' | 'major' | 'blocking'
    is_recoverable: boolean
    /** Auto-retry policy flag, mirrors ``ClassifiedError.retryable``. */
    retryable: boolean
    /** Provider-side billing/account restriction marker (drives the
     *  "Contact 360dialog" support banner + bundle CTA). */
    provider_billing_block: boolean
    advice_ar: string | null
    count: number
  }>
  /** Aggregated provider-side billing/account block signal. When
   *  ``detected`` is true the UI MUST: show the support banner, hide
   *  the dispatch CTA, and surface the "نسخ تقرير الدعم" button which
   *  fetches the support bundle. None of these errors are
   *  merchant-recoverable from the dashboard — the workflow is to
   *  contact 360dialog with the support bundle attached. */
  provider_block: {
    detected: boolean
    count: number
    error_keys: Array<{
      key: string
      count: number
      label_ar: string
    }>
    first_seen_at: string | null
    last_seen_at: string | null
    /** The most-common provider block key encountered. Drives the
     *  banner sub-title. */
    primary_key?: string | null
    primary_label_ar: string | null
    /** Fixed Arabic copy for the merchant banner — kept on the
     *  backend so every client renders the same message. */
    support_message_ar: string | null
    support_provider?: string
  }
  /** Audience funnel — every stage between segment match and the
   *  Meta send call. Used to render the "🚫 تم استبعاد X عميل" panel
   *  when no rows materialised. */
  audience_funnel: {
    raw_audience: number
    after_reachable_filter: number
    materialized_rows: number
    queued_for_send: number
    skipped_at_snapshot: number
    frequency_cap_skipped: number
    audience_count_campaign: number
  }
  /** Per-reason exclusion breakdown in Arabic. Powers the granular
   *  "بدون رقم جوال — 2 عملاء" list in the diagnostic panel. */
  excluded_reasons_summary: Array<{
    status: string
    skip_reason: string | null
    label_ar: string
    count: number
  }>
  excluded_before_send_count: number
  /** Per-customer drill-down for the first 10 excluded recipients.
   *  Lets support spot patterns like "all 4 are missing
   *  normalized_phone" without paging through the customer list.
   *  ``has_whatsapp`` is intentionally tri-state (true / false /
   *  null) — null means unknown and would have been delivered to
   *  Meta; only explicit false blocks the send. */
  sample_excluded_before_send: Array<{
    customer_id: number
    name: string
    phone_masked: string
    reason_key:
      | 'no_phone'
      | 'phone_not_normalized'
      | 'unsubscribed'
      | 'pending_unsubscribe'
      | 'marketing_opt_out'
      | 'no_whatsapp_confirmed'
      | 'unknown'
    reason_label_ar: string
    fields: {
      has_phone: boolean
      phone_normalized_valid: boolean
      whatsapp_opted_out: boolean
      /** TRI-STATE: true / false / null (unknown). */
      has_whatsapp: boolean | null
      is_unsubscribed: boolean
      pending_unsubscribe: boolean
      marketing_opt_out: boolean
    }
  }>
  /** Frequency-cap diagnostics + audit trail for skipped_duplicate
   *  rows tied to ``frequency_cap_marketing``. */
  frequency_cap: {
    bypassed: boolean
    cap_days: number
    capped_count: number
    /** Same data as ``source_rows`` — canonical key requested by API
     *  contract. */
    frequency_cap_source_rows: Array<{
      phone_masked: string
      skip_reason: string | null
      last_successful_sent_at: string | null
      last_successful_campaign_id: number | null
    }>
    /** Deprecated alias of ``frequency_cap_source_rows``. */
    source_rows: Array<{
      phone_masked: string
      skip_reason: string | null
      last_successful_sent_at: string | null
      last_successful_campaign_id: number | null
    }>
    /** Most recent successful send among capped phones (ISO timestamp). */
    last_successful_sent_at: string | null
    last_successful_campaign_id: number | null
  }
  /** Raw Meta API request/response fingerprints captured when the
   *  classifier falls back to "unknown". Used by support to add new
   *  Meta error codes to the canonical classifier. */
  raw_meta_error_samples: Array<{
    ts: string
    recipient: string
    meta_error_code: string | null
    meta_error_subcode: string | null
    meta_error_type: string | null
    meta_error_message: string | null
    fbtrace_id: string | null
    request_payload: Record<string, unknown>
    response_payload: Record<string, unknown>
    classified_key: string
    template_summary?: {
      template_name?: string | null
      language?: string | null
      category?: string | null
      recipient?: string | null
      component_count?: number
      header_params?: number
      body_params?: number
      button_params?: number
      media?: boolean
    } | null
    component_diff?: Array<{
      component: 'BODY' | 'HEADER' | 'BUTTONS' | string
      index: number | null
      kind: string
      expected: number | string
      sent: number | string
      message_ar: string
    }>
  }>
  sample_sent: Array<{
    phone: string
    provider_message_id: string | null
    /** False when status='sent' but provider_message_id is missing —
     *  the row is corrupt and we should NOT count it as accepted by
     *  Meta. Surfaced in the UI as a hard warning pill. */
    has_provider_message_id: boolean
    sent_at: string | null
    /** Per-recipient delivery stage derived from the WhatsApp status
     *  webhook timestamps. One of:
     *    accepted_by_provider | delivered | read | failed_after_accept
     *  Mirrors the keys in `delivery_summary` so the UI can render
     *  one shared color/icon mapping. */
    delivery_stage: 'accepted_by_provider' | 'delivered' | 'read' | 'failed_after_accept'
    delivered_at: string | null
    read_at: string | null
    failed_at: string | null
  }>
  /** Aggregate breakdown of post-accept delivery status across every
   *  ``sent`` row in this campaign. Populated by the WhatsApp status
   *  webhook over time (delivered/read/failed events from Meta).
   *  ``unknown_delivery`` is rows that Meta accepted but for which no
   *  downstream webhook has arrived yet. ``missing_provider_message_id``
   *  is the corruption canary — rows in status='sent' WITHOUT a
   *  wamid. The UI surfaces it as a hard warning. */
  delivery_summary: {
    accepted_by_provider:        number
    delivered:                   number
    read:                        number
    failed_after_accept:         number
    unknown_delivery:            number
    missing_provider_message_id: number
  }
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

/** Provider-escalation support bundle. Returned by
 *  ``GET /campaigns/{id}/support-bundle``. The shape is versioned
 *  (``version: "1"``) and intentionally stable — merchants paste it
 *  straight into 360dialog tickets and downstream tooling may
 *  consume it as well. */
export interface CampaignSupportBundle {
  version: string
  kind: 'nahla.campaign.support_bundle'
  generated_at: string
  tenant_id: number
  support_provider: string
  campaign: {
    id: number
    name: string
    status: string
    campaign_type: string | null
    audience_count: number
    sent_count: number
    launched_at: string | null
    created_at: string | null
  }
  template: {
    id: number
    name: string
    language: string
    category: string
    status: string
  } | null
  wa_connection: {
    provider: string | null
    phone_number_id: string | null
    business_account_id: string | null
    status: string | null
  } | null
  provider_block: {
    detected: boolean
    count: number
    error_keys: Array<{
      key: string
      count: number
      label_ar: string | null
    }>
    primary_key: string | null
    primary_label_ar: string | null
  }
  sample_recipients: Array<{
    phone_masked: string
    error_code: string | null
    error_label_ar: string | null
    error_message_raw: string
    meta_error_code: string | null
    meta_error_subcode: string | null
    meta_error_type: string | null
    meta_error_message: string | null
    attempt_count: number
    occurred_at: string | null
  }>
  raw_meta_samples: unknown[]
  support_message_ar: string
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

  /** Kick the dispatcher for a campaign that's stuck in
   *  ``pending_dispatch`` or ``failed``. Runs in the background on
   *  the server (the dispatcher has 1.5s+ pauses between sends and
   *  would exceed our 25s HTTP timeout for any sizeable audience),
   *  so this endpoint returns immediately with ``kicked: true``.
   *  The merchant watches progress via the standard /campaigns list
   *  refresh + /campaigns/{id}/debug. Idempotent — rows already in
   *  ``status='sent'`` are NOT re-sent. */
  dispatchNow: (
    id: number,
    opts?: { bypassFrequencyCap?: boolean },
  ) =>
    apiCall<{
      campaign_id: number
      ok: boolean
      kicked?: boolean
      skipped?: boolean
      reason?: string
      message?: string
      status?: string
      error?: string
      bypass_frequency_cap?: boolean
      /** Number of failed rows promoted back to ``queued`` before
       *  the dispatch task was kicked. */
      rescheduled_failed?: number
      /** Number of zombie ``sending`` rows the watchdog revived. */
      revived_zombies?: number
    }>(
      `/campaigns/${id}/dispatch-now${
        opts?.bypassFrequencyCap === true ? '?bypass_frequency_cap=true' : ''
      }`,
      { method: 'POST' },
    ),

  /** Pull a provider-escalation support bundle. Read-only — safe to
   *  call any number of times. The merchant clicks "نسخ تقرير الدعم"
   *  in the rose banner when ``debug.provider_block.detected`` is
   *  true; the UI copies this JSON straight to the clipboard so the
   *  merchant can paste it into a 360dialog ticket. */
  supportBundle: (id: number) =>
    apiCall<CampaignSupportBundle>(`/campaigns/${id}/support-bundle`),

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
