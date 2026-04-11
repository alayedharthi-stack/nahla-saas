import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Sparkles, Eye, EyeOff, AlertCircle, Loader2, CheckCircle } from 'lucide-react'
import { API_BASE } from '../api/client'

export default function ResetPassword() {
  const navigate = useNavigate()
  const [token,    setToken]    = useState('')
  const [password, setPassword] = useState('')
  const [confirm,  setConfirm]  = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState('')
  const [done,     setDone]     = useState(false)

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const t = params.get('token') ?? ''
    if (!t) setError('رابط إعادة التعيين غير صالح.')
    setToken(t)
  }, [])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    if (password !== confirm) { setError('كلمتا المرور غير متطابقتين'); return }
    if (password.length < 8)  { setError('كلمة المرور يجب أن تكون 8 أحرف على الأقل'); return }

    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/auth/reset-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, password }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'فشلت عملية الاستعادة'); return }
      setDone(true)
      setTimeout(() => navigate('/login', { replace: true }), 3000)
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
          <p className="text-slate-400 text-sm mt-1">تعيين كلمة مرور جديدة</p>
        </div>

        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          {done ? (
            <div className="text-center space-y-4 py-2">
              <CheckCircle className="w-10 h-10 text-emerald-500 mx-auto" />
              <h2 className="font-bold text-slate-900">تم تغيير كلمة المرور ✅</h2>
              <p className="text-slate-500 text-sm">سيتم تحويلك لصفحة تسجيل الدخول...</p>
            </div>
          ) : (
            <>
              <h2 className="text-base font-semibold text-slate-800 text-center">
                كلمة مرور جديدة
              </h2>

              {error && (
                <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2.5 text-sm">
                  <AlertCircle className="w-4 h-4 shrink-0" />
                  {error}
                </div>
              )}

              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1.5">
                    كلمة المرور الجديدة
                  </label>
                  <div className="relative">
                    <input
                      type={showPw ? 'text' : 'password'}
                      required
                      value={password}
                      onChange={e => setPassword(e.target.value)}
                      placeholder="8 أحرف على الأقل"
                      dir="ltr"
                      className="w-full px-3 py-2.5 pe-10 text-sm border border-slate-200 rounded-lg
                                 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                    <button type="button" onClick={() => setShowPw(s => !s)}
                      className="absolute inset-y-0 end-0 pe-3 flex items-center text-slate-400 hover:text-slate-600">
                      {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-medium text-slate-600 mb-1.5">
                    تأكيد كلمة المرور
                  </label>
                  <div className="relative">
                    <input
                      type={showPw ? 'text' : 'password'}
                      required
                      value={confirm}
                      onChange={e => setConfirm(e.target.value)}
                      placeholder="أعد إدخال كلمة المرور"
                      dir="ltr"
                      className="w-full px-3 py-2.5 pe-10 text-sm border border-slate-200 rounded-lg
                                 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                    />
                    {confirm && (
                      <span className="absolute inset-y-0 end-0 pe-3 flex items-center">
                        {confirm === password
                          ? <CheckCircle className="w-4 h-4 text-emerald-500" />
                          : <AlertCircle className="w-4 h-4 text-red-400" />}
                      </span>
                    )}
                  </div>
                </div>

                <button
                  type="submit"
                  disabled={loading || !token}
                  className="w-full bg-brand-500 hover:bg-brand-600 disabled:opacity-60
                             text-white font-semibold py-2.5 rounded-lg text-sm transition-colors
                             flex items-center justify-center gap-2"
                >
                  {loading && <Loader2 className="w-4 h-4 animate-spin" />}
                  {loading ? 'جارٍ الحفظ...' : 'حفظ كلمة المرور'}
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
