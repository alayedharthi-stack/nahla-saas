// ── Offer Decisions API client ───────────────────────────────────────────
// Read-only telemetry over the OfferDecisionService ledger.
//
// The widget on the Analytics page consumes `summary()` to render the
// "Smart Offer Performance" KPIs (redemption rate + attributed revenue +
// surface/source split). `breakdown()` powers the explainability tabs
// (reason codes + discount-bucket lift).

import { apiCall } from './client'

export interface OfferDecisionsSummary {
  window_days:         number
  decisions_total:     number
  offers_issued:       number
  offers_attributed:   number
  redemption_rate_pct: number
  attributed_revenue:  number
  policy_version:      string
  by_surface:          Record<string, number>
  by_source:           Record<string, number>
}

export interface OfferDecisionsReasonRow {
  code:  string
  count: number
}

export interface OfferDecisionsBucketRow {
  issued:     number
  attributed: number
  revenue:    number
}

export interface OfferDecisionsBreakdown {
  window_days:         number
  reason_codes:        OfferDecisionsReasonRow[]
  matrix:              Record<string, Record<string, number>>
  by_discount_bucket:  Record<string, OfferDecisionsBucketRow>
}

export const offerDecisionsApi = {
  summary: (days = 30) =>
    apiCall<OfferDecisionsSummary>(`/offers/decisions/summary?days=${days}`),
  breakdown: (days = 30) =>
    apiCall<OfferDecisionsBreakdown>(`/offers/decisions/breakdown?days=${days}`),
}

// ── Display metadata (Arabic-first) ──────────────────────────────────────

export const SURFACE_LABELS: Record<string, string> = {
  automation:     'الحملات الذكية',
  chat:           'محادثات الذكاء',
  segment_change: 'تغيّر الشريحة',
}

export const SOURCE_LABELS: Record<string, string> = {
  promotion: 'عرض ترويجي',
  coupon:    'كوبون',
  none:      'بدون عرض',
}

export const REASON_CODE_LABELS: Record<string, string> = {
  explicit_promotion_id:        'عرض محدّد يدوياً',
  auto_promotion_match:         'تطابق عرض نشط',
  explicit_coupon_pct:          'كوبون بنسبة محدّدة',
  segment_default_pct:          'الافتراضي للشريحة',
  caller_suggested_pct:         'اقتراح المساعد',
  signal_nudge_price_sensitive: 'تحفيز للعملاء حساسي السعر',
  signal_nudge_high_value:      'تحفيز للعملاء عاليي القيمة',
  cap_max_discount:             'تطبيق الحدّ الأقصى للخصم',
  cap_frequency:                'حدّ تكرار العروض',
  no_signal_match:              'لا توجد إشارة كافية',
  rule_disabled:                'القاعدة معطّلة',
  policy_exception:             'استثناء في السياسة',
  unknown_surface:              'سياق غير معروف',
}
