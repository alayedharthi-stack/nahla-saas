import { useEffect, useState } from 'react'
import { API_BASE } from '../api/client'
import { getToken } from '../auth'
import {
  Users, TrendingUp, DollarSign, Activity,
  Package, Clock, CheckCircle, AlertCircle,
} from 'lucide-react'

interface PlatformStats {
  merchants:     { total: number; active: number; trial: number }
  subscriptions: { active: number; trial: number; total: number; by_plan: Record<string, { name_ar: string; count: number; price: number }> }
  revenue:       { total_sar: number; today_sar: number; mrr_sar: number }
  recent_payments:  any[]
  recent_merchants: any[]
}

function KPICard({
  label, value, sub, icon: Icon, color, prefix = '', suffix = '',
}: {
  label: string; value: string | number; sub?: string
  icon: React.ElementType; color: string; prefix?: string; suffix?: string
}) {
  return (
    <div className="bg-white rounded-2xl border border-slate-100 p-5 flex flex-col gap-3 shadow-sm hover:shadow-md transition-shadow">
      <div className="flex items-center justify-between">
        <span className="text-slate-500 text-sm font-medium">{label}</span>
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${color}`}>
          <Icon className="w-4 h-4 text-white" />
        </div>
      </div>
      <div>
        <div className="text-2xl font-black text-slate-800">
          {prefix}{typeof value === 'number' ? value.toLocaleString('ar-SA') : value}{suffix}
        </div>
        {sub && <div className="text-xs text-slate-400 mt-0.5">{sub}</div>}
      </div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    active:        { label: 'نشط',       cls: 'bg-green-100 text-green-700' },
    trialing:      { label: 'تجربة',     cls: 'bg-blue-100 text-blue-700'  },
    canceled:      { label: 'ملغى',      cls: 'bg-red-100 text-red-700'    },
    none:          { label: 'بلا باقة',  cls: 'bg-slate-100 text-slate-500'},
    connected:     { label: 'مربوط',     cls: 'bg-green-100 text-green-700'},
    not_connected: { label: 'غير مربوط', cls: 'bg-slate-100 text-slate-400'},
    paid:          { label: 'مدفوع',     cls: 'bg-green-100 text-green-700'},
    pending:       { label: 'معلق',      cls: 'bg-yellow-100 text-yellow-700'},
    failed:        { label: 'فاشل',      cls: 'bg-red-100 text-red-700'    },
  }
  const s = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

export default function AdminDashboard() {
  const [stats, setStats]     = useState<PlatformStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState('')

  useEffect(() => {
    const token = getToken()
    fetch(`${API_BASE}/admin/stats`, { headers: { Authorization: `Bearer ${token}` } })
      .then(async r => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `خطأ ${r.status}`)
        return r.json()
      })
      .then(setStats)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'خطأ غير معروف'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-amber-500" />
    </div>
  )
  if (error) return <div className="p-6 text-red-500 text-center">{error}</div>
  if (!stats) return null

  const byPlan = stats.subscriptions.by_plan ?? {}

  const kpis = [
    { label: 'إجمالي التجار',    value: stats.merchants.total,               icon: Users,       color: 'bg-blue-500',   sub: `${stats.merchants.active} نشط` },
    { label: 'في التجربة',       value: stats.merchants.trial,               icon: Clock,       color: 'bg-sky-500',    sub: 'تجربة مجانية' },
    { label: 'Starter',          value: byPlan.starter?.count ?? 0,          icon: Package,     color: 'bg-slate-500',  sub: `${byPlan.starter?.price ?? 899} ر.س/شهر` },
    { label: 'Growth',           value: byPlan.growth?.count ?? 0,           icon: TrendingUp,  color: 'bg-violet-500', sub: `${byPlan.growth?.price ?? 1699} ر.س/شهر` },
    { label: 'Scale',            value: byPlan.scale?.count ?? 0,            icon: Activity,    color: 'bg-amber-500',  sub: `${byPlan.scale?.price ?? 2999} ر.س/شهر` },
    { label: 'MRR',              value: stats.revenue.mrr_sar.toFixed(0),    icon: TrendingUp,  color: 'bg-emerald-500',sub: 'الإيرادات الشهرية', suffix: ' ر.س' },
    { label: 'إيرادات اليوم',   value: stats.revenue.today_sar.toFixed(0),  icon: DollarSign,  color: 'bg-green-500',  sub: 'منذ منتصف الليل', suffix: ' ر.س' },
    { label: 'إجمالي الإيرادات', value: stats.revenue.total_sar.toFixed(0), icon: CheckCircle, color: 'bg-teal-500',   sub: 'كل الأوقات',       suffix: ' ر.س' },
  ]

  return (
    <div className="p-6 space-y-7" dir="rtl">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="w-11 h-11 rounded-2xl bg-amber-500 flex items-center justify-center text-xl shadow-lg shadow-amber-500/30">👑</div>
        <div>
          <h1 className="text-xl font-black text-slate-800">نظرة عامة على المنصة</h1>
          <p className="text-slate-400 text-sm">نحلة AI · آخر تحديث الآن</p>
        </div>
      </div>

      {/* KPI Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {kpis.map(k => (
          <KPICard key={k.label} {...k} />
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent Merchants */}
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm">
          <div className="px-5 py-4 border-b border-slate-50 flex items-center justify-between">
            <h2 className="font-bold text-slate-700 text-sm flex items-center gap-2">
              <Users className="w-4 h-4 text-slate-400" /> آخر التجار
            </h2>
            <a href="/admin/merchants" className="text-xs text-amber-600 hover:text-amber-700 font-medium">عرض الكل ←</a>
          </div>
          <div className="divide-y divide-slate-50">
            {stats.recent_merchants.length === 0 ? (
              <p className="text-center py-10 text-slate-400 text-sm">لا يوجد تجار بعد</p>
            ) : stats.recent_merchants.map((m: any) => (
              <div key={m.id} className="px-5 py-3 flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-slate-700 truncate">{m.store_name || m.email}</p>
                  <p className="text-xs text-slate-400 truncate">{m.email}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <StatusBadge status={m.sub_status ?? 'none'} />
                  <span className="text-xs text-slate-400">{m.plan}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Recent Payments */}
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm">
          <div className="px-5 py-4 border-b border-slate-50 flex items-center justify-between">
            <h2 className="font-bold text-slate-700 text-sm flex items-center gap-2">
              <DollarSign className="w-4 h-4 text-slate-400" /> آخر المدفوعات
            </h2>
            <a href="/admin/revenue" className="text-xs text-amber-600 hover:text-amber-700 font-medium">عرض الكل ←</a>
          </div>
          <div className="divide-y divide-slate-50">
            {stats.recent_payments.length === 0 ? (
              <div className="text-center py-10">
                <AlertCircle className="w-8 h-8 text-slate-200 mx-auto mb-2" />
                <p className="text-slate-400 text-sm">لا توجد مدفوعات بعد</p>
              </div>
            ) : stats.recent_payments.map((p: any) => (
              <div key={p.id} className="px-5 py-3 flex items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold text-slate-700">{Number(p.amount).toLocaleString('ar-SA')} {p.currency}</p>
                  <p className="text-xs text-slate-400">{p.gateway} · {p.created_at ? new Date(p.created_at).toLocaleDateString('ar-SA') : '—'}</p>
                </div>
                <StatusBadge status={p.status} />
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Platform summary bar */}
      <div className="bg-gradient-to-r from-slate-800 to-slate-900 rounded-2xl p-5 text-white flex flex-wrap gap-6 items-center justify-between">
        <div>
          <p className="text-slate-400 text-xs mb-1">منصة نحلة AI</p>
          <p className="font-black text-lg">nahlah.ai</p>
        </div>
        {[
          { label: 'تجار', val: stats.merchants.total },
          { label: 'اشتراك نشط', val: stats.subscriptions.active },
          { label: 'MRR', val: `${stats.revenue.mrr_sar.toLocaleString('ar-SA')} ر.س` },
          { label: 'الإجمالي', val: `${stats.revenue.total_sar.toLocaleString('ar-SA')} ر.س` },
        ].map(item => (
          <div key={item.label} className="text-center">
            <p className="text-xl font-black">{item.val}</p>
            <p className="text-slate-400 text-xs">{item.label}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
