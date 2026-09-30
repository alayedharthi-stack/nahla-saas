import { useEffect, useState } from 'react'
import { Bot, Cpu, DollarSign, HelpCircle, RefreshCw } from 'lucide-react'
import { adminApi, type AdminAIUsageTenant, type AdminAICostsSummary } from '../api/admin'

const PERIODS = [
  { value: '24h', label: 'آخر 24 ساعة' },
  { value: '7d', label: 'آخر 7 أيام' },
  { value: 'mtd', label: 'من بداية الشهر' },
  { value: 'all', label: 'كل الفترة' },
] as const

function costDisplay(value: number, unpriced: number) {
  return unpriced > 0 ? 'غير مكتملة' : `$${value.toFixed(6)}`
}

export default function AdminAiUsage() {
  const [rows, setRows] = useState<AdminAIUsageTenant[]>([])
  const [costs, setCosts] = useState<AdminAICostsSummary | null>(null)
  const [period, setPeriod] = useState('7d')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let active = true
    let inFlight = false
    setLoading(true)
    setRows([])
    setCosts(null)
    setError(null)
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const [usage, costSummary] = await Promise.all([adminApi.aiUsage(period), adminApi.aiCosts(period)])
        if (!active) return
        setRows(usage.tenants)
        setCosts(costSummary)
        setError(null)
      } catch {
        if (!active) return
        setRows([])
        setCosts(null)
        setError('تعذر قراءة سجل تكلفة الذكاء الاصطناعي. الأرقام غير متاحة حاليًا.')
      } finally {
        inFlight = false
        if (active) setLoading(false)
      }
    }
    void refresh()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, 30_000)
    const onFocus = () => { void refresh() }
    window.addEventListener('focus', onFocus)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [period, reload])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-amber-500" />
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6" dir="rtl">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-indigo-500 flex items-center justify-center shadow-lg shadow-indigo-500/30">
            <Bot className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">استخدام الذكاء الاصطناعي</h1>
            <p className="text-slate-400 text-xs">تكلفة محسوبة من سجل الاستخدام بالدولار — تحديث كل 30 ثانية</p>
          </div>
        </div>
        <button onClick={() => setReload(n => n + 1)} className="text-sm flex items-center gap-2 text-slate-600">
          <RefreshCw className="w-4 h-4" /> تحديث
        </button>
        <select
          value={period}
          onChange={e => setPeriod(e.target.value)}
          className="text-sm border border-slate-200 rounded-xl px-3 py-2 bg-white text-slate-700"
        >
          {PERIODS.map(p => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
      </div>

      {error && <div role="alert" className="rounded-xl bg-red-50 text-red-700 p-4">{error}</div>}
      {costs && !error && <>
      <div className="text-xs text-slate-500 space-y-1">
        {costs.unpriced_calls > 0 && <p role="alert">يوجد {costs.unpriced_calls} استدعاء بلا تكلفة محفوظة؛ إجمالي التكلفة غير مكتمل.</p>}
        <p>تكلفة الرموز المسجلة = الاستهلاك الذي أبلغه المزود × التسعير المحفوظ. تقدير الرموز منفصل؛ مبلغ الفاتورة الفعلي من المزود غير متاح.</p>
        <p>حدود الفترة بتوقيت UTC: {costs.period_start ?? 'بداية السجل'} → {costs.period_end}؛ «بداية الشهر» تعني بداية الشهر بتوقيت UTC.</p>
        <p>إصدارات التسعير: {Object.entries(costs.pricing_versions ?? {}).map(([version, count]) => `${version} (${count})`).join('، ') || 'لا توجد سجلات'}؛ السجلات القديمة تحتفظ بتسعيرها الأصلي، وبعض أسعار النماذج مؤقتة حتى تأكيدها.</p>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">تكلفة رموز مسجلة</span>
            <DollarSign className="w-4 h-4 text-emerald-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{costDisplay(costs.actual_total_cost_usd, costs.unpriced_calls)}</p>
          <p className="text-[10px] text-emerald-600 mt-1">محسوبة من الرموز المسجلة؛ ليست مبلغ فاتورة</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">تكلفة رموز تقديرية</span>
            <DollarSign className="w-4 h-4 text-amber-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{costDisplay(costs.estimated_total_cost_usd, costs.unpriced_calls)}</p>
          <p className="text-[10px] text-amber-600 mt-1">Estimated — بدون usage فعلي</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">تكلفة غير منسوبة</span>
            <HelpCircle className="w-4 h-4 text-slate-400" />
          </div>
          <p className="text-2xl font-black text-slate-800">{costDisplay(costs.unattributed_total_cost_usd, costs.unpriced_calls)}</p>
          <p className="text-[10px] text-slate-500 mt-1">Unattributed / Unknown tenant</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">استدعاءات LLM</span>
            <Cpu className="w-4 h-4 text-violet-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{costs.calls_total.toLocaleString('ar-SA')}</p>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-50">
          <h2 className="font-bold text-slate-700 text-sm">حسب المتجر</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-100">
                {['المتجر', 'استدعاءات', 'تكلفة رموز مسجلة', 'تكلفة رموز تقديرية', 'مزوّد', 'نموذج', 'سبب'].map(h => (
                  <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-slate-500">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {rows.filter(row => row.calls_total > 0).map(row => (
                <tr key={row.tenant_id} className="hover:bg-slate-50/50">
                  <td className="px-4 py-3 font-semibold text-slate-700">{row.tenant_name ?? `متجر #${row.tenant_id}`}</td>
                  <td className="px-4 py-3 text-slate-600">{row.calls_total.toLocaleString('ar-SA')}</td>
                  <td className="px-4 py-3 text-emerald-700">{costDisplay(row.actual_total_cost_usd, row.unpriced_calls)}</td>
                  <td className="px-4 py-3 text-amber-700">{costDisplay(row.estimated_total_cost_usd, row.unpriced_calls)}</td>
                  <td className="px-4 py-3 text-slate-500 text-xs">
                    {row.providers.map(p => `${p.provider}: ${p.count}`).join(' | ') || '—'}
                  </td>
                  <td className="px-4 py-3 text-slate-500 text-xs">
                    {row.models.slice(0, 2).map(m => `${m.model}: ${m.count}`).join(' | ') || '—'}
                  </td>
                  <td className="px-4 py-3 text-slate-500 text-xs">
                    {row.reasons?.slice(0, 2).map(r => `${r.reason}: ${r.count}`).join(' | ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      </>}
    </div>
  )
}
