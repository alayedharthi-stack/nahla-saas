import { useEffect, useState } from 'react'
import { LifeBuoy, Search } from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'

export default function AdminTroubleshooting() {
  const [search, setSearch] = useState('')
  const [tenants, setTenants] = useState<AdminTenantSummary[]>([])
  const [selectedTenantId, setSelectedTenantId] = useState<number | null>(null)
  const [details, setDetails] = useState<any>(null)

  useEffect(() => {
    adminApi.tenants({ search, limit: 50 }).then(data => {
      setTenants(data.tenants)
      if (!selectedTenantId && data.tenants[0]) setSelectedTenantId(data.tenants[0].id)
    }).catch(() => setTenants([]))
  }, [search, selectedTenantId])

  useEffect(() => {
    if (!selectedTenantId) return
    adminApi.troubleshootTenant(selectedTenantId).then(setDetails).catch(() => setDetails(null))
  }, [selectedTenantId])

  return (
    <div className="p-6 space-y-5" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-sky-500 flex items-center justify-center shadow-lg shadow-sky-500/30">
          <LifeBuoy className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">Troubleshooting</h1>
          <p className="text-slate-400 text-xs">تشخيص حالة التاجر والدعم والوضع التشغيلي بسرعة</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
          <div className="p-4 border-b border-slate-50">
            <div className="relative">
              <Search className="w-4 h-4 text-slate-400 absolute right-3 top-1/2 -translate-y-1/2" />
              <input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="ابحث عن متجر..."
                className="w-full pr-9 pl-4 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-sky-400"
              />
            </div>
          </div>
          <div className="divide-y divide-slate-50 max-h-[28rem] overflow-y-auto">
            {tenants.map(tenant => (
              <button
                key={tenant.id}
                onClick={() => setSelectedTenantId(tenant.id)}
                className={`w-full text-right px-4 py-3 hover:bg-slate-50 transition ${selectedTenantId === tenant.id ? 'bg-sky-50' : ''}`}
              >
                <p className="font-semibold text-slate-700">{tenant.name}</p>
                <p className="text-xs text-slate-400">{tenant.domain || `Tenant #${tenant.id}`}</p>
              </button>
            ))}
          </div>
        </div>

        <div className="lg:col-span-2 space-y-4">
          {!details ? (
            <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-8 text-center text-slate-400">
              اختر متجراً لعرض تفاصيل التشخيص
            </div>
          ) : (
            <>
              <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
                <h2 className="font-bold text-slate-700 text-sm mb-3">ملخص التاجر</h2>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                  <div><span className="text-slate-400">المتجر:</span> <span className="font-medium text-slate-700">{details.tenant.name}</span></div>
                  <div><span className="text-slate-400">الخطة:</span> <span className="font-medium text-slate-700">{details.tenant.subscription.plan}</span></div>
                  <div><span className="text-slate-400">واتساب:</span> <span className="font-medium text-slate-700">{details.tenant.whatsapp.status}</span></div>
                  <div><span className="text-slate-400">الدعم:</span> <span className="font-medium text-slate-700">{details.support_access.enabled ? 'مفعّل' : 'غير مفعّل'}</span></div>
                </div>
              </div>

              <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
                <h2 className="font-bold text-slate-700 text-sm mb-3">آخر مزامنة</h2>
                <div className="text-sm text-slate-600">
                  <p>الحالة: {details.latest_sync.status}</p>
                  <p>النوع: {details.latest_sync.sync_type || '—'}</p>
                  <p>آخر تشغيل: {details.latest_sync.created_at ? new Date(details.latest_sync.created_at).toLocaleString('ar-SA') : '—'}</p>
                  {details.latest_sync.error_message && <p className="text-red-500 mt-2">{details.latest_sync.error_message}</p>}
                </div>
              </div>

              <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-slate-50">
                  <h2 className="font-bold text-slate-700 text-sm">آخر الأحداث</h2>
                </div>
                <div className="divide-y divide-slate-50">
                  {(details.recent_events ?? []).map((event: any) => (
                    <div key={event.id} className="px-5 py-3">
                      <p className="text-xs text-slate-400">{event.category} · {event.event_type}</p>
                      <p className="text-sm text-slate-700">{event.summary || '—'}</p>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
