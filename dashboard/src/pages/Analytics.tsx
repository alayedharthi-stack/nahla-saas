import { useEffect, useMemo, useState } from 'react'
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts'
import { DollarSign, TrendingUp, ShoppingCart, MessageSquare } from 'lucide-react'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import SmartOfferPerformance from '../components/SmartOfferPerformance'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import { featureRealityApi, type AnalyticsDashboard } from '../api/featureReality'

const emptyData: AnalyticsDashboard = {
  summary: {
    current_month_revenue_sar: 0,
    conversion_rate_pct: 0,
    current_month_orders: 0,
    current_month_conversations: 0,
    today_revenue_sar: 0,
    pending_orders: 0,
    completed_today: 0,
  },
  revenue_trend: [],
  conversion_trend: [],
  source_breakdown: [],
  top_products: [],
}

export default function Analytics() {
  const [data, setData] = useState<AnalyticsDashboard>(emptyData)
  const [loading, setLoading] = useState(true)
  const { t, lang } = useLanguage()
  const ap = t(tr => tr.analyticsPage)
  void UI_ONLY_GUARD
  const locale = lang === 'ar' ? 'ar-SA' : 'en-US'

  const tableHeaders = useMemo(() => [
    ap.table.rank,
    ap.table.product,
    ap.table.orders,
    ap.table.revenue,
    ap.table.trend,
  ], [ap])

  useEffect(() => {
    featureRealityApi.analytics()
      .then(setData)
      .catch(() => setData(emptyData))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="space-y-5">
      <PageHeader title={ap.title} subtitle={ap.subtitle} />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label={ap.cards.revenue}       value={`${data.summary.current_month_revenue_sar.toLocaleString(locale)} ${ap.currency}`} icon={DollarSign}    iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label={ap.cards.conversionRate} value={`${data.summary.conversion_rate_pct}%`} icon={TrendingUp}    iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label={ap.cards.orders}        value={String(data.summary.current_month_orders)} icon={ShoppingCart}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label={ap.cards.conversations} value={String(data.summary.current_month_conversations)} icon={MessageSquare} iconColor="text-purple-600"  iconBg="bg-purple-50" />
      </div>

      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">{ap.revenueTrend}</h2>
            <p className="text-xs text-slate-400 mt-0.5">{ap.last6Months}</p>
          </div>
          <div className="text-end">
            <p className="text-sm font-bold text-slate-700">{data.summary.today_revenue_sar.toLocaleString(locale)} {ap.currency}</p>
            <p className="text-xs text-slate-400">{loading ? ap.loading : ap.todayRevenue}</p>
          </div>
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={data.revenue_trend} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="colorRev" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#f59e0b" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <Tooltip
              formatter={(v: number) => [`${v.toLocaleString(locale)} ${ap.currency}`, ap.revenueLabel]}
              contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }}
            />
            <Area type="monotone" dataKey="revenue" stroke="#f59e0b" strokeWidth={2} fill="url(#colorRev)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">{ap.convVsConv}</h2>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={data.conversion_trend}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }} />
              <Bar dataKey="conversations" name={ap.conversationsBar} fill="#e2e8f0" radius={[4, 4, 0, 0]} />
              <Bar dataKey="conversions"   name={ap.conversionsBar}   fill="#f59e0b" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">{ap.orderSources}</h2>
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie
                data={data.source_breakdown}
                dataKey="count"
                nameKey="source"
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={80}
                paddingAngle={3}
              >
                {data.source_breakdown.map((_, i) => (
                  <Cell key={i} fill={['#f59e0b', '#3b82f6', '#10b981', '#8b5cf6'][i % 4]} />
                ))}
              </Pie>
              <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }} />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card p-0 overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">{ap.topProducts}</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/60">
                {tableHeaders.map((h, i) => (
                  <th key={i} className="px-5 py-3 text-xs font-semibold text-slate-500 text-start">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.top_products.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-5 py-8 text-center text-xs text-slate-400">{ap.noProductData}</td>
                </tr>
              ) : data.top_products.map((p, i) => (
                <tr key={p.name} className="border-b border-slate-50 hover:bg-slate-50/50">
                  <td className="px-5 py-3.5 text-xs text-slate-400">{i + 1}</td>
                  <td className="px-5 py-3.5 text-xs font-medium text-slate-800">{p.name}</td>
                  <td className="px-5 py-3.5 text-xs text-slate-600">{p.orders}</td>
                  <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">{p.revenue.toLocaleString(locale)} {ap.currency}</td>
                  <td className="px-5 py-3.5 text-xs text-slate-500">{p.trend ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <SmartOfferPerformance />
    </div>
  )
}
