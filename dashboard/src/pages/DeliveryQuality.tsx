/**
 * DeliveryQuality.tsx
 * ───────────────────
 * Route: /delivery-quality
 *
 * Phase 3 of the Delivery Quality Intelligence Layer — a
 * read-only dashboard that surfaces, for each WhatsApp Business
 * number connected to the tenant:
 *
 *   • Nahla's internal Quality Score (Excellent / Healthy /
 *     Warning / Risky / Critical) — a LEADING indicator built
 *     from our own event table, NOT Meta's trailing GREEN/YELLOW/
 *     RED label.
 *   • Delivery / read / failure / suppression metrics.
 *   • Historical trend of the score from the snapshot table.
 *   • Per-error-key failure breakdown so the merchant can act on
 *     the *right* lever (audience clean-up vs. template review
 *     vs. throttling).
 *   • Recommendation cards derived from the live metrics — not
 *     hard-coded — so they appear/disappear with the underlying
 *     signal.
 *
 * **No send-behaviour side effects.** The only mutating call this
 * page makes is the "أخذ لقطة الآن" button → POST snapshot,
 * which writes one analytics row. Phase 4 will add the
 * pre-send governor in a SEPARATE module so this page never
 * needs to gate anything.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, Activity, BarChart3, Camera, CheckCircle, ChevronDown,
  Clock, Eye, Flame, Gauge, Info, MessageSquareWarning, Phone,
  RefreshCw, Send, Shield, ShieldAlert, Sparkles, TrendingDown, Users,
} from 'lucide-react'
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'

import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import {
  deliveryQualityApi,
  type FailureBreakdownResponse,
  type FailureBreakdownRow,
  type QualityHistoryResponse,
  type QualityNumberRow,
  type QualityNumbersResponse,
  type QualitySnapshot,
  type QualityTier,
  type TierThreshold,
} from '../api/deliveryQuality'


// ── Tier visual mapping ──────────────────────────────────────────
//
// Stable across the whole page so the gauge, badge, chart line,
// and recommendation cards all stay in sync. The Arabic labels
// are the merchant-facing copy; the colour classes are Tailwind
// utility tokens (NOT raw hex) so the design-system can evolve
// without re-touching this file.

interface TierStyle {
  label_ar:    string
  badge:       'green' | 'amber' | 'red' | 'blue' | 'slate' | 'purple'
  textClass:   string
  bgClass:     string
  borderClass: string
  ringClass:   string
  chartHex:    string  // recharts needs an actual colour string
}

const TIER_STYLE: Record<QualityTier, TierStyle> = {
  excellent: {
    label_ar: 'ممتاز',
    badge: 'green',
    textClass: 'text-emerald-700',
    bgClass: 'bg-emerald-50',
    borderClass: 'border-emerald-200',
    ringClass: 'ring-emerald-300',
    chartHex: '#10b981',
  },
  healthy: {
    label_ar: 'جيد',
    badge: 'blue',
    textClass: 'text-blue-700',
    bgClass: 'bg-blue-50',
    borderClass: 'border-blue-200',
    ringClass: 'ring-blue-300',
    chartHex: '#3b82f6',
  },
  warning: {
    label_ar: 'تحذير',
    badge: 'amber',
    textClass: 'text-amber-700',
    bgClass: 'bg-amber-50',
    borderClass: 'border-amber-200',
    ringClass: 'ring-amber-300',
    chartHex: '#f59e0b',
  },
  risky: {
    label_ar: 'محفوف بالمخاطر',
    badge: 'amber',
    textClass: 'text-orange-700',
    bgClass: 'bg-orange-50',
    borderClass: 'border-orange-200',
    ringClass: 'ring-orange-300',
    chartHex: '#ea580c',
  },
  critical: {
    label_ar: 'حرج',
    badge: 'red',
    textClass: 'text-red-700',
    bgClass: 'bg-red-50',
    borderClass: 'border-red-200',
    ringClass: 'ring-red-300',
    chartHex: '#dc2626',
  },
}

function tierStyle(tier?: QualityTier | string | null): TierStyle {
  // Defensive: a tier we don't recognise (forward-compat with
  // future bands) renders as ``slate`` so we never blow up.
  const t = (tier as QualityTier) || 'healthy'
  return TIER_STYLE[t] || TIER_STYLE.healthy
}


// ── Number / percent helpers ──────────────────────────────────────
//
// Always pass through ``toLocaleString('ar-SA')`` for digit
// rendering so the dashboard stays consistent with the rest of
// the Arabic UI. ``fmtPct(null)`` falls back to "—" so the table
// rows don't render an awkward "null%".

function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return Number(value).toLocaleString('ar-SA')
}

function fmtScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(1)
}

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const diff = Date.now() - t
  if (diff < 60_000)         return 'قبل لحظات'
  if (diff < 3_600_000)      return `قبل ${Math.floor(diff / 60_000)} دقيقة`
  if (diff < 86_400_000)     return `قبل ${Math.floor(diff / 3_600_000)} ساعة`
  return `قبل ${Math.floor(diff / 86_400_000)} يوم`
}


// ── Score Gauge ───────────────────────────────────────────────────
//
// A pure-SVG semicircle gauge. Why hand-rolled instead of
// recharts: the gauge needs to render even when the score is
// ``null`` ("لا توجد بيانات كافية") — which recharts handles
// poorly — and we want the tier-coloured arc to span only the
// ``[0, score]`` range so a Risky tenant at 42 visually fills
// less than half the dial. Drawing the arc by hand is ~15 lines
// and avoids pulling in a second chart concept.

interface GaugeProps {
  score: number | null
  tier:  QualityTier
}

function ScoreGauge({ score, tier }: GaugeProps) {
  const style = tierStyle(tier)
  const value = Math.max(0, Math.min(100, score ?? 0))

  // Build a 180° arc from (0,80) → (200,80). Centre is at (100,80),
  // radius 80. We split the dial into a background path (the full
  // semicircle in slate-100) and a foreground path that ends at the
  // ``value`` angle so the fill grows with the score.
  const R = 80
  const cx = 100
  const cy = 80
  const angle = Math.PI * (1 - value / 100)        // 180°..0°
  const ex = cx + R * Math.cos(angle)
  const ey = cy - R * Math.sin(angle)
  const largeArc = value > 50 ? 1 : 0

  // Background arc — always the same.
  const bgPath = `M 20 80 A 80 80 0 0 1 180 80`
  const fgPath = `M 20 80 A 80 80 0 ${largeArc} 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`

  return (
    <div className={`flex flex-col items-center justify-center rounded-2xl border-2 ${style.borderClass} ${style.bgClass} p-5`}>
      <div className="w-full max-w-[200px]">
        <svg viewBox="0 0 200 100" className="w-full">
          <path d={bgPath} stroke="#e2e8f0" strokeWidth="14" fill="none" strokeLinecap="round" />
          {score !== null && (
            <path d={fgPath} stroke={style.chartHex} strokeWidth="14" fill="none" strokeLinecap="round" />
          )}
        </svg>
      </div>
      <div className="-mt-2 text-center">
        <div className={`text-3xl font-bold ${style.textClass}`}>
          {score === null ? '—' : fmtScore(score)}
        </div>
        <div className={`text-sm font-semibold mt-1 ${style.textClass}`}>
          {style.label_ar}
        </div>
        {score === null && (
          <div className="text-[10px] text-slate-500 mt-0.5 max-w-[16ch]">
            في انتظار بيانات كافية للتقييم
          </div>
        )}
      </div>
    </div>
  )
}


// ── Metric tile ───────────────────────────────────────────────────

interface MetricTileProps {
  label: string
  value: string
  hint?: string
  icon:  React.ComponentType<{ className?: string }>
  tone?: 'neutral' | 'good' | 'warn' | 'bad'
}

function MetricTile({ label, value, hint, icon: Icon, tone = 'neutral' }: MetricTileProps) {
  const toneClasses = {
    neutral: 'text-slate-600',
    good:    'text-emerald-600',
    warn:    'text-amber-600',
    bad:     'text-red-600',
  }[tone]
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex items-center gap-2 mb-2">
        <Icon className={`w-4 h-4 ${toneClasses}`} />
        <span className="text-xs text-slate-500">{label}</span>
      </div>
      <div className={`text-xl font-bold ${toneClasses}`}>{value}</div>
      {hint && <div className="text-[11px] text-slate-400 mt-1">{hint}</div>}
    </div>
  )
}


// ── Recommendation derivation ─────────────────────────────────────
//
// The four recommendation cards specified by Phase 3 are NOT
// hard-coded toggles — each is gated by a metric threshold so the
// merchant only sees advice that actually matches their state.
// This keeps the dashboard from crying wolf and trains the
// merchant to trust the recommendations when they DO appear.
//
// Thresholds are intentionally conservative: a 1% quality_risk
// rate or a single critical event in the window is enough to
// surface the relevant card.

interface Recommendation {
  id:        string
  title:     string
  body:      string
  icon:      React.ComponentType<{ className?: string }>
  tone:      'warn' | 'bad' | 'info'
  cta?:      { label: string; to: string }
}

function deriveRecommendations(
  live: QualitySnapshot,
  failures: FailureBreakdownRow[],
): Recommendation[] {
  const out: Recommendation[] = []
  const raw = live.raw_metrics || {}
  const qualityRisk = Number(raw.quality_risk || 0)
  const criticalCnt = Number(raw.critical || 0)
  const failed = Number(raw.failed || 0)
  const suppressedInWindow = Number(raw.suppressed_in_window || 0)
  const sample = live.sample_size || 0

  // 1) Audience clean-up — triggered when the failure mix is
  //    dominated by ``not_on_whatsapp`` / ``invalid_phone``
  //    (both ``quality_risk`` tier signals).
  const audienceFailures = failures.filter(f =>
    f.error_key === 'not_on_whatsapp' || f.error_key === 'invalid_phone',
  )
  const audienceCnt = audienceFailures.reduce((s, r) => s + r.count, 0)
  if (qualityRisk >= 1 && (audienceCnt / Math.max(failed, 1)) >= 0.30) {
    out.push({
      id: 'clean-audience',
      title: 'نظّف الأرقام الضعيفة',
      body: `لاحظنا ${fmtInt(audienceCnt)} حالة فشل بسبب أرقام لا تملك واتساب أو أرقام غير صالحة. ` +
            `هذه الحالات تضر جودة رقمك لدى Meta عند تكرارها. ` +
            `استخدم أداة "تنظيف أسماء العملاء" لاستبعاد هذه الأرقام قبل الحملة القادمة.`,
      icon: Users,
      tone: 'warn',
      cta: { label: 'فتح أداة تنظيف العملاء', to: '/customers' },
    })
  }

  // 2) Throttle campaign size — triggered when suppress_rate is
  //    high (we auto-blocked >5% of the audience the merchant
  //    actually reached) OR when ``rate_limited`` events appear.
  const rateLimited = failures.find(f => f.error_key === 'rate_limited')?.count || 0
  const suppressRate = live.suppress_rate ?? 0
  if (suppressRate >= 0.05 || rateLimited > 0) {
    out.push({
      id: 'throttle',
      title: 'قلّل حجم الحملات',
      body: `${suppressRate >= 0.05 ? `تم استبعاد ${fmtPct(suppressRate)} من الجمهور تلقائياً بسبب جودة منخفضة. ` : ''}` +
            `${rateLimited > 0 ? `سجلنا ${fmtInt(rateLimited)} حالة rate_limit من Meta. ` : ''}` +
            `قسّم حملاتك الكبيرة على دفعات أصغر (مثلاً 500 رسالة/ساعة) لتقليل الضغط على رقمك.`,
      icon: TrendingDown,
      tone: 'warn',
    })
  }

  // 3) Template review — triggered when ``template_*`` errors
  //    appear, or when policy_violation lands in the window.
  const templateFailures = failures.filter(f =>
    f.error_key.startsWith('template_') || f.error_key === 'policy_violation',
  )
  const templateCnt = templateFailures.reduce((s, r) => s + r.count, 0)
  if (templateCnt > 0) {
    out.push({
      id: 'review-template',
      title: 'راجع القالب',
      body: `${fmtInt(templateCnt)} حالة فشل مرتبطة بالقالب أو بسياسات Meta. ` +
            `راجع نصوص القوالب — قد يحتوي القالب على عبارات تعتبرها Meta مخالفة، ` +
            `أو أن القالب لم يعد معتمداً.`,
      icon: MessageSquareWarning,
      tone: 'bad',
      cta: { label: 'فتح القوالب', to: '/templates' },
    })
  }

  // 4) Audience-hygiene warning.
  //
  // Important policy note (mirrors services/quality_score.py):
  // a high overall ``failure_rate`` indicates **bad phone
  // numbers** in the audience (not-on-whatsapp, invalid format,
  // permanent failures) — NOT customers who simply haven't
  // engaged recently. Inactivity by itself is not a quality
  // problem; re-engagement campaigns to inactive-but-reachable
  // customers are a legitimate (and important) use case.
  //
  // So this card is intentionally phrased around PHONE HYGIENE,
  // not "cold audience". The merchant gets pointed at the cleanup
  // tool, not asked to narrow their audience.
  const failureRate = live.failure_rate ?? 0
  if (sample >= 100 && failureRate >= 0.08) {
    out.push({
      id: 'audience-hygiene',
      title: 'جودة الأرقام في جمهورك تحتاج تنظيف',
      body: `معدل الفشل ${fmtPct(failureRate)} مرتفع — وهذا غالباً ` +
            `بسبب أرقام غير صالحة أو غير مسجلة على واتساب، وليس بسبب ` +
            `عدم تفاعل العملاء. الإرسال لعملاء غير متفاعلين (حملات ` +
            `إعادة التنشيط) أمر طبيعي ومفيد طالما الأرقام نفسها سليمة. ` +
            `استخدم أداة "تنظيف أسماء العملاء" لاستبعاد الأرقام السيئة فقط.`,
      icon: Phone,
      tone: 'warn',
      cta: { label: 'فتح صفحة العملاء', to: '/customers' },
    })
  }

  // 5) Critical event escalation — overrides everything else.
  //    A single critical event in the window deserves a dedicated
  //    card because it indicates a policy-level issue, not just
  //    audience hygiene.
  if (criticalCnt > 0) {
    out.unshift({
      id: 'critical-escalation',
      title: 'حدث جسيم على الرقم',
      body: `سجلنا ${fmtInt(criticalCnt)} حدث جسيم (policy violation / template paused / account locked) خلال هذه النافذة. ` +
            `أوقف الحملات النشطة فوراً وراجع رسائل Meta على البريد/Business Manager قبل المتابعة.`,
      icon: ShieldAlert,
      tone: 'bad',
    })
  }

  // 6) ``suppressed_in_window`` follow-up — the merchant should
  //    know how many recipients we auto-blocked this window even
  //    if the rate isn't above the throttle threshold.
  if (suppressedInWindow >= 10 && suppressRate < 0.05) {
    out.push({
      id: 'suppressions-info',
      title: 'تم استبعاد بعض المستلمين تلقائياً',
      body: `قام النظام باستبعاد ${fmtInt(suppressedInWindow)} رقم تلقائياً بسبب فشل متكرر. ` +
            `هؤلاء العملاء لن يستقبلوا أي حملة لاحقة حتى يراسلوك أولاً — لا حاجة لإجراء يدوي.`,
      icon: Shield,
      tone: 'info',
    })
  }

  return out
}


// ── Failure-table helpers ─────────────────────────────────────────
//
// Visual classification mapping: which Lucide icon + colour
// describes each ``error_key``. The mapping covers the six error
// classes the user explicitly named (``blocked_by_user`` etc.)
// and falls back gracefully for anything else the classifier
// emits over time.

function failureRowVisuals(row: FailureBreakdownRow) {
  const key = row.error_key
  if (key === 'blocked_by_user') {
    return { icon: ShieldAlert, tone: 'bad' as const }
  }
  if (key === 'invalid_phone' || key === 'not_on_whatsapp') {
    return { icon: Phone, tone: 'warn' as const }
  }
  if (key === 'rate_limited' || key === 'temporary_failure') {
    return { icon: Clock, tone: 'warn' as const }
  }
  if (key === 'permanent_failure' || key === 'recipient_quality_low') {
    return { icon: TrendingDown, tone: 'bad' as const }
  }
  if (key === 'unknown_error') {
    return { icon: Info, tone: 'info' as const }
  }
  // Map by tier as a safety net.
  if (row.quality_tier === 'critical') {
    return { icon: AlertTriangle, tone: 'bad' as const }
  }
  if (row.quality_tier === 'quality_risk') {
    return { icon: Phone, tone: 'warn' as const }
  }
  return { icon: Info, tone: 'info' as const }
}


// ── Main page ────────────────────────────────────────────────────

export default function DeliveryQuality() {
  const [numbersResp, setNumbersResp]   = useState<QualityNumbersResponse | null>(null)
  const [selectedId,  setSelectedId]    = useState<number | null>(null)
  const [history,     setHistory]       = useState<QualityHistoryResponse | null>(null)
  const [failures,    setFailures]      = useState<FailureBreakdownResponse | null>(null)
  const [loading,     setLoading]       = useState(true)
  const [snapshotBusy, setSnapshotBusy] = useState(false)
  const [error,       setError]         = useState<string | null>(null)
  const [windowKey,   setWindowKey]     = useState<'short' | 'long'>('short')

  const loadOverview = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const resp = await deliveryQualityApi.numbers()
      setNumbersResp(resp)
      // Auto-select the first connection on first load. Subsequent
      // refreshes preserve whatever the user picked.
      if (resp.numbers.length > 0 && selectedId === null) {
        setSelectedId(resp.numbers[0].connection.id)
      }
    } catch (e: any) {
      setError(e?.detail || e?.message || 'تعذر تحميل بيانات الجودة')
    } finally {
      setLoading(false)
    }
  // selectedId in deps would cause an infinite loop on first set;
  // the conditional inside the function gates that case.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadDetails = useCallback(async (connectionId: number, windowHours: number) => {
    try {
      const [h, f] = await Promise.all([
        deliveryQualityApi.history(connectionId, 90),
        deliveryQualityApi.failures(connectionId, windowHours),
      ])
      setHistory(h)
      setFailures(f)
    } catch (e: any) {
      // Detail panels failing shouldn't blow away the whole page —
      // keep the overview rendered with an inline error.
      setError(e?.detail || e?.message || 'تعذر تحميل تفاصيل الرقم')
    }
  }, [])

  useEffect(() => { loadOverview() }, [loadOverview])

  // When the user picks a different number, refetch its history
  // and failure breakdown. We DON'T refetch the overview here —
  // those values are already in the listing response.
  const selected: QualityNumberRow | null = useMemo(() => {
    if (!numbersResp || selectedId === null) return null
    return numbersResp.numbers.find(n => n.connection.id === selectedId) || null
  }, [numbersResp, selectedId])

  const windowHours = useMemo(() => {
    if (!numbersResp) return 168
    return windowKey === 'short'
      ? numbersResp.default_window_hours
      : (numbersResp.alt_window_hours ?? 720)
  }, [numbersResp, windowKey])

  useEffect(() => {
    if (selectedId !== null) {
      loadDetails(selectedId, windowHours)
    }
  }, [selectedId, windowHours, loadDetails])

  // ── Manual snapshot trigger ─────────────────────────────────────
  // POSTs and then refreshes the local data so the gauge + chart
  // pick up the new row immediately.
  const handleTakeSnapshot = async () => {
    if (selectedId === null) return
    try {
      setSnapshotBusy(true)
      await deliveryQualityApi.takeSnapshot(selectedId)
      // Reload the overview (refreshes ``latest_snapshot``) and the
      // history chart in parallel — fire and refresh, no spinner
      // chains.
      await Promise.all([
        loadOverview(),
        loadDetails(selectedId, windowHours),
      ])
    } catch (e: any) {
      setError(e?.detail || e?.message || 'تعذر أخذ لقطة الآن')
    } finally {
      setSnapshotBusy(false)
    }
  }

  // ── Chart data prep ─────────────────────────────────────────────
  // recharts wants data in chronological order; the backend returns
  // newest-first so we reverse here. We project each snapshot to a
  // {x, score} tuple — the tier band overlay (ReferenceLine) is
  // sourced from ``tier_thresholds``.
  const chartData = useMemo(() => {
    if (!history) return []
    return [...history.snapshots]
      .filter(s => s.nahla_quality_score !== null)
      .reverse()
      .map(s => ({
        ts:    new Date(s.taken_at).getTime(),
        label: new Date(s.taken_at).toLocaleDateString('ar-SA', {
          month: 'short', day: 'numeric',
        }),
        score: Math.round((s.nahla_quality_score || 0) * 10) / 10,
        tier:  s.nahla_quality_tier,
      }))
  }, [history])

  // ── Recommendations derived live from metrics ───────────────────
  const recommendations: Recommendation[] = useMemo(() => {
    if (!selected) return []
    return deriveRecommendations(selected.live, failures?.breakdown || [])
  }, [selected, failures])

  // ── Render ──────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <PageHeader
        title="جودة الإرسال"
        subtitle="تقييم داخلي لجودة كل رقم واتساب مع تحليل أسباب الفشل وتوصيات عملية"
        action={
          <div className="flex items-center gap-2">
            <button
              onClick={() => loadOverview()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50"
              title="تحديث البيانات"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              تحديث
            </button>
            <button
              onClick={handleTakeSnapshot}
              disabled={selectedId === null || snapshotBusy}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed"
              title="حفظ لقطة جودة الآن (لا تغيّر سلوك الإرسال)"
            >
              <Camera className={`w-3.5 h-3.5 ${snapshotBusy ? 'animate-pulse' : ''}`} />
              {snapshotBusy ? 'جاري الالتقاط…' : 'أخذ لقطة الآن'}
            </button>
          </div>
        }
      />

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-600 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* ── Empty state: no connections at all ───────────────────── */}
      {!loading && numbersResp && numbersResp.numbers.length === 0 && (
        <div className="rounded-2xl border border-slate-200 bg-white p-10 text-center">
          <Phone className="w-12 h-12 text-slate-300 mx-auto mb-3" />
          <h2 className="text-base font-semibold text-slate-700 mb-1">
            لا يوجد رقم واتساب مربوط بعد
          </h2>
          <p className="text-sm text-slate-500">
            اربط رقم واتساب أعمال من صفحة الإعدادات لتفعيل تقييم جودة الإرسال.
          </p>
        </div>
      )}

      {/* ── Loading skeleton ─────────────────────────────────────── */}
      {loading && !numbersResp && (
        <div className="space-y-4 animate-pulse">
          <div className="h-12 bg-slate-100 rounded-xl" />
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
            <div className="lg:col-span-1 h-48 bg-slate-100 rounded-2xl" />
            <div className="lg:col-span-3 grid grid-cols-2 lg:grid-cols-3 gap-4">
              {[1, 2, 3, 4, 5, 6].map(i => (
                <div key={i} className="h-24 bg-slate-100 rounded-xl" />
              ))}
            </div>
          </div>
          <div className="h-64 bg-slate-100 rounded-2xl" />
        </div>
      )}

      {/* ── Main body ────────────────────────────────────────────── */}
      {selected && numbersResp && (
        <>
          <ConnectionSelector
            numbers={numbersResp.numbers}
            selectedId={selectedId}
            onChange={setSelectedId}
            windowKey={windowKey}
            onChangeWindow={setWindowKey}
            tierThresholds={numbersResp.tier_thresholds}
          />

          <HeroPanel row={selected} tierThresholds={numbersResp.tier_thresholds} />

          <HistoryChart
            data={chartData}
            tierThresholds={numbersResp.tier_thresholds}
            isEmpty={chartData.length === 0}
          />

          <FailuresPanel
            failures={failures}
            windowHours={windowHours}
            totalEvents={Number(selected.live.raw_metrics?.total || 0)}
          />

          <PolicyNote />
          <RecommendationsPanel recs={recommendations} />
        </>
      )}
    </div>
  )
}


// ── Sub-components ────────────────────────────────────────────────


interface SelectorProps {
  numbers:        QualityNumberRow[]
  selectedId:     number | null
  onChange:       (id: number) => void
  windowKey:      'short' | 'long'
  onChangeWindow: (w: 'short' | 'long') => void
  tierThresholds: TierThreshold[]
}

function ConnectionSelector({
  numbers, selectedId, onChange, windowKey, onChangeWindow,
}: SelectorProps) {
  // Single-number tenants get a static card; multi-number tenants
  // get a dropdown. We don't render a dropdown for one entry
  // because it adds visual noise without value.
  const single = numbers.length === 1
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 flex items-center justify-between flex-wrap gap-3">
      <div className="flex items-center gap-3">
        <Phone className="w-5 h-5 text-slate-500" />
        {single ? (
          <div>
            <div className="text-sm font-semibold text-slate-700">
              {numbers[0].connection.business_display_name
                || numbers[0].connection.phone_number
                || 'رقم بدون اسم'}
            </div>
            <div className="text-[11px] text-slate-400" dir="ltr">
              {numbers[0].connection.phone_number || '—'}
            </div>
          </div>
        ) : (
          <div className="relative">
            <select
              className="text-sm font-semibold bg-transparent border border-slate-200 rounded-lg pe-8 ps-3 py-1.5 appearance-none focus:outline-none focus:ring-2 focus:ring-brand-500"
              value={selectedId || ''}
              onChange={e => onChange(Number(e.target.value))}
            >
              {numbers.map(n => (
                <option key={n.connection.id} value={n.connection.id}>
                  {n.connection.business_display_name || n.connection.phone_number || `#${n.connection.id}`}
                </option>
              ))}
            </select>
            <ChevronDown className="w-4 h-4 text-slate-400 absolute end-2 top-1/2 -translate-y-1/2 pointer-events-none" />
          </div>
        )}
      </div>
      <div className="flex items-center gap-1 bg-slate-100 rounded-lg p-1">
        <button
          onClick={() => onChangeWindow('short')}
          className={`text-xs px-3 py-1 rounded-md font-medium ${
            windowKey === 'short' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'
          }`}
        >
          آخر 7 أيام
        </button>
        <button
          onClick={() => onChangeWindow('long')}
          className={`text-xs px-3 py-1 rounded-md font-medium ${
            windowKey === 'long' ? 'bg-white text-slate-800 shadow-sm' : 'text-slate-500'
          }`}
        >
          آخر 30 يوم
        </button>
      </div>
    </div>
  )
}


interface HeroProps {
  row: QualityNumberRow
  tierThresholds: TierThreshold[]
}

function HeroPanel({ row, tierThresholds: _tierThresholds }: HeroProps) {
  const live = row.live
  const raw  = live.raw_metrics || {}
  const total = Number(raw.total || 0)
  const delivered = Number(raw.delivered || 0)
  const reads = Number(raw.read || 0)
  const failed = Number(raw.failed || 0)
  const qualityRisk = Number(raw.quality_risk || 0)
  const critical = Number(raw.critical || 0)
  const suppressed = Number(raw.suppressed_in_window || 0)

  // Surface a small "last persisted snapshot" footer so the merchant
  // can sanity-check that the scheduler is running.
  const lastSnapshotIso = row.latest_snapshot?.taken_at || null

  return (
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
      <div className="lg:col-span-1">
        <ScoreGauge score={live.nahla_quality_score} tier={live.nahla_quality_tier} />
        {row.connection.meta_quality_rating && (
          // We display Meta's own label alongside but visually
          // subdued — the Nahla score is the headline. This is
          // intentional: Meta's label trails the actual quality
          // drift by hours-to-days.
          <div className="mt-3 rounded-xl border border-slate-200 bg-white p-3 text-center">
            <div className="text-[10px] uppercase tracking-wide text-slate-400 mb-1">
              تقييم Meta الحالي
            </div>
            <Badge
              label={row.connection.meta_quality_rating}
              variant={
                row.connection.meta_quality_rating === 'GREEN'  ? 'green' :
                row.connection.meta_quality_rating === 'YELLOW' ? 'amber' :
                row.connection.meta_quality_rating === 'RED'    ? 'red'   :
                'slate'
              }
            />
            <div className="text-[10px] text-slate-400 mt-1">
              يتأخر مؤشر Meta عادة عن جودتك الفعلية بساعات
            </div>
          </div>
        )}
      </div>

      <div className="lg:col-span-3 grid grid-cols-2 md:grid-cols-3 gap-3">
        <MetricTile
          label="معدل الوصول"
          value={fmtPct(live.delivery_rate)}
          hint={`${fmtInt(delivered + reads)} / ${fmtInt(total)}`}
          icon={Send}
          tone={(live.delivery_rate ?? 1) >= 0.9 ? 'good' : (live.delivery_rate ?? 0) >= 0.75 ? 'warn' : 'bad'}
        />
        <MetricTile
          label="معدل القراءة"
          value={fmtPct(live.read_rate)}
          hint={`${fmtInt(reads)} رسالة مقروءة`}
          icon={Eye}
          tone={(live.read_rate ?? 0) >= 0.3 ? 'good' : 'neutral'}
        />
        <MetricTile
          label="معدل الفشل"
          value={fmtPct(live.failure_rate)}
          hint={`${fmtInt(failed)} حالة فشل`}
          icon={AlertTriangle}
          tone={(live.failure_rate ?? 0) <= 0.05 ? 'good' : (live.failure_rate ?? 0) <= 0.15 ? 'warn' : 'bad'}
        />
        <MetricTile
          label="المستلمون المستبعدون تلقائياً"
          value={fmtInt(suppressed)}
          hint={`${fmtPct(live.suppress_rate)} من جمهور الفترة`}
          icon={Shield}
          tone={suppressed === 0 ? 'good' : suppressed < 10 ? 'warn' : 'bad'}
        />
        <MetricTile
          label="أحداث جودة (Quality Risk)"
          value={fmtInt(qualityRisk)}
          hint="فشل مرتبط بجودة الجمهور"
          icon={Activity}
          tone={qualityRisk === 0 ? 'good' : qualityRisk < 20 ? 'warn' : 'bad'}
        />
        <MetricTile
          label="أحداث جسيمة (Critical)"
          value={fmtInt(critical)}
          hint={critical > 0 ? 'يحتاج تدخّل فوري' : 'لا توجد أحداث جسيمة'}
          icon={Flame}
          tone={critical === 0 ? 'good' : 'bad'}
        />
      </div>

      <div className="lg:col-span-4 text-[11px] text-slate-400 flex items-center gap-2 px-1">
        <Clock className="w-3 h-3" />
        <span>
          نافذة القياس: آخر {live.metrics_window_hours} ساعة
          {' • '}
          عدد الأحداث المُحلَّلة: {fmtInt(live.sample_size)}
          {lastSnapshotIso && (
            <>
              {' • '}
              آخر لقطة محفوظة: {fmtRelative(lastSnapshotIso)}
            </>
          )}
        </span>
      </div>
    </div>
  )
}


interface ChartProps {
  data:           { ts: number; label: string; score: number; tier: QualityTier }[]
  tierThresholds: TierThreshold[]
  isEmpty:        boolean
}

function HistoryChart({ data, tierThresholds, isEmpty }: ChartProps) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <BarChart3 className="w-4 h-4 text-slate-500" />
          <h2 className="text-sm font-semibold text-slate-700">
            مسار الجودة عبر الوقت
          </h2>
        </div>
        <div className="text-[11px] text-slate-400">
          مبني على لقطات الجودة المحفوظة
        </div>
      </div>

      {isEmpty ? (
        <div className="text-center py-10 text-sm text-slate-400">
          <Sparkles className="w-8 h-8 mx-auto mb-2 text-slate-300" />
          لا توجد لقطات كافية بعد. اضغط "أخذ لقطة الآن" لإنشاء أول نقطة على المخطط.
        </div>
      ) : (
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 5, right: 12, left: -10, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                stroke="#cbd5e1"
              />
              <YAxis
                domain={[0, 100]}
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                stroke="#cbd5e1"
              />
              <Tooltip
                contentStyle={{
                  fontSize: '12px', borderRadius: 8,
                  border: '1px solid #e2e8f0',
                }}
                formatter={(value: any) => [`${value}`, 'الجودة']}
              />
              {/*
               * Tier bands as reference lines — gives the merchant
               * a visual anchor for "am I above warning?" without
               * adding a legend.
               */}
              {tierThresholds.map(t => (
                t.lower_bound > 0 && (
                  <ReferenceLine
                    key={t.label}
                    y={t.lower_bound}
                    stroke={tierStyle(t.label).chartHex}
                    strokeDasharray="4 4"
                    strokeOpacity={0.4}
                  />
                )
              ))}
              <Line
                type="monotone"
                dataKey="score"
                stroke="#0ea5e9"
                strokeWidth={2.5}
                dot={{ r: 3, fill: '#0ea5e9' }}
                activeDot={{ r: 5 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}


interface FailuresPanelProps {
  failures:     FailureBreakdownResponse | null
  windowHours:  number
  totalEvents:  number
}

function FailuresPanel({ failures, windowHours, totalEvents }: FailuresPanelProps) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-500" />
          <h2 className="text-sm font-semibold text-slate-700">
            تفصيل أسباب الفشل
          </h2>
        </div>
        <div className="text-[11px] text-slate-400">
          آخر {windowHours} ساعة
          {' • '}
          إجمالي الأحداث: {fmtInt(totalEvents)}
        </div>
      </div>

      {!failures || failures.breakdown.length === 0 ? (
        <div className="text-center py-8 text-sm text-emerald-600">
          <CheckCircle className="w-8 h-8 mx-auto mb-2" />
          لا توجد حالات فشل خلال هذه الفترة. عمل ممتاز.
        </div>
      ) : (
        <div className="space-y-2">
          {failures.breakdown.map(row => {
            const visuals = failureRowVisuals(row)
            const tone = visuals.tone === 'bad'  ? 'text-red-600 bg-red-50'    :
                         visuals.tone === 'warn' ? 'text-amber-600 bg-amber-50' :
                                                   'text-slate-600 bg-slate-50'
            const Icon = visuals.icon
            return (
              <div
                key={row.error_key}
                className="rounded-xl border border-slate-100 bg-slate-50/50 p-3 flex items-start gap-3"
              >
                <div className={`w-9 h-9 shrink-0 rounded-lg flex items-center justify-center ${tone}`}>
                  <Icon className="w-4 h-4" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="text-sm font-semibold text-slate-800">
                      {row.label_ar}
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <Badge
                        label={tierStyle(row.quality_tier as QualityTier).label_ar}
                        variant={tierStyle(row.quality_tier as QualityTier).badge}
                      />
                      {row.suppress_on_repeat && (
                        <Badge label="يفعّل الاستبعاد التلقائي" variant="purple" />
                      )}
                    </div>
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {fmtInt(row.count)} حالة
                    {' • '}
                    {fmtPct(row.share)} من إجمالي الفشل
                    {' • '}
                    {fmtInt(row.distinct_phones)} رقم مختلف
                  </div>
                  {row.advice_ar && (
                    <div className="text-[11px] text-slate-500 mt-1.5 border-t border-slate-100 pt-1.5">
                      💡 {row.advice_ar}
                    </div>
                  )}
                  <div className="text-[10px] text-slate-300 mt-1 font-mono" dir="ltr">
                    {row.error_key}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}


/**
 * Inline policy note that clarifies the platform's stance on
 * "cold-but-real" customers vs. "bad phones". This is displayed
 * immediately above the recommendations so a merchant who
 * launches a win-back campaign doesn't read the cards in panic.
 *
 * Mirrors the architectural policy documented in
 * ``services/quality_score.py`` ("What this score does NOT use").
 */
function PolicyNote() {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-[11px] text-slate-600 flex items-start gap-2">
      <Info className="w-3.5 h-3.5 mt-0.5 shrink-0 text-slate-400" />
      <div>
        <span className="font-semibold text-slate-700">سياسة نحلة:</span>
        {' '}
        عدم تفاعل العميل (Inactivity) <span className="font-semibold">لا</span> يُعتبر خطراً على جودة رقمك.
        حملات إعادة التنشيط مسموحة وموصى بها طالما الأرقام نفسها سليمة.
        فقط الأرقام السيئة (غير مسجلة على واتساب، غير صالحة، محظورة، فشل دائم) تضر بتقييم رقمك لدى Meta.
      </div>
    </div>
  )
}


function RecommendationsPanel({ recs }: { recs: Recommendation[] }) {
  if (recs.length === 0) {
    return (
      <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-center">
        <CheckCircle className="w-10 h-10 text-emerald-500 mx-auto mb-2" />
        <h2 className="text-sm font-semibold text-emerald-800">
          لا توجد توصيات نشطة — جودة رقمك ممتازة حالياً
        </h2>
        <p className="text-xs text-emerald-600 mt-1">
          استمر بنفس النهج. ستظهر هنا أي تحذيرات فور رصد أي مؤشر سلبي.
        </p>
      </div>
    )
  }
  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <Gauge className="w-4 h-4 text-slate-500" />
        <h2 className="text-sm font-semibold text-slate-700">
          توصيات لتحسين الجودة
        </h2>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {recs.map(rec => {
          const Icon = rec.icon
          const border = rec.tone === 'bad'  ? 'border-red-200    bg-red-50/50'    :
                         rec.tone === 'warn' ? 'border-amber-200  bg-amber-50/50'  :
                                               'border-blue-200   bg-blue-50/50'
          const iconTone = rec.tone === 'bad'  ? 'text-red-600 bg-red-100'    :
                           rec.tone === 'warn' ? 'text-amber-600 bg-amber-100' :
                                                 'text-blue-600 bg-blue-100'
          return (
            <div key={rec.id} className={`rounded-xl border p-4 ${border}`}>
              <div className="flex items-start gap-3">
                <div className={`w-9 h-9 shrink-0 rounded-lg flex items-center justify-center ${iconTone}`}>
                  <Icon className="w-4 h-4" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-semibold text-slate-800">
                    {rec.title}
                  </div>
                  <p className="text-xs text-slate-600 mt-1 leading-relaxed">
                    {rec.body}
                  </p>
                  {rec.cta && (
                    <a
                      href={rec.cta.to}
                      className="inline-flex items-center gap-1 text-xs font-medium text-brand-700 hover:text-brand-800 mt-2"
                    >
                      {rec.cta.label} ←
                    </a>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
