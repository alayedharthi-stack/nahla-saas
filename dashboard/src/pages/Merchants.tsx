import { useEffect, useState } from 'react'
import { UserPlus, ToggleLeft, ToggleRight, Trash2, Loader2, Store, Link2, Copy, CheckCircle, LogIn } from 'lucide-react'
import { merchantsApi, type Merchant } from '../api/merchants'
import { apiCall, API_BASE } from '../api/client'
import { getToken, startImpersonation } from '../auth'
import { useLanguage } from '../i18n/context'

interface FormState {
  email: string
  password: string
  store_name: string
  phone: string
}

const EMPTY_FORM: FormState = { email: '', password: '', store_name: '', phone: '' }

export default function Merchants() {
  const { t } = useLanguage()
  const m = t(tr => tr.merchants)
  const [merchants, setMerchants] = useState<Merchant[]>([])
  const [loading,   setLoading]   = useState(true)
  const [error,     setError]     = useState('')
  const [showForm,  setShowForm]  = useState(false)
  const [form,      setForm]      = useState<FormState>(EMPTY_FORM)
  const [saving,       setSaving]       = useState(false)
  const [formError,    setFormError]    = useState('')
  const [inviteEmail,  setInviteEmail]  = useState('')
  const [inviteUrl,    setInviteUrl]    = useState('')
  const [inviting,     setInviting]     = useState(false)
  const [showInvite,   setShowInvite]   = useState(false)
  const [copied,       setCopied]       = useState(false)
  const [entering,     setEntering]     = useState<number | null>(null)

  const load = () => {
    setLoading(true)
    merchantsApi.list()
      .then(r => setMerchants(r.merchants))
      .catch(() => setError('تعذّر تحميل قائمة التجار'))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError('')
    setSaving(true)
    try {
      const newMerchant = await merchantsApi.create(form)
      setMerchants(prev => [newMerchant, ...prev])
      setForm(EMPTY_FORM)
      setShowForm(false)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'حدث خطأ أثناء الإنشاء'
      setFormError(msg)
    } finally {
      setSaving(false)
    }
  }

  const handleToggle = async (id: number) => {
    try {
      const updated = await merchantsApi.toggle(id)
      setMerchants(prev => prev.map(m => m.id === id ? updated : m))
    } catch {
      alert('تعذّر تغيير الحالة')
    }
  }

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault()
    setInviting(true)
    setInviteUrl('')
    try {
      const data = await apiCall<{ invite_url: string }>('/admin/invitations', {
        method: 'POST',
        body: JSON.stringify({ email: inviteEmail }),
      })
      setInviteUrl(data.invite_url)
    } catch {
      alert('تعذّر إنشاء رابط الدعوة')
    } finally {
      setInviting(false)
    }
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(inviteUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleEnterStore = async (m: Merchant) => {
    setEntering(m.id)
    try {
      const token = getToken()
      const res = await fetch(`${API_BASE}/admin/merchants/${m.id}/impersonate`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error('فشل الدخول للمتجر')
      const data = await res.json()
      startImpersonation(data.access_token, m.store_name || m.email, m.email)
      window.location.href = '/overview'
    } catch (e: unknown) {
      alert(e instanceof Error ? e.message : 'حدث خطأ')
    } finally {
      setEntering(null)
    }
  }

  const handleDelete = async (id: number, email: string) => {
    if (!confirm(`${m.confirmDel} ${email}?`)) return
    try {
      await merchantsApi.remove(id)
      setMerchants(prev => prev.filter(m => m.id !== id))
    } catch {
      alert('تعذّر حذف الحساب')
    }
  }

  return (
    <div className="p-6 space-y-6" dir="rtl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-900">{m.title}</h1>
          <p className="text-sm text-slate-500 mt-0.5">{m.subtitle}</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { setShowInvite(v => !v); setInviteUrl('') }}
            className="flex items-center gap-2 border border-slate-300 hover:bg-slate-50 text-slate-700 text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            <Link2 className="w-4 h-4" />
            {m.sendInvite}
          </button>
          <button
            onClick={() => { setShowForm(true); setFormError('') }}
            className="flex items-center gap-2 bg-brand-500 hover:bg-brand-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            <UserPlus className="w-4 h-4" />
            {m.newMerchant}
          </button>
        </div>
      </div>

      {/* Invite form */}
      {showInvite && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <h2 className="font-semibold text-slate-800 mb-1">{m.inviteTitle}</h2>
          <p className="text-xs text-slate-500 mb-4">{m.inviteDesc}</p>
          <form onSubmit={handleInvite} className="flex gap-3">
            <input
              type="email"
              dir="ltr"
              value={inviteEmail}
              onChange={e => setInviteEmail(e.target.value)}
              placeholder={m.invitePh}
              className="flex-1 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
            <button
              type="submit"
              disabled={inviting}
              className="flex items-center gap-2 px-4 py-2 text-sm bg-brand-500 hover:bg-brand-600 disabled:opacity-60 text-white font-medium rounded-lg transition-colors"
            >
              {inviting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Link2 className="w-3.5 h-3.5" />}
              {m.createLink}
            </button>
          </form>
          {inviteUrl && (
            <div className="mt-4 flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2.5">
              <span className="flex-1 text-xs font-mono text-emerald-800 truncate" dir="ltr">{inviteUrl}</span>
              <button onClick={handleCopy} className="shrink-0 text-emerald-600 hover:text-emerald-800 transition-colors">
                {copied ? <CheckCircle className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Create form */}
      {showForm && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <h2 className="font-semibold text-slate-800 mb-4">{m.formTitle}</h2>
          {formError && (
            <div className="mb-3 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {formError}
            </div>
          )}
          <form onSubmit={handleCreate} className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">{m.storeName}</label>
              <input
                required
                value={form.store_name}
                onChange={e => setForm(f => ({ ...f, store_name: e.target.value }))}
                placeholder={m.storeNamePh}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">{m.emailCol}</label>
              <input
                required
                type="email"
                dir="ltr"
                value={form.email}
                onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                placeholder={m.emailPh}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">{t(tr => tr.common.password)}</label>
              <input
                required
                type="password"
                dir="ltr"
                value={form.password}
                onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                placeholder={m.passwordPh}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">{t(tr => tr.common.phone)} ({t(tr => tr.common.optional)})</label>
              <input
                type="tel"
                dir="ltr"
                value={form.phone}
                onChange={e => setForm(f => ({ ...f, phone: e.target.value }))}
                placeholder={m.phonePh}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div className="sm:col-span-2 flex gap-3 justify-end">
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg"
              >
                {t(tr => tr.actions.cancel)}
              </button>
              <button
                type="submit"
                disabled={saving}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-brand-500 hover:bg-brand-600 disabled:opacity-60 text-white font-medium rounded-lg transition-colors"
              >
                {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                {saving ? m.creating : m.createFirst}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Table */}
      {loading ? (
        <div className="flex justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin text-slate-400" />
        </div>
      ) : error ? (
        <div className="text-center py-16 text-red-500">{error}</div>
      ) : merchants.length === 0 ? (
        <div className="text-center py-16 space-y-2">
          <Store className="w-10 h-10 text-slate-300 mx-auto" />
          <p className="text-slate-500 text-sm">{m.noMerchants}</p>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">{m.storeName}</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">{m.emailCol}</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">{m.tenantId}</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">{m.createdAt}</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">{m.statusCol}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {merchants.map(m => (
                <tr key={m.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3 font-medium text-slate-800">{m.store_name || '—'}</td>
                  <td className="px-4 py-3 text-slate-600 font-mono text-xs" dir="ltr">{m.email}</td>
                  <td className="px-4 py-3 text-slate-500">{m.tenant_id}</td>
                  <td className="px-4 py-3 text-slate-500">
                    {m.created_at ? new Date(m.created_at).toLocaleDateString('ar-SA') : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                      m.is_active
                        ? 'bg-green-100 text-green-700'
                        : 'bg-slate-100 text-slate-500'
                    }`}>
                      {m.is_active ? t(tr => tr.common.active) : t(tr => tr.common.inactive)}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2 justify-end">
                      <button
                        onClick={() => handleEnterStore(m)}
                        disabled={entering === m.id}
                        title="الدخول كالتاجر"
                        className="flex items-center gap-1 text-xs bg-amber-500 hover:bg-amber-600 text-white px-2.5 py-1.5 rounded-lg transition disabled:opacity-50"
                      >
                        {entering === m.id
                          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          : <LogIn className="w-3.5 h-3.5" />}
                        دخول
                      </button>
                      <button
                        onClick={() => handleToggle(m.id)}
                        title={m.is_active ? 'تعطيل' : 'تفعيل'}
                        className="text-slate-400 hover:text-brand-500 transition-colors"
                      >
                        {m.is_active
                          ? <ToggleRight className="w-5 h-5 text-green-500" />
                          : <ToggleLeft  className="w-5 h-5" />}
                      </button>
                      <button
                        onClick={() => handleDelete(m.id, m.email)}
                        title="حذف"
                        className="text-slate-400 hover:text-red-500 transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
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
  )
}
