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
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-modal-title"
    >
      <div className="bg-white rounded-2xl w-full max-w-md shadow-xl" dir="rtl" lang="ar">
        <div className="px-5 py-4 border-b border-slate-100">
          <h3 id="confirm-modal-title" className="font-semibold text-slate-900">{title}</h3>
        </div>
        <div className="p-5">
          <p className="text-sm text-slate-600">{message}</p>
        </div>
        <div className="px-5 py-4 border-t border-slate-100 flex flex-row-reverse justify-start gap-2">
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
