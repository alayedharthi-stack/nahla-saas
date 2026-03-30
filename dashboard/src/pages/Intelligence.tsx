import { useState, useEffect, useCallback } from 'react'
import {
  RefreshCw,
  Brain,
  TrendingUp,
  AlertTriangle,
  Crown,
  Zap,
  Users,
  ShoppingCart,
  CheckCircle,
  ArrowLeft,
  Sparkles,
  Target,
  Clock,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  automationsApi,
  IntelligenceDashboard,
  IntelligenceSuggestion,
  CustomerSegment,
} from '../api/automations'

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatArabicDate(dateStr: string): string {
  try {
    const date = new Date(dateStr)
    return date.toLocaleDateString('ar-SA', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    })
  } catch {
    return dateStr
  }
}

function confidenceVariant(confidence: number): 'green' | 'amber' | 'red' {
  if (confidence > 80) return 'green'
  if (confidence > 60) return 'amber'
  return 'red'
}

function inactiveDaysVariant(days: number): 'red' | 'amber' {
  return days > 90 ? 'red' : 'amber'
}

function priorityDotColor(priority: 'high' | 'medium' | 'low'): string {
  if (priority === 'high') return 'bg-red-500'
  if (priority === 'medium') return 'bg-amber-500'
  return 'bg-slate-400'
}

function SuggestionIcon({ type }: { type: string }) {
  if (type === 'reorder') return <RefreshCw className="w-4 h-4 text-brand-500 shrink-0" />
  if (type === 'winback') return <Users className="w-4 h-4 text-blue-500 shrink-0" />
  if (type === 'vip') return <Crown className="w-4 h-4 text-amber-500 shrink-0" />
  return <Sparkles className="w-4 h-4 text-purple-500 shrink-0" />
}

function segmentBarColor(color: string): string {
  const map: Record<string, string> = {
    green: 'bg-emerald-500',
    blue: 'bg-blue-500',
    amber: 'bg-amber-500',
    slate: 'bg-slate-300',
    red: 'bg-red-500',
    purple: 'bg-purple-500',
  }
  return map[color] ?? 'bg-slate-300'
}

// ── Loading Spinner ────────────────────────────────────────────────────────────

function LoadingState() {
  return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <div className="w-10 h-10 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
      <p className="text-sm text-slate-500 font-medium">جارٍ تحليل بيانات العملاء…</p>
    </div>
  )
}

// ── Error State ───────────────────────────────────────────────────────────────

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <AlertTriangle className="w-10 h-10 text-red-400" />
      <p className="text-sm text-slate-600 font-medium">تعذّر تحميل البيانات</p>
      <button
        onClick={onRetry}
        className="btn-primary text-sm flex items-center gap-2"
      >
        <RefreshCw className="w-4 h-4" />
        إعادة المحاولة
      </button>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Intelligence() {
  useLanguage() // initialise RTL context

  const [data, setData] = useState<IntelligenceDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      const result = await automationsApi.getIntelligence()
      setData(result)
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleApplySuggestion = (suggestion: IntelligenceSuggestion) => {
    alert(`سيتم تطبيق التوصية: ${suggestion.title}`)
  }

  return (
    <div className="space-y-6">
      {/* ── Page Header ───────────────────────────────────────────────────── */}
      <PageHeader
        title="نهلة الذكية"
        subtitle="رؤى تنبؤية وتوصيات تسويقية مبنية على الذكاء الاصطناعي"
        action={
          <button
            onClick={load}
            disabled={loading}
            className="btn-secondary text-sm flex items-center gap-2"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            تحديث
          </button>
        }
      />

      {/* ── States ────────────────────────────────────────────────────────── */}
      {loading && <LoadingState />}
      {!loading && error && <ErrorState onRetry={load} />}

      {!loading && !error && data && (
        <div className="space-y-6">
          {/* ── Summary StatCards ──────────────────────────────────────────── */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard
              label="سيُعيدون الطلب قريباً"
              value={String(data.summary.reorder_soon_count)}
              change={0}
              icon={RefreshCw}
              iconColor="text-emerald-600"
              iconBg="bg-emerald-50"
            />
            <StatCard
              label="في خطر المغادرة"
              value={String(data.summary.churn_risk_count)}
              change={0}
              icon={AlertTriangle}
              iconColor="text-red-600"
              iconBg="bg-red-50"
            />
            <StatCard
              label="عملاء VIP"
              value={String(data.summary.vip_count)}
              change={0}
              icon={Crown}
              iconColor="text-amber-600"
              iconBg="bg-amber-50"
            />
            <StatCard
              label="أتمتة نشطة"
              value={String(data.summary.active_automations)}
              change={0}
              icon={Zap}
              iconColor="text-brand-600"
              iconBg="bg-brand-50"
            />
          </div>

          {/* ── Suggestions Panel ──────────────────────────────────────────── */}
          {data.suggestions.length > 0 && (
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  توصيات نهلة الذكية 💡
                </h2>
              </div>
              <ul className="divide-y divide-slate-100">
                {data.suggestions.map((suggestion) => (
                  <li
                    key={suggestion.id}
                    className="flex items-center gap-3 px-5 py-3.5 hover:bg-slate-50 transition-colors"
                  >
                    {/* Priority dot */}
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${priorityDotColor(suggestion.priority)}`}
                    />
                    {/* Type icon */}
                    <SuggestionIcon type={suggestion.type} />
                    {/* Text */}
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-slate-900 truncate">
                        {suggestion.title}
                      </p>
                      <p className="text-xs text-slate-500 truncate mt-0.5">
                        {suggestion.desc}
                      </p>
                    </div>
                    {/* Apply button */}
                    <button
                      onClick={() => handleApplySuggestion(suggestion)}
                      className="btn-primary text-xs shrink-0 py-1.5 px-3"
                    >
                      تطبيق
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ── Two-column: Reorder Predictions + Churn Risk ───────────────── */}
          <div className="grid lg:grid-cols-2 gap-4">
            {/* Reorder Predictions */}
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  عملاء يُتوقع إعادة طلبهم قريباً 🐝
                </h2>
              </div>
              {data.reorder_predictions.length === 0 ? (
                <p className="text-xs text-slate-400 text-center py-8">لا توجد تنبؤات حالياً</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-slate-100 bg-slate-50">
                        <th className="text-start px-5 py-2.5 font-medium text-slate-500">
                          العميل
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                          المنتج
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                          التاريخ المتوقع
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">
                          الثقة
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data.reorder_predictions.map((pred, idx) => (
                        <tr key={idx} className="hover:bg-slate-50 transition-colors">
                          <td className="px-5 py-3">
                            <p className="font-medium text-slate-900 truncate max-w-[110px]">
                              {pred.customer_name}
                            </p>
                            <p
                              dir="ltr"
                              className="text-slate-400 mt-0.5 font-mono truncate max-w-[110px]"
                            >
                              {pred.phone}
                            </p>
                          </td>
                          <td className="px-3 py-3 text-slate-600 truncate max-w-[100px]">
                            {pred.product_name}
                          </td>
                          <td className="px-3 py-3 text-slate-600 whitespace-nowrap">
                            {formatArabicDate(pred.predicted_date)}
                          </td>
                          <td className="px-3 py-3 pe-5">
                            <Badge
                              label={`${pred.confidence}%`}
                              variant={confidenceVariant(pred.confidence)}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Churn Risk */}
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  عملاء في خطر المغادرة ⚠️
                </h2>
              </div>
              {data.churn_risk.length === 0 ? (
                <p className="text-xs text-slate-400 text-center py-8">لا يوجد عملاء في خطر حالياً</p>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {data.churn_risk.map((customer, idx) => (
                    <li key={idx} className="px-5 py-3.5 space-y-2 hover:bg-slate-50 transition-colors">
                      {/* Name + inactive days + target button */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <p className="text-xs font-semibold text-slate-900 truncate">
                            {customer.customer_name}
                          </p>
                          <Badge
                            label={`${customer.days_inactive} يوم`}
                            variant={inactiveDaysVariant(customer.days_inactive)}
                          />
                        </div>
                        <button className="btn-secondary text-xs py-1 px-2.5 shrink-0">
                          استهدف
                        </button>
                      </div>
                      {/* Last purchase */}
                      <p className="text-xs text-slate-400">
                        آخر شراء:{' '}
                        <span className="text-slate-600">
                          {formatArabicDate(customer.last_purchase)}
                        </span>
                      </p>
                      {/* Risk bar */}
                      <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                        <div
                          className={`h-1.5 rounded-full transition-all ${
                            customer.risk_score > 70 ? 'bg-red-500' : 'bg-amber-500'
                          }`}
                          style={{ width: `${Math.min(customer.risk_score, 100)}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {/* ── Customer Segments ──────────────────────────────────────────── */}
          {data.segments.length > 0 && (
            <div className="card p-5">
              <h2 className="text-sm font-semibold text-slate-900 mb-4">شرائح العملاء</h2>
              {(() => {
                const total = data.segments.reduce((sum, s) => sum + s.count, 0) || 1
                return (
                  <div className="space-y-3">
                    {data.segments.map((segment) => {
                      const pct = Math.round((segment.count / total) * 100)
                      return (
                        <div key={segment.key} className="flex items-center gap-3">
                          {/* Label */}
                          <span className="text-xs text-slate-600 w-28 shrink-0 text-end">
                            {segment.label}
                          </span>
                          {/* Bar track */}
                          <div className="flex-1 bg-slate-100 rounded-full h-4 overflow-hidden">
                            <div
                              className={`h-4 rounded-full flex items-center justify-end pe-2 transition-all ${segmentBarColor(
                                segment.color
                              )}`}
                              style={{ width: `${Math.max(pct, 4)}%` }}
                            >
                              {pct >= 12 && (
                                <span className="text-white text-xs font-semibold leading-none">
                                  {segment.count}
                                </span>
                              )}
                            </div>
                          </div>
                          {/* Count outside bar when bar is narrow */}
                          {pct < 12 && (
                            <span className="text-xs font-semibold text-slate-700 shrink-0 w-8">
                              {segment.count}
                            </span>
                          )}
                          {pct >= 12 && <span className="w-8 shrink-0" />}
                        </div>
                      )
                    })}
                  </div>
                )
              })()}
            </div>
          )}

          {/* ── VIP Customers ──────────────────────────────────────────────── */}
          {data.vip_customers.length > 0 && (
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  أفضل العملاء قيمةً 👑
                </h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-slate-100 bg-slate-50">
                      <th className="text-start px-5 py-2.5 font-medium text-slate-500 w-8">
                        #
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الاسم
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الإنفاق الكلي
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الطلبات
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">
                        الشريحة
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.vip_customers.map((vip, idx) => (
                      <tr key={idx} className="hover:bg-slate-50 transition-colors">
                        <td className="px-5 py-3 text-slate-400 font-medium">{idx + 1}</td>
                        <td className="px-3 py-3">
                          <div className="flex items-center gap-2">
                            {idx === 0 && (
                              <Crown className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                            )}
                            <span className="font-medium text-slate-900 truncate max-w-[140px]">
                              {vip.customer_name}
                            </span>
                          </div>
                        </td>
                        <td className="px-3 py-3 font-semibold text-slate-900 whitespace-nowrap">
                          {vip.total_spent.toLocaleString('ar-SA')} ر.س
                        </td>
                        <td className="px-3 py-3 text-slate-600">{vip.orders}</td>
                        <td className="px-3 py-3 pe-5">
                          <Badge
                            label={vip.segment}
                            variant={
                              vip.segment === 'VIP'
                                ? 'amber'
                                : vip.segment === 'نشط'
                                ? 'green'
                                : 'slate'
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
