import { useEffect, useMemo, useState } from 'react'
import { Search, Store, ToggleLeft, ToggleRight } from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'

const SUB_STATUS_AR: Record<string, string> = {
  active: 'نشط', trialing: 'تجربة', canceled: 'ملغى',
  past_due: 'متأخر', none: 'بلا باقة', incomplete: 'غير مكتمل',
}
const WA_STATUS_AR: Record<string, string> = {
  connected: 'مربوط', not_connected: 'غير مربوط', pending: 'معلق',
  request_submitted: 'طلب إرسال', pending_activation: 'ينتظر التفعيل',
  action_required: 'يحتاج إجراء', disconnected: 'مقطوع',
}

function StatusBadge({ active }: { active: boolean }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${active ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
      {active ? 'نشط' : 'موقوف'}
    </span>
  )
}

export default function AdminTenants() {
  const [tenants, setTenants] = useState<AdminTenantSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'' | 'active' | 'inactive'>('')
  const [togglingTenantId, setTogglingTenantId] = useState<number | null>(null)

  useEffect(() => {
    setLoading(true)
    adminApi.tenants({ search, status, limit: 100 })
      .then(data => setTenants(data.tenants))
      .catch(() => setTenants([]))
      .finally(() => setLoading(false))
  }, [search, status])

  const rows = useMemo(() => tenants, [tenants])

  const toggleTenant = async (tenant: AdminTenantSummary) => {
    setTogglingTenantId(tenant.id)
    try {
      const updated = await adminApi.updateTenantStatus(tenant.id, !tenant.is_active)
      setTenants(prev => prev.map(row => row.id === tenant.id ? updated : row))
    } finally {
      setTogglingTenantId(null)
    }
  }

  return (
    <div className="p-6 space-y-5" dir="rtl">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-500 flex items-center justify-center shadow-lg shadow-amber-500/30">
            <Store className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">المتاجر</h1>
            <p className="text-slate-400 text-xs">إدارة المتاجر والحسابات وحالتها التشغيلية</p>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search className="w-4 h-4 text-slate-400 absolute right-3 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="ابحث بالاسم أو الدومين..."
              className="pr-9 pl-4 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-400 w-64"
            />
          </div>
          <select
            value={status}
            onChange={e => setStatus(e.target.value as '' | 'active' | 'inactive')}
            className="px-3 py-2 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-amber-400"
          >
            <option value="">كل الحالات</option>
            <option value="active">نشط</option>
            <option value="inactive">موقوف</option>
          </select>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-40">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-amber-500" />
          </div>
        ) : rows.length === 0 ? (
          <div className="text-center py-16 text-slate-400">لا توجد متاجر مطابقة</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-100">
                  {['المتجر', 'الخطة', 'واتساب', 'الطلبات', 'المحادثات', 'الإيراد', 'الحالة', 'إجراء'].map(h => (
                    <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-slate-500">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {rows.map(tenant => (
                  <tr key={tenant.id} className="hover:bg-slate-50/50">
                    <td className="px-4 py-3">
                      <p className="font-semibold text-slate-700">{tenant.name}</p>
                      <p className="text-xs text-slate-400">{tenant.domain || '—'}</p>
                    </td>
                    <td className="px-4 py-3">
                      <p className="text-slate-700">{tenant.subscription.plan || '—'}</p>
                      <p className="text-xs text-slate-400">{SUB_STATUS_AR[tenant.subscription.status] ?? tenant.subscription.status}</p>
                    </td>
                    <td className="px-4 py-3 text-slate-600">{WA_STATUS_AR[tenant.whatsapp.status] ?? tenant.whatsapp.status}</td>
                    <td className="px-4 py-3 text-slate-600">{tenant.stats.orders.toLocaleString('ar-SA')}</td>
                    <td className="px-4 py-3 text-slate-600">{tenant.stats.conversations.toLocaleString('ar-SA')}</td>
                    <td className="px-4 py-3 text-slate-600">{tenant.stats.revenue_sar.toLocaleString('ar-SA')} ر.س</td>
                    <td className="px-4 py-3"><StatusBadge active={tenant.is_active} /></td>
                    <td className="px-4 py-3">
                      <button
                        onClick={() => toggleTenant(tenant)}
                        disabled={togglingTenantId === tenant.id}
                        className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50"
                        title={tenant.is_active ? 'إيقاف المتجر' : 'تفعيل المتجر'}
                      >
                        {tenant.is_active ? <ToggleRight className="w-4 h-4 text-green-500" /> : <ToggleLeft className="w-4 h-4 text-red-500" />}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
