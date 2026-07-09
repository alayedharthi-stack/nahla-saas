import { useEffect, useState } from 'react'
import { Loader2, User } from 'lucide-react'

export const CUSTOMER_NAME_MAX_LEN = 80

export interface EditCustomerNameLabels {
  title: string
  fieldLabel: string
  save: string
  cancel: string
  nameRequired: string
  nameTooLong: string
}

interface Props {
  open: boolean
  onClose: () => void
  initialName: string
  saving: boolean
  onSave: (name: string) => void
  labels: EditCustomerNameLabels
  dir: 'rtl' | 'ltr'
}

export default function EditCustomerNameModal({
  open,
  onClose,
  initialName,
  saving,
  onSave,
  labels,
  dir,
}: Props) {
  const [value, setValue] = useState(initialName)

  useEffect(() => {
    if (open) setValue(initialName)
  }, [open, initialName])

  if (!open) return null

  const trimmed = value.trim()
  const tooLong = trimmed.length > CUSTOMER_NAME_MAX_LEN
  const empty = !trimmed
  const canSave = !empty && !tooLong && !saving

  const validationMsg = tooLong
    ? labels.nameTooLong.replace('{max}', String(CUSTOMER_NAME_MAX_LEN))
    : empty && value.length > 0
      ? labels.nameRequired
      : ''

  const submitSave = () => {
    if (canSave) onSave(trimmed)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      submitSave()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      if (!saving) onClose()
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-sm p-4"
      onClick={() => { if (!saving) onClose() }}
    >
      <div
        className="bg-white rounded-2xl shadow-xl w-full max-w-md p-6 space-y-4"
        dir={dir}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-brand-100 rounded-xl flex items-center justify-center shrink-0">
            <User className="w-5 h-5 text-brand-600" />
          </div>
          <h3 className="text-base font-semibold text-slate-900">{labels.title}</h3>
        </div>

        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1.5">
            {labels.fieldLabel}
          </label>
          <input
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            maxLength={CUSTOMER_NAME_MAX_LEN + 10}
            disabled={saving}
            autoFocus
            className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-400 focus:border-brand-400 disabled:opacity-60"
          />
          {validationMsg && (
            <p className="mt-1.5 text-xs text-rose-600">{validationMsg}</p>
          )}
        </div>

        <div className="flex gap-3 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="flex-1 btn-secondary text-sm"
          >
            {labels.cancel}
          </button>
          <button
            type="button"
            onClick={submitSave}
            disabled={!canSave}
            className="flex-1 inline-flex items-center justify-center gap-2 text-sm bg-brand-600 hover:bg-brand-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {labels.save}
          </button>
        </div>
      </div>
    </div>
  )
}
