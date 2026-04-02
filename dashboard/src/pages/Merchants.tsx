import { useEffect, useState } from 'react'
import { UserPlus, ToggleLeft, ToggleRight, Trash2, Loader2, Store } from 'lucide-react'
import { merchantsApi, type Merchant } from '../api/merchants'

interface FormState {
  email: string
  password: string
  store_name: string
  phone: string
}

const EMPTY_FORM: FormState = { email: '', password: '', store_name: '', phone: '' }

export default function Merchants() {
  const [merchants, setMerchants] = useState<Merchant[]>([])
  const [loading,   setLoading]   = useState(true)
  const [error,     setError]     = useState('')
  const [showForm,  setShowForm]  = useState(false)
  const [form,      setForm]      = useState<FormState>(EMPTY_FORM)
  const [saving,    setSaving]    = useState(false)
  const [formError, setFormError] = useState('')

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
      const msg = err instanceof Error ? err.message : ''
      setFormError(msg.includes('400') ? 'البريد الإلكتروني مسجَّل مسبقاً' : 'حدث خطأ أثناء الإنشاء')
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

  const handleDelete = async (id: number, email: string) => {
    if (!confirm(`هل تريد حذف حساب ${email} نهائياً؟`)) return
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
          <h1 className="text-xl font-bold text-slate-900">إدارة التجار</h1>
          <p className="text-sm text-slate-500 mt-0.5">أنشئ حسابات للتجار وتحكّم في صلاحياتهم</p>
        </div>
        <button
          onClick={() => { setShowForm(true); setFormError('') }}
          className="flex items-center gap-2 bg-brand-500 hover:bg-brand-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
        >
          <UserPlus className="w-4 h-4" />
          تاجر جديد
        </button>
      </div>

      {/* Create form */}
      {showForm && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm">
          <h2 className="font-semibold text-slate-800 mb-4">إضافة تاجر جديد</h2>
          {formError && (
            <div className="mb-3 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {formError}
            </div>
          )}
          <form onSubmit={handleCreate} className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">اسم المتجر</label>
              <input
                required
                value={form.store_name}
                onChange={e => setForm(f => ({ ...f, store_name: e.target.value }))}
                placeholder="متجر أحمد للملابس"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">البريد الإلكتروني</label>
              <input
                required
                type="email"
                dir="ltr"
                value={form.email}
                onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                placeholder="merchant@example.com"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">كلمة المرور</label>
              <input
                required
                type="password"
                dir="ltr"
                value={form.password}
                onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                placeholder="كلمة مرور قوية"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">رقم الهاتف (اختياري)</label>
              <input
                type="tel"
                dir="ltr"
                value={form.phone}
                onChange={e => setForm(f => ({ ...f, phone: e.target.value }))}
                placeholder="+966 5x xxx xxxx"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </div>
            <div className="sm:col-span-2 flex gap-3 justify-end">
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="px-4 py-2 text-sm text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg"
              >
                إلغاء
              </button>
              <button
                type="submit"
                disabled={saving}
                className="flex items-center gap-2 px-4 py-2 text-sm bg-brand-500 hover:bg-brand-600 disabled:opacity-60 text-white font-medium rounded-lg transition-colors"
              >
                {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                {saving ? 'جارٍ الحفظ...' : 'إنشاء الحساب'}
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
          <p className="text-slate-500 text-sm">لا يوجد تجار بعد — أنشئ أول حساب</p>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">المتجر</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">البريد</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">Tenant ID</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">تاريخ الإنشاء</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-slate-500">الحالة</th>
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
                      {m.is_active ? 'نشط' : 'معطّل'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2 justify-end">
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
