import { useEffect, useRef, useState } from 'react'
import {
  LifeBuoy, Search, Wifi, WifiOff, CheckCircle2,
  Eye, EyeOff, Loader2, AlertCircle,
} from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'
import { apiCall } from '../api/client'

// ── WhatsApp Force-Connect panel ──────────────────────────────────────────────

function ForceConnectPanel({ tenantId, tenantName, onSuccess }: {
  tenantId: number; tenantName: string; onSuccess: () => void
}) {
  const [phoneNumberId, setPhoneNumberId] = useState('')
  const [accessToken, setAccessToken]     = useState('')
  const [wabaId, setWabaId]               = useState('')
  const [phoneNumber, setPhoneNumber]     = useState('')
  const [displayName, setDisplayName]     = useState('')
  const [showToken, setShowToken]         = useState(false)
  const [busy, setBusy]                   = useState(false)
  const [error, setError]                 = useState('')
  const [success, setSuccess]             = useState('')

  const handleConnect = async () => {
    if (!phoneNumberId.trim()) { setError('Phone Number ID مطلوب'); return }
    if (!accessToken.trim())   { setError('Access Token مطلوب');   return }
    if (!wabaId.trim())        { setError('WABA ID مطلوب');        return }
    setBusy(true); setError(''); setSuccess('')
    try {
      await apiCall('/admin/whatsapp/force-connect', {
        method: 'POST',
        body: JSON.stringify({
          tenant_id:       tenantId,
          phone_number_id: phoneNumberId.trim(),
          access_token:    accessToken.trim(),
          waba_id:         wabaId.trim(),
          phone_number:    phoneNumber.trim() || undefined,
          display_name:    displayName.trim() || undefined,
        }),
      })
      setSuccess('تم ربط واتساب بنجاح ✅')
      setAccessToken('') // clear token from UI
      onSuccess()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'خطأ في الربط')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-violet-100 shadow-sm p-5 space-y-4">
      <div className="flex items-center gap-2 pb-1">
        <div className="w-8 h-8 rounded-lg bg-violet-100 flex items-center justify-center">
          <Wifi className="w-4 h-4 text-violet-600" />
        </div>
        <div>
          <h2 className="font-bold text-slate-800 text-sm">ربط واتساب مباشر (Admin)</h2>
          <p className="text-xs text-slate-400">{tenantName} — إدخال بيانات Meta مباشرة</p>
        </div>
      </div>

      <div className="grid sm:grid-cols-2 gap-3">
        {/* Phone Number ID */}
        <div className="space-y-1">
          <label className="text-xs font-medium text-slate-600">Phone Number ID <span className="text-red-500">*</span></label>
          <input
            value={phoneNumberId}
            onChange={e => setPhoneNumberId(e.target.value)}
            placeholder="1234567890123456"
            className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
            dir="ltr"
          />
        </div>

        {/* WABA ID */}
        <div className="space-y-1">
          <label className="text-xs font-medium text-slate-600">WABA ID <span className="text-red-500">*</span></label>
          <input
            value={wabaId}
            onChange={e => setWabaId(e.target.value)}
            placeholder="1234567890123456"
            className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
            dir="ltr"
          />
        </div>

        {/* Access Token */}
        <div className="space-y-1 sm:col-span-2">
          <label className="text-xs font-medium text-slate-600">Access Token (Permanent) <span className="text-red-500">*</span></label>
          <div className="relative">
            <input
              type={showToken ? 'text' : 'password'}
              value={accessToken}
              onChange={e => setAccessToken(e.target.value)}
              placeholder="EAAxxxxxxxx..."
              className="w-full border border-slate-200 rounded-xl px-3 py-2 pr-10 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
              dir="ltr"
            />
            <button
              type="button"
              onClick={() => setShowToken(v => !v)}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
            >
              {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <p className="text-xs text-slate-400">استخدم Permanent Page Access Token أو System User Token من Meta Business Manager</p>
        </div>

        {/* Phone Number (display) */}
        <div className="space-y-1">
          <label className="text-xs font-medium text-slate-600">رقم الهاتف (اختياري)</label>
          <input
            value={phoneNumber}
            onChange={e => setPhoneNumber(e.target.value)}
            placeholder="+9665XXXXXXXX"
            className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
            dir="ltr"
          />
        </div>

        {/* Display Name */}
        <div className="space-y-1">
          <label className="text-xs font-medium text-slate-600">اسم العرض (اختياري)</label>
          <input
            value={displayName}
            onChange={e => setDisplayName(e.target.value)}
            placeholder="متجر نحلة"
            className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400"
          />
        </div>
      </div>

      {error && (
        <div className="flex items-center gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2 text-sm text-red-700">
          <AlertCircle className="w-4 h-4 shrink-0" /> {error}
        </div>
      )}
      {success && (
        <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-xl px-3 py-2 text-sm text-emerald-700">
          <CheckCircle2 className="w-4 h-4 shrink-0" /> {success}
        </div>
      )}

      <button
        onClick={handleConnect}
        disabled={busy}
        className="w-full flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-60 text-white font-bold py-2.5 rounded-xl text-sm transition-all"
      >
        {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ الربط...</> : <><Wifi className="w-4 h-4" /> ربط واتساب مباشرة</>}
      </button>

      <p className="text-xs text-slate-400 text-center">
        سيتم كتابة البيانات مباشرة في قاعدة البيانات وتفعيل الإرسال فورًا
      </p>
    </div>
  )
}

// ── WhatsApp status card ──────────────────────────────────────────────────────

function WaStatusCard({ tenantId, tenantName, onRefreshNeeded }: {
  tenantId: number; tenantName: string; onRefreshNeeded: () => void
}) {
  const [wa, setWa]       = useState<any>(null)
  const [loading, setLoading] = useState(true)

  const load = () => {
    setLoading(true)
    apiCall<any>(`/admin/troubleshooting/tenants/${tenantId}/whatsapp`)
      .then(d => setWa(d.connection))
      .catch(() => setWa(null))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [tenantId])

  if (loading) return (
    <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 flex items-center gap-2 text-slate-400 text-sm">
      <Loader2 className="w-4 h-4 animate-spin" /> جارٍ تحميل حالة واتساب...
    </div>
  )

  const connected = wa?.status === 'connected' && wa?.sending_enabled

  return (
    <div className={`bg-white rounded-2xl border shadow-sm p-5 space-y-3 ${connected ? 'border-emerald-200' : 'border-slate-100'}`}>
      <div className="flex items-center justify-between">
        <h2 className="font-bold text-slate-700 text-sm">حالة واتساب الحالية</h2>
        <button onClick={() => { load(); onRefreshNeeded() }} className="text-xs text-sky-600 hover:underline">تحديث</button>
      </div>
      {!wa ? (
        <div className="flex items-center gap-2 text-slate-400 text-sm">
          <WifiOff className="w-4 h-4" /> لا يوجد ربط
        </div>
      ) : (
        <div className="space-y-2 text-sm">
          <div className="flex items-center gap-2">
            {connected
              ? <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
              : <WifiOff className="w-4 h-4 text-red-400 shrink-0" />
            }
            <span className={`font-semibold ${connected ? 'text-emerald-700' : 'text-red-600'}`}>{wa.status}</span>
            {wa.sending_enabled
              ? <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full">إرسال مفعّل</span>
              : <span className="text-xs bg-red-100 text-red-600 px-2 py-0.5 rounded-full">إرسال معطّل</span>
            }
          </div>
          {wa.phone_number && <p className="font-mono text-xs text-slate-600">{wa.phone_number}</p>}
          {wa.business_display_name && <p className="text-xs text-slate-500">{wa.business_display_name}</p>}
          {wa.connection_type && <p className="text-xs text-slate-400">النوع: {wa.connection_type} · {wa.provider ?? '—'}</p>}
          {wa.last_error && (
            <div className="mt-1 text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
              {wa.last_error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdminTroubleshooting() {
  const [search, setSearch]                   = useState('')
  const [tenants, setTenants]                 = useState<AdminTenantSummary[]>([])
  const [selectedTenantId, setSelectedTenantId] = useState<number | null>(null)
  const [details, setDetails]                 = useState<any>(null)
  const [waRefreshKey, setWaRefreshKey]       = useState(0)

  const selectedTenant = tenants.find(t => t.id === selectedTenantId)

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
          <h1 className="text-lg font-black text-slate-800">تشخيص المشاكل</h1>
          <p className="text-slate-400 text-xs">تشخيص حالة التاجر والدعم والوضع التشغيلي بسرعة</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Tenant list */}
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
                <p className="text-xs text-slate-400">{tenant.domain || `متجر #${tenant.id}`}</p>
              </button>
            ))}
          </div>
        </div>

        {/* Details panel */}
        <div className="lg:col-span-2 space-y-4">
          {!selectedTenantId ? (
            <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-8 text-center text-slate-400">
              اختر متجراً لعرض تفاصيل التشخيص
            </div>
          ) : (
            <>
              {/* ── WhatsApp current status ── */}
              <WaStatusCard
                key={`wa-${selectedTenantId}-${waRefreshKey}`}
                tenantId={selectedTenantId}
                tenantName={selectedTenant?.name ?? ''}
                onRefreshNeeded={() => setWaRefreshKey(k => k + 1)}
              />

              {/* ── Admin force-connect form ── */}
              <ForceConnectPanel
                tenantId={selectedTenantId}
                tenantName={selectedTenant?.name ?? ''}
                onSuccess={() => setWaRefreshKey(k => k + 1)}
              />

              {/* ── General summary ── */}
              {details && (
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
            </>
          )}
        </div>
      </div>
    </div>
  )
}
