import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Clock, Loader2, RefreshCw, Search, Send, ShieldCheck, X } from 'lucide-react'
import {
  supportAccessApi,
  type SupportAccessTarget,
  type SupportAccessTargetStatus,
} from '../../api/supportAccess'

const STATUS_LABELS: Record<SupportAccessTargetStatus, string> = {
  NONE: 'لا يوجد طلب',
  PENDING: 'PENDING',
  APPROVED: 'APPROVED',
  REVOKED: 'REVOKED',
  EXPIRED: 'EXPIRED',
  REJECTED: 'REJECTED',
}

const STATUS_CLASSES: Record<SupportAccessTargetStatus, string> = {
  NONE: 'bg-slate-100 text-slate-600',
  PENDING: 'bg-amber-100 text-amber-800',
  APPROVED: 'bg-green-100 text-green-800',
  REVOKED: 'bg-slate-200 text-slate-700',
  EXPIRED: 'bg-orange-100 text-orange-800',
  REJECTED: 'bg-red-100 text-red-800',
}

const DURATIONS = [1, 2, 4, 8, 24, 48]

export default function SupportAccessTargetsPanel() {
  const [targets, setTargets] = useState<SupportAccessTarget[]>([])
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<SupportAccessTarget | null>(null)
  const [purpose, setPurpose] = useState('')
  const [durationHours, setDurationHours] = useState(4)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const result = await supportAccessApi.targets()
      setTargets(result.targets ?? [])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'تعذر تحميل أهداف وصول الدعم')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const visibleTargets = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return targets
    return targets.filter(target =>
      String(target.tenant_id) === needle
      || target.tenant_name.toLowerCase().includes(needle),
    )
  }, [search, targets])

  const submit = async () => {
    if (!selected || purpose.trim().length < 5) return
    setSubmitting(true)
    setError('')
    try {
      await supportAccessApi.request({
        tenant_id: selected.tenant_id,
        purpose: purpose.trim(),
        duration_hours: durationHours,
      })
      setSelected(null)
      setPurpose('')
      setDurationHours(4)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'فشل إرسال طلب الوصول')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="rounded-2xl border border-amber-200 bg-amber-50/50 p-4 space-y-4" aria-label="أهداف وصول الدعم حسب المتجر">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-amber-700" />
            <h2 className="font-black text-slate-900">وصول الدعم حسب المتجر</h2>
          </div>
          <p className="mt-1 text-xs text-slate-600">
            اختر Tenant مباشرة. يبقى الطلب معلقًا حتى يوافق حساب التاجر المخوّل صراحةً.
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading} className="inline-flex items-center gap-2 rounded-xl border border-amber-300 bg-white px-3 py-2 text-xs font-bold text-amber-800 disabled:opacity-50">
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>
      </div>

      <div className="relative max-w-md">
        <Search className="absolute right-3 top-2.5 h-4 w-4 text-slate-400" />
        <input
          value={search}
          onChange={event => setSearch(event.target.value)}
          placeholder="ابحث باسم المتجر أو Tenant ID"
          className="w-full rounded-xl border border-slate-200 bg-white py-2 pr-9 pl-3 text-sm"
        />
      </div>

      {error && <p className="rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-slate-500"><Loader2 className="h-4 w-4 animate-spin" /> جارٍ التحميل...</div>
      ) : (
        <div className="max-h-72 overflow-auto rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-slate-50 text-xs text-slate-500">
              <tr><th className="p-3 text-right">Tenant</th><th className="p-3 text-right">الحالة</th><th className="p-3 text-right">الطلب</th></tr>
            </thead>
            <tbody>
              {visibleTargets.map(target => (
                <tr key={target.tenant_id} className="border-t border-slate-100">
                  <td className="p-3"><span className="font-bold text-slate-800">{target.tenant_name}</span><span className="mr-2 text-xs text-slate-400">#{target.tenant_id}</span></td>
                  <td className="p-3"><span className={`rounded-full px-2 py-1 text-[11px] font-bold ${STATUS_CLASSES[target.status]}`}>{STATUS_LABELS[target.status]}</span></td>
                  <td className="p-3">
                    <button
                      type="button"
                      disabled={!target.can_request}
                      onClick={() => { setSelected(target); setError('') }}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-bold text-white disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-500"
                    >
                      <Send className="h-3.5 w-3.5" /> طلب وصول
                    </button>
                    {!target.approval_recipient_available && <span className="mr-2 text-xs text-red-600">لا يوجد حساب موافقة مؤهل</span>}
                    {target.request_reference && <span className="mr-2 text-xs text-slate-400">{target.request_reference}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center p-4">
          <button type="button" aria-label="إغلاق" className="absolute inset-0 bg-black/50" onClick={() => setSelected(null)} />
          <div className="relative w-full max-w-md space-y-4 rounded-2xl bg-white p-6 shadow-2xl">
            <div className="flex items-start justify-between gap-3">
              <div><h3 className="font-black text-slate-900">طلب وصول للدعم</h3><p className="text-xs text-slate-500">{selected.tenant_name} · Tenant #{selected.tenant_id}</p></div>
              <button type="button" onClick={() => setSelected(null)}><X className="h-5 w-5 text-slate-500" /></button>
            </div>
            <div className="flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
              <AlertTriangle className="h-4 w-4 shrink-0" /> لن يُنشأ أي grant قبل موافقة التاجر الصريحة.
            </div>
            <label className="block space-y-1 text-xs font-bold text-slate-700">
              <span>الغرض</span>
              <textarea value={purpose} onChange={event => setPurpose(event.target.value)} rows={3} maxLength={300} className="w-full rounded-xl border border-slate-200 p-3 text-sm font-normal" placeholder="اكتب الغرض من طلب الوصول" />
            </label>
            <label className="block space-y-1 text-xs font-bold text-slate-700">
              <span className="inline-flex items-center gap-1"><Clock className="h-3.5 w-3.5" /> المدة</span>
              <select value={durationHours} onChange={event => setDurationHours(Number(event.target.value))} className="w-full rounded-xl border border-slate-200 p-2.5 text-sm font-normal">
                {DURATIONS.map(hours => <option key={hours} value={hours}>{hours} ساعة</option>)}
              </select>
            </label>
            <button type="button" onClick={() => void submit()} disabled={submitting || purpose.trim().length < 5} className="flex w-full items-center justify-center gap-2 rounded-xl bg-amber-500 py-2.5 text-sm font-black text-white disabled:opacity-50">
              {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />} إرسال طلب PENDING
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
