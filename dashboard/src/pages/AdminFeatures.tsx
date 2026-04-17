import { useEffect, useMemo, useState } from 'react'
import { Flag, ToggleLeft, ToggleRight } from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'

// Platform-managed tenant flags that may not yet be present in a tenant's
// `extra_metadata.tenant_features` blob. We surface them with a default of
// `false` so support/staff can flip them on for the first time without
// having to touch the database.
//
// Keep this list short — every entry shows up on every tenant. Add a row
// only when the flag is wired into a real per-tenant code path.
interface KnownTenantFlag {
  key:          string
  label:        string   // Arabic-first label
  description:  string   // short Arabic explainer
  group?:       string   // optional grouping header
}

const KNOWN_TENANT_FLAGS: KnownTenantFlag[] = [
  {
    key:         'offer_decision_service_advisory',
    label:       'الوضع الاستشاري لمحرّك العروض',
    description: 'يحسب القرار ويُسجّله في السجل (Ledger) دون تغيير سلوك إصدار الكوبون — مرحلة المراقبة الآمنة قبل التفعيل الكامل. يتقدّم على الوضع الإلزامي عند تشغيل الاثنين معاً.',
    group:       'OfferDecisionService',
  },
  {
    key:         'offer_decision_service',
    label:       'محرّك قرارات العروض الذكية (إلزامي)',
    description: 'يجعل المحرّك المرجعَ الوحيد لإصدار الكوبونات والعروض في الأتمتة والمحادثة وتغيّر شريحة العميل. لا يُفعَّل إلا بعد التحقّق من الوضع الاستشاري.',
    group:       'OfferDecisionService',
  },
]

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

  // Merge the live tenant flags with the platform-managed catalogue so a
  // never-set flag still renders (with a default of `false`). Live values
  // win — the catalogue only fills in what's missing.
  const mergedTenantEntries = useMemo<Array<[string, boolean, KnownTenantFlag | undefined]>>(() => {
    const merged: Record<string, boolean> = { ...tenantFeatures }
    for (const flag of KNOWN_TENANT_FLAGS) {
      if (!(flag.key in merged)) merged[flag.key] = false
    }
    const knownByKey = new Map(KNOWN_TENANT_FLAGS.map(f => [f.key, f]))
    return Object.entries(merged).map(([key, enabled]) => [key, enabled, knownByKey.get(key)])
  }, [tenantFeatures])

  return (
    <div className="p-6 space-y-5" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-fuchsia-500 flex items-center justify-center shadow-lg shadow-fuchsia-500/30">
          <Flag className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">الميزات التجريبية</h1>
          <p className="text-slate-400 text-xs">تحكم مركزي في تفعيل القدرات على مستوى المنصة</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div className="bg-white rounded-2xl border border-slate-100 shadow-sm divide-y divide-slate-50">
          <div className="px-5 py-4">
            <h2 className="font-bold text-slate-700 text-sm">الإعدادات العامة</h2>
          </div>
          {entries.length === 0 ? (
            <div className="p-8 text-center text-slate-400">لا توجد ميزات حالياً</div>
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
            <h2 className="font-bold text-slate-700 text-sm">إعدادات المتاجر</h2>
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
          {mergedTenantEntries.length === 0 ? (
            <div className="p-8 text-center text-slate-400">لا توجد تخصيصات لهذا المتجر</div>
          ) : mergedTenantEntries.map(([key, enabled, meta]) => (
            <div key={key} className="px-5 py-4 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="text-sm font-semibold text-slate-700">{meta?.label ?? key}</p>
                  {meta?.group && (
                    <span className="px-1.5 py-0.5 rounded bg-slate-100 text-[10px] font-medium text-slate-500">
                      {meta.group}
                    </span>
                  )}
                </div>
                {meta && (
                  <p className="text-[10px] font-mono text-slate-400 mt-0.5">{key}</p>
                )}
                <p className="text-xs text-slate-500 mt-1">
                  {meta?.description ?? (enabled ? 'مفعّل للمتجر' : 'متوقف للمتجر')}
                </p>
                {meta && (
                  <p className="text-[11px] mt-1 font-medium" style={{ color: enabled ? '#16a34a' : '#94a3b8' }}>
                    {enabled ? 'مفعّل لهذا المتجر' : 'متوقف لهذا المتجر'}
                  </p>
                )}
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
                className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50 shrink-0"
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
