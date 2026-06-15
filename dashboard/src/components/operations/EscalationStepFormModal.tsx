import { useEffect, useState } from 'react'
import type { BranchEscalationStep, EscalationStepInput } from '../../api/operationsCenter'

type FormState = {
  display_name: string
  role: string
  phone_e164: string
  is_active: boolean
}

const emptyForm = (): FormState => ({
  display_name: '',
  role: '',
  phone_e164: '',
  is_active: true,
})

function fromStep(step: BranchEscalationStep): FormState {
  return {
    display_name: step.display_name,
    role: step.role || '',
    phone_e164: step.phone_e164,
    is_active: step.is_active,
  }
}

function validatePhone(raw: string, label: string): string | null {
  const t = raw.trim()
  if (!t) return `${label} مطلوب`
  const digits = t.replace(/\D/g, '')
  if (digits.length < 9 || digits.length > 15) {
    return `${label} غير صالح — استخدم صيغة مثل 05xxxxxxxx أو +9665xxxxxxxx`
  }
  return null
}

interface EscalationStepFormModalProps {
  open: boolean
  mode: 'create' | 'edit'
  step?: BranchEscalationStep | null
  nextLevel: number
  saving?: boolean
  onClose: () => void
  onSave: (body: EscalationStepInput) => Promise<void>
}

export default function EscalationStepFormModal({
  open,
  mode,
  step,
  nextLevel,
  saving = false,
  onClose,
  onSave,
}: EscalationStepFormModalProps) {
  const [form, setForm] = useState<FormState>(emptyForm)
  const [error, setError] = useState('')

  const level = mode === 'edit' && step ? step.escalation_level : nextLevel

  useEffect(() => {
    if (!open) return
    setError('')
    setForm(step && mode === 'edit' ? fromStep(step) : emptyForm())
  }, [open, step, mode])

  const save = async () => {
    setError('')
    const name = form.display_name.trim()
    if (!name) {
      setError('اسم جهة التصعيد مطلوب')
      return
    }
    const phoneErr = validatePhone(form.phone_e164, 'رقم الجوال')
    if (phoneErr) {
      setError(phoneErr)
      return
    }

    const body: EscalationStepInput = {
      escalation_level: level,
      display_name: name,
      phone_e164: form.phone_e164.trim(),
      role: form.role.trim() || undefined,
      is_active: form.is_active,
    }

    try {
      await onSave(body)
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ مستوى التصعيد')
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-lg shadow-xl">
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
              ? `المستوى ${level} في سلسلة التصعيد — التعديل لا يغيّر ترتيب المستوى`
              : level === 1
                ? 'سيُضاف كالمستوى الأول — نقطة التصعيد الأولى التي يتواصل معها النظام'
                : `سيُضاف كالمستوى ${level} — يُتصل به العميل بعد المستوى ${level - 1} عند الحاجة`}
          </p>
        </div>

        <div className="p-5 space-y-3">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
            المستوى <span className="font-semibold text-brand-700">{level}</span>
          </div>

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">اسم جهة التصعيد *</span>
            <input
              className="input w-full"
              placeholder="مثل: أمين، هشام"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
          </label>

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">الدور</span>
            <input
              className="input w-full"
              placeholder="مثل: بائع المعرض، خدمة العملاء، الإدارة"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value })}
            />
          </label>

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">رقم الجوال *</span>
            <input
              className="input w-full font-mono text-sm"
              dir="ltr"
              placeholder="05xxxxxxxx"
              value={form.phone_e164}
              onChange={(e) => setForm({ ...form, phone_e164: e.target.value })}
            />
          </label>

          <label className="inline-flex items-center gap-2 text-sm cursor-pointer pt-1">
            <input
              type="checkbox"
              className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
              checked={form.is_active}
              onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
            />
            <span className="text-slate-700">نشط — يُستخدم في سلسلة التصعيد</span>
          </label>
        </div>

        <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2">
          <button type="button" className="btn-secondary" disabled={saving} onClick={onClose}>
            إلغاء
          </button>
          <button type="button" className="btn-primary" disabled={saving} onClick={save}>
            {saving ? 'جاري الحفظ…' : 'حفظ'}
          </button>
        </div>
      </div>
    </div>
  )
}
