import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import {
  DollarSign, MessageSquare, ShoppingCart, TrendingUp, Bot, User, ExternalLink,
  Sparkles, Clock, AlertTriangle, TrendingUp as ArrowUp,
} from 'lucide-react'
import StatCard from '../components/ui/StatCard'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { apiCall } from '../api/client'
import { whatsappConnectApi } from '../api/whatsappConnect'

// Placeholder chart data — replaced with real data when store is synced
const PLACEHOLDER_CHART = [
  { day: 'الاثنين', revenue: 0 },
  { day: 'الثلاثاء', revenue: 0 },
  { day: 'الأربعاء', revenue: 0 },
  { day: 'الخميس', revenue: 0 },
  { day: 'الجمعة', revenue: 0 },
  { day: 'السبت', revenue: 0 },
  { day: 'الأحد', revenue: 0 },
]

const statusVariant = (s: string) =>
  s === 'paid'    ? 'green'  :
  s === 'pending' ? 'amber'  :
  s === 'failed'  ? 'red'    : 'slate'

interface OverviewStats {
  conversations_today: number
  orders_today: number
  revenue_today: number
  ai_rate: number
  ai_revenue: number
  ai_orders: number
  recent_conversations: any[]
  recent_orders: any[]
  revenue_chart: { day: string; revenue: number }[]
}

interface WaUsage {
  conversations_used:   number
  conversations_limit:  number
  usage_pct:            number
  exceeded:             boolean
  near_limit:           boolean
  unlimited:            boolean
}

export default function Overview() {
  const { t } = useLanguage()
  const ov = t(tr => tr.overview)
  const [stats, setStats]     = useState<OverviewStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [waUsage, setWaUsage] = useState<WaUsage | null>(null)

  useEffect(() => {
    // Load WhatsApp usage stats
    apiCall<WaUsage>('/whatsapp/usage').then(setWaUsage).catch(() => null)

    // Try to load real stats; gracefully ignore errors (no store connected yet)
    Promise.all([
      apiCall<any>('/store-sync/status').catch(() => null),
      apiCall<any>('/store-sync/knowledge').catch(() => null),
    ]).then(([syncStatus]) => {
      if (syncStatus) {
        setStats({
          conversations_today: syncStatus.conversations_today ?? 0,
          orders_today:        syncStatus.orders_today        ?? 0,
          revenue_today:       syncStatus.revenue_today       ?? 0,
          ai_rate:             syncStatus.ai_rate             ?? 0,
          ai_revenue:          syncStatus.ai_revenue          ?? 0,
          ai_orders:           syncStatus.ai_orders           ?? 0,
          recent_conversations: syncStatus.recent_conversations ?? [],
          recent_orders:        syncStatus.recent_orders        ?? [],
          revenue_chart:        syncStatus.revenue_chart        ?? PLACEHOLDER_CHART,
        })
      }
    }).finally(() => setLoading(false))
  }, [])

  const revenueData         = stats?.revenue_chart        ?? PLACEHOLDER_CHART
  const recentConversations = stats?.recent_conversations ?? []
  const recentOrders        = stats?.recent_orders        ?? []
  const hasRealData         = (stats?.orders_today ?? 0) > 0 || recentOrders.length > 0

  const statusLabel = (s: string) => {
    if (s === 'paid')    return ov.statusPaid
    if (s === 'pending') return ov.statusPending
    if (s === 'failed')  return ov.statusFailed
    return ov.statusCancelled
  }

  return (
    <div className="space-y-6">
      {/* Nahla Impact Banner — "موظف مبيعات يعمل 24/7" */}
      <div className="rounded-2xl overflow-hidden bg-gradient-to-l from-brand-600 to-amber-500 p-px">
        <div className="bg-gradient-to-l from-brand-600/10 to-amber-500/10 rounded-2xl px-5 py-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-brand-500/20 flex items-center justify-center shrink-0">
              <Sparkles className="w-5 h-5 text-brand-600" />
            </div>
            <div>
              <p className="text-xs text-white/80 font-medium">{ov.aiSalesLabel}</p>
              <p className="text-2xl font-black text-white leading-none mt-0.5">
                {(stats?.ai_revenue ?? 0).toLocaleString('ar-SA')} <span className="text-sm font-bold text-white/90">ر.س</span>
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-center hidden sm:block">
              <p className="text-xs text-white/80 font-medium">{ov.aiOrdersLabel}</p>
              <p className="text-lg font-bold text-white">{stats?.ai_orders ?? 0}</p>
            </div>
            <div className="h-8 w-px bg-slate-200 hidden sm:block" />
            <div className="flex items-center gap-1.5 text-xs text-slate-500 bg-white rounded-xl px-3 py-2 border border-slate-200">
              <Clock className="w-3.5 h-3.5 text-brand-500" />
              <span>{ov.salesBot.replace('24/7', '')} <strong className="text-slate-700">24/7</strong></span>
            </div>
          </div>
        </div>
      </div>

      {/* WhatsApp Conversation Usage Widget */}
      {waUsage && (
        <div className={`rounded-2xl border p-4 ${
          waUsage.exceeded  ? 'bg-red-50    border-red-200'
          : waUsage.near_limit ? 'bg-amber-50  border-amber-200'
          : 'bg-white border-slate-200'
        }`}>
          <div className="flex items-center justify-between gap-3 flex-wrap">
            {/* Left: label + bar */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-2">
                <MessageSquare className={`w-4 h-4 ${
                  waUsage.exceeded ? 'text-red-500' : waUsage.near_limit ? 'text-amber-500' : 'text-emerald-500'
                }`} />
                <span className="text-sm font-semibold text-slate-700">
                  استخدام واتساب هذا الشهر
                </span>
                {waUsage.exceeded && (
                  <span className="flex items-center gap-1 text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> تجاوزت الحد
                  </span>
                )}
                {waUsage.near_limit && !waUsage.exceeded && (
                  <span className="flex items-center gap-1 text-xs font-bold text-amber-600 bg-amber-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> 80% مستخدم
                  </span>
                )}
              </div>

              {/* Progress bar */}
              <div className="w-full h-2.5 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${
                    waUsage.exceeded  ? 'bg-red-500'
                    : waUsage.near_limit ? 'bg-amber-400'
                    : 'bg-emerald-500'
                  }`}
                  style={{ width: `${Math.min(waUsage.unlimited ? 0 : waUsage.usage_pct, 100)}%` }}
                />
              </div>

              <div className="flex items-center justify-between mt-1.5">
                <span className="text-xs text-slate-500">
                  {waUsage.unlimited
                    ? `${waUsage.conversations_used.toLocaleString('ar-SA')} محادثة (غير محدودة)`
                    : `${waUsage.conversations_used.toLocaleString('ar-SA')} / ${waUsage.conversations_limit.toLocaleString('ar-SA')} محادثة`
                  }
                </span>
                {!waUsage.unlimited && (
                  <span className={`text-xs font-bold ${
                    waUsage.exceeded ? 'text-red-600' : waUsage.near_limit ? 'text-amber-600' : 'text-slate-400'
                  }`}>
                    {waUsage.usage_pct}%
                  </span>
                )}
              </div>
            </div>

            {/* Right: upgrade CTA if near/over limit */}
            {(waUsage.exceeded || waUsage.near_limit) && (
              <Link
                to="/billing"
                className={`flex items-center gap-1.5 text-xs font-bold px-3 py-2 rounded-xl shrink-0 transition-all ${
                  waUsage.exceeded
                    ? 'bg-red-600 text-white hover:bg-red-500'
                    : 'bg-amber-500 text-white hover:bg-amber-400'
                }`}
              >
                <ArrowUp className="w-3.5 h-3.5" />
                ارقِّ باقتك
              </Link>
            )}
          </div>

          {waUsage.exceeded && (
            <p className="text-xs text-red-600 mt-2 font-medium">
              ⛔ تم إيقاف الردود التلقائية — ارقِّ باقتك لاستئنافها
            </p>
          )}
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label={ov.kpiRevenue}
          value={loading ? '—' : `${(stats?.revenue_today ?? 0).toLocaleString('ar-SA')} ر.س`}
          icon={DollarSign}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label={ov.kpiConversations}
          value={loading ? '—' : String(stats?.conversations_today ?? 0)}
          icon={MessageSquare}
          iconColor="text-blue-600"
          iconBg="bg-blue-50"
        />
        <StatCard
          label={ov.kpiOrders}
          value={loading ? '—' : String(stats?.orders_today ?? 0)}
          icon={ShoppingCart}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label={ov.kpiAiRate}
          value={loading ? '—' : `${(stats?.ai_rate ?? 0).toFixed(1)}%`}
          icon={TrendingUp}
          iconColor="text-purple-600"
          iconBg="bg-purple-50"
        />
      </div>

      {/* Revenue Chart */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">الإيرادات — آخر 7 أيام</h2>
            <p className="text-xs text-slate-400 mt-0.5">المجموع: 45,300 ر.س</p>
          </div>
          <select className="text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white text-slate-600 focus:outline-none">
            <option>آخر 7 أيام</option>
            <option>آخر 30 يوم</option>
            <option>هذا الشهر</option>
          </select>
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={revenueData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="colorRevenue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#f59e0b" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="day" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <Tooltip
              contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8, boxShadow: '0 4px 6px -1px rgb(0 0 0 / .1)' }}
              formatter={(v: number) => [`${v.toLocaleString('ar-SA')} ر.س`, 'الإيرادات']}
            />
            <Area type="monotone" dataKey="revenue" stroke="#f59e0b" strokeWidth={2} fill="url(#colorRevenue)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Two-column: Conversations + Orders */}
      <div className="grid lg:grid-cols-2 gap-4">
        {/* Recent Conversations */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">{ov.recentConvTitle}</h2>
            <a href="/conversations" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          {recentConversations.length === 0 ? (
            <div className="py-10 text-center text-xs text-slate-400">
              {loading ? 'جاري التحميل...' : 'لا توجد محادثات بعد — ابدأ بتفعيل WhatsApp'}
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {recentConversations.map((c: any) => (
                <li key={c.id} className="flex items-start gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                  <div className="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center shrink-0 mt-0.5">
                    <span className="text-slate-600 text-xs font-semibold">
                      {String(c.customer ?? '?').split(' ').map((n: string) => n[0]).join('').slice(0, 2)}
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-xs font-medium text-slate-900 truncate">{c.customer}</p>
                      {c.isAI
                        ? <Bot  className="w-3 h-3 text-brand-500 shrink-0" />
                        : <User className="w-3 h-3 text-slate-400 shrink-0" />}
                    </div>
                    <p className="text-xs text-slate-500 truncate mt-0.5">{c.lastMsg}</p>
                  </div>
                  <div className="flex flex-col items-end gap-1 shrink-0">
                    <span className="text-xs text-slate-400">{c.time}</span>
                    <Badge
                      label={c.status === 'active' ? 'نشطة' : c.status === 'human' ? 'بشري' : 'مغلقة'}
                      variant={c.status === 'active' ? 'green' : c.status === 'human' ? 'amber' : 'slate'}
                      dot
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Recent Orders */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">{ov.recentOrdTitle}</h2>
            <a href="/orders" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          {recentOrders.length === 0 ? (
            <div className="py-10 text-center text-xs text-slate-400">
              {loading ? 'جاري التحميل...' : 'لا توجد طلبات بعد — قم بربط متجرك'}
            </div>
          ) : (
          <ul className="divide-y divide-slate-100">
            {recentOrders.map((o: any) => (
              <li key={o.id} className="flex items-center gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono font-medium text-slate-700">{o.id}</span>
                    <Badge label={o.source === 'AI' ? ov.aiBadge : ov.sourceManual} variant={o.source === 'AI' ? 'purple' : 'slate'} />
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{o.customer}</p>
                </div>
                <div className="text-end shrink-0">
                  <p className="text-xs font-semibold text-slate-900">{o.amount}</p>
                  <div className="mt-0.5">
                    <Badge label={statusLabel(o.status)} variant={statusVariant(o.status) as 'green' | 'amber' | 'red' | 'slate'} />
                  </div>
                </div>
              </li>
            ))}
          </ul>
          )}
        </div>
      </div>
    </div>
  )
}
