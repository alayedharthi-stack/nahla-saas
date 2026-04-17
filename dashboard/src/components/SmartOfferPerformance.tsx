// ── SmartOfferPerformance ───────────────────────────────────────────────
// Analytics widget that surfaces the OfferDecisionService ledger to the
// merchant. It answers two questions every dashboard owner asks about an
// AI-driven incentive system:
//
//   1. "What did the AI decide?"  → surface × source matrix + reason codes
//   2. "Did it work?"             → redemption rate + attributed revenue
//
// The widget is read-only: ledger rows are written by the backend service
// and updated only by the order-paid attribution hook. The merchant sets
// the policy rules elsewhere (Coupons / Promotions / Settings); here they
// only observe the outcome.

import { useEffect, useMemo, useState } from 'react'
import { Sparkles, Target, TrendingUp, Banknote, Info } from 'lucide-react'
import {
  offerDecisionsApi,
  REASON_CODE_LABELS,
  SOURCE_LABELS,
  SURFACE_LABELS,
  type OfferDecisionsBreakdown,
  type OfferDecisionsSummary,
} from '../api/offerDecisions'

const WINDOWS: { label: string; days: number }[] = [
  { label: '7 أيام',  days: 7  },
  { label: '30 يوم',  days: 30 },
  { label: '90 يوم',  days: 90 },
]

function fmtPct(v: number): string {
  return `${(v ?? 0).toLocaleString('ar-SA', { maximumFractionDigits: 1 })}%`
}

function fmtSar(v: number): string {
  return `${(v ?? 0).toLocaleString('ar-SA', { maximumFractionDigits: 0 })} ر.س`
}

function fmtInt(v: number): string {
  return (v ?? 0).toLocaleString('ar-SA')
}

interface MiniKpiProps {
  label: string
  value: string
  sub?:  string
  icon:  typeof Sparkles
  tint:  'amber' | 'emerald' | 'sky' | 'violet'
}

const TINTS: Record<MiniKpiProps['tint'], { bg: string; fg: string }> = {
  amber:   { bg: 'bg-amber-50',   fg: 'text-amber-600'   },
  emerald: { bg: 'bg-emerald-50', fg: 'text-emerald-600' },
  sky:     { bg: 'bg-sky-50',     fg: 'text-sky-600'     },
  violet:  { bg: 'bg-violet-50',  fg: 'text-violet-600'  },
}

function MiniKpi({ label, value, sub, icon: Icon, tint }: MiniKpiProps) {
  const t = TINTS[tint]
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-medium text-slate-500 uppercase tracking-wide">{label}</p>
          <p className="text-2xl font-bold text-slate-900 mt-1 tracking-tight">{value}</p>
          {sub && <p className="text-[11px] text-slate-400 mt-1">{sub}</p>}
        </div>
        <div className={`w-9 h-9 ${t.bg} rounded-lg flex items-center justify-center shrink-0`}>
          <Icon className={`w-4 h-4 ${t.fg}`} />
        </div>
      </div>
    </div>
  )
}

export default function SmartOfferPerformance() {
  const [days, setDays] = useState<number>(30)
  const [summary, setSummary] = useState<OfferDecisionsSummary | null>(null)
  const [breakdown, setBreakdown] = useState<OfferDecisionsBreakdown | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    Promise.all([offerDecisionsApi.summary(days), offerDecisionsApi.breakdown(days)])
      .then(([s, b]) => {
        if (cancelled) return
        setSummary(s)
        setBreakdown(b)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'تعذر تحميل بيانات قرارات العروض')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [days])

  const surfaces = useMemo(() => Object.keys(SURFACE_LABELS), [])
  const sources  = useMemo(() => ['promotion', 'coupon', 'none'] as const, [])

  return (
    <div className="card p-5">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3 min-w-0">
          <div className="shrink-0 w-9 h-9 rounded-lg bg-gradient-to-br from-amber-400 to-orange-500 text-white flex items-center justify-center shadow-sm shadow-amber-200">
            <Sparkles className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-slate-900">أداء العروض الذكية</h2>
              <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/50">
                <span className="text-[9px] font-black text-amber-700 leading-none tracking-wider">AI</span>
              </span>
            </div>
            <p className="text-[11px] text-slate-500 mt-0.5">
              يقرر Autopilot تلقائياً متى يُقدَّم العرض، ولمن، وبأي قيمة — هذه نتائج قراراته.
            </p>
          </div>
        </div>

        {/* Window switch */}
        <div className="inline-flex rounded-lg border border-slate-200 p-0.5 bg-slate-50 shrink-0">
          {WINDOWS.map((w) => {
            const active = w.days === days
            return (
              <button
                key={w.days}
                type="button"
                onClick={() => setDays(w.days)}
                className={
                  'px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors ' +
                  (active
                    ? 'bg-white text-slate-900 shadow-sm'
                    : 'text-slate-500 hover:text-slate-700')
                }
              >
                {w.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* Body */}
      {loading ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-24 rounded-xl bg-slate-100 animate-pulse" />
          ))}
        </div>
      ) : error ? (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </div>
      ) : summary && summary.decisions_total === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center">
          <Info className="w-5 h-5 text-slate-400 mx-auto mb-2" />
          <p className="text-xs font-medium text-slate-700">لا توجد قرارات عروض بعد في هذه الفترة</p>
          <p className="text-[11px] text-slate-500 mt-1">
            ستظهر هنا فور أن يبدأ Autopilot بإصدار عروض ضمن حملاتك أو محادثاتك التلقائية.
          </p>
        </div>
      ) : summary ? (
        <>
          {/* Headline KPIs */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <MiniKpi
              label="معدل الاسترداد"
              value={fmtPct(summary.redemption_rate_pct)}
              sub={`${fmtInt(summary.offers_attributed)} من ${fmtInt(summary.offers_issued)} عرض`}
              icon={TrendingUp}
              tint="emerald"
            />
            <MiniKpi
              label="إيراد منسوب للعروض"
              value={fmtSar(summary.attributed_revenue)}
              sub="طلبات مدفوعة عَبر كوبونات Autopilot"
              icon={Banknote}
              tint="amber"
            />
            <MiniKpi
              label="عروض صادرة"
              value={fmtInt(summary.offers_issued)}
              sub={`من إجمالي ${fmtInt(summary.decisions_total)} قرار`}
              icon={Target}
              tint="sky"
            />
            <MiniKpi
              label="نسخة السياسة"
              value={summary.policy_version}
              sub="مرجع تدقيقي لكل قرار"
              icon={Sparkles}
              tint="violet"
            />
          </div>

          {/* Surface × Source matrix */}
          <div className="mt-5 grid lg:grid-cols-2 gap-4">
            <div>
              <p className="text-[11px] font-semibold text-slate-700 mb-2 uppercase tracking-wide">
                توزيع القرارات حسب القناة
              </p>
              <div className="rounded-xl border border-slate-100 overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="text-start px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">القناة</th>
                      {sources.map((s) => (
                        <th key={s} className="text-end px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
                          {SOURCE_LABELS[s]}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-50">
                    {surfaces.map((surface) => {
                      const row = breakdown?.matrix?.[surface] ?? {}
                      const rowTotal = sources.reduce((sum, src) => sum + (row[src] ?? 0), 0)
                      return (
                        <tr key={surface} className="hover:bg-slate-50/60">
                          <td className="px-3 py-2.5 font-medium text-slate-700">
                            {SURFACE_LABELS[surface]}
                            <div className="text-[10px] text-slate-400">{fmtInt(rowTotal)} قرار</div>
                          </td>
                          {sources.map((s) => {
                            const v = row[s] ?? 0
                            const pct = rowTotal > 0 ? Math.round((v / rowTotal) * 100) : 0
                            return (
                              <td key={s} className="px-3 py-2.5 text-end">
                                <span className="font-semibold text-slate-900">{fmtInt(v)}</span>
                                <span className="text-[10px] text-slate-400 ms-1">({pct}%)</span>
                              </td>
                            )
                          })}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Reason codes */}
            <div>
              <p className="text-[11px] font-semibold text-slate-700 mb-2 uppercase tracking-wide">
                أسباب القرارات الأكثر تكراراً
              </p>
              <div className="rounded-xl border border-slate-100 p-3 space-y-2">
                {breakdown && breakdown.reason_codes.length > 0 ? (
                  breakdown.reason_codes.slice(0, 6).map((row) => {
                    const max = breakdown.reason_codes[0]?.count || 1
                    const pct = Math.max(4, Math.round((row.count / max) * 100))
                    return (
                      <div key={row.code}>
                        <div className="flex items-center justify-between text-[11px]">
                          <span className="text-slate-700 truncate">
                            {REASON_CODE_LABELS[row.code] ?? row.code}
                          </span>
                          <span className="text-slate-500 font-medium tabular-nums">{fmtInt(row.count)}</span>
                        </div>
                        <div className="mt-1 h-1.5 rounded-full bg-slate-100 overflow-hidden">
                          <div
                            className="h-full bg-gradient-to-r from-amber-400 to-orange-500"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </div>
                    )
                  })
                ) : (
                  <p className="text-[11px] text-slate-400 py-4 text-center">
                    لا توجد أسباب مسجَّلة بعد لهذه الفترة.
                  </p>
                )}
              </div>
            </div>
          </div>

          {/* Discount-bucket lift */}
          {breakdown && Object.keys(breakdown.by_discount_bucket).length > 0 && (
            <div className="mt-5">
              <p className="text-[11px] font-semibold text-slate-700 mb-2 uppercase tracking-wide">
                الأداء حسب شريحة الخصم (نسبة مئوية)
              </p>
              <div className="rounded-xl border border-slate-100 overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="text-start px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">الشريحة</th>
                      <th className="text-end px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">عروض صادرة</th>
                      <th className="text-end px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">مستردة</th>
                      <th className="text-end px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">معدل</th>
                      <th className="text-end px-3 py-2 text-[10px] font-semibold text-slate-500 uppercase tracking-wide">إيراد منسوب</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-50">
                    {Object.entries(breakdown.by_discount_bucket)
                      .sort(([a], [b]) => parseInt(a, 10) - parseInt(b, 10))
                      .map(([bucket, row]) => {
                        const rate = row.issued > 0 ? (row.attributed / row.issued) * 100 : 0
                        return (
                          <tr key={bucket} className="hover:bg-slate-50/60">
                            <td className="px-3 py-2.5 font-medium text-slate-700">{bucket}</td>
                            <td className="px-3 py-2.5 text-end text-slate-900">{fmtInt(row.issued)}</td>
                            <td className="px-3 py-2.5 text-end text-slate-900">{fmtInt(row.attributed)}</td>
                            <td className="px-3 py-2.5 text-end font-semibold text-emerald-600">{fmtPct(rate)}</td>
                            <td className="px-3 py-2.5 text-end text-slate-900">{fmtSar(row.revenue)}</td>
                          </tr>
                        )
                      })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      ) : null}
    </div>
  )
}
