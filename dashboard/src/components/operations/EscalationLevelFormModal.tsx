import { useEffect, useState } from 'react'
import type { BranchContact, EscalationLevelInput } from '../../api/operationsCenter'

interface EscalationLevelFormModalProps {
  open: boolean
  mode: 'create' | 'edit'
  level?: number
  nextLevel: number
  contacts: BranchContact[]
  selectedContactIds?: number[]
  saving?: boolean
  onClose: () => void
  onSave: (body: EscalationLevelInput) => Promise<void>
}

export default function EscalationLevelFormModal({
  open,
  mode,
  level,
  nextLevel,
  contacts,
  selectedContactIds = [],
  saving = false,
  onClose,
  onSave,
}: EscalationLevelFormModalProps) {
  const [picked, setPicked] = useState<number[]>([])
  const [error, setError] = useState('')

  const levelNum = mode === 'edit' && level ? level : nextLevel
  const activeContacts = contacts.filter(c => c.is_active)

  useEffect(() => {
    if (!open) return
    setError('')
    setPicked([...selectedContactIds])
  }, [open, selectedContactIds])

  const toggle = (contactId: number) => {
    setPicked(prev =>
      prev.includes(contactId)
        ? prev.filter(id => id !== contactId)
        : [...prev, contactId],
    )
  }

  const save = async () => {
    setError('')
    if (picked.length === 0) {
      setError('اختر موظفاً واحداً على الأقل من جهات التواصل')
      return
    }
    try {
      await onSave({ contact_ids: picked })
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ مستوى التصعيد')
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-xl">
        <div className="px-5 py-4 border-b border-slate-100">
          <div className="flex items-center justify-between gap-3">
            <h3 className="font-semibold text-slate-900">
              {mode === 'edit' ? 'تعديل مستوى التصعيد' : 'إضافة مستوى تصعيد'}
            </h3>
            <button
              type="button"
              className="text-slate-400 hover:text-slate-600 text-xl leading-none"
              disabled={saving}
              onClick={onClose}
            >
              ×
            </button>
          </div>
          <p className="text-xs text-slate-500 mt-1.5">
            {mode === 'edit'
              ? `المستوى ${levelNum} — اختر من جهات التواصل المضافة مسبقاً (يمكن أكثر من موظف)`
              : levelNum === 1
                ? 'المستوى الأول — اختر من يتواصل معه النظام أولاً عند التصعيد'
                : `المستوى ${levelNum} — يُستخدم بعد المستوى ${levelNum - 1} عند الحاجة`}
          </p>
        </div>

        <div className="p-5 space-y-3">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
            المستوى <span className="font-semibold text-brand-700">{levelNum}</span>
          </div>

          {activeContacts.length === 0 ? (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 text-sm text-amber-900">
              لا توجد جهات تواصل نشطة.{' '}
              <span className="text-slate-600">
                أضف الموظفين أولاً من تبويب «جهات التواصل» ثم ارجع لبناء سلسلة التصعيد.
              </span>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-sm font-medium text-slate-700">اختر الموظفين</p>
              {activeContacts.map(contact => {
                const checked = picked.includes(contact.id)
                return (
                  <label
                    key={contact.id}
                    className={`flex items-start gap-3 p-3 rounded-xl border cursor-pointer transition-colors ${
                      checked
                        ? 'border-brand-400 bg-brand-50/50'
                        : 'border-slate-200 hover:border-slate-300'
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="mt-1 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                      checked={checked}
                      onChange={() => toggle(contact.id)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block font-medium text-slate-900">{contact.display_name}</span>
                      {contact.role && (
                        <span className="block text-sm text-slate-600">{contact.role}</span>
                      )}
                      <span className="block text-xs text-slate-500 font-mono mt-0.5" dir="ltr">
                        {contact.phone_e164}
                      </span>
                    </span>
                  </label>
                )
              })}
            </div>
          )}

          {picked.length > 1 && (
            <p className="text-xs text-brand-700 bg-brand-50 rounded-lg px-3 py-2">
              هذا المستوى يتضمن {picked.length} موظفين — يُستخدمون ضمن نفس مرحلة التصعيد.
            </p>
          )}
        </div>

        <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2">
          <button type="button" className="btn-secondary" disabled={saving} onClick={onClose}>
            إلغاء
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={saving || activeContacts.length === 0}
            onClick={save}
          >
            {saving ? 'جاري الحفظ…' : 'حفظ'}
          </button>
        </div>
      </div>
    </div>
  )
}
