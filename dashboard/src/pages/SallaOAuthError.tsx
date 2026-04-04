import { useEffect, useState } from 'react'
import { XCircle, ArrowLeft, RefreshCw } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

const REASON_LABELS: Record<string, string> = {
  missing_code:       'لم يتم استلام رمز التفويض من سلة.',
  token_exchange_failed: 'فشل تبادل رمز التفويض مع سلة. تأكد من صحة بيانات التطبيق.',
  app_not_configured: 'التطبيق غير مهيأ بالكامل. تواصل مع الدعم.',
  db_save_failed:     'فشل حفظ بيانات الربط في قاعدة البيانات.',
  network_error:      'حدث خطأ في الشبكة أثناء التواصل مع سلة. حاول مرة أخرى.',
  access_denied:      'رفض المستخدم منح الصلاحيات لنحلة AI.',
}

export default function SallaOAuthError() {
  const navigate = useNavigate()
  const [reason, setReason] = useState('')

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    setReason(params.get('reason') || '')
  }, [])

  const label = REASON_LABELS[reason] || `حدث خطأ أثناء ربط المتجر. (${reason || 'unknown'})`

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center p-4" dir="rtl">
      <div className="bg-white rounded-2xl shadow-lg border border-slate-200 p-8 max-w-md w-full text-center space-y-6">
        <div className="w-16 h-16 bg-red-50 rounded-full flex items-center justify-center mx-auto">
          <XCircle className="w-8 h-8 text-red-500" />
        </div>

        <div className="space-y-2">
          <h1 className="text-2xl font-bold text-slate-900">فشل ربط المتجر</h1>
          <p className="text-slate-600 text-sm">{label}</p>
        </div>

        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700 text-start">
          <p className="font-semibold text-red-900 mb-1">خطوات لحل المشكلة:</p>
          <p>• تأكد أن التطبيق مفعّل في لوحة تحكم سلة</p>
          <p>• تأكد أن Redirect URI صحيح: <span className="font-mono text-xs">api.nahlah.ai/oauth/salla/callback</span></p>
          <p>• تواصل مع الدعم إذا استمرت المشكلة</p>
        </div>

        <div className="flex gap-3">
          <button
            onClick={() => navigate('/store-integration')}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors"
          >
            <ArrowLeft className="w-4 h-4" />
            العودة
          </button>
          <button
            onClick={() => window.location.href = '/store-integration'}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-medium hover:bg-brand-600 transition-colors"
          >
            <RefreshCw className="w-4 h-4" />
            حاول مجدداً
          </button>
        </div>
      </div>
    </div>
  )
}
