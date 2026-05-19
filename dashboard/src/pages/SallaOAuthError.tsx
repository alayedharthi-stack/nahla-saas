import { useEffect, useState } from 'react'
import { XCircle, ArrowLeft, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useEmbeddedLocale } from '../hooks/useEmbeddedLocale'
import type { EmbeddedStrings } from '../i18n/embedded'

type ReasonKey = keyof EmbeddedStrings['oauthError']['reasons']

export default function SallaOAuthError() {
  const navigate = useNavigate()
  const { isRTL, t } = useEmbeddedLocale()
  const [reason, setReason] = useState('')

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    setReason(params.get('reason') || '')
  }, [])

  const known = (t.oauthError.reasons as Record<string, string>)[reason as ReasonKey]
  const label = known
    ? known
    : `${t.oauthError.fallbackReason} (${reason || 'unknown'})`

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4" dir={isRTL ? 'rtl' : 'ltr'}>
      <div className="bg-white rounded-2xl shadow-lg border border-slate-200 p-8 max-w-md w-full text-center space-y-6">
        <div className="w-16 h-16 bg-red-50 rounded-full flex items-center justify-center mx-auto">
          <XCircle className="w-8 h-8 text-red-500" />
        </div>

        <div className="space-y-2">
          <h1 className="text-2xl font-bold text-slate-900">{t.oauthError.title}</h1>
          <p className="text-slate-600 text-sm">{label}</p>
        </div>

        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700 text-start">
          <p className="font-semibold text-red-900 mb-1">{t.oauthError.howToFixTitle}</p>
          <p>{t.oauthError.fix1}</p>
          <p>{t.oauthError.fix2}</p>
          <p>{t.oauthError.fix3}</p>
        </div>

        <div className="flex gap-3">
          <button
            onClick={() => navigate('/store-integration')}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            {t.oauthError.btnBack}
          </button>
          <button
            onClick={() => window.location.href = '/store-integration'}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-medium hover:bg-brand-600 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            {t.oauthError.btnRetry}
          </button>
        </div>
      </div>
    </div>
  )
}
