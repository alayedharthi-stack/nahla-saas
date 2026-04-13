import { useEffect, useState } from 'react'
import { adminApi, type AdminPayment } from '../api/admin'
import { DollarSign, TrendingUp, AlertCircle, Calendar } from 'lucide-react'

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    paid:    { label: 'مدفوع', cls: 'bg-green-100 text-green-700' },
    pending: { label: 'معلق',  cls: 'bg-yellow-100 text-yellow-700' },
    failed:  { label: 'فاشل',  cls: 'bg-red-100 text-red-700' },
  }
  const s = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

export default function AdminRevenue() {
  const [data, setData]     = useState<{
    revenue: { total_sar: number; today_sar: number; mrr_sar: number }
    recent_payments: Array<{ id: number; tenant_id: number; amount: number; currency: string; status: string; gateway: string; created_at: string | null }>
  } | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([adminApi.revenueSummary(), adminApi.billingPayments()])
      .then(([summary, payments]) => setData({
        revenue: {
          total_sar: summary.total_sar,
          today_sar: summary.today_sar,
          mrr_sar: summary.mrr_sar,
        },
        recent_payments: payments.payments.slice(0, 20).map((payment: AdminPayment) => ({
          id: payment.id,
          tenant_id: payment.tenant_id,
          amount: payment.amount_sar,
          currency: payment.currency,
          status: payment.status,
          gateway: payment.gateway,
          created_at: payment.created_at,
        })),
      }))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-amber-500" />
    </div>
  )

  const rev = data?.revenue ?? { total_sar: 0, today_sar: 0, mrr_sar: 0 }
  const payments = data?.recent_payments ?? []
  const failed = payments.filter(p => p.status === 'failed')

  const summary = [
    { label: 'إيرادات اليوم',    value: rev.today_sar, icon: Calendar,   color: 'bg-blue-500'    },
    { label: 'الإيرادات الشهرية (MRR)', value: rev.mrr_sar,   icon: TrendingUp,  color: 'bg-emerald-500' },
    { label: 'إجمالي الإيرادات', value: rev.total_sar, icon: DollarSign,  color: 'bg-amber-500'  },
    { label: 'مدفوعات فاشلة',   value: failed.length, icon: AlertCircle, color: 'bg-red-500'     },
  ]

  return (
    <div className="p-6 space-y-6" dir="rtl">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-emerald-500 flex items-center justify-center shadow-lg shadow-emerald-500/30">
          <DollarSign className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="text-lg font-black text-slate-800">الإيرادات</h1>
          <p className="text-slate-400 text-xs">نظرة مالية سريعة</p>
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {summary.map(s => (
          <div key={s.label} className="bg-white rounded-2xl border border-slate-100 p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <span className="text-slate-500 text-xs font-medium">{s.label}</span>
              <div className={`w-8 h-8 rounded-xl flex items-center justify-center ${s.color}`}>
                <s.icon className="w-4 h-4 text-white" />
              </div>
            </div>
            <p className="text-2xl font-black text-slate-800">
              {typeof s.value === 'number' && s.label !== 'مدفوعات فاشلة'
                ? `${Number(s.value).toLocaleString('ar-SA')} ر.س`
                : s.value}
            </p>
          </div>
        ))}
      </div>

      {/* Payments table */}
      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-50">
          <h2 className="font-bold text-slate-700 text-sm">آخر المدفوعات</h2>
        </div>
        {payments.length === 0 ? (
          <div className="text-center py-14 text-slate-400">
            <DollarSign className="w-10 h-10 mx-auto mb-3 text-slate-200" />
            <p>لا توجد مدفوعات بعد</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-100">
                  {['رقم العملية', 'المبلغ', 'البوابة', 'الحالة', 'التاريخ'].map(h => (
                    <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-slate-500">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {payments.map(p => (
                  <tr key={p.id} className="hover:bg-slate-50/50">
                    <td className="px-4 py-3 text-slate-400 text-xs">#{p.id}</td>
                    <td className="px-4 py-3 font-semibold text-slate-700">
                      {Number(p.amount).toLocaleString('ar-SA')} {p.currency}
                    </td>
                    <td className="px-4 py-3 text-slate-500">{p.gateway}</td>
                    <td className="px-4 py-3"><StatusBadge status={p.status} /></td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {p.created_at ? new Date(p.created_at).toLocaleDateString('ar-SA') : '—'}
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
