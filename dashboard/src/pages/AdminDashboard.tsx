import { useEffect, useState } from 'react'
import { apiCall, API_BASE } from '../api/client'
import { getToken } from '../auth'

interface PlatformStats {
  merchants:     { total: number; active: number }
  tenants:       { total: number }
  subscriptions: { active: number; total: number }
  revenue:       { total_sar: number }
  recent_payments:  any[]
  recent_merchants: any[]
}

export default function AdminDashboard() {
  const [stats, setStats]   = useState<PlatformStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError]   = useState('')

  useEffect(() => {
    const token = getToken()
    fetch(`${API_BASE}/admin/stats`, {
      headers: { Authorization: `Bearer ${token}` }
    })
      .then(r => r.json())
      .then((data: PlatformStats) => setStats(data))
      .catch(() => setError('تعذّر تحميل إحصائيات المنصة'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-brand-500" />
    </div>
  )

  if (error) return (
    <div className="text-center py-20 text-red-500">{error}</div>
  )

  const cards = [
    { label: 'إجمالي التجار',       value: stats?.merchants.total  ?? 0, icon: '🏪', color: 'bg-blue-50 border-blue-200' },
    { label: 'تجار نشطون',          value: stats?.merchants.active ?? 0, icon: '✅', color: 'bg-green-50 border-green-200' },
    { label: 'اشتراكات نشطة',       value: stats?.subscriptions.active ?? 0, icon: '💳', color: 'bg-purple-50 border-purple-200' },
    { label: 'إجمالي الإيرادات (ر.س)', value: (stats?.revenue.total_sar ?? 0).toLocaleString('ar-SA'), icon: '💰', color: 'bg-yellow-50 border-yellow-200' },
  ]

  return (
    <div className="p-6 space-y-6" dir="rtl">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-brand-500 flex items-center justify-center text-2xl">👑</div>
        <div>
          <h1 className="text-2xl font-bold text-slate-800">لوحة تحكم المالك</h1>
          <p className="text-slate-500 text-sm">نحلة AI · تركي بن عايد الحارثي</p>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {cards.map(c => (
          <div key={c.label} className={`rounded-2xl border p-4 ${c.color}`}>
            <div className="text-3xl mb-2">{c.icon}</div>
            <div className="text-2xl font-bold text-slate-800">{c.value}</div>
            <div className="text-xs text-slate-500 mt-1">{c.label}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Recent Merchants */}
        <div className="bg-white rounded-2xl border border-slate-200 p-5">
          <h2 className="font-bold text-slate-700 mb-4 flex items-center gap-2">
            <span>🏪</span> آخر التجار المسجلين
          </h2>
          {stats?.recent_merchants.length === 0 ? (
            <p className="text-slate-400 text-sm text-center py-6">لا يوجد تجار بعد</p>
          ) : (
            <div className="space-y-3">
              {stats?.recent_merchants.map((m: any) => (
                <div key={m.id} className="flex items-center justify-between py-2 border-b border-slate-100 last:border-0">
                  <div>
                    <p className="text-sm font-medium text-slate-700">{m.store_name || m.email}</p>
                    <p className="text-xs text-slate-400">{m.email}</p>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded-full ${m.is_active ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                    {m.is_active ? 'نشط' : 'موقوف'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Recent Payments */}
        <div className="bg-white rounded-2xl border border-slate-200 p-5">
          <h2 className="font-bold text-slate-700 mb-4 flex items-center gap-2">
            <span>💳</span> آخر المدفوعات
          </h2>
          {stats?.recent_payments.length === 0 ? (
            <div className="text-center py-6">
              <p className="text-slate-400 text-sm">لا توجد مدفوعات بعد</p>
              <p className="text-slate-300 text-xs mt-1">ستظهر هنا عند اشتراك التجار</p>
            </div>
          ) : (
            <div className="space-y-3">
              {stats?.recent_payments.map((p: any) => (
                <div key={p.id} className="flex items-center justify-between py-2 border-b border-slate-100 last:border-0">
                  <div>
                    <p className="text-sm font-medium text-slate-700">{p.amount} {p.currency}</p>
                    <p className="text-xs text-slate-400">{p.gateway} · {new Date(p.created_at).toLocaleDateString('ar-SA')}</p>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded-full ${p.status === 'paid' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>
                    {p.status === 'paid' ? 'مدفوع' : p.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Platform Info */}
      <div className="bg-gradient-to-r from-brand-500 to-brand-600 rounded-2xl p-5 text-white">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-bold text-lg">منصة نحلة AI</h3>
            <p className="text-brand-100 text-sm mt-1">nahlah.ai · api.nahlah.ai</p>
          </div>
          <div className="text-right">
            <p className="text-brand-100 text-xs">الإصدار</p>
            <p className="font-bold">1.0.0</p>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-3 gap-4 text-center">
          <div>
            <p className="text-2xl font-bold">{stats?.tenants.total ?? 0}</p>
            <p className="text-brand-100 text-xs">متجر مربوط</p>
          </div>
          <div>
            <p className="text-2xl font-bold">{stats?.subscriptions.total ?? 0}</p>
            <p className="text-brand-100 text-xs">اشتراك كلي</p>
          </div>
          <div>
            <p className="text-2xl font-bold">{(stats?.revenue.total_sar ?? 0).toLocaleString('ar-SA')}</p>
            <p className="text-brand-100 text-xs">ريال إجمالي</p>
          </div>
        </div>
      </div>
    </div>
  )
}
