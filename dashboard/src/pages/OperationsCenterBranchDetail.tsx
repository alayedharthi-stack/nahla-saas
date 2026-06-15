import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowRight, Pencil, Phone, Plus, Star, Trash2,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import Badge from '../components/ui/Badge'
import ConfirmModal from '../components/ui/ConfirmModal'
import BranchHoursEditor from '../components/operations/BranchHoursEditor'
import ContactFormModal from '../components/operations/ContactFormModal'
import EscalationChainPanel from '../components/operations/EscalationChainPanel'
import EscalationLevelFormModal from '../components/operations/EscalationLevelFormModal'
import BranchArrivalRulesPanel from '../components/operations/BranchArrivalRulesPanel'
import {
  parseHoursJson,
  serializeHoursJson,
  type DaySchedule,
} from '../lib/branchHours'
import type { EscalationChainType } from '../lib/escalationTypes'
import {
  operationsCenterApi,
  type BranchContact,
  type BranchInput,
  type ContactInput,
  type EscalationLevel,
  type EscalationLevelInput,
  type MerchantBranch,
} from '../api/operationsCenter'

type TabId = 'info' | 'contacts' | 'escalation' | 'arrival-rules'

const TABS: { id: TabId; label: string }[] = [
  { id: 'info', label: 'بيانات الفرع' },
  { id: 'contacts', label: 'جهات التواصل' },
  { id: 'escalation', label: 'التصعيد' },
  { id: 'arrival-rules', label: 'قواعد الموقع والوصول' },
]

export default function OperationsCenterBranchDetail() {
  const { branchId } = useParams()
  const id = Number(branchId)
  const navigate = useNavigate()
  const [tab, setTab] = useState<TabId>('info')
  const [branch, setBranch] = useState<MerchantBranch | null>(null)
  const [contacts, setContacts] = useState<BranchContact[]>([])
  const [levels, setLevels] = useState<EscalationLevel[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const [infoForm, setInfoForm] = useState<BranchInput>({ name: '' })
  const [hoursSchedule, setHoursSchedule] = useState<DaySchedule[]>([])

  const [contactModalOpen, setContactModalOpen] = useState(false)
  const [contactModalMode, setContactModalMode] = useState<'create' | 'edit'>('create')
  const [editingContact, setEditingContact] = useState<BranchContact | null>(null)
  const [contactSaving, setContactSaving] = useState(false)
  const [contactError, setContactError] = useState('')
  const [deleteContactTarget, setDeleteContactTarget] = useState<BranchContact | null>(null)
  const [deleteContactLoading, setDeleteContactLoading] = useState(false)
  const [deleteLevelTarget, setDeleteLevelTarget] = useState<EscalationLevel | null>(null)
  const [deleteLevelLoading, setDeleteLevelLoading] = useState(false)
  const [levelModalOpen, setLevelModalOpen] = useState(false)
  const [levelModalMode, setLevelModalMode] = useState<'create' | 'edit'>('create')
  const [editingLevel, setEditingLevel] = useState<EscalationLevel | null>(null)
  const [levelSaving, setLevelSaving] = useState(false)
  const [stepError, setStepError] = useState('')
  const [levelReordering, setLevelReordering] = useState(false)
  const [chainType, setChainType] = useState<EscalationChainType>('general')

  const load = useCallback(async () => {
    if (!id) return
    setError('')
    try {
      const [b, c, lv] = await Promise.all([
        operationsCenterApi.getBranch(id),
        operationsCenterApi.listContacts(id),
        operationsCenterApi.listEscalationLevels(id),
      ])
      setBranch(b)
      setContacts(c.contacts || [])
      setLevels(lv.levels || [])
      setInfoForm({
        name: b.name,
        city: b.city,
        district: b.district,
        address: b.address,
        maps_url: b.maps_url,
        is_active: b.is_active,
        sort_order: b.sort_order,
      })
      setHoursSchedule(parseHoursJson(b.hours_json))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل الفرع')
    }
  }, [id])

  useEffect(() => { load() }, [load])

  const saveInfo = async () => {
    if (!id) return
    setSaving(true)
    try {
      const hours_json = serializeHoursJson(hoursSchedule)
      await operationsCenterApi.updateBranch(id, { ...infoForm, hours_json })
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ البيانات')
    } finally {
      setSaving(false)
    }
  }

  const openCreateContact = () => {
    setContactError('')
    setEditingContact(null)
    setContactModalMode('create')
    setContactModalOpen(true)
  }

  const openEditContact = (contact: BranchContact) => {
    setContactError('')
    setEditingContact(contact)
    setContactModalMode('edit')
    setContactModalOpen(true)
  }

  const saveContact = async (body: ContactInput) => {
    setContactSaving(true)
    try {
      if (contactModalMode === 'edit' && editingContact) {
        await operationsCenterApi.updateContact(id, editingContact.id, body)
      } else {
        await operationsCenterApi.createContact(id, body)
      }
      await load()
    } catch (e: unknown) {
      throw e
    } finally {
      setContactSaving(false)
    }
  }

  const confirmDeleteContact = async () => {
    if (!deleteContactTarget) return
    setDeleteContactLoading(true)
    setContactError('')
    try {
      await operationsCenterApi.deleteContact(id, deleteContactTarget.id)
      setDeleteContactTarget(null)
      await load()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'تعذّر حذف جهة التواصل'
      setContactError(
        msg.includes('contact_used_in_escalation')
          ? 'لا يمكن حذف جهة التواصل — مُستخدمة في سلسلة التصعيد. احذفها من التصعيد أولاً.'
          : msg,
      )
    } finally {
      setDeleteContactLoading(false)
    }
  }

  const setDefaultReception = async (contactId: number) => {
    setContactError('')
    try {
      await operationsCenterApi.setDefaultReception(id, contactId)
      await load()
    } catch (e: unknown) {
      setContactError(e instanceof Error ? e.message : 'تعذّر تعيين الاستقبال الافتراضي')
    }
  }

  const openCreateLevel = () => {
    setStepError('')
    setEditingLevel(null)
    setLevelModalMode('create')
    setLevelModalOpen(true)
  }

  const openEditLevel = (level: EscalationLevel) => {
    setStepError('')
    setEditingLevel(level)
    setLevelModalMode('edit')
    setLevelModalOpen(true)
  }

  const saveLevel = async (body: EscalationLevelInput) => {
    setLevelSaving(true)
    try {
      if (levelModalMode === 'edit' && editingLevel) {
        await operationsCenterApi.updateEscalationLevel(id, editingLevel.escalation_level, body)
      } else {
        await operationsCenterApi.createEscalationLevel(id, body)
      }
      await load()
    } catch (e: unknown) {
      throw e
    } finally {
      setLevelSaving(false)
    }
  }

  const confirmDeleteLevel = async () => {
    if (!deleteLevelTarget) return
    setDeleteLevelLoading(true)
    setStepError('')
    try {
      await operationsCenterApi.deleteEscalationLevel(id, deleteLevelTarget.escalation_level)
      setDeleteLevelTarget(null)
      await load()
    } catch (e: unknown) {
      setStepError(e instanceof Error ? e.message : 'تعذّر حذف مستوى التصعيد')
    } finally {
      setDeleteLevelLoading(false)
    }
  }

  const moveLevel = async (index: number, direction: -1 | 1) => {
    const target = index + direction
    if (target < 0 || target >= levels.length) return
    setLevelReordering(true)
    setStepError('')
    try {
      const ordered = levels.map(l => l.escalation_level)
      ;[ordered[index], ordered[target]] = [ordered[target], ordered[index]]
      await operationsCenterApi.reorderEscalationLevels(id, ordered)
      await load()
    } catch (e: unknown) {
      setStepError(e instanceof Error ? e.message : 'تعذّر إعادة ترتيب المستويات')
    } finally {
      setLevelReordering(false)
    }
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
        <div className="card p-5 space-y-4 max-w-3xl">
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
          <div className="block text-sm">
            <span className="text-slate-600 mb-2 block">ساعات العمل</span>
            <BranchHoursEditor value={hoursSchedule} onChange={setHoursSchedule} />
          </div>
          <button type="button" className="btn-primary" disabled={saving} onClick={saveInfo}>
            {saving ? 'جاري الحفظ…' : 'حفظ بيانات الفرع'}
          </button>
        </div>
      )}

      {tab === 'contacts' && (
        <div className="space-y-4">
          {contactError && (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {contactError}
            </div>
          )}
          <div className="flex justify-end">
            <button type="button" className="btn-primary flex items-center gap-2" onClick={openCreateContact}>
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
                    <th className="text-right p-3">الحالة</th>
                    <th className="text-right p-3">استقبال</th>
                    <th className="text-right p-3">إجراءات</th>
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
                        <Badge
                          label={c.is_active ? 'نشط' : 'معطّل'}
                          variant={c.is_active ? 'green' : 'slate'}
                        />
                      </td>
                      <td className="p-3">
                        {c.is_default_reception ? (
                          <Badge label="افتراضي" variant="green" />
                        ) : (
                          <button
                            type="button"
                            className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1"
                            onClick={() => setDefaultReception(c.id)}
                          >
                            <Star className="w-3 h-3" />
                            تعيين
                          </button>
                        )}
                      </td>
                      <td className="p-3">
                        <div className="flex items-center gap-1">
                          <button
                            type="button"
                            className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500"
                            title="تعديل"
                            onClick={() => openEditContact(c)}
                          >
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            className="p-1.5 rounded-lg hover:bg-red-50 text-red-500"
                            title="حذف"
                            onClick={() => {
                              setContactError('')
                              setDeleteContactTarget(c)
                            }}
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <ContactFormModal
            open={contactModalOpen}
            mode={contactModalMode}
            contact={editingContact}
            saving={contactSaving}
            onClose={() => {
              if (!contactSaving) setContactModalOpen(false)
            }}
            onSave={saveContact}
          />

          <ConfirmModal
            open={!!deleteContactTarget}
            title="حذف جهة التواصل"
            message={
              deleteContactTarget
                ? `هل تريد حذف «${deleteContactTarget.display_name}»؟ لا يمكن التراجع عن هذا الإجراء.`
                : ''
            }
            confirmLabel="حذف"
            destructive
            loading={deleteContactLoading}
            onCancel={() => {
              if (!deleteContactLoading) setDeleteContactTarget(null)
            }}
            onConfirm={confirmDeleteContact}
          />
        </div>
      )}

      {tab === 'escalation' && (
        <div className="max-w-3xl">
          <EscalationChainPanel
            levels={levels}
            contacts={contacts}
            chainType={chainType}
            error={stepError}
            reordering={levelReordering}
            onChainTypeChange={setChainType}
            onAdd={openCreateLevel}
            onEdit={openEditLevel}
            onDelete={(level) => {
              setStepError('')
              setDeleteLevelTarget(level)
            }}
            onMove={moveLevel}
          />

          <EscalationLevelFormModal
            open={levelModalOpen}
            mode={levelModalMode}
            level={editingLevel?.escalation_level}
            nextLevel={levels.length + 1}
            contacts={contacts}
            selectedContactIds={editingLevel?.contact_ids}
            saving={levelSaving}
            onClose={() => {
              if (!levelSaving) setLevelModalOpen(false)
            }}
            onSave={saveLevel}
          />
        </div>
      )}

      {tab === 'arrival-rules' && (
        <div className="max-w-3xl">
          <BranchArrivalRulesPanel
            branchId={id}
            branch={branch}
            onBranchUpdated={load}
          />
        </div>
      )}

      <ConfirmModal
        open={!!deleteLevelTarget}
        title="حذف مستوى التصعيد"
        message={
          deleteLevelTarget
            ? `هل تريد حذف المستوى ${deleteLevelTarget.escalation_level}؟`
            : ''
        }
        confirmLabel="حذف"
        destructive
        loading={deleteLevelLoading}
        onCancel={() => { if (!deleteLevelLoading) setDeleteLevelTarget(null) }}
        onConfirm={confirmDeleteLevel}
      />
    </div>
  )
}
