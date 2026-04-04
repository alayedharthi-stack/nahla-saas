import { useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Sparkles, Eye, EyeOff, AlertCircle, Loader2 } from 'lucide-react'
import { login } from '../auth'

export default function Login() {
  const navigate = useNavigate()
  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [error,    setError]    = useState('')
  const [loading,  setLoading]  = useState(false)

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    const ok = await login(email, password)
    if (ok) {
      navigate('/overview', { replace: true })
    } else {
      setError('البريد الإلكتروني أو كلمة المرور غير صحيحة')
      setLoading(false)
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center bg-slate-900 px-4"
      dir="rtl"
    >
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <img src="/logo.png" alt="نحلة" className="w-20 h-20 object-contain mb-3 drop-shadow-xl" />
          <h1 className="text-2xl font-bold text-white">نحلة</h1>
          <p className="text-slate-400 text-sm mt-1">لوحة التحكم</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          <h2 className="text-base font-semibold text-slate-800 text-center">
            تسجيل الدخول
          </h2>

          {error && (
            <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2.5 text-sm">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
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
                placeholder="admin@nahlah.ai"
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
                  autoComplete="current-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="••••••••"
                  dir="ltr"
                  className="w-full px-3 py-2.5 pe-10 text-sm border border-slate-200 rounded-lg
                             focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                             placeholder:text-slate-300"
                />
                <button
                  type="button"
                  onClick={() => setShowPw(s => !s)}
                  className="absolute inset-y-0 end-0 pe-3 flex items-center text-slate-400 hover:text-slate-600"
                  aria-label={showPw ? 'إخفاء' : 'إظهار'}
                >
                  {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <div className="flex justify-end">
              <Link to="/forgot-password" className="text-xs text-brand-600 hover:underline">
                نسيت كلمة المرور؟
              </Link>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-brand-500 hover:bg-brand-600 disabled:opacity-60 disabled:cursor-not-allowed
                         text-white font-semibold py-2.5 rounded-lg text-sm transition-colors
                         flex items-center justify-center gap-2"
            >
              {loading && <Loader2 className="w-4 h-4 animate-spin" />}
              {loading ? 'جارٍ تسجيل الدخول...' : 'تسجيل الدخول'}
            </button>
          </form>
        </div>

        <p className="text-center text-slate-500 text-xs mt-4">
          متجر جديد؟{' '}
          <Link to="/register" className="text-brand-400 font-medium hover:underline">
            أنشئ حساباً مجاناً
          </Link>
        </p>

        <p className="text-center text-slate-600 text-xs mt-3">
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
