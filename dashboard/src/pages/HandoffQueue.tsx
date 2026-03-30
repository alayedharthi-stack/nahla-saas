import { useState, useEffect, useCallback } from 'react'
import { UserCheck, PhoneCall, Clock, CheckCircle2, Loader2, Users } from 'lucide-react'
import { handoffApi, type HandoffSession } from '../api/handoff'

export default function HandoffQueue() {
  const [tab, setTab] = useState<'active' | 'resolved'>('active')
  const [sessions, setSessions] = useState<HandoffSession[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [resolvingId, setResolvingId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await handoffApi.getSessions({ status: tab, limit: 50 })
      setSessions(data.sessions)
      setTotal(data.total)
    } catch {
      setSessions([])
    } finally {
      setLoading(false)
    }
  }, [tab])

  useEffect(() => { load() }, [load])

  async function handleResolve(id: number) {
    setResolvingId(id)
    try {
      await handoffApi.resolveSession(id)
      await load()
    } finally {
      setResolvingId(null)
    }
  }

  function timeAgo(iso: string | null) {
    if (!iso) return '—'
    const diff = Date.now() - new Date(iso).getTime()
    const m = Math.floor(diff / 60000)
    if (m < 1) return 'الآن'
    if (m < 60) return `منذ ${m} دقيقة`
    const h = Math.floor(m / 60)
    if (h < 24) return `منذ ${h} ساعة`
    return `منذ ${Math.floor(h / 24)} يوم`
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">طابور التحويل البشري</h1>
          <p className="text-slate-500 text-sm mt-1">
            المحادثات التي طلبت موظفاً بشرياً — يجب الرد عليها يدوياً
          </p>
        </div>
        <div className="flex items-center gap-2 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2">
          <Users className="w-4 h-4 text-amber-600" />
          <span className="text-amber-700 text-sm font-medium">
            {tab === 'active' ? total : '—'} نشط
          </span>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-slate-100 rounded-lg p-1 w-fit">
        {(['active', 'resolved'] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
              tab === t
                ? 'bg-white text-slate-900 shadow-sm'
                : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            {t === 'active' ? 'نشط' : 'تم الحل'}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="w-6 h-6 text-brand-500 animate-spin" />
          </div>
        ) : sessions.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-slate-400">
            <UserCheck className="w-10 h-10 mb-3 text-slate-300" />
            <p className="font-medium">
              {tab === 'active' ? 'لا توجد محادثات نشطة تنتظر موظفاً' : 'لا توجد محادثات محلولة'}
            </p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="text-right px-4 py-3 text-slate-600 font-medium">العميل</th>
                <th className="text-right px-4 py-3 text-slate-600 font-medium">رقم الجوال</th>
                <th className="text-right px-4 py-3 text-slate-600 font-medium">آخر رسالة</th>
                <th className="text-right px-4 py-3 text-slate-600 font-medium">الوقت</th>
                <th className="text-right px-4 py-3 text-slate-600 font-medium">الحالة</th>
                {tab === 'active' && (
                  <th className="text-right px-4 py-3 text-slate-600 font-medium">إجراء</th>
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {sessions.map(s => (
                <tr key={s.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <div className="w-8 h-8 rounded-full bg-brand-100 flex items-center justify-center shrink-0">
                        <span className="text-brand-600 text-xs font-semibold">
                          {(s.customer_name || '?')[0]}
                        </span>
                      </div>
                      <span className="font-medium text-slate-800">{s.customer_name || '—'}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-500 font-mono text-xs">{s.customer_phone}</td>
                  <td className="px-4 py-3 text-slate-600 max-w-xs">
                    <p className="truncate">{s.last_message || '—'}</p>
                  </td>
                  <td className="px-4 py-3 text-slate-400 text-xs whitespace-nowrap">
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {timeAgo(s.created_at)}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {s.status === 'active' ? (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                        نشط
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-700">
                        <CheckCircle2 className="w-3 h-3" />
                        {s.resolved_by ? `حُل بواسطة ${s.resolved_by}` : 'تم الحل'}
                      </span>
                    )}
                  </td>
                  {tab === 'active' && (
                    <td className="px-4 py-3">
                      <button
                        onClick={() => handleResolve(s.id)}
                        disabled={resolvingId === s.id}
                        className="flex items-center gap-1.5 px-3 py-1.5 bg-brand-600 text-white text-xs rounded-lg hover:bg-brand-700 disabled:opacity-50 transition-colors"
                      >
                        {resolvingId === s.id ? (
                          <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                          <PhoneCall className="w-3 h-3" />
                        )}
                        تم الحل
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
