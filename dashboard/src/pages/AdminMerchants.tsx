import { useEffect, useState, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { API_BASE } from '../api/client'
import { getToken, startImpersonation } from '../auth'
import {
  Users, Search, LogIn, ToggleLeft, ToggleRight,
  Send, Clock, CheckCircle, Bell, X, Plus,
  Loader2, ExternalLink, Wifi, WifiOff,
  ShieldCheck, Eye, EyeOff,
} from 'lucide-react'

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
  created_at: string | null
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

// ── Add Merchant Modal ────────────────────────────────────────────────────────

function AddMerchantModal({ onClose, onCreated }: { onClose: () => void; onCreated: (m: Merchant) => void }) {
  const [email, setEmail]           = useState('')
  const [password, setPassword]     = useState('')
  const [storeName, setStoreName]   = useState('')
  const [showPass, setShowPass]     = useState(false)
  const [busy, setBusy]             = useState(false)
  const [error, setError]           = useState('')

  const submit = async () => {
    if (!email.trim() || !password.trim() || !storeName.trim()) {
      setError('جميع الحقول مطلوبة'); return
    }
    setBusy(true); setError('')
    try {
      const res = await fetch(`${API_BASE}/admin/merchants`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${getToken()}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim().toLowerCase(), password, store_name: storeName.trim() }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail || 'فشل إنشاء الحساب'); return }
      onCreated(data)
      onClose()
    } catch {
      setError('خطأ في الاتصال بالخادم')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" dir="rtl">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6 space-y-5">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-black text-slate-800">إضافة تاجر جديد</h2>
            <p className="text-xs text-slate-400 mt-0.5">سيُنشأ حساب وmتجر جديد فوراً</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-600">اسم المتجر <span className="text-red-500">*</span></label>
            <input
              value={storeName}
              onChange={e => setStoreName(e.target.value)}
              placeholder="مثال: متجر النور للعطور"
              className="w-full border border-slate-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-600">البريد الإلكتروني <span className="text-red-500">*</span></label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="merchant@example.com"
              className="w-full border border-slate-200 rounded-xl px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400"
              dir="ltr"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium text-slate-600">كلمة المرور <span className="text-red-500">*</span></label>
            <div className="relative">
              <input
                type={showPass ? 'text' : 'password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="8 أحرف على الأقل"
                className="w-full border border-slate-200 rounded-xl px-3 py-2.5 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400"
                dir="ltr"
              />
              <button
                type="button"
                onClick={() => setShowPass(v => !v)}
                className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
              >
                {showPass ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
        </div>

        {error && (
          <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-xl px-3 py-2">{error}</div>
        )}

        <div className="flex gap-3">
          <button onClick={onClose} className="flex-1 py-2.5 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition">
            إلغاء
          </button>
          <button
            onClick={submit}
            disabled={busy}
            className="flex-1 flex items-center justify-center gap-2 py-2.5 bg-amber-500 hover:bg-amber-600 disabled:opacity-60 text-white font-bold rounded-xl text-sm transition"
          >
            {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ الإنشاء...</> : <><Plus className="w-4 h-4" /> إنشاء الحساب</>}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Merchant Detail Drawer ────────────────────────────────────────────────────

function MerchantDrawer({
  m, onClose, onToggle, onEnter, accessState, enterBusy,
}: {
  m: Merchant
  onClose: () => void
  onToggle: () => void
  onEnter: () => void
  accessState: AccessState
  enterBusy: boolean
}) {
  const navigate = useNavigate()

  return (
    <div className="fixed inset-0 z-50 flex justify-start" dir="rtl">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="relative bg-white w-full max-w-sm h-full shadow-2xl overflow-y-auto flex flex-col">
        {/* Header */}
        <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between shrink-0">
          <div className="min-w-0">
            <h2 className="font-black text-slate-800 text-base truncate">{m.store_name || m.email}</h2>
            <p className="text-xs text-slate-400 truncate">{m.email}</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 shrink-0 mr-3">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Info cards */}
        <div className="flex-1 p-5 space-y-4">
          {/* Status strip */}
          <div className="flex flex-wrap gap-2">
            <StatusBadge status={m.sub_status ?? 'none'} />
            <StatusBadge status={m.wa_status ?? 'not_connected'} />
            {!m.is_active && (
              <span className="text-xs px-2 py-0.5 rounded-full font-medium bg-red-100 text-red-600">موقوف</span>
            )}
          </div>

          {/* Detail rows */}
          <div className="bg-slate-50 rounded-2xl divide-y divide-slate-100">
            {[
              { label: 'اسم المتجر',     value: m.store_name || '—' },
              { label: 'Tenant ID',       value: m.tenant_id ? `#${m.tenant_id}` : '—', mono: true },
              { label: 'الباقة',          value: m.plan || '—' },
              { label: 'الاشتراك',        value: m.sub_status || '—' },
              { label: 'تاريخ التسجيل',  value: m.created_at ? new Date(m.created_at).toLocaleDateString('ar-SA') : '—' },
            ].map(row => (
              <div key={row.label} className="flex items-center justify-between px-4 py-2.5 text-sm">
                <span className="text-slate-400">{row.label}</span>
                <span className={`font-medium text-slate-700 ${row.mono ? 'font-mono text-xs' : ''}`}>{row.value}</span>
              </div>
            ))}
          </div>

          {/* WhatsApp status */}
          <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border ${m.wa_status === 'connected' ? 'border-emerald-200 bg-emerald-50' : 'border-slate-200 bg-slate-50'}`}>
            {m.wa_status === 'connected'
              ? <Wifi className="w-4 h-4 text-emerald-600 shrink-0" />
              : <WifiOff className="w-4 h-4 text-slate-400 shrink-0" />
            }
            <div>
              <p className="text-xs font-medium text-slate-700">واتساب</p>
              <p className={`text-xs ${m.wa_status === 'connected' ? 'text-emerald-700' : 'text-slate-400'}`}>
                {m.wa_status === 'connected' ? 'مربوط ومفعّل' : 'غير مربوط'}
              </p>
            </div>
            {m.wa_status !== 'connected' && m.tenant_id && (
              <button
                onClick={() => navigate('/admin/troubleshooting')}
                className="mr-auto text-xs text-violet-600 hover:underline flex items-center gap-1"
              >
                ربط <ExternalLink className="w-3 h-3" />
              </button>
            )}
          </div>

          {/* Actions */}
          <div className="space-y-2">
            {/* Enter as merchant */}
            {m.tenant_id && (
              <button
                onClick={onEnter}
                disabled={enterBusy}
                className={`w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-bold transition
                  ${accessState === 'has_access'
                    ? 'bg-green-500 hover:bg-green-600 text-white'
                    : accessState === 'requested'
                    ? 'bg-blue-50 text-blue-600 cursor-not-allowed'
                    : 'bg-amber-500 hover:bg-amber-600 text-white'
                  } disabled:opacity-60`}
              >
                {enterBusy ? (
                  <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ الدخول...</>
                ) : accessState === 'has_access' ? (
                  <><CheckCircle className="w-4 h-4" /> دخول للوحة التاجر</>
                ) : accessState === 'requested' ? (
                  <><Clock className="w-4 h-4" /> بانتظار موافقة التاجر</>
                ) : (
                  <><Send className="w-4 h-4" /> طلب وصول للمتجر</>
                )}
              </button>
            )}

            {/* Troubleshooting */}
            {m.tenant_id && (
              <button
                onClick={() => navigate('/admin/troubleshooting')}
                className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-medium text-slate-600 border border-slate-200 hover:bg-slate-50 transition"
              >
                <ShieldCheck className="w-4 h-4" /> تشخيص المشاكل
              </button>
            )}

            {/* Toggle active */}
            <button
              onClick={onToggle}
              className={`w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-medium transition
                ${m.is_active
                  ? 'border border-red-200 text-red-500 hover:bg-red-50'
                  : 'border border-green-200 text-green-600 hover:bg-green-50'
                }`}
            >
              {m.is_active
                ? <><ToggleLeft className="w-4 h-4" /> إيقاف الحساب</>
                : <><ToggleRight className="w-4 h-4" /> تفعيل الحساب</>
              }
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdminMerchants() {
  const [merchants, setMerchants]         = useState<Merchant[]>([])
  const [loading, setLoading]             = useState(true)
  const [search, setSearch]               = useState('')
  const [accessState, setAccessState]     = useState<Record<number, AccessState>>({})
  const [toggling, setToggling]           = useState<number | null>(null)
  const [approvedAlerts, setApprovedAlerts] = useState<{id: number; name: string}[]>([])
  const [selectedMerchant, setSelectedMerchant] = useState<Merchant | null>(null)
  const [showAddModal, setShowAddModal]   = useState(false)
  const pollingRef                        = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadMerchants = () => {
    setLoading(true)
    adminApi.stats()
      .then(d => setMerchants(d.all_merchants ?? d.recent_merchants ?? []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => { loadMerchants() }, [])

  // Poll every 15s for merchants in "requested" state to detect approval
  const checkApprovals = useCallback(async () => {
    const requested = Object.entries(accessState)
      .filter(([, s]) => s === 'requested')
      .map(([id]) => parseInt(id))
    if (requested.length === 0) return

    for (const userId of requested) {
      const m = merchants.find(x => x.id === userId)
      if (!m?.tenant_id) continue
      try {
        const res = await fetch(`${API_BASE}/admin/impersonate/${m.tenant_id}`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${getToken()}` },
        })
        if (res.ok) {
          setAccessState(prev => ({ ...prev, [userId]: 'has_access' }))
          setApprovedAlerts(prev => [...prev, { id: userId, name: m.store_name || m.email }])
          setTimeout(() => {
            setApprovedAlerts(prev => prev.filter(a => a.id !== userId))
          }, 8000)
        }
      } catch { /* ignore */ }
    }
  }, [accessState, merchants])

  useEffect(() => {
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(checkApprovals, 15_000)
    return () => { if (pollingRef.current) clearInterval(pollingRef.current) }
  }, [checkApprovals])

  const filtered = merchants.filter(m =>
    !search ||
    m.store_name?.toLowerCase().includes(search.toLowerCase()) ||
    m.email?.toLowerCase().includes(search.toLowerCase())
  )

  const setMState = (id: number, state: AccessState) =>
    setAccessState(prev => ({ ...prev, [id]: state }))

  const handleEnter = useCallback(async (m: Merchant) => {
    const state = accessState[m.id] ?? 'idle'

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
        if (!res.ok) { alert(data.detail || 'فشل الدخول'); setMState(m.id, 'idle'); return }
        startImpersonation(data.access_token, m.store_name || m.email, m.email)
        window.location.href = '/overview'
      } catch { alert('حدث خطأ أثناء الدخول'); setMState(m.id, 'idle') }
      return
    }

    const tid = m.tenant_id
    if (!tid) { alert('هذا التاجر لا يملك متجراً مرتبطاً بعد.'); return }

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
      if (res.status === 403) {
        setMState(m.id, 'requesting')
        const reqRes = await fetch(`${API_BASE}/admin/request-access/${tid}`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${getToken()}` },
        })
        const reqData = await reqRes.json()
        if (reqRes.status === 409 && reqData.detail?.includes('منح الوصول مسبقاً')) {
          setMState(m.id, 'has_access')
          handleEnter(m)
          return
        }
        if (!reqRes.ok) { alert(reqData.detail || 'فشل إرسال الطلب'); setMState(m.id, 'idle'); return }
        setMState(m.id, 'requested')
      } else {
        alert(data.detail || 'فشل الدخول')
        setMState(m.id, 'idle')
      }
    } catch { alert('حدث خطأ'); setMState(m.id, 'idle') }
  }, [accessState])

  const handleToggle = async (m: Merchant) => {
    setToggling(m.id)
    try {
      const res = await fetch(`${API_BASE}/admin/merchants/${m.id}/toggle`, {
        method: 'PUT',
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!res.ok) throw new Error('فشل التحديث')
      const updated = { ...m, is_active: !m.is_active }
      setMerchants(prev => prev.map(x => x.id === m.id ? updated : x))
      if (selectedMerchant?.id === m.id) setSelectedMerchant(updated)
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : 'حدث خطأ')
    } finally {
      setToggling(null)
    }
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

        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search className="w-4 h-4 text-slate-400 absolute right-3 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="ابحث باسم المتجر أو البريد..."
              className="pr-9 pl-4 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-400 w-60"
            />
          </div>
          <button
            onClick={() => setShowAddModal(true)}
            className="flex items-center gap-1.5 px-4 py-2 bg-amber-500 hover:bg-amber-600 text-white font-bold rounded-xl text-sm transition shadow-sm"
          >
            <Plus className="w-4 h-4" /> إضافة تاجر
          </button>
        </div>
      </div>

      {/* Approval notifications */}
      {approvedAlerts.map(a => (
        <div key={a.id} className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-4 py-3">
          <Bell className="w-4 h-4 text-green-600 shrink-0" />
          <p className="text-sm text-green-800 font-medium">
            ✓ وافق <strong>{a.name}</strong> على طلب الوصول — يمكنك الدخول الآن
          </p>
        </div>
      ))}

      {/* Info banner */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-100 rounded-xl px-4 py-3">
        <LogIn className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
        <p className="text-xs text-blue-700">
          اضغط على اسم أي تاجر لفتح تفاصيله والإجراءات المتاحة.
          لدخول لوحة التاجر اضغط <strong>طلب وصول</strong> — سيصل إشعار للتاجر ويمكنه الموافقة أو الرفض.
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
                {filtered.map(m => {
                  const state = accessState[m.id] ?? 'idle'
                  return (
                    <tr
                      key={m.id}
                      className={`hover:bg-amber-50/30 transition-colors cursor-pointer ${!m.is_active ? 'opacity-60' : ''} ${selectedMerchant?.id === m.id ? 'bg-amber-50/50' : ''}`}
                      onClick={() => setSelectedMerchant(m)}
                    >
                      <td className="px-4 py-3">
                        <p className="font-semibold text-amber-700 hover:underline">{m.store_name || '—'}</p>
                        {!m.is_active && <span className="text-[10px] text-red-500 font-medium">موقوف</span>}
                      </td>
                      <td className="px-4 py-3 text-slate-500 text-xs">{m.email}</td>
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
                      <td className="px-4 py-3" onClick={e => e.stopPropagation()}>
                        <div className="flex items-center gap-2">
                          {/* Quick enter button */}
                          {(state === 'entering' || state === 'requesting') ? (
                            <div className="p-1.5 rounded-lg bg-amber-50 text-amber-500">
                              <div className="w-3.5 h-3.5 border border-amber-500 border-t-transparent rounded-full animate-spin" />
                            </div>
                          ) : state === 'requested' ? (
                            <div className="flex items-center gap-1 px-2 py-1 rounded-lg bg-blue-50 text-blue-600 text-xs font-medium">
                              <Clock className="w-3 h-3" /><span>بانتظار</span>
                            </div>
                          ) : state === 'has_access' ? (
                            <button
                              onClick={() => handleEnter(m)}
                              className="flex items-center gap-1 px-2 py-1 rounded-lg bg-green-50 hover:bg-green-100 text-green-600 text-xs font-medium transition"
                            >
                              <CheckCircle className="w-3 h-3" /><span>دخول</span>
                            </button>
                          ) : (
                            <button
                              onClick={() => handleEnter(m)}
                              className="flex items-center gap-1 px-2 py-1 rounded-lg bg-amber-50 hover:bg-amber-100 text-amber-600 text-xs font-medium transition"
                            >
                              <Send className="w-3 h-3" /><span>طلب وصول</span>
                            </button>
                          )}
                          {/* Toggle */}
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
                                : <ToggleLeft className="w-3.5 h-3.5" />
                            }
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Detail drawer */}
      {selectedMerchant && (
        <MerchantDrawer
          m={selectedMerchant}
          onClose={() => setSelectedMerchant(null)}
          onToggle={() => handleToggle(selectedMerchant)}
          onEnter={() => handleEnter(selectedMerchant)}
          accessState={accessState[selectedMerchant.id] ?? 'idle'}
          enterBusy={accessState[selectedMerchant.id] === 'entering' || accessState[selectedMerchant.id] === 'requesting'}
        />
      )}

      {/* Add merchant modal */}
      {showAddModal && (
        <AddMerchantModal
          onClose={() => setShowAddModal(false)}
          onCreated={(m) => {
            setMerchants(prev => [m, ...prev])
          }}
        />
      )}
    </div>
  )
}
