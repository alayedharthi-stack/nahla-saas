import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Sparkles, AlertCircle, Loader2, CheckCircle } from 'lucide-react'
import { API_BASE } from '../api/client'

export default function ForgotPassword() {
  const [email,   setEmail]   = useState('')
  const [loading, setLoading] = useState(false)
  const [sent,    setSent]    = useState(false)
  const [error,   setError]   = useState('')

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await fetch(`${API_BASE}/auth/forgot-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim().toLowerCase() }),
      })
      setSent(true)
    } catch {
      setError('تعذّر الاتصال بالخادم. حاول مرة أخرى.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir="rtl">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 bg-brand-500 rounded-2xl flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">
            <Sparkles className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">نحلة</h1>
          <p className="text-slate-400 text-sm mt-1">إعادة تعيين كلمة المرور</p>
        </div>

        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          {sent ? (
            <div className="text-center space-y-4 py-2">
              <CheckCircle className="w-10 h-10 text-emerald-500 mx-auto" />
              <h2 className="font-bold text-slate-900">تم إرسال الرابط</h2>
              <p className="text-slate-500 text-sm">
                إذا كان البريد مسجَّلاً ستصلك رسالة قريباً. تحقق من مجلد الرسائل غير المرغوب فيها.
              </p>
              <Link to="/login" className="block text-brand-600 text-sm font-medium hover:underline">
                العودة لتسجيل الدخول
              </Link>
            </div>
          ) : (
            <>
              <h2 className="text-base font-semibold text-slate-800 text-center">
                نسيت كلمة المرور؟
              </h2>
              <p className="text-slate-500 text-sm text-center">
                أدخل بريدك وسنرسل لك رابط إعادة التعيين.
              </p>

              {error && (
                <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2.5 text-sm">
                  <AlertCircle className="w-4 h-4 shrink-0" />
                  {error}
                </div>
              )}

              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1.5">
                    البريد الإلكتروني
                  </label>
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    placeholder="you@example.com"
                    dir="ltr"
                    className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                               focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                               placeholder:text-slate-300"
                  />
                </div>
                <button
                  type="submit"
                  disabled={loading}
                  className="w-full bg-brand-500 hover:bg-brand-600 disabled:opacity-60
                             text-white font-semibold py-2.5 rounded-lg text-sm transition-colors
                             flex items-center justify-center gap-2"
                >
                  {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                  {loading ? 'جارٍ الإرسال...' : 'إرسال رابط الاستعادة'}
                </button>
              </form>

              <p className="text-center text-xs text-slate-500">
                <Link to="/login" className="text-brand-600 font-medium hover:underline">
                  العودة لتسجيل الدخول
                </Link>
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
