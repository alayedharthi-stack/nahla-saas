/**
 * StoreAIPausedBanner
 * Shown below the header when the merchant has disabled store-wide AI replies.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bot, RefreshCw } from 'lucide-react'
import { settingsApi } from '../../api/settings'
import { throttleFocusRefetch } from '../../lib/focusThrottleRefetch'

export default function StoreAIPausedBanner() {
  const navigate = useNavigate()
  const [storeAiEnabled, setStoreAiEnabled] = useState<boolean | null>(null)
  const [error, setError] = useState(false)
  const [retrying, setRetrying] = useState(false)
  const lastFocusPollRef = useRef(0)

  const fetchStatus = useCallback(async () => {
    try {
      setRetrying(true)
      setError(false)
      const data = await settingsApi.getAll()
      setStoreAiEnabled(data.ai.store_ai_enabled !== false)
    } catch {
      setError(true)
    } finally {
      setRetrying(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
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

  if (error) {
    return (
      <div className="bg-slate-100 px-4 py-2 flex items-center justify-between gap-3 text-sm" dir="rtl">
        <span className="text-slate-500 text-xs">تعذّر تحميل حالة الذكاء للمتجر</span>
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

  if (storeAiEnabled !== false) return null

  return (
    <div className="bg-violet-700 text-white px-4 py-3 flex items-center justify-between gap-3 text-sm" dir="rtl">
      <div className="flex items-center gap-2 flex-1 min-w-0">
        <Bot className="w-5 h-5 shrink-0" />
        <div>
          <span className="font-bold block">الذكاء متوقف لهذا المتجر</span>
          <span className="text-violet-100 text-xs block mt-0.5">
            لن يرد الذكاء على العملاء، وستبقى الرسائل محفوظة ويمكنك الرد يدويًا.
          </span>
        </div>
      </div>
      <button
        onClick={() => navigate('/intelligence')}
        className="shrink-0 bg-white text-violet-700 text-xs font-bold px-4 py-2 rounded-lg hover:bg-violet-50 transition-colors"
      >
        إعدادات الذكاء
      </button>
    </div>
  )
}
