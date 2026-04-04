import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Sparkles, Eye, EyeOff, AlertCircle, Loader2, CheckCircle, Mail } from 'lucide-react'
import { register } from '../auth'

export default function Register() {
  const navigate = useNavigate()
  const [storeName,    setStoreName]    = useState('')
  const [email,        setEmail]        = useState('')
  const [phone,        setPhone]        = useState('')
  const [password,     setPassword]     = useState('')
  const [confirm,      setConfirm]      = useState('')
  const [showPw,       setShowPw]       = useState(false)
  const [error,        setError]        = useState('')
  const [loading,      setLoading]      = useState(false)
  const [inviteToken,  setInviteToken]  = useState('')
  const [inviteValid,  setInviteValid]  = useState<boolean | null>(null)

  // Extract invite token from URL ?invite=...
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('invite') ?? ''
    setInviteToken(token)
    if (token) {
      // Validate invite token against backend
      fetch(`/api/auth/invite/${token}`)
        .then(r => r.json())
        .then(data => {
          setInviteValid(!!data.valid)
          if (data.invited_email) setEmail(data.invited_email)
        })
        .catch(() => setInviteValid(false))
    } else {
      setInviteValid(null) // unknown — backend will decide
    }
  }, [])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')

    if (password !== confirm) {
      setError('كلمتا المرور غير متطابقتين')
      return
    }
    if (password.length < 8) {
      setError('كلمة المرور يجب أن تكون 8 أحرف على الأقل')
      return
    }

    setLoading(true)
    const result = await register(email, password, storeName, phone, inviteToken)
    if (result.ok) {
      navigate('/overview', { replace: true })
    } else {
      setError(result.error ?? 'فشل التسجيل')
      setLoading(false)
    }
  }

  // If invite token is present but invalid, show error page
  if (inviteToken && inviteValid === false) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir="rtl">
        <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl p-8 text-center space-y-4">
          <AlertCircle className="w-10 h-10 text-red-500 mx-auto" />
          <h2 className="text-lg font-bold text-slate-900">رابط الدعوة غير صالح</h2>
          <p className="text-slate-500 text-sm">هذا الرابط منتهي الصلاحية أو غير صحيح. تواصل مع المالك للحصول على رابط جديد.</p>
          <Link to="/login" className="block text-brand-600 text-sm font-medium hover:underline">
            تسجيل الدخول
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center bg-slate-900 px-4 py-8"
      dir="rtl"
    >
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <img src="/logo.png" alt="نحلة" className="w-20 h-20 object-contain mb-3 drop-shadow-xl" />
          <h1 className="text-2xl font-bold text-white">نحلة</h1>
          <p className="text-slate-400 text-sm mt-1">إنشاء حساب تاجر جديد</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          <h2 className="text-base font-semibold text-slate-800 text-center">
            تسجيل متجر جديد
          </h2>

          {error && (
            <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2.5 text-sm">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Store Name */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                اسم المتجر
              </label>
              <input
                type="text"
                required
                value={storeName}
                onChange={e => setStoreName(e.target.value)}
                placeholder="متجر الإلكترونيات"
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Email */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                البريد الإلكتروني
              </label>
              <input
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                dir="ltr"
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Phone (optional) */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                رقم الجوال
                <span className="text-slate-400 font-normal me-1">(اختياري)</span>
              </label>
              <input
                type="tel"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="05xxxxxxxx"
                dir="ltr"
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Password */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                كلمة المرور
              </label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  required
                  autoComplete="new-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="8 أحرف على الأقل"
                  dir="ltr"
                  className="w-full px-3 py-2.5 pe-10 text-sm border border-slate-200 rounded-lg
                             focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                             placeholder:text-slate-300"
                />
                <button
                  type="button"
                  onClick={() => setShowPw(s => !s)}
                  className="absolute inset-y-0 end-0 pe-3 flex items-center text-slate-400 hover:text-slate-600"
                >
                  {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            {/* Confirm Password */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                تأكيد كلمة المرور
              </label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  required
                  autoComplete="new-password"
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                  placeholder="أعد إدخال كلمة المرور"
                  dir="ltr"
                  className="w-full px-3 py-2.5 pe-10 text-sm border border-slate-200 rounded-lg
                             focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                             placeholder:text-slate-300"
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
              disabled={loading}
              className="w-full bg-brand-500 hover:bg-brand-600 disabled:opacity-60 disabled:cursor-not-allowed
                         text-white font-semibold py-2.5 rounded-lg text-sm transition-colors
                         flex items-center justify-center gap-2"
            >
              {loading && <Loader2 className="w-4 h-4 animate-spin" />}
              {loading ? 'جارٍ إنشاء الحساب...' : 'إنشاء الحساب'}
            </button>
          </form>

          <p className="text-center text-xs text-slate-500">
            لديك حساب بالفعل؟{' '}
            <Link to="/login" className="text-brand-600 font-medium hover:underline">
              تسجيل الدخول
            </Link>
          </p>
        </div>

        <p className="text-center text-slate-600 text-xs mt-6">
          مدعوم بواسطة نحلة AI
        </p>

        <div className="text-center mt-4 pb-2">
          <p className="text-slate-400 text-xs">
            تطوير وإدارة:{' '}
            <span className="text-slate-500 font-medium">تركي بن عايد الحارثي</span>
          </p>
          <p className="text-slate-400 text-xs">
            المدير التنفيذي والمؤسس · nahlah.ai
          </p>
        </div>
      </div>
    </div>
  )
}
