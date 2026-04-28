/**
 * AdminSallaActivations.tsx  —  /admin/salla-activations
 * -------------------------------------------------------
 * Admin panel for activating Salla merchants whose token was received by email
 * (per Salla's email-notification flow).
 *
 * Features:
 *   • Form to activate a merchant by pasting token + email
 *   • Full list of all Salla integrations with status
 *   • Copy-activation link for each activated merchant
 */
import { useEffect, useState, useCallback } from 'react'
import { API_BASE } from '../api/client'
import { getToken } from '../auth'
import {
  Mail, CheckCircle, XCircle, RefreshCw, Copy, ExternalLink,
  User, Store, Clock, Zap, Loader2,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Activation {
  integration_id: number
  tenant_id: number
  tenant_name: string
  store_id: string
  store_name: string
  email: string
  enabled: boolean
  activated_from_email: boolean
  activated_at: string
  last_seen: string
}

interface ActivateResult {
  access_token: string
  tenant_id: number
  store_name: string
  email: string
  is_new: boolean
  status: string
}

// ── Activate Form ─────────────────────────────────────────────────────────────

function ActivateForm({ onSuccess }: { onSuccess: () => void }) {
  const [token, setToken]     = useState('')
  const [email, setEmail]     = useState('')
  const [storeId, setStoreId] = useState('')
  const [busy, setBusy]       = useState(false)
  const [result, setResult]   = useState<ActivateResult | null>(null)
  const [error, setError]     = useState('')
  const [copied, setCopied]   = useState(false)

  const submit = async () => {
    if (!token.trim()) { setError('التوكن مطلوب'); return }
    setBusy(true); setError(''); setResult(null)

    try {
      const res = await fetch(`${API_BASE}/api/salla/activate-from-email`, {
        method:  'POST',
        headers: {
          'Content-Type':  'application/json',
          Authorization:   `Bearer ${getToken()}`,
        },
        body: JSON.stringify({
          token:          token.trim(),
          merchant_email: email.trim().toLowerCase(),
          store_id:       storeId.trim() || undefined,
        }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail || `Error ${res.status}`); return }
      setResult(data)
      onSuccess()
    } catch (e) {
      setError('تعذر الاتصال بالخادم')
    } finally {
      setBusy(false)
    }
  }

  const copyLink = () => {
    if (!result?.access_token) return
    // Build a direct login link using the token via salla-callback mechanism
    const url = `${window.location.origin}/salla-callback?token=${result.access_token}&status=connected&new=${result.is_new ? '1' : '0'}&store=${result.tenant_id}`
    navigator.clipboard.writeText(url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div
      className="rounded-2xl p-6"
      style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
    >
      <div className="flex items-center gap-2 mb-5">
        <Mail className="w-4 h-4 text-amber-400" />
        <h3 className="text-white font-bold text-base">تفعيل تاجر من الإيميل</h3>
      </div>

      {result ? (
        /* ── Success ── */
        <div className="space-y-4">
          <div
            className="flex items-start gap-3 p-4 rounded-xl"
            style={{ background: 'rgba(74,222,128,0.08)', border: '1px solid rgba(74,222,128,0.2)' }}
          >
            <CheckCircle className="w-5 h-5 text-green-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <p className="text-green-400 font-bold text-sm">تم التفعيل بنجاح</p>
              <p className="text-slate-400 text-xs">
                المتجر: <strong className="text-slate-200">{result.store_name || '—'}</strong>
                &nbsp;·&nbsp; الإيميل: <strong className="text-slate-200">{result.email}</strong>
                &nbsp;·&nbsp; tenant_id: <strong className="text-slate-200">{result.tenant_id}</strong>
              </p>
              {result.is_new && (
                <span className="inline-block text-xs bg-amber-400/10 text-amber-400 border border-amber-400/20 px-2 py-0.5 rounded">
                  حساب جديد
                </span>
              )}
            </div>
          </div>

          <button
            onClick={copyLink}
            className="flex items-center gap-2 w-full py-3 rounded-xl text-sm font-bold justify-center"
            style={{ background: copied ? 'rgba(74,222,128,0.1)' : 'rgba(245,158,11,0.1)', color: copied ? '#4ade80' : '#f59e0b', border: `1px solid ${copied ? 'rgba(74,222,128,0.2)' : 'rgba(245,158,11,0.2)'}` }}
          >
            <Copy className="w-3.5 h-3.5" />
            {copied ? 'تم النسخ!' : 'نسخ رابط تسجيل الدخول للتاجر'}
          </button>

          <button
            onClick={() => { setResult(null); setToken(''); setEmail(''); setStoreId('') }}
            className="w-full py-2 text-slate-500 text-sm hover:text-slate-400"
          >
            تفعيل تاجر آخر
          </button>
        </div>
      ) : (
        /* ── Form ── */
        <div className="space-y-4">
          <div className="space-y-1">
            <label className="text-slate-400 text-xs font-medium">توكن سلة (مطلوب)</label>
            <textarea
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder="الصق توكن سلة المرسل بالإيميل هنا..."
              rows={3}
              dir="ltr"
              className="w-full rounded-xl px-4 py-3 text-xs font-mono text-slate-200 resize-none"
              style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', outline: 'none' }}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label className="text-slate-400 text-xs font-medium">إيميل التاجر (اختياري)</label>
              <input
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="merchant@store.com"
                type="email"
                dir="ltr"
                className="w-full rounded-xl px-4 py-2.5 text-sm text-slate-200"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', outline: 'none' }}
              />
            </div>
            <div className="space-y-1">
              <label className="text-slate-400 text-xs font-medium">معرف المتجر (اختياري)</label>
              <input
                value={storeId}
                onChange={e => setStoreId(e.target.value)}
                placeholder="12345"
                dir="ltr"
                className="w-full rounded-xl px-4 py-2.5 text-sm text-slate-200"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', outline: 'none' }}
              />
            </div>
          </div>

          {error && (
            <div className="flex items-center gap-2 text-red-400 text-xs">
              <XCircle className="w-3.5 h-3.5" />
              {error}
            </div>
          )}

          <button
            onClick={submit}
            disabled={busy || !token.trim()}
            className="flex items-center justify-center gap-2 w-full py-3 rounded-xl font-bold text-sm disabled:opacity-50"
            style={{ background: '#f59e0b', color: '#0f172a' }}
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
            {busy ? 'جاري التفعيل...' : 'تفعيل الحساب'}
          </button>
        </div>
      )}
    </div>
  )
}

// ── Activations list ──────────────────────────────────────────────────────────

function ActivationsList({ activations, loading }: { activations: Activation[]; loading: boolean }) {
  const [filter, setFilter] = useState<'all' | 'email' | 'active'>('all')
  const [search, setSearch] = useState('')

  const filtered = activations.filter(a => {
    if (filter === 'email'  && !a.activated_from_email) return false
    if (filter === 'active' && !a.enabled)              return false
    if (search) {
      const q = search.toLowerCase()
      return (
        a.email?.toLowerCase().includes(q) ||
        a.store_name?.toLowerCase().includes(q) ||
        a.store_id?.includes(q) ||
        String(a.tenant_id).includes(q)
      )
    }
    return true
  })

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="بحث بالإيميل أو المتجر..."
          className="flex-1 min-w-0 rounded-xl px-4 py-2 text-sm text-slate-200"
          style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', outline: 'none' }}
        />
        <div className="flex rounded-xl overflow-hidden border" style={{ borderColor: 'rgba(255,255,255,0.08)' }}>
          {([['all', 'الكل'], ['email', 'بالإيميل'], ['active', 'نشط']] as const).map(([v, l]) => (
            <button
              key={v}
              onClick={() => setFilter(v)}
              className="px-3 py-2 text-xs font-medium transition-colors"
              style={{
                background: filter === v ? 'rgba(245,158,11,0.15)' : 'transparent',
                color:      filter === v ? '#f59e0b' : '#94a3b8',
              }}
            >
              {l}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-6 h-6 text-slate-600 animate-spin" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-slate-600 text-sm">لا توجد نتائج</div>
      ) : (
        <div className="space-y-2">
          {filtered.map(a => (
            <div
              key={a.integration_id}
              className="flex items-start gap-4 rounded-xl p-4"
              style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)' }}
            >
              {/* Status dot */}
              <div className={`w-2.5 h-2.5 rounded-full mt-1.5 shrink-0 ${a.enabled ? 'bg-green-400' : 'bg-slate-600'}`} />

              <div className="flex-1 min-w-0 space-y-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-slate-100 font-semibold text-sm truncate">
                    {a.store_name || '—'}
                  </span>
                  {a.activated_from_email && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-400 border border-amber-400/15">
                      📧 إيميل
                    </span>
                  )}
                  {!a.enabled && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-500">
                      معطّل
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-[11px] text-slate-500 flex-wrap">
                  {a.email && (
                    <span className="flex items-center gap-1">
                      <Mail className="w-3 h-3" />{a.email}
                    </span>
                  )}
                  <span className="flex items-center gap-1">
                    <Store className="w-3 h-3" />ID: {a.store_id || '—'}
                  </span>
                  <span className="flex items-center gap-1">
                    <User className="w-3 h-3" />tenant: {a.tenant_id}
                  </span>
                  {a.last_seen && (
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />{new Date(a.last_seen).toLocaleDateString('ar-SA')}
                    </span>
                  )}
                </div>
              </div>

              <a
                href={`/admin/merchants?search=${encodeURIComponent(a.email || a.store_id || '')}`}
                className="shrink-0 text-slate-600 hover:text-slate-400 transition-colors"
                title="فتح في لوحة التجار"
              >
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            </div>
          ))}
        </div>
      )}

      <p className="text-slate-700 text-xs text-center">
        {filtered.length} من {activations.length} تفعيل
      </p>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AdminSallaActivations() {
  const [activations, setActivations] = useState<Activation[]>([])
  const [loading, setLoading]         = useState(true)
  const [error, setError]             = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const res = await fetch(`${API_BASE}/admin/salla-activations`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail || `Error ${res.status}`); return }
      setActivations(data.activations ?? [])
    } catch {
      setError('تعذر تحميل البيانات')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="space-y-6 pb-10" dir="rtl">
      <div className="flex items-start justify-between">
        <PageHeader
          title="تفعيلات سلة"
          subtitle="إدارة تفعيل تجار سلة من التوكنات المرسلة بالإيميل"
        />
        <button
          onClick={load}
          className="flex items-center gap-1.5 text-slate-400 hover:text-slate-200 text-xs mt-1"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          تحديث
        </button>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: 'إجمالي التفعيلات', value: activations.length },
          { label: 'عبر الإيميل',       value: activations.filter(a => a.activated_from_email).length },
          { label: 'نشط حالياً',         value: activations.filter(a => a.enabled).length },
        ].map(({ label, value }) => (
          <div
            key={label}
            className="rounded-xl p-4 text-center"
            style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
          >
            <p className="text-2xl font-black text-white">{value}</p>
            <p className="text-slate-500 text-xs mt-1">{label}</p>
          </div>
        ))}
      </div>

      {error && (
        <div className="flex items-center gap-2 text-red-400 text-sm p-4 rounded-xl bg-red-400/5 border border-red-400/15">
          <XCircle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Activate form */}
      <ActivateForm onSuccess={load} />

      {/* List */}
      <div
        className="rounded-2xl p-6"
        style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
      >
        <h3 className="text-white font-bold text-base mb-4">سجل التفعيلات</h3>
        <ActivationsList activations={activations} loading={loading} />
      </div>
    </div>
  )
}
