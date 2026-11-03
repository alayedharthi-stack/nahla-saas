import { useState } from 'react'
import { Loader2, X } from 'lucide-react'
import type { DashboardCoupon } from '../api/featureReality'
import { featureRealityApi } from '../api/featureReality'
import { validateValue, validateDate, toRiyadhDate, buildPatch } from '../utils/couponTerms'

interface Props {
  coupon: DashboardCoupon
  /** Called after confirmed update — caller should close editor and reload list. */
  onSaved: () => void
  /** Called after pending/failed response — caller should reload list but NOT close editor. */
  onRefresh: () => void
  onClose: () => void
}

export default function CouponTermsEditor({ coupon, onSaved, onRefresh, onClose }: Props) {
  const [value, setValue] = useState(String(coupon.value))
  const [expires, setExpires] = useState(toRiyadhDate(coupon.expires))
  const [saving, setSaving] = useState(false)
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [serverMessage, setServerMessage] = useState<string | null>(null)
  const [serverPending, setServerPending] = useState(false)

  function clearErrors() {
    setFieldError(null)
    setServerMessage(null)
    setServerPending(false)
  }

  function handleSave(e: React.FormEvent) {
    e.preventDefault()
    clearErrors()

    const vv = validateValue(value, coupon.type)
    if (!vv.valid) { setFieldError(vv.error ?? 'قيمة غير صحيحة'); return }
    const vd = validateDate(expires)
    if (!vd.valid) { setFieldError(vd.error ?? 'تاريخ غير صحيح'); return }

    const patchOrMsg = buildPatch(coupon, value, expires)
    if (typeof patchOrMsg === 'string') { setFieldError(patchOrMsg); return }

    setSaving(true)
    featureRealityApi
      .updateCoupon(coupon.id, patchOrMsg as Record<string, unknown>)
      .then(res => {
        setSaving(false)
        if (res.updated) {
          onSaved()
          return
        }
        // pending or failed — keep modal open, never claim success
        const isPending = res.sync_status === 'pending'
        setServerPending(isPending)
        setServerMessage(
          isPending
            ? 'طلب التحديث قيد المعالجة. يمكنك المحاولة مرة أخرى لاحقًا.'
            : 'لم يتم تأكيد التحديث. يرجى المحاولة مرة أخرى.',
        )
        onRefresh()
      })
      .catch(() => {
        setSaving(false)
        setServerMessage('حدث خطأ في الاتصال. يرجى المحاولة مرة أخرى.')
      })
  }

  const typeLabel = coupon.type === 'percentage' ? 'قيمة الخصم (%)' : 'قيمة الخصم (ر.س)'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      dir="rtl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="coupon-terms-title"
    >
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <h2 id="coupon-terms-title" className="text-base font-semibold text-slate-800">
            تعديل شروط الكوبون
          </h2>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="text-slate-400 hover:text-slate-600 disabled:opacity-50"
            aria-label="إغلاق"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Coupon info */}
        <div className="px-6 pt-4 pb-2">
          <div className="text-xs text-slate-500 mb-1">كود الكوبون</div>
          <div className="font-mono text-sm font-semibold text-slate-700 bg-slate-50 rounded px-3 py-1.5 inline-block">
            {coupon.code}
          </div>
          <div className="text-xs text-slate-400 mt-1">
            {coupon.type === 'percentage' ? 'نسبة مئوية' : 'مبلغ ثابت'}
          </div>
        </div>

        {/* Form */}
        <form onSubmit={handleSave} className="px-6 pb-6 pt-2 space-y-4">
          {/* Value */}
          <div>
            <label htmlFor="coupon-terms-value" className="block text-sm font-medium text-slate-700 mb-1">
              {typeLabel}
            </label>
            <input
              id="coupon-terms-value"
              type="number"
              step="any"
              value={value}
              onChange={e => { setValue(e.target.value); clearErrors() }}
              disabled={saving}
              className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-brand-400 disabled:opacity-50"
              required
            />
          </div>

          {/* Expires */}
          <div>
            <label htmlFor="coupon-terms-expires" className="block text-sm font-medium text-slate-700 mb-1">
              تاريخ الانتهاء
              {coupon.salla_coupon_id
                ? <span className="text-xs text-slate-400 mr-1">(مطلوب — مرتبط بسلة)</span>
                : <span className="text-xs text-slate-400 mr-1">(اختياري)</span>
              }
            </label>
            <input
              id="coupon-terms-expires"
              type="date"
              value={expires}
              onChange={e => { setExpires(e.target.value); clearErrors() }}
              disabled={saving}
              className="w-full border border-slate-200 rounded-lg px-3 py-2 text-sm text-slate-800 focus:outline-none focus:ring-2 focus:ring-brand-400 disabled:opacity-50"
            />
          </div>

          {/* Inline error */}
          {fieldError && (
            <p role="alert" className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
              {fieldError}
            </p>
          )}

          {/* Server message (pending or failed) */}
          {serverMessage && (
            <p
              role="status"
              className={`text-xs rounded-lg px-3 py-2 ${
                serverPending ? 'text-amber-700 bg-amber-50' : 'text-red-600 bg-red-50'
              }`}
            >
              {serverMessage}
            </p>
          )}

          {/* Actions */}
          <div className="flex items-center gap-3 pt-2">
            <button
              type="submit"
              disabled={saving}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-700 disabled:opacity-50 transition-colors"
            >
              {saving && <Loader2 className="w-4 h-4 animate-spin" />}
              حفظ التغييرات
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 rounded-lg border border-slate-200 text-slate-600 text-sm font-medium hover:bg-slate-50 disabled:opacity-50 transition-colors"
            >
              إلغاء
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
