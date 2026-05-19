import { useEffect, useState } from 'react'
import { CheckCircle, ArrowLeft, Store } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useEmbeddedLocale } from '../hooks/useEmbeddedLocale'

export default function SallaOAuthSuccess() {
  const navigate = useNavigate()
  const { isRTL, t } = useEmbeddedLocale()
  const [storeName, setStoreName] = useState('')
  const [storeId, setStoreId] = useState('')

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    setStoreName(decodeURIComponent(params.get('name') || ''))
    setStoreId(params.get('store') || '')
  }, [])

  // Inline-format the "{storeName}" placeholder while keeping the substituted
  // text bold — done in JSX so we don't lose the visual emphasis.
  const subtitleParts = t.oauthSuccess.subtitleWithStore.split('{storeName}')

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4" dir={isRTL ? 'rtl' : 'ltr'}>
      <div className="bg-white rounded-2xl shadow-lg border border-slate-200 p-8 max-w-md w-full text-center space-y-6">
        <div className="w-16 h-16 bg-emerald-50 rounded-full flex items-center justify-center mx-auto">
          <CheckCircle className="w-8 h-8 text-emerald-500" />
        </div>

        <div className="space-y-2">
          <h1 className="text-2xl font-bold text-slate-900">{t.oauthSuccess.title}</h1>
          {storeName && (
            <p className="text-slate-600 text-sm">
              {subtitleParts[0]}
              <span className="font-semibold text-slate-800">{storeName}</span>
              {subtitleParts[1] ?? ''}
            </p>
          )}
          {storeId && (
            <p className="text-slate-400 text-xs font-mono">
              {t.oauthSuccess.storeIdLabel} {storeId}
            </p>
          )}
        </div>

        <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-4 text-sm text-emerald-700 space-y-1.5 text-start">
          <div className="flex items-center gap-2">
            <Store className="w-4 h-4 shrink-0" />
            <span className="font-semibold text-emerald-900">{t.oauthSuccess.whatNow}</span>
          </div>
          <p>{t.oauthSuccess.bullet1}</p>
          <p>{t.oauthSuccess.bullet2}</p>
          <p>{t.oauthSuccess.bullet3}</p>
        </div>

        <div className="flex gap-3">
          <button
            onClick={() => navigate('/store-integration')}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            {t.oauthSuccess.btnSettings}
          </button>
          <button
            onClick={() => navigate('/overview')}
            className="flex-1 px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-medium hover:bg-brand-600 transition-colors"
          >
            {t.oauthSuccess.btnHome}
          </button>
        </div>
      </div>
    </div>
  )
}
