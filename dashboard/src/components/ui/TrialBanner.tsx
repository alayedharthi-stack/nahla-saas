/**
 * TrialBanner
 * Shown below the header on every page.
 * - During trial: shows plan, days remaining, upgrade CTA
 * - Trial expired: warning banner blocking automation features
 * - Active paid plan: hidden (returns null)
 * - API error: shows a subtle retry-able fallback
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Clock, AlertTriangle, Zap, X, RefreshCw } from 'lucide-react'
import { billingApi, type BillingStatus } from '../../api/billing'
import { throttleFocusRefetch } from '../../lib/focusThrottleRefetch'

export default function TrialBanner() {
  const navigate = useNavigate()
  const [status, setStatus]       = useState<BillingStatus | null>(null)
  const [dismissed, setDismissed] = useState(false)
  const [error, setError]         = useState(false)
  const [retrying, setRetrying]   = useState(false)
  const lastFocusPollRef          = useRef(0)

  const fetchStatus = useCallback(async () => {
    try {
      setRetrying(true)
      setError(false)
      const data = await billingApi.getStatus()
      setStatus(data)
    } catch {
      setError(true)
    } finally {
      setRetrying(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    // Refetch when the tab regains focus — covers the "merchant paid in
    // another tab" case (Moyasar redirect), where the activation flips
    // server-side but this banner is still rendering its last-known
    // state. Also catches the case where reconcile activated a sub
    // moments ago but the cached status still says trial.
    const onFocus = () =>
      throttleFocusRefetch(
        25_000,
        () => lastFocusPollRef.current,
        (t) => {
          lastFocusPollRef.current = t
        },
        fetchStatus,
      )
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [fetchStatus])

  if (dismissed) return null

  if (error) {
    return (
      <div className="bg-slate-100 px-4 py-2 flex items-center justify-between gap-3 text-sm" dir="rtl">
        <span className="text-slate-500 text-xs">تعذّر تحميل حالة الاشتراك</span>
        <button
          onClick={fetchStatus}
          disabled={retrying}
          className="flex items-center gap-1 text-xs text-brand-600 hover:text-brand-700 font-medium disabled:opacity-50"
        >
          <RefreshCw className={`w-3 h-3 ${retrying ? 'animate-spin' : ''}`} />
          إعادة المحاولة
        </button>
      </div>
    )
  }

  if (!status) return null
  if (status.has_subscription && !status.is_trial) return null

  const { is_trial, trial_days_remaining, trial_expired } = status

  /* ── Trial expired banner ──────────────────────────────────────────── */
  if (trial_expired) {
    return (
      <div className="bg-red-600 text-white px-4 py-3 flex items-center justify-between gap-3 text-sm" dir="rtl">
        <div className="flex items-center gap-2 flex-1">
          <AlertTriangle className="w-5 h-5 shrink-0" />
          <div>
            <span className="font-bold block">
              انتهت فترة التجربة المجانية
            </span>
            <span className="text-red-200 text-xs block mt-0.5">
              الطيار الآلي والحملات والرد الذكي متوقفة مؤقتاً — فعّل خطة نحلة للمتابعة
            </span>
          </div>
        </div>
        <button
          onClick={() => navigate('/billing')}
          className="shrink-0 bg-white text-red-600 text-xs font-bold px-4 py-2 rounded-lg hover:bg-red-50 transition-colors"
        >
          ترقية الباقة
        </button>
      </div>
    )
  }

  /* ── Active trial banner ───────────────────────────────────────────── */
  if (is_trial) {
    const urgency = trial_days_remaining <= 3

    return (
      <div
        className={[
          'px-4 py-2.5 flex items-center justify-between gap-3 text-sm',
          urgency
            ? 'bg-amber-500 text-white'
            : 'bg-gradient-to-l from-brand-600 to-brand-500 text-white',
        ].join(' ')}
        dir="rtl"
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <Clock className="w-4 h-4 shrink-0" />
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold">
              باقي{' '}
              <span className="font-bold text-lg leading-none">
                {trial_days_remaining}
              </span>
              {' '}{trial_days_remaining === 1 ? 'يوم' : 'أيام'} على انتهاء التجربة المجانية
            </span>
            {urgency && (
              <span className="text-amber-100 font-medium hidden sm:inline">
                · قم بالترقية لتجنب توقف النظام
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => navigate('/billing')}
            className="flex items-center gap-1.5 bg-white text-brand-600 text-xs font-bold px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors"
          >
            <Zap className="w-3.5 h-3.5" />
            ترقية الباقة
          </button>
          {!urgency && (
            <button
              onClick={() => setDismissed(true)}
              className="opacity-70 hover:opacity-100 transition-opacity"
              aria-label="إخفاء"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>
    )
  }

  return null
}
