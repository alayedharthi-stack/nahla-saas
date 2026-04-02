/**
 * TrialBanner
 * Shown below the header on every page.
 * - During trial: shows plan, days remaining, upgrade CTA
 * - Trial expired: warning banner blocking automation features
 * - Active paid plan: hidden (returns null)
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Clock, AlertTriangle, Zap, X } from 'lucide-react'
import { billingApi, type BillingStatus } from '../../api/billing'

export default function TrialBanner() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<BillingStatus | null>(null)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    billingApi.getStatus().then(setStatus).catch(() => {})
  }, [])

  // Don't show if dismissed, still loading, or if on a paid plan
  if (!status || dismissed) return null
  if (status.has_subscription && !status.is_trial) return null

  const { is_trial, trial_days_remaining, trial_expired } = status

  /* ── Trial expired banner ──────────────────────────────────────────── */
  if (trial_expired) {
    return (
      <div className="bg-red-600 text-white px-4 py-2.5 flex items-center justify-between gap-3 text-sm" dir="rtl">
        <div className="flex items-center gap-2 flex-1">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span className="font-medium">
            انتهت فترة التجربة المجانية — ميزات الطيار الآلي موقوفة مؤقتاً
          </span>
          <span className="hidden sm:inline text-red-200">
            · فعّل خطة نحلة للمتابعة
          </span>
        </div>
        <button
          onClick={() => navigate('/billing')}
          className="shrink-0 bg-white text-red-600 text-xs font-bold px-3 py-1.5 rounded-lg hover:bg-red-50 transition-colors"
        >
          اشترك الآن
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
            <span className="font-semibold">الخطة الحالية: تجربة مجانية</span>
            <span className="opacity-80">·</span>
            <span>
              متبقي{' '}
              <span className="font-bold">
                {trial_days_remaining} {trial_days_remaining === 1 ? 'يوم' : 'أيام'}
              </span>
            </span>
            {urgency && (
              <span className="text-amber-100 font-medium hidden sm:inline">
                · فعّل خطتك قبل انتهاء التجربة
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
            ترقية الخطة
          </button>
          <button
            onClick={() => setDismissed(true)}
            className="opacity-70 hover:opacity-100 transition-opacity"
            aria-label="إخفاء"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
    )
  }

  return null
}
