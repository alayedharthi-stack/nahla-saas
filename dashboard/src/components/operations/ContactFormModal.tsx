import { useEffect, useState } from 'react'
import type { BranchContact, ContactInput } from '../../api/operationsCenter'

export type ContactFormState = {
  display_name: string
  role: string
  phone_e164: string
  whatsapp_e164: string
  is_active: boolean
  is_default_reception: boolean
  customer_can_contact_directly: boolean
}

const emptyForm = (): ContactFormState => ({
  display_name: '',
  role: '',
  phone_e164: '',
  whatsapp_e164: '',
  is_active: true,
  is_default_reception: false,
  customer_can_contact_directly: false,
})

function fromContact(c: BranchContact): ContactFormState {
  return {
    display_name: c.display_name,
    role: c.role || '',
    phone_e164: c.phone_e164,
    whatsapp_e164: c.whatsapp_e164 || '',
    is_active: c.is_active,
    is_default_reception: c.is_default_reception,
    customer_can_contact_directly: Boolean(
      c.customer_can_contact_directly
      || c.customer_visibility === 'customer_visible'
      || c.customer_visibility === 'both',
    ),
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

interface ContactFormModalProps {
  open: boolean
  mode: 'create' | 'edit'
  contact?: BranchContact | null
  saving?: boolean
  onClose: () => void
  onSave: (body: ContactInput) => Promise<void>
}

export default function ContactFormModal({
  open,
  mode,
  contact,
  saving = false,
  onClose,
  onSave,
}: ContactFormModalProps) {
  const [form, setForm] = useState<ContactFormState>(emptyForm)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!open) return
    setError('')
    setForm(contact ? fromContact(contact) : emptyForm())
  }, [open, contact])

  const save = async () => {
    setError('')
    const name = form.display_name.trim()
    if (!name) {
      setError('اسم الموظف مطلوب')
      return
    }
    const phoneErr = validatePhone(form.phone_e164, 'رقم الجوال')
    if (phoneErr) {
      setError(phoneErr)
      return
    }
    if (form.whatsapp_e164.trim()) {
      const waErr = validatePhone(form.whatsapp_e164, 'رقم واتساب')
      if (waErr) {
        setError(waErr)
        return
      }
    }

    const body: ContactInput = {
      display_name: name,
      role: form.role.trim() || undefined,
      phone_e164: form.phone_e164.trim(),
      whatsapp_e164: form.whatsapp_e164.trim() || undefined,
      is_active: form.is_active,
      is_default_reception: form.is_default_reception,
      customer_visibility: form.customer_can_contact_directly ? 'customer_visible' : 'internal_only',
    }

    try {
      await onSave(body)
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ جهة التواصل')
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto shadow-xl">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
          <h3 className="font-semibold text-slate-900">
            {mode === 'edit' ? 'تعديل جهة تواصل' : 'إضافة جهة تواصل'}
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

        <div className="p-5 space-y-3">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">اسم الموظف *</span>
            <input
              className="input w-full"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            />
          </label>

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">الدور</span>
            <input
              className="input w-full"
              placeholder="مثل: reception / showroom"
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

          <label className="block text-sm">
            <span className="text-slate-600 mb-1 block">رقم واتساب (اختياري)</span>
            <input
              className="input w-full font-mono text-sm"
              dir="ltr"
              placeholder="05xxxxxxxx"
              value={form.whatsapp_e164}
              onChange={(e) => setForm({ ...form, whatsapp_e164: e.target.value })}
            />
          </label>

          <div className="flex flex-col gap-2 pt-1">
            <label className="inline-flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
              />
              <span className="text-slate-700">نشط</span>
            </label>
            <label className="inline-flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                checked={form.is_default_reception}
                onChange={(e) => setForm({
                  ...form,
                  is_default_reception: e.target.checked,
                  customer_can_contact_directly: e.target.checked
                    ? true
                    : form.customer_can_contact_directly,
                })}
              />
              <span className="text-slate-700">استقبال افتراضي لهذا الفرع</span>
            </label>
            <label className="inline-flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                checked={form.customer_can_contact_directly}
                onChange={(e) => setForm({ ...form, customer_can_contact_directly: e.target.checked })}
              />
              <span className="text-slate-700">يمكن للعميل التواصل مباشرة؟</span>
            </label>
          </div>
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
