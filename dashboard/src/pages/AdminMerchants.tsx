import { useEffect, useState } from 'react'
import { API_BASE } from '../api/client'
import { getToken, startImpersonation } from '../auth'
import { Users, Search, LogIn, ToggleLeft, ToggleRight, Send, Clock, CheckCircle } from 'lucide-react'

interface Merchant {
  id: number
  tenant_id: number | null
  email: string
  store_name: string
  phone: string
  is_active: boolean
  plan: string
  sub_status: string
  wa_status: string
  created_at: string
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    active:        { label: 'نشط',       cls: 'bg-green-100 text-green-700' },
    trialing:      { label: 'تجربة',     cls: 'bg-blue-100 text-blue-700'  },
    canceled:      { label: 'ملغى',      cls: 'bg-red-100 text-red-700'    },
    none:          { label: 'بلا باقة',  cls: 'bg-slate-100 text-slate-500'},
    connected:     { label: 'مربوط',     cls: 'bg-green-100 text-green-700'},
    not_connected: { label: 'غير مربوط', cls: 'bg-slate-100 text-slate-400'},
    pending:       { label: 'معلق',      cls: 'bg-yellow-100 text-yellow-700'},
  }
  const s = map[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

type AccessState = 'idle' | 'requesting' | 'requested' | 'entering' | 'has_access'

export default function AdminMerchants() {
  const [merchants, setMerchants]         = useState<Merchant[]>([])
  const [loading, setLoading]             = useState(true)
  const [search, setSearch]               = useState('')
  const [accessState, setAccessState]     = useState<Record<number, AccessState>>({})
  const [toggling, setToggling]           = useState<number | null>(null)

  useEffect(() => {
    fetch(`${API_BASE}/admin/stats`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then(r => r.json())
      .then(d => setMerchants(d.all_merchants ?? d.recent_merchants ?? []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const filtered = merchants.filter(m =>
    !search ||
    m.store_name?.toLowerCase().includes(search.toLowerCase()) ||
    m.email?.toLowerCase().includes(search.toLowerCase())
  )

  const setMState = (id: number, state: AccessState) =>
    setAccessState(prev => ({ ...prev, [id]: state }))

  const handleEnter = async (m: Merchant) => {
    const state = accessState[m.id] ?? 'idle'

    // If already has access — try to enter directly
    if (state === 'has_access') {
      const tid = m.tenant_id
      if (!tid) { alert('هذا التاجر لا يملك متجراً مرتبطاً.'); return }
      setMState(m.id, 'entering')
      try {
        const res = await fetch(`${API_BASE}/admin/impersonate/${tid}`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${getToken()}` },
        })
        const data = await res.json()
        if (!res.ok) {
          alert(data.detail || 'فشل الدخول')
          setMState(m.id, 'idle')
          return
        }
        startImpersonation(data.access_token, m.store_name || m.email, m.email)
        window.location.href = '/overview'
      } catch {
        alert('حدث خطأ أثناء الدخول')
        setMState(m.id, 'idle')
      }
      return
    }

    const tid = m.tenant_id
    if (!tid) {
      alert('هذا التاجر لا يملك متجراً مرتبطاً بعد.')
      return
    }

    // First: try to enter (in case merchant already enabled access)
    setMState(m.id, 'entering')
    try {
      const res = await fetch(`${API_BASE}/admin/impersonate/${tid}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      const data = await res.json()
      if (res.ok) {
        startImpersonation(data.access_token, m.store_name || m.email, m.email)
        window.location.href = '/overview'
        return
      }
      // 403 = no access granted yet — send request
      if (res.status === 403) {
        setMState(m.id, 'requesting')
        const reqRes = await fetch(`${API_BASE}/admin/request-access/${tid}`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${getToken()}` },
        })
        const reqData = await reqRes.json()
        if (reqRes.status === 409 && reqData.detail?.includes('منح الوصول مسبقاً')) {
          // Race condition: access was just granted
          setMState(m.id, 'has_access')
          handleEnter(m)
          return
        }
        if (!reqRes.ok) {
          alert(reqData.detail || 'فشل إرسال الطلب')
          setMState(m.id, 'idle')
          return
        }
        setMState(m.id, 'requested')
      } else {
        alert(data.detail || 'فشل الدخول')
        setMState(m.id, 'idle')
      }
    } catch {
      alert('حدث خطأ')
      setMState(m.id, 'idle')
    }
  }

  const handleToggle = async (m: Merchant) => {
    setToggling(m.id)
    const action = m.is_active ? 'suspend' : 'activate'
    try {
      const res = await fetch(`${API_BASE}/admin/merchants/${m.id}/${action}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!res.ok) throw new Error('فشل التحديث')
      setMerchants(prev => prev.map(x => x.id === m.id ? { ...x, is_active: !x.is_active } : x))
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : 'حدث خطأ')
    } finally {
      setToggling(null)
    }
  }

  function AccessButton({ m }: { m: Merchant }) {
    const state = accessState[m.id] ?? 'idle'

    if (state === 'entering' || state === 'requesting') {
      return (
        <div className="p-1.5 rounded-lg bg-amber-50 text-amber-500">
          <div className="w-3.5 h-3.5 border border-amber-500 border-t-transparent rounded-full animate-spin" />
        </div>
      )
    }

    if (state === 'requested') {
      return (
        <div className="flex items-center gap-1 px-2 py-1 rounded-lg bg-blue-50 text-blue-600 text-xs font-medium">
          <Clock className="w-3 h-3" />
          <span>بانتظار الموافقة</span>
        </div>
      )
    }

    if (state === 'has_access') {
      return (
        <button
          onClick={() => handleEnter(m)}
          title="دخول للمتجر"
          className="flex items-center gap-1 px-2 py-1 rounded-lg bg-green-50 hover:bg-green-100 text-green-600 text-xs font-medium transition"
        >
          <CheckCircle className="w-3 h-3" />
          <span>دخول</span>
        </button>
      )
    }

    return (
      <button
        onClick={() => handleEnter(m)}
        title="طلب الوصول أو الدخول"
        className="flex items-center gap-1 px-2 py-1 rounded-lg bg-amber-50 hover:bg-amber-100 text-amber-600 text-xs font-medium transition"
      >
        <Send className="w-3 h-3" />
        <span>طلب وصول</span>
      </button>
    )
  }

  return (
    <div className="p-6 space-y-5" dir="rtl">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-500 flex items-center justify-center shadow-lg shadow-amber-500/30">
            <Users className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">إدارة التجار</h1>
            <p className="text-slate-400 text-xs">{merchants.length} تاجر مسجل</p>
          </div>
        </div>

        <div className="relative">
          <Search className="w-4 h-4 text-slate-400 absolute right-3 top-1/2 -translate-y-1/2" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="ابحث باسم المتجر أو البريد..."
            className="pr-9 pl-4 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-400 w-64"
          />
        </div>
      </div>

      {/* Info banner */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-100 rounded-xl px-4 py-3">
        <LogIn className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
        <p className="text-xs text-blue-700">
          لدخول لوحة أي تاجر، اضغط <strong>طلب وصول</strong> — سيصل إشعار للتاجر في لوحته ويمكنه الموافقة أو الرفض.
          الوصول مؤقت ومُسجَّل بالكامل.
        </p>
      </div>

      {/* Table */}
      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-40">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-amber-500" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-16 text-slate-400">
            <Users className="w-10 h-10 mx-auto mb-3 text-slate-200" />
            <p>لا يوجد تجار</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-100">
                  {['المتجر', 'البريد', 'الباقة', 'الاشتراك', 'واتساب', 'تاريخ التسجيل', 'إجراءات'].map(h => (
                    <th key={h} className="text-right px-4 py-3 text-xs font-semibold text-slate-500">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {filtered.map(m => (
                  <tr key={m.id} className={`hover:bg-slate-50/50 transition-colors ${!m.is_active ? 'opacity-60' : ''}`}>
                    <td className="px-4 py-3">
                      <p className="font-semibold text-slate-700">{m.store_name || '—'}</p>
                      {!m.is_active && <span className="text-[10px] text-red-500 font-medium">موقوف</span>}
                    </td>
                    <td className="px-4 py-3 text-slate-500">{m.email}</td>
                    <td className="px-4 py-3">
                      <span className="text-slate-700 font-medium">{m.plan || '—'}</span>
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={m.sub_status ?? 'none'} />
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge status={m.wa_status ?? 'not_connected'} />
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {m.created_at ? new Date(m.created_at).toLocaleDateString('ar-SA') : '—'}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <AccessButton m={m} />
                        <button
                          onClick={() => handleToggle(m)}
                          disabled={toggling === m.id}
                          title={m.is_active ? 'إيقاف' : 'تفعيل'}
                          className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50"
                        >
                          {toggling === m.id
                            ? <div className="w-3.5 h-3.5 border border-slate-400 border-t-transparent rounded-full animate-spin" />
                            : m.is_active
                              ? <ToggleRight className="w-3.5 h-3.5 text-green-500" />
                              : <ToggleLeft className="w-3.5 h-3.5" />}
                        </button>
                      </div>
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
