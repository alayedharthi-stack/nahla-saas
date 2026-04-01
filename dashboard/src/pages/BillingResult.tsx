import { useState, useEffect, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { CheckCircle, XCircle, Loader2, ArrowRight, RefreshCw } from 'lucide-react'
import { billingApi, type PaymentResult } from '../api/billing'

const MAX_POLLS  = 12
const POLL_DELAY = 2500   // ms

export default function BillingResult() {
  const navigate      = useNavigate()
  const [params]      = useSearchParams()
  const rawStatus     = params.get('status')      // 'paid' | 'failed' | null
  const subIdStr      = params.get('sub_id')

  const [result,   setResult]   = useState<PaymentResult | null>(null)
  const [polling,  setPolling]  = useState(false)
  const [attempts, setAttempts] = useState(0)
  const [error,    setError]    = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const poll = async (subId: number, attempt: number) => {
    if (attempt > MAX_POLLS) {
      setPolling(false)
      setError('لم يتم تأكيد الدفع بعد. تحقق من حالة الاشتراك لاحقاً.')
      return
    }
    try {
      const res = await billingApi.getPaymentResult(subId)
      setResult(res)
      setAttempts(attempt)

      if (res.activated) {
        setPolling(false)
      } else if (res.status === 'payment_failed') {
        setPolling(false)
      } else {
        // Still pending — poll again
        pollRef.current = setTimeout(() => poll(subId, attempt + 1), POLL_DELAY)
      }
    } catch {
      setPolling(false)
      setError('تعذّر التحقق من حالة الدفع.')
    }
  }

  useEffect(() => {
    const subId = subIdStr ? parseInt(subIdStr, 10) : null
    if (!subId || isNaN(subId)) {
      setError('معرّف الاشتراك مفقود.')
      return
    }

    if (rawStatus === 'paid') {
      setPolling(true)
      poll(subId, 1)
    } else {
      // Moyasar returned an error status
      billingApi.getPaymentResult(subId)
        .then(r => setResult(r))
        .catch(() => setError('تعذّر التحقق من حالة الدفع.'))
    }

    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Determine UI state ───────────────────────────────────────────────────────

  const isActivated  = result?.activated === true
  const isFailed     = result?.status === 'payment_failed' || rawStatus === 'failed'
  const isPending    = polling || (!isActivated && !isFailed && !error)

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4" dir="rtl">
      <div className="w-full max-w-sm">

        {/* Logo */}
        <div className="flex justify-center mb-6">
          <div className="w-12 h-12 bg-brand-500 rounded-2xl flex items-center justify-center shadow-lg shadow-brand-500/30">
            <span className="text-white font-black text-lg">ن</span>
          </div>
        </div>

        <div className="bg-white rounded-2xl shadow-xl p-8 text-center space-y-5">

          {/* Polling / loading */}
          {isPending && !error && (
            <>
              <Loader2 className="w-14 h-14 animate-spin text-brand-500 mx-auto" />
              <div>
                <p className="text-base font-bold text-slate-900">جارٍ تأكيد الدفع…</p>
                <p className="text-xs text-slate-400 mt-1">
                  يرجى الانتظار بينما نتحقق من حالة الدفع
                  {attempts > 0 && ` (${attempts}/${MAX_POLLS})`}
                </p>
              </div>
            </>
          )}

          {/* Success */}
          {isActivated && (
            <>
              <CheckCircle className="w-14 h-14 text-emerald-500 mx-auto" />
              <div>
                <p className="text-base font-bold text-slate-900">تم الدفع بنجاح!</p>
                <p className="text-sm text-slate-500 mt-1">
                  خطة{' '}
                  <strong>{result?.plan_name_ar ?? 'نهلة'}</strong>{' '}
                  فعّالة الآن. الطيار الآلي جاهز.
                </p>
                {result?.amount_sar && (
                  <p className="text-xs text-slate-400 mt-1">
                    المبلغ المدفوع: {result.amount_sar.toLocaleString('ar-SA')} ر.س
                  </p>
                )}
              </div>
              <button
                onClick={() => navigate('/overview')}
                className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-2.5 rounded-xl text-sm transition-colors flex items-center justify-center gap-2"
              >
                الذهاب إلى لوحة التحكم
                <ArrowRight className="w-4 h-4 rtl:rotate-180" />
              </button>
            </>
          )}

          {/* Failed */}
          {isFailed && !isActivated && (
            <>
              <XCircle className="w-14 h-14 text-red-400 mx-auto" />
              <div>
                <p className="text-base font-bold text-slate-900">لم يتم الدفع</p>
                <p className="text-sm text-slate-500 mt-1">
                  حدث خطأ أثناء معالجة الدفع أو تم إلغاؤه.
                  يمكنك المحاولة مجدداً.
                </p>
              </div>
              <div className="flex flex-col gap-2">
                <button
                  onClick={() => navigate('/billing')}
                  className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-2.5 rounded-xl text-sm transition-colors flex items-center justify-center gap-2"
                >
                  <RefreshCw className="w-4 h-4" />
                  حاول مجدداً
                </button>
                <button
                  onClick={() => navigate('/overview')}
                  className="w-full border border-slate-200 hover:bg-slate-50 text-slate-600 font-medium py-2.5 rounded-xl text-sm transition-colors"
                >
                  العودة للوحة التحكم
                </button>
              </div>
            </>
          )}

          {/* Error */}
          {error && (
            <>
              <XCircle className="w-14 h-14 text-amber-400 mx-auto" />
              <div>
                <p className="text-base font-bold text-slate-900">خطأ في التحقق</p>
                <p className="text-sm text-slate-500 mt-1">{error}</p>
              </div>
              <div className="flex flex-col gap-2">
                <button
                  onClick={() => navigate('/billing')}
                  className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-2.5 rounded-xl text-sm transition-colors"
                >
                  الذهاب لصفحة الاشتراك
                </button>
              </div>
            </>
          )}
        </div>

        <p className="text-center text-slate-500 text-xs mt-4">
          مشكلة في الدفع؟ تواصل معنا عبر واتساب
        </p>
      </div>
    </div>
  )
}
