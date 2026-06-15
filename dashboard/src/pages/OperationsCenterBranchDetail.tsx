import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowRight, Building2, ChevronDown, ChevronUp, Phone, Plus, Star, Trash2,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import {
  operationsCenterApi,
  type BranchContact,
  type BranchEscalationStep,
  type BranchInput,
  type ContactInput,
  type EscalationStepInput,
  type MerchantBranch,
} from '../api/operationsCenter'

type TabId = 'info' | 'contacts' | 'escalation'

const TABS: { id: TabId; label: string }[] = [
  { id: 'info', label: 'بيانات الفرع' },
  { id: 'contacts', label: 'جهات التواصل' },
  { id: 'escalation', label: 'التصعيد' },
]

export default function OperationsCenterBranchDetail() {
  const { branchId } = useParams()
  const id = Number(branchId)
  const navigate = useNavigate()
  const [tab, setTab] = useState<TabId>('info')
  const [branch, setBranch] = useState<MerchantBranch | null>(null)
  const [contacts, setContacts] = useState<BranchContact[]>([])
  const [steps, setSteps] = useState<BranchEscalationStep[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const [infoForm, setInfoForm] = useState<BranchInput>({ name: '' })
  const [hoursText, setHoursText] = useState('')

  const load = useCallback(async () => {
    if (!id) return
    setError('')
    try {
      const [b, c, s] = await Promise.all([
        operationsCenterApi.getBranch(id),
        operationsCenterApi.listContacts(id),
        operationsCenterApi.listEscalationSteps(id),
      ])
      setBranch(b)
      setContacts(c.contacts || [])
      setSteps(s.steps || [])
      setInfoForm({
        name: b.name,
        city: b.city,
        district: b.district,
        address: b.address,
        maps_url: b.maps_url,
        is_active: b.is_active,
        sort_order: b.sort_order,
      })
      setHoursText(b.hours_json ? JSON.stringify(b.hours_json, null, 2) : '')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل الفرع')
    }
  }, [id])

  useEffect(() => { load() }, [load])

  const saveInfo = async () => {
    if (!id) return
    setSaving(true)
    try {
      let hours_json: Record<string, unknown> | null | undefined
      if (hoursText.trim()) {
        hours_json = JSON.parse(hoursText) as Record<string, unknown>
      } else {
        hours_json = null
      }
      await operationsCenterApi.updateBranch(id, { ...infoForm, hours_json })
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ البيانات')
    } finally {
      setSaving(false)
    }
  }

  const addContact = async () => {
    const display_name = window.prompt('اسم الموظف')?.trim()
    const phone_e164 = window.prompt('رقم الجوال')?.trim()
    if (!display_name || !phone_e164) return
    const role = window.prompt('الدور (مثل: showroom / reception)')?.trim() || ''
    const body: ContactInput = { display_name, phone_e164, role }
    await operationsCenterApi.createContact(id, body)
    await load()
  }

  const addStep = async () => {
    const display_name = window.prompt('اسم جهة التصعيد')?.trim()
    const phone_e164 = window.prompt('رقم الجوال')?.trim()
    if (!display_name || !phone_e164) return
    const level = steps.length + 1
    const body: EscalationStepInput = {
      escalation_level: level,
      display_name,
      phone_e164,
      role: window.prompt('الدور')?.trim() || '',
    }
    await operationsCenterApi.createEscalationStep(id, body)
    await load()
  }

  const moveStep = async (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= steps.length) return
    const ids = steps.map(s => s.id)
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    await operationsCenterApi.reorderEscalationSteps(id, ids)
    await load()
  }

  if (!branch && !error) {
    return <div className="p-8 text-center text-slate-500 text-sm">جاري التحميل…</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-slate-500">
        <Link to="/operations-center" className="hover:text-brand-600">الفروع</Link>
        <span>/</span>
        <span className="text-slate-800">{branch?.name || '…'}</span>
      </div>

      <PageHeader
        title={branch?.name || 'تفاصيل الفرع'}
        subtitle="إدارة بيانات الفرع وجهات التواصل وسلسلة التصعيد"
        action={
          <button type="button" className="btn-secondary flex items-center gap-2" onClick={() => navigate('/operations-center')}>
            <ArrowRight className="w-4 h-4" />
            رجوع
          </button>
        }
      />

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
      )}

      <div className="border-b border-slate-200">
        <div className="flex gap-1 overflow-x-auto">
          {TABS.map(({ id: tid, label }) => (
            <button
              key={tid}
              type="button"
              onClick={() => setTab(tid)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px whitespace-nowrap ${
                tab === tid
                  ? 'border-brand-500 text-brand-600'
                  : 'border-transparent text-slate-500 hover:text-slate-700'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {tab === 'info' && (
        <div className="card p-5 space-y-4 max-w-2xl">
          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">اسم الفرع</span>
            <input className="input w-full" value={infoForm.name} onChange={e => setInfoForm({ ...infoForm, name: e.target.value })} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-sm">
              <span className="text-slate-600 mb-1 block">المدينة</span>
              <input className="input w-full" value={infoForm.city || ''} onChange={e => setInfoForm({ ...infoForm, city: e.target.value })} />
            </label>
            <label className="block text-sm">
              <span className="text-slate-600 mb-1 block">الحي</span>
              <input className="input w-full" value={infoForm.district || ''} onChange={e => setInfoForm({ ...infoForm, district: e.target.value })} />
            </label>
          </div>
          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">العنوان</span>
            <textarea className="input w-full min-h-[80px]" value={infoForm.address || ''} onChange={e => setInfoForm({ ...infoForm, address: e.target.value })} />
          </label>
          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">رابط Google Maps</span>
            <input className="input w-full" dir="ltr" value={infoForm.maps_url || ''} onChange={e => setInfoForm({ ...infoForm, maps_url: e.target.value })} />
          </label>
          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">ساعات العمل (JSON)</span>
            <textarea className="input w-full min-h-[100px] font-mono text-xs" dir="ltr" value={hoursText} onChange={e => setHoursText(e.target.value)} placeholder='{"sat":"9-22"}' />
          </label>
          <button type="button" className="btn-primary" disabled={saving} onClick={saveInfo}>
            {saving ? 'جاري الحفظ…' : 'حفظ بيانات الفرع'}
          </button>
        </div>
      )}

      {tab === 'contacts' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <button type="button" className="btn-primary flex items-center gap-2" onClick={addContact}>
              <Plus className="w-4 h-4" />
              إضافة جهة تواصل
            </button>
          </div>
          <div className="card overflow-hidden">
            {contacts.length === 0 ? (
              <div className="p-8 text-center text-slate-500 text-sm">لا توجد جهات تواصل لهذا الفرع.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-100 text-slate-500">
                    <th className="text-right p-3">الاسم</th>
                    <th className="text-right p-3">الدور</th>
                    <th className="text-right p-3">الرقم</th>
                    <th className="text-right p-3">واتساب</th>
                    <th className="text-right p-3">استقبال</th>
                    <th className="text-right p-3" />
                  </tr>
                </thead>
                <tbody>
                  {contacts.map(c => (
                    <tr key={c.id} className="border-b border-slate-50">
                      <td className="p-3 font-medium">{c.display_name}</td>
                      <td className="p-3 text-slate-600">{c.role || '—'}</td>
                      <td className="p-3 font-mono text-xs" dir="ltr">{c.phone_e164}</td>
                      <td className="p-3 font-mono text-xs" dir="ltr">{c.whatsapp_e164 || '—'}</td>
                      <td className="p-3">
                        {c.is_default_reception ? (
                          <Badge label="افتراضي" variant="green" />
                        ) : (
                          <button
                            type="button"
                            className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1"
                            onClick={async () => {
                              await operationsCenterApi.setDefaultReception(id, c.id)
                              await load()
                            }}
                          >
                            <Star className="w-3 h-3" />
                            تعيين
                          </button>
                        )}
                      </td>
                      <td className="p-3">
                        <button
                          type="button"
                          className="text-red-500 hover:bg-red-50 p-1 rounded"
                          onClick={async () => {
                            if (!window.confirm('حذف جهة التواصل؟')) return
                            await operationsCenterApi.deleteContact(id, c.id)
                            await load()
                          }}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {tab === 'escalation' && (
        <div className="space-y-4">
          <div className="flex justify-end">
            <button type="button" className="btn-primary flex items-center gap-2" onClick={addStep}>
              <Plus className="w-4 h-4" />
              إضافة مستوى
            </button>
          </div>
          <div className="space-y-3">
            {steps.length === 0 ? (
              <div className="card p-8 text-center text-slate-500 text-sm">لا توجد مستويات تصعيد.</div>
            ) : steps.map((step, index) => (
              <div key={step.id} className="card p-4 flex items-center gap-4">
                <div className="w-10 h-10 rounded-full bg-brand-50 text-brand-700 flex items-center justify-center font-bold text-sm">
                  {step.escalation_level}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-900">{step.display_name}</div>
                  <div className="text-xs text-slate-500 flex items-center gap-2 mt-1">
                    <Phone className="w-3 h-3" />
                    <span dir="ltr">{step.phone_e164}</span>
                    {step.role && <span>· {step.role}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button type="button" className="p-1 rounded hover:bg-slate-100" onClick={() => moveStep(index, -1)} disabled={index === 0}>
                    <ChevronUp className="w-4 h-4" />
                  </button>
                  <button type="button" className="p-1 rounded hover:bg-slate-100" onClick={() => moveStep(index, 1)} disabled={index === steps.length - 1}>
                    <ChevronDown className="w-4 h-4" />
                  </button>
                  <button
                    type="button"
                    className="p-1 rounded hover:bg-red-50 text-red-500"
                    onClick={async () => {
                      if (!window.confirm('حذف مستوى التصعيد؟')) return
                      await operationsCenterApi.deleteEscalationStep(id, step.id)
                      await load()
                    }}
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
