import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, Save, Users } from 'lucide-react'
import ContactFormModal from '../components/operations/ContactFormModal'
import {
  operationsCenterApi,
  type BranchContact,
  type ContactInput,
  type EscalationPreviewStep,
} from '../api/operationsCenter'
import { useLanguage } from '../i18n/context'

const PLACEHOLDER =
  'اكتب لنا ببساطة:\nمن يتواصل معه العميل أولاً؟\nماذا نفعل إذا لم يرد؟\nمتى نرسل رقم موظف آخر؟\nومتى نصعّد للإدارة؟'

export default function SalesChannelsContactsTab() {
  const { t } = useLanguage()
  const sc = t(tr => tr.pages.salesChannels)
  const copy = sc.contactsTab

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState('')
  const [branchId, setBranchId] = useState<number | null>(null)
  const [contacts, setContacts] = useState<BranchContact[]>([])
  const [instruction, setInstruction] = useState('')
  const [steps, setSteps] = useState<EscalationPreviewStep[]>([])
  const [unresolved, setUnresolved] = useState<string>('')
  const [canConfirm, setCanConfirm] = useState(false)
  const [conflicts, setConflicts] = useState<Array<{ title: string; kb_phone: string; message: string }>>([])
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<BranchContact | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setError('')
    try {
      const team = await operationsCenterApi.listTeam()
      setBranchId(team.default_branch_id)
      setContacts(team.contacts || [])
      setInstruction(team.instruction_text || '')
      setSteps(team.preview_steps || [])
      setConflicts(team.kb_conflicts || [])
      setCanConfirm(Boolean((team.preview_steps || []).length))
      setUnresolved('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : copy.loadError)
    } finally {
      setLoading(false)
    }
  }, [copy.loadError])

  useEffect(() => {
    void load()
  }, [load])

  const preview = async (text: string) => {
    if (!text.trim()) {
      setSteps([])
      setUnresolved('')
      setCanConfirm(false)
      return
    }
    try {
      const draft = await operationsCenterApi.previewEscalationPolicy({
        instruction_text: text,
        branch_id: branchId,
      })
      setSteps(draft.steps || [])
      setUnresolved(draft.unresolved_message || '')
      setCanConfirm(Boolean(draft.can_confirm))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : copy.previewError)
    }
  }

  const saveContact = async (body: ContactInput) => {
    let targetBranch = branchId
    if (!targetBranch) {
      const created = await operationsCenterApi.createBranch({ name: 'الفرع الرئيسي', is_active: true })
      targetBranch = created.id
      setBranchId(created.id)
    }
    if (editing) {
      await operationsCenterApi.updateContact(editing.branch_id || targetBranch, editing.id, body)
    } else {
      await operationsCenterApi.createContact(targetBranch, body)
    }
    await load()
  }

  const savePolicy = async () => {
    setError('')
    setSaved('')
    if (!instruction.trim()) {
      setError(copy.instructionRequired)
      return
    }
    setSaving(true)
    try {
      let targetBranch = branchId
      if (!targetBranch) {
        const created = await operationsCenterApi.createBranch({ name: 'الفرع الرئيسي', is_active: true })
        targetBranch = created.id
        setBranchId(created.id)
      }
      const draft = await operationsCenterApi.previewEscalationPolicy({
        instruction_text: instruction,
        branch_id: targetBranch,
      })
      if (!draft.can_confirm) {
        setSteps(draft.steps || [])
        setUnresolved(draft.unresolved_message || copy.unresolvedDefault)
        setCanConfirm(false)
        return
      }
      await operationsCenterApi.confirmEscalationPolicy({
        instruction_text: instruction,
        branch_id: targetBranch,
        confirm: true,
        steps: draft.steps,
      })
      setSaved(copy.saved)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : copy.saveError)
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return <div className="card p-6 text-sm text-slate-500">{copy.loading}</div>
  }

  return (
    <div className="space-y-6">
      <section className="card p-6 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Users className="w-5 h-5 text-brand-500" />
            <h2 className="text-sm font-semibold text-slate-900">{copy.teamTitle}</h2>
          </div>
          <button
            type="button"
            className="btn-primary flex items-center gap-2"
            onClick={() => {
              setEditing(null)
              setModalOpen(true)
            }}
          >
            <Plus className="w-4 h-4" />
            {copy.addContact}
          </button>
        </div>
        <p className="text-sm text-slate-600">{copy.teamHint}</p>
        {contacts.length === 0 ? (
          <p className="text-sm text-slate-500">{copy.emptyTeam}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-slate-500 text-right">
                  <th className="p-2 font-medium">{copy.colName}</th>
                  <th className="p-2 font-medium">{copy.colRole}</th>
                  <th className="p-2 font-medium">{copy.colPhone}</th>
                  <th className="p-2 font-medium">{copy.colBranch}</th>
                  <th className="p-2 font-medium">{copy.colVisible}</th>
                </tr>
              </thead>
              <tbody>
                {contacts.map((c) => (
                  <tr key={c.id} className="border-t border-slate-100">
                    <td className="p-2">
                      <button
                        type="button"
                        className="text-brand-700 hover:underline"
                        onClick={() => {
                          setEditing(c)
                          setModalOpen(true)
                        }}
                      >
                        {c.display_name}
                      </button>
                    </td>
                    <td className="p-2">{c.role || '—'}</td>
                    <td className="p-2 font-mono text-xs" dir="ltr">{c.phone_e164}</td>
                    <td className="p-2">{c.branch_name || '—'}</td>
                    <td className="p-2">{c.customer_can_contact_directly ? copy.yes : copy.no}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card p-6 space-y-3">
        <h2 className="text-sm font-semibold text-slate-900">{copy.instructionTitle}</h2>
        <textarea
          className="input w-full min-h-[140px]"
          placeholder={PLACEHOLDER}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onBlur={() => void preview(instruction)}
        />
      </section>

      <section className="card p-6 space-y-3">
        <h2 className="text-sm font-semibold text-slate-900">{copy.previewTitle}</h2>
        {unresolved && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {unresolved}
          </div>
        )}
        {steps.length === 0 ? (
          <p className="text-sm text-slate-500">{copy.emptyPreview}</p>
        ) : (
          <ol className="space-y-2">
            {steps.map((step) => (
              <li key={`${step.order}-${step.contact_id}`} className="flex items-start gap-3 text-sm">
                <span className="w-7 h-7 rounded-full bg-brand-600 text-white flex items-center justify-center text-xs font-bold shrink-0">
                  {step.order}
                </span>
                <div>
                  <div className="font-medium text-slate-900">{step.display_name || step.role}</div>
                  <div className="text-slate-500">{step.preview_action_label || step.permitted_action}</div>
                </div>
              </li>
            ))}
          </ol>
        )}
        {conflicts.length > 0 && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 space-y-1">
            {conflicts.map((item) => (
              <p key={`${item.title}-${item.kb_phone}`}>
                {item.message}: {item.kb_phone}
              </p>
            ))}
          </div>
        )}
        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
        )}
        {saved && (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{saved}</div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
          <Link to="/sales-channels/branches" className="text-sm text-slate-500 hover:text-slate-700">
            {copy.advancedLink}
          </Link>
          <button
            type="button"
            className="btn-primary flex items-center gap-2"
            disabled={saving}
            onClick={() => void savePolicy()}
          >
            <Save className="w-4 h-4" />
            {saving ? copy.saving : copy.save}
          </button>
        </div>
      </section>

      <ContactFormModal
        open={modalOpen}
        mode={editing ? 'edit' : 'create'}
        contact={editing}
        onClose={() => setModalOpen(false)}
        onSave={saveContact}
      />
    </div>
  )
}
