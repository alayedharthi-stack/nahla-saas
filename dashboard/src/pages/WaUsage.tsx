/**
 * WaUsage.tsx
 * ────────────
 * Full WhatsApp conversation usage detail page.
 * Route: /wa-usage
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts'
import {
  MessageSquare, AlertTriangle, CheckCircle, TrendingUp, Calendar,
  RefreshCw, ShieldCheck, Megaphone, HeadphonesIcon,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { apiCall } from '../api/client'

// ── Types ─────────────────────────────────────────────────────────────────────
interface WaUsageDetail {
  service_conversations_used:    number
  marketing_conversations_used:  number
  conversations_used:            number
  conversations_limit:           number
  usage_pct:                     number
  exceeded:                      boolean
  near_limit:                    boolean
  hard_stop:                     boolean
  unlimited:                     boolean
  month:                         number
  year:                          number
  reset_date:                    string
  daily_breakdown:               DailyRow[]
}

interface DailyRow {
  day:       string   // "YYYY-MM-DD"
  service:   number
  marketing: number
  total:     number
}

const MONTH_NAMES: Record<number, string> = {
  1: 'يناير', 2: 'فبراير', 3: 'مارس',    4: 'أبريل',
  5: 'مايو',  6: 'يونيو',  7: 'يوليو',   8: 'أغسطس',
  9: 'سبتمبر', 10: 'أكتوبر', 11: 'نوفمبر', 12: 'ديسمبر',
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function StatusBadge({ exceeded, near_limit, hard_stop }: {
  exceeded: boolean; near_limit: boolean; hard_stop: boolean
}) {
  if (hard_stop) return (
    <span className="inline-flex items-center gap-1 text-xs font-bold text-red-700 bg-red-100 px-2.5 py-1 rounded-full">
      <AlertTriangle className="w-3.5 h-3.5" /> إيقاف كامل
    </span>
  )
  if (exceeded) return (
    <span className="inline-flex items-center gap-1 text-xs font-bold text-orange-700 bg-orange-100 px-2.5 py-1 rounded-full">
      <AlertTriangle className="w-3.5 h-3.5" /> الحد مُستنفَد
    </span>
  )
  if (near_limit) return (
    <span className="inline-flex items-center gap-1 text-xs font-bold text-amber-700 bg-amber-100 px-2.5 py-1 rounded-full">
      <AlertTriangle className="w-3.5 h-3.5" /> 80% مُستخدَم
    </span>
  )
  return (
    <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-700 bg-emerald-100 px-2.5 py-1 rounded-full">
      <CheckCircle className="w-3.5 h-3.5" /> ضمن الحد
    </span>
  )
}

function ProgressBar({ pct, exceeded, near_limit }: {
  pct: number; exceeded: boolean; near_limit: boolean
}) {
  const barColor = exceeded ? 'bg-red-500' : near_limit ? 'bg-amber-400' : 'bg-emerald-500'
  const width    = Math.min(pct, 100)
  return (
    <div className="relative w-full h-4 bg-slate-100 rounded-full overflow-hidden">
      {/* 80% marker */}
      <div className="absolute top-0 h-full w-px bg-slate-300 z-10" style={{ left: '80%' }} />
      <div
        className={`h-full rounded-full transition-all duration-700 ${barColor}`}
        style={{ width: `${width}%` }}
      />
    </div>
  )
}

// ── Stat mini-card ────────────────────────────────────────────────────────────
function MiniStat({
  label, value, icon: Icon, color, bg,
}: {
  label: string; value: string | number; icon: React.ElementType; color: string; bg: string
}) {
  return (
    <div className={`rounded-xl border border-slate-200 p-4 flex items-center gap-3 ${bg}`}>
      <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${bg}`}>
        <Icon className={`w-5 h-5 ${color}`} />
      </div>
      <div>
        <p className="text-xs text-slate-500 font-medium">{label}</p>
        <p className="text-xl font-black text-slate-800">{value}</p>
      </div>
    </div>
  )
}

// ── Blocking policy info ──────────────────────────────────────────────────────
function BlockingPolicyCard() {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex items-center gap-2 mb-3">
        <ShieldCheck className="w-4 h-4 text-slate-600" />
        <h3 className="text-sm font-semibold text-slate-700">سياسة الإيقاف عند بلوغ الحد</h3>
      </div>
      <div className="space-y-2 text-xs text-slate-600">
        <div className="flex items-start gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-400 mt-1 shrink-0" />
          <span><strong>أقل من 100%</strong> — كل الرسائل تعمل بشكل طبيعي.</span>
        </div>
        <div className="flex items-start gap-2">
          <span className="w-2 h-2 rounded-full bg-orange-400 mt-1 shrink-0" />
          <span><strong>100% مُستنفَد</strong> — إيقاف الحملات التسويقية فقط. ردود خدمة العملاء لا تزال تعمل.</span>
        </div>
        <div className="flex items-start gap-2">
          <span className="w-2 h-2 rounded-full bg-red-500 mt-1 shrink-0" />
          <span><strong>تجاوز 110%</strong> — إيقاف كامل لجميع الرسائل. ارقِّ باقتك لاستئناف الخدمة.</span>
        </div>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function WaUsage() {
  const [data, setData]       = useState<WaUsageDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(false)

  const load = () => {
    setLoading(true)
    apiCall<WaUsageDetail>('/whatsapp/usage?breakdown=true')
      .then(d => { setData(d); setError(false) })
      .catch(() => setError(true))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  const monthLabel = data ? `${MONTH_NAMES[data.month]} ${data.year}` : '…'

  return (
    <div className="space-y-6">
      <PageHeader
        title="استخدام واتساب"
        subtitle={`الفترة: ${monthLabel}`}
        action={
          <button
            onClick={load}
            className="flex items-center gap-2 text-sm text-slate-500 hover:text-slate-700 border border-slate-200 rounded-xl px-3 py-2 transition-colors"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            تحديث
          </button>
        }
      />

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-600">
          تعذّر تحميل البيانات. تأكد من اتصالك بالإنترنت وحاول مرة أخرى.
        </div>
      )}

      {loading && !data && (
        <div className="space-y-4 animate-pulse">
          <div className="h-24 bg-slate-100 rounded-2xl" />
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {[1,2,3,4].map(i => <div key={i} className="h-20 bg-slate-100 rounded-xl" />)}
          </div>
          <div className="h-56 bg-slate-100 rounded-2xl" />
        </div>
      )}

      {data && (
        <>
          {/* ── Main usage card ─────────────────────────────────────────── */}
          <div className={`rounded-2xl border p-5 ${
            data.hard_stop  ? 'bg-red-50 border-red-200'
            : data.exceeded ? 'bg-orange-50 border-orange-200'
            : data.near_limit ? 'bg-amber-50 border-amber-200'
            : 'bg-white border-slate-200'
          }`}>
            <div className="flex items-start justify-between gap-3 flex-wrap mb-4">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <MessageSquare className="w-5 h-5 text-slate-600" />
                  <h2 className="text-base font-bold text-slate-800">
                    استخدام المحادثات — {monthLabel}
                  </h2>
                </div>
                <p className="text-xs text-slate-500">
                  يُعاد التصفير في: {data.reset_date}
                </p>
              </div>
              <StatusBadge exceeded={data.exceeded} near_limit={data.near_limit} hard_stop={data.hard_stop} />
            </div>

            {/* Progress bar */}
            <ProgressBar pct={data.usage_pct} exceeded={data.exceeded} near_limit={data.near_limit} />

            <div className="flex items-center justify-between mt-2 text-sm">
              <span className="font-semibold text-slate-700">
                {data.conversations_used.toLocaleString('ar-SA')}
                <span className="text-slate-400 font-normal">
                  {' / '}{data.conversations_limit.toLocaleString('ar-SA')} محادثة
                </span>
              </span>
              <span className={`font-bold text-sm ${
                data.hard_stop  ? 'text-red-600'
                : data.exceeded ? 'text-orange-600'
                : data.near_limit ? 'text-amber-600'
                : 'text-emerald-600'
              }`}>
                {data.usage_pct}%
              </span>
            </div>

            {/* 80% marker label */}
            <p className="text-xs text-slate-400 mt-1">
              الخط الأحمر: 80% ({Math.round(data.conversations_limit * 0.8).toLocaleString('ar-SA')} محادثة)
            </p>

            {(data.exceeded || data.hard_stop) && (
              <Link
                to="/billing"
                className="mt-4 inline-flex items-center gap-2 bg-brand-600 text-white text-sm font-bold px-4 py-2 rounded-xl hover:bg-brand-500 transition-colors"
              >
                <TrendingUp className="w-4 h-4" />
                ارقِّ باقتك الآن
              </Link>
            )}
          </div>

          {/* ── 4 mini-stats ────────────────────────────────────────────── */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <MiniStat
              label="خدمة العملاء"
              value={data.service_conversations_used.toLocaleString('ar-SA')}
              icon={HeadphonesIcon}
              color="text-blue-600"
              bg="bg-blue-50"
            />
            <MiniStat
              label="تسويقية"
              value={data.marketing_conversations_used.toLocaleString('ar-SA')}
              icon={Megaphone}
              color="text-purple-600"
              bg="bg-purple-50"
            />
            <MiniStat
              label="الإجمالي"
              value={data.conversations_used.toLocaleString('ar-SA')}
              icon={MessageSquare}
              color="text-slate-600"
              bg="bg-slate-50"
            />
            <MiniStat
              label="المتبقي"
              value={Math.max(0, data.conversations_limit - data.conversations_used).toLocaleString('ar-SA')}
              icon={Calendar}
              color="text-emerald-600"
              bg="bg-emerald-50"
            />
          </div>

          {/* ── Daily breakdown chart ────────────────────────────────────── */}
          <div className="rounded-2xl border border-slate-200 bg-white p-5">
            <h3 className="text-sm font-semibold text-slate-700 mb-4">
              التوزيع اليومي — {monthLabel}
            </h3>

            {data.daily_breakdown.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-slate-400">
                <MessageSquare className="w-10 h-10 mb-2 opacity-30" />
                <p className="text-sm">لا توجد محادثات مسجّلة بعد هذا الشهر.</p>
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <BarChart
                  data={data.daily_breakdown}
                  margin={{ top: 4, right: 8, left: -10, bottom: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                  <XAxis
                    dataKey="day"
                    tickFormatter={v => v.slice(8)}   // show day number only
                    tick={{ fontSize: 11, fill: '#94a3b8' }}
                  />
                  <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} allowDecimals={false} />
                  <Tooltip
                    formatter={(val: number, name: string) => [
                      val,
                      name === 'service' ? 'خدمة العملاء' : 'تسويقية',
                    ]}
                    labelFormatter={l => `يوم ${l}`}
                    contentStyle={{ fontFamily: 'inherit', fontSize: 12 }}
                  />
                  <Legend
                    formatter={v => v === 'service' ? 'خدمة العملاء' : 'تسويقية'}
                    wrapperStyle={{ fontSize: 12 }}
                  />
                  <Bar dataKey="service"   fill="#3b82f6" radius={[4,4,0,0]} />
                  <Bar dataKey="marketing" fill="#a855f7" radius={[4,4,0,0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* ── Blocking policy info ─────────────────────────────────────── */}
          <BlockingPolicyCard />

          {/* ── Upgrade nudge ────────────────────────────────────────────── */}
          <div className="rounded-2xl border border-brand-100 bg-brand-50 p-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
            <div>
              <p className="text-sm font-bold text-brand-800 mb-0.5">هل تحتاج المزيد من المحادثات؟</p>
              <p className="text-xs text-brand-600">
                الباقات المتوفرة: Starter (1,000) · Growth (5,000) · Scale (15,000) — تُجدَّد شهرياً
              </p>
            </div>
            <Link
              to="/billing"
              className="shrink-0 bg-brand-600 text-white text-sm font-bold px-4 py-2 rounded-xl hover:bg-brand-500 transition-colors"
            >
              مقارنة الباقات
            </Link>
          </div>
        </>
      )}
    </div>
  )
}
