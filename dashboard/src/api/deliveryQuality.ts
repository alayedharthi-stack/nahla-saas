/**
 * api/deliveryQuality.ts
 * ──────────────────────
 * Frontend client for the Delivery Quality Intelligence Layer
 * (Phase 2 service + Phase 3 dashboard).
 *
 * All endpoints here are **read/analytical**. None of them affect
 * send behaviour — Phase 4 will introduce pre-send gating in a
 * separate module so the dashboard never has to worry about it.
 *
 * The shapes below are mirrored 1:1 from the FastAPI responses in
 * ``backend/routers/delivery_quality.py``; if the backend payload
 * changes, update this file at the same time.
 */
import { apiCall } from './client'

// ── Shared shapes ─────────────────────────────────────────────────

export type QualityTier =
  | 'excellent'
  | 'healthy'
  | 'warning'
  | 'risky'
  | 'critical'

export interface TierThreshold {
  label:       QualityTier
  lower_bound: number
}

export interface QualityConnection {
  id:                     number
  provider:               string
  phone_number:           string | null
  phone_number_id:        string | null
  business_display_name:  string | null
  status:                 string
  connection_type:        string | null
  /** Meta's own GREEN / YELLOW / RED label. Plotted ALONGSIDE the
   *  Nahla score (never used as an input) — the whole point of the
   *  Quality Score is to be a leading indicator vs. Meta's
   *  trailing one. */
  meta_quality_rating:    string | null
  meta_messaging_limit:   string | null
  meta_tier_updated_at:   string | null
  sending_enabled:        boolean
  connected_at:           string | null
}

/** Common snapshot shape — both persisted rows and the in-memory
 *  ``live`` payload from ``/quality/numbers`` use this. */
export interface QualitySnapshot {
  id:                    number | null
  taken_at:              string
  metrics_window_hours:  number
  meta_quality_rating:   string | null
  meta_messaging_limit:  string | null
  /** ``null`` when sample size was below threshold. */
  nahla_quality_score:   number | null
  nahla_quality_tier:    QualityTier
  delivery_rate:         number | null
  read_rate:             number | null
  failure_rate:          number | null
  suppress_rate:         number | null
  complaint_rate:        number | null
  sample_size:           number
  /** Free-form JSON with numerator/denominator pairs the dashboard
   *  uses for "X delivered / Y total" labels. */
  raw_metrics:           Record<string, any> | null
  triggered_by:          string | null
}

// ── /quality/numbers ──────────────────────────────────────────────

export interface QualityNumberRow {
  connection:      QualityConnection
  /** Freshly computed from the events table at request time.
   *  NOT persisted — use the snapshot for trend history. */
  live:            QualitySnapshot
  /** Most recent persisted snapshot row, or null if the scheduler
   *  hasn't run for this number yet. */
  latest_snapshot: QualitySnapshot | null
}

export interface QualityNumbersResponse {
  numbers:              QualityNumberRow[]
  tier_thresholds:      TierThreshold[]
  default_window_hours: number
  alt_window_hours?:    number
}

// ── /quality/numbers/{id}/history ─────────────────────────────────

export interface QualityHistoryResponse {
  connection:       QualityConnection
  snapshots:        QualitySnapshot[]
  tier_thresholds:  TierThreshold[]
}

// ── /quality/numbers/{id}/failures ────────────────────────────────

export interface FailureBreakdownRow {
  /** Canonical key from ``services.meta_errors.ERRORS``
   *  (e.g. ``"blocked_by_user"``, ``"not_on_whatsapp"``). */
  error_key:           string
  count:               number
  share:               number          // 0..1 of total_failures
  distinct_phones:     number
  quality_tier:        QualityTier | string
  /** True when ``ClassifiedError.suppress_on_repeat`` is set — the
   *  dashboard surfaces a "this triggers auto-suppression" hint. */
  suppress_on_repeat:  boolean
  /** Merchant-facing Arabic label from the classifier. */
  label_ar:            string
  /** One-line "what to do" copy, or ``null`` if the classifier
   *  doesn't carry one. */
  advice_ar:           string | null
}

export interface FailureBreakdownResponse {
  connection_id:    number
  window_hours:     number
  since:            string
  total_failures:   number
  breakdown:        FailureBreakdownRow[]
}

// ── /quality/numbers/{id}/snapshot ────────────────────────────────

export interface SnapshotResponse {
  snapshot: QualitySnapshot | null
}


// ── Client ────────────────────────────────────────────────────────

export const deliveryQualityApi = {
  /** List all WABA numbers for the tenant with their live + last
   *  persisted scores. */
  numbers() {
    return apiCall<QualityNumbersResponse>('/quality/numbers')
  },

  /** Time-series history for one number. ``limit`` is capped to
   *  365 server-side — anything bigger is silently clamped. */
  history(connectionId: number, limit = 90) {
    const q = new URLSearchParams({ limit: String(limit) })
    return apiCall<QualityHistoryResponse>(
      `/quality/numbers/${connectionId}/history?${q.toString()}`,
    )
  },

  /** Failure-reason breakdown over a window. Used by the
   *  dashboard's failure table + the "تفصيل أسباب الفشل" panel. */
  failures(connectionId: number, windowHours = 168) {
    const q = new URLSearchParams({ window_hours: String(windowHours) })
    return apiCall<FailureBreakdownResponse>(
      `/quality/numbers/${connectionId}/failures?${q.toString()}`,
    )
  },

  /** Force a fresh snapshot (writes a ``triggered_by="manual"``
   *  row). The dashboard's "أخذ لقطة الآن" button uses this. */
  takeSnapshot(connectionId: number) {
    return apiCall<SnapshotResponse>(
      `/quality/numbers/${connectionId}/snapshot`,
      { method: 'POST' },
    )
  },
}
