import { useEffect, useState } from 'react'
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  MessageSquare,
  Phone,
  RefreshCw,
  Smartphone,
  Store,
  User,
  Zap,
} from 'lucide-react'
import { adminApi, type CoexistenceRequest, type CoexistenceActivatePayload } from '../api/admin'

// ── Status badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    request_submitted:  { label: 'طلب جديد',        cls: 'bg-amber-100 text-amber-700' },
    pending_activation: { label: 'جارٍ التفعيل',    cls: 'bg-blue-100 text-blue-700' },
    action_required:    { label: 'يحتاج تدخل',      cls: 'bg-red-100 text-red-700' },
    connected:          { label: 'مفعّل',            cls: 'bg-emerald-100 text-emerald-700' },
    not_connected:      { label: 'غير مربوط',       cls: 'bg-slate-100 text-slate-500' },
  }
  const { label, cls } = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${cls}`}>
      {label}
    </span>
  )
}

// ── Activate form ─────────────────────────────────────────────────────────────

function ActivateForm({
  req,
  onSuccess,
  onCancel,
}: {
  req: CoexistenceRequest
  onSuccess: () => void
  onCancel: () => void
}) {
  const [form, setForm] = useState<Partial<CoexistenceActivatePayload>>({
    tenant_id: req.tenant_id,
    phone_number: req.requested_phone ?? '',
    display_name: req.display_name ?? '',
    configure_webhook: true,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<Record<string, unknown> | null>(null)

  const set = (k: keyof CoexistenceActivatePayload, v: string | boolean) =>
    setForm(f => ({ ...f, [k]: v }))

  const submit = async () => {
    if (!form.phone_number_id || !form.phone_number || !form.api_key) {
      setError('phone_number_id و phone_number و api_key مطلوبة.')
      return
    }
    setBusy(true)
    setError('')
    try {
      const res = await adminApi.activateCoexistence(form as CoexistenceActivatePayload)
      setResult(res)
      onSuccess()
    } catch (e: any) {
      setError(e?.message ?? 'فشل التفعيل')
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="rounded-xl bg-emerald-50 border border-emerald-200 p-4 text-center space-y-2">
        <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto" />
        <p className="font-bold text-emerald-700">تم التفعيل بنجاح</p>
        <p className="text-xs text-emerald-600">الحالة: {String(result.status)}</p>
      </div>
    )
  }

  return (
    <div className="mt-4 border border-violet-200 rounded-xl bg-violet-50 p-4 space-y-3" dir="rtl">
      <p className="font-bold text-violet-800 text-sm flex items-center gap-2">
        <Zap className="w-4 h-4" /> تفعيل خدمة 360dialog
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Phone Number *</label>
          <input
            value={form.phone_number ?? ''}
            onChange={e => set('phone_number', e.target.value)}
            placeholder="+9665XXXXXXXX"
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
            dir="ltr"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Phone Number ID * (من 360dialog)</label>
          <input
            value={form.phone_number_id ?? ''}
            onChange={e => set('phone_number_id', e.target.value)}
            placeholder="360dialog phone_number_id"
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
            dir="ltr"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">API Key * (D360-API-KEY)</label>
          <input
            value={form.api_key ?? ''}
            onChange={e => set('api_key', e.target.value)}
            placeholder="D360-XXXXXXXXXXXXXXXX"
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-mono"
            dir="ltr"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">WABA ID (اختياري)</label>
          <input
            value={form.waba_id ?? ''}
            onChange={e => set('waba_id', e.target.value)}
            placeholder="WABA ID"
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
            dir="ltr"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Channel ID (اختياري)</label>
          <input
            value={form.channel_id ?? ''}
            onChange={e => set('channel_id', e.target.value)}
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
            dir="ltr"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-600 mb-1">Display Name (اختياري)</label>
          <input
            value={form.display_name ?? ''}
            onChange={e => set('display_name', e.target.value)}
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
          />
        </div>
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-700 cursor-pointer">
        <input
          type="checkbox"
          checked={!!form.configure_webhook}
          onChange={e => set('configure_webhook', e.target.checked)}
          className="rounded"
        />
        إعداد Webhook تلقائيًا لدى 360dialog
      </label>

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-red-700 text-xs">{error}</div>
      )}

      <div className="flex gap-3">
        <button
          onClick={submit}
          disabled={busy}
          className="flex-1 rounded-xl bg-violet-600 py-2.5 text-sm font-bold text-white hover:bg-violet-500 disabled:opacity-60 transition"
        >
          {busy ? 'جارٍ التفعيل...' : 'تفعيل الآن'}
        </button>
        <button
          onClick={onCancel}
          className="px-4 rounded-xl border border-slate-200 text-sm text-slate-500 hover:bg-slate-50 transition"
        >
          إلغاء
        </button>
      </div>
    </div>
  )
}

// ── Request card ──────────────────────────────────────────────────────────────

function RequestCard({ req, onRefresh }: { req: CoexistenceRequest; onRefresh: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [activating, setActivating] = useState(false)

  const isNew = req.wa_status === 'request_submitted'

  const fmt = (iso: string | null) => {
    if (!iso) return '—'
    const d = new Date(iso)
    return d.toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh', dateStyle: 'medium', timeStyle: 'short' })
  }

  return (
    <div
      className={`rounded-2xl border shadow-sm transition ${isNew ? 'border-amber-300 bg-amber-50' : 'border-slate-200 bg-white'}`}
    >
      {/* Header */}
      <div className="p-4 flex items-start justify-between gap-4">
        <div className="flex items-start gap-3 min-w-0">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 ${isNew ? 'bg-amber-500' : 'bg-slate-200'}`}>
            <Smartphone className={`w-5 h-5 ${isNew ? 'text-white' : 'text-slate-400'}`} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-bold text-slate-800 text-sm">
                {req.display_name || req.tenant_name || `Tenant #${req.tenant_id}`}
              </span>
              <StatusBadge status={req.wa_status} />
            </div>
            <div className="flex items-center gap-4 mt-1 text-xs text-slate-500 flex-wrap">
              <span className="flex items-center gap-1"><Store className="w-3 h-3" /> #{req.tenant_id}</span>
              {req.merchant_email && <span className="flex items-center gap-1"><User className="w-3 h-3" />{req.merchant_email}</span>}
              {req.requested_phone && <span className="flex items-center gap-1" dir="ltr"><Phone className="w-3 h-3" />{req.requested_phone}</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {req.submitted_at && (
            <span className="text-xs text-slate-400 flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {fmt(req.submitted_at)}
            </span>
          )}
          <button onClick={() => setExpanded(e => !e)} className="p-1.5 rounded-lg hover:bg-slate-100 transition text-slate-400">
            {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t border-slate-100 pt-3" dir="rtl">
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 text-xs">
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">رقم واتساب التاجر</p>
              <p className="font-mono text-slate-700 dir-ltr" dir="ltr">{req.requested_phone || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">اسم النشاط</p>
              <p className="text-slate-700">{req.display_name || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">البريد الإلكتروني</p>
              <p className="text-slate-700">{req.merchant_email || '—'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">تطبيق WA Business</p>
              <p className="text-slate-700">{req.has_whatsapp_business_app ? 'نعم ✓' : 'لا'}</p>
            </div>
            <div>
              <p className="text-slate-400 font-semibold mb-0.5">وقت تقديم الطلب</p>
              <p className="text-slate-700">{fmt(req.submitted_at)}</p>
            </div>
            {req.connected_at && (
              <div>
                <p className="text-slate-400 font-semibold mb-0.5">وقت التفعيل</p>
                <p className="text-slate-700">{fmt(req.connected_at)}</p>
              </div>
            )}
          </div>

          {req.notes && (
            <div className="rounded-lg bg-slate-100 p-3">
              <p className="text-xs font-semibold text-slate-500 mb-1 flex items-center gap-1">
                <MessageSquare className="w-3 h-3" /> ملاحظات التاجر
              </p>
              <p className="text-sm text-slate-700 whitespace-pre-wrap">{req.notes}</p>
            </div>
          )}

          {req.last_error && (
            <div className="rounded-lg bg-red-50 border border-red-200 p-3 text-xs text-red-700">
              آخر خطأ: {req.last_error}
            </div>
          )}

          {/* Activate button (only for pending requests) */}
          {(req.wa_status === 'request_submitted' || req.wa_status === 'action_required') && !activating && (
            <button
              onClick={() => setActivating(true)}
              className="w-full mt-2 rounded-xl bg-violet-600 py-2.5 text-sm font-bold text-white hover:bg-violet-500 transition"
            >
              تفعيل هذا التاجر
            </button>
          )}

          {activating && (
            <ActivateForm
              req={req}
              onSuccess={() => { setActivating(false); onRefresh() }}
              onCancel={() => setActivating(false)}
            />
          )}
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdminCoexistence() {
  const [requests, setRequests] = useState<CoexistenceRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('request_submitted')

  const load = () => {
    setLoading(true)
    adminApi.coexistenceRequests(statusFilter)
      .then(data => setRequests(data.requests))
      .catch(() => setRequests([]))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [statusFilter])

  const pending   = requests.filter(r => r.wa_status === 'request_submitted').length
  const activated = requests.filter(r => r.wa_status === 'connected').length

  return (
    <div className="p-6 space-y-5" dir="rtl">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-600 flex items-center justify-center shadow-lg shadow-violet-500/30">
            <Smartphone className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">طلبات واتساب الجوال + الذكاء</h1>
            <p className="text-slate-400 text-xs">إدارة وتفعيل طلبات التاجر لخدمة 360dialog Coexistence</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-2 rounded-xl border border-slate-200 px-4 py-2 text-sm font-semibold text-slate-600 hover:bg-slate-50 transition"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {[
          { label: 'إجمالي الطلبات', value: requests.length, color: 'text-slate-700' },
          { label: 'طلبات جديدة',    value: pending,          color: 'text-amber-600' },
          { label: 'مفعّلون',        value: activated,        color: 'text-emerald-600' },
        ].map(s => (
          <div key={s.label} className="bg-white rounded-2xl border border-slate-100 shadow-sm p-4">
            <p className={`text-2xl font-black ${s.color}`}>{s.value}</p>
            <p className="text-xs text-slate-400 mt-0.5">{s.label}</p>
          </div>
        ))}
      </div>

      {/* Filter */}
      <div className="flex gap-2 flex-wrap">
        {[
          { key: 'request_submitted',  label: 'طلبات جديدة' },
          { key: 'connected',          label: 'مفعّلون' },
          { key: 'action_required',    label: 'يحتاج تدخل' },
          { key: 'all',                label: 'الكل' },
        ].map(f => (
          <button
            key={f.key}
            onClick={() => setStatusFilter(f.key)}
            className={`rounded-full px-4 py-1.5 text-xs font-semibold transition ${
              statusFilter === f.key
                ? 'bg-violet-600 text-white'
                : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* List */}
      {loading ? (
        <div className="text-center py-16 text-slate-400 text-sm">جارٍ التحميل...</div>
      ) : requests.length === 0 ? (
        <div className="text-center py-16 text-slate-400">
          <Smartphone className="w-12 h-12 mx-auto mb-3 opacity-20" />
          <p className="text-sm">لا توجد طلبات {statusFilter !== 'all' ? 'بهذه الحالة' : ''}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {requests.map(req => (
            <RequestCard key={req.tenant_id} req={req} onRefresh={load} />
          ))}
        </div>
      )}
    </div>
  )
}
