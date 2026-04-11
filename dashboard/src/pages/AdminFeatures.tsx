import { useEffect, useState } from 'react'
import { Flag, ToggleLeft, ToggleRight } from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'

export default function AdminFeatures() {
  const [features, setFeatures] = useState<Record<string, boolean>>({})
  const [tenants, setTenants] = useState<AdminTenantSummary[]>([])
  const [selectedTenantId, setSelectedTenantId] = useState<number | null>(null)
  const [tenantFeatures, setTenantFeatures] = useState<Record<string, boolean>>({})
  const [loadingKey, setLoadingKey] = useState<string | null>(null)

  useEffect(() => {
    adminApi.globalFeatures().then(data => setFeatures(data.features)).catch(() => setFeatures({}))
    adminApi.tenants({ limit: 50 }).then(data => {
      setTenants(data.tenants)
      if (data.tenants[0]) setSelectedTenantId(data.tenants[0].id)
    }).catch(() => setTenants([]))
  }, [])

  useEffect(() => {
    if (!selectedTenantId) return
    adminApi.tenantFeatures(selectedTenantId)
      .then(data => setTenantFeatures(data.features))
      .catch(() => setTenantFeatures({}))
  }, [selectedTenantId])

  const toggleFeature = async (featureKey: string, enabled: boolean) => {
    setLoadingKey(featureKey)
    try {
      const data = await adminApi.updateGlobalFeature(featureKey, enabled)
      setFeatures(data.features)
    } finally {
      setLoadingKey(null)
    }
  }

  const entries = Object.entries(features)
  const tenantEntries = Object.entries(tenantFeatures)

  return (
    <div className="p-6 space-y-5" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-fuchsia-500 flex items-center justify-center shadow-lg shadow-fuchsia-500/30">
          <Flag className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">Feature Flags</h1>
          <p className="text-slate-400 text-xs">تحكم مركزي في تفعيل القدرات على مستوى المنصة</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm divide-y divide-slate-50">
          <div className="px-5 py-4">
            <h2 className="font-bold text-slate-700 text-sm">Global Flags</h2>
          </div>
          {entries.length === 0 ? (
            <div className="p-8 text-center text-slate-400">لا توجد flags حالياً</div>
          ) : entries.map(([key, enabled]) => (
            <div key={key} className="px-5 py-4 flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-700">{key}</p>
                <p className="text-xs text-slate-400">{enabled ? 'مفعّل' : 'متوقف'}</p>
              </div>
              <button
                onClick={() => toggleFeature(key, !enabled)}
                disabled={loadingKey === key}
                className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50"
              >
                {enabled ? <ToggleRight className="w-5 h-5 text-green-500" /> : <ToggleLeft className="w-5 h-5 text-red-500" />}
              </button>
            </div>
          ))}
        </div>

        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm divide-y divide-slate-50">
          <div className="px-5 py-4 flex items-center justify-between gap-3">
            <h2 className="font-bold text-slate-700 text-sm">Tenant Overrides</h2>
            <select
              value={selectedTenantId ?? ''}
              onChange={e => setSelectedTenantId(Number(e.target.value))}
              className="px-3 py-2 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-fuchsia-400"
            >
              {tenants.map(tenant => (
                <option key={tenant.id} value={tenant.id}>{tenant.name}</option>
              ))}
            </select>
          </div>
          {tenantEntries.length === 0 ? (
            <div className="p-8 text-center text-slate-400">لا توجد overrides لهذا المتجر</div>
          ) : tenantEntries.map(([key, enabled]) => (
            <div key={key} className="px-5 py-4 flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-semibold text-slate-700">{key}</p>
                <p className="text-xs text-slate-400">{enabled ? 'مفعّل للمتجر' : 'متوقف للمتجر'}</p>
              </div>
              <button
                onClick={async () => {
                  if (!selectedTenantId) return
                  setLoadingKey(`tenant:${key}`)
                  try {
                    const data = await adminApi.updateTenantFeature(selectedTenantId, key, !enabled)
                    setTenantFeatures(data.features)
                  } finally {
                    setLoadingKey(null)
                  }
                }}
                disabled={loadingKey === `tenant:${key}`}
                className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50"
              >
                {enabled ? <ToggleRight className="w-5 h-5 text-green-500" /> : <ToggleLeft className="w-5 h-5 text-red-500" />}
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
