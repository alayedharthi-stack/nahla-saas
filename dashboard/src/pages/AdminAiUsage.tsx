import { useEffect, useState } from 'react'
import { Bot, Cpu, DollarSign } from 'lucide-react'
import { adminApi, type AdminAIUsageTenant } from '../api/admin'

export default function AdminAiUsage() {
  const [rows, setRows] = useState<AdminAIUsageTenant[]>([])
  const [totalCost, setTotalCost] = useState(0)
  const [totalTokens, setTotalTokens] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([adminApi.aiUsage(), adminApi.aiCosts()])
      .then(([usage, costs]) => {
        setRows(usage.tenants)
        setTotalCost(costs.estimated_total_cost_usd)
        setTotalTokens(costs.estimated_total_tokens)
      })
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-amber-500" />
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-indigo-500 flex items-center justify-center shadow-lg shadow-indigo-500/30">
          <Bot className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">استخدام الذكاء الاصطناعي</h1>
          <p className="text-slate-400 text-xs">تقارير تقديرية للتكلفة، التوكنات، ومزوّدات التنفيذ</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">إجمالي التكلفة التقديرية</span>
            <DollarSign className="w-4 h-4 text-emerald-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">${totalCost.toFixed(4)}</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">إجمالي التوكنات التقديرية</span>
            <Cpu className="w-4 h-4 text-violet-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{totalTokens.toLocaleString('ar-SA')}</p>
        </div>
        <div className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-slate-500 text-xs font-medium">عدد المتاجر ذات نشاط AI</span>
            <Bot className="w-4 h-4 text-amber-500" />
          </div>
          <p className="text-2xl font-black text-slate-800">{rows.length.toLocaleString('ar-SA')}</p>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-50">
          <h2 className="font-bold text-slate-700 text-sm">تفصيل استخدام المتاجر</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-100">
                {['المتجر', 'المحادثات', 'مُنسّقة', 'الأفعال', 'متوسط الزمن (ث)', 'التوكنات (تقديري)', 'التكلفة (تقديري)', 'المزوّدات'].map(h => (
                  <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-slate-500">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {rows.map(row => (
                <tr key={row.tenant_id} className="hover:bg-slate-50/50">
                  <td className="px-4 py-3 font-semibold text-slate-700">{row.tenant_name ?? `متجر #${row.tenant_id}`}</td>
                  <td className="px-4 py-3 text-slate-600">{row.turns_total.toLocaleString('ar-SA')}</td>
                  <td className="px-4 py-3 text-slate-600">{row.turns_orchestrated.toLocaleString('ar-SA')}</td>
                  <td className="px-4 py-3 text-slate-600">{row.ai_actions_logged.toLocaleString('ar-SA')}</td>
                  <td className="px-4 py-3 text-slate-600">{row.avg_latency_ms.toLocaleString('ar-SA')} مللي ث</td>
                  <td className="px-4 py-3 text-slate-600">{row.estimated_total_tokens.toLocaleString('ar-SA')}</td>
                  <td className="px-4 py-3 text-slate-600">${row.estimated_total_cost_usd.toFixed(4)}</td>
                  <td className="px-4 py-3 text-slate-500 text-xs">
                    {row.providers.map(provider => `${provider.provider}: ${provider.count}`).join(' | ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
