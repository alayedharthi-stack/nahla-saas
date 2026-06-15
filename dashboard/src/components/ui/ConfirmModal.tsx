interface ConfirmModalProps {
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export default function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = 'تأكيد',
  cancelLabel = 'إلغاء',
  destructive = false,
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-2 sm:p-6">
      <div className="bg-white rounded-2xl w-full max-w-md shadow-xl">
        <div className="px-5 py-4 border-b border-slate-100">
          <h3 className="font-semibold text-slate-900">{title}</h3>
        </div>
        <div className="p-5">
          <p className="text-sm text-slate-600">{message}</p>
        </div>
        <div className="px-5 py-4 border-t border-slate-100 flex justify-end gap-2">
          <button type="button" className="btn-secondary" disabled={loading} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={destructive ? 'btn-primary bg-red-600 hover:bg-red-700' : 'btn-primary'}
            disabled={loading}
            onClick={onConfirm}
          >
            {loading ? 'جاري التنفيذ…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
