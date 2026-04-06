import { useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Eye, EyeOff, AlertCircle, Loader2 } from 'lucide-react'
import { login } from '../auth'
import { useLanguage } from '../i18n/context'
export default function Login() {
  const navigate = useNavigate()
  const { t, lang, setLang, dir } = useLanguage()
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
      setError(t(tr => tr.login.invalidCreds))
      setLoading(false)
    }
  }

  return (
    <div
      className="min-h-dvh flex items-center justify-center bg-slate-900 px-4 py-safe-top pb-safe-bottom"
      dir={dir}
    >
      <div className="w-full max-w-sm">
        {/* Language toggle */}
        <div className="flex justify-end mb-4">
          <button
            onClick={() => setLang(lang === 'ar' ? 'en' : 'ar')}
            className="text-xs text-slate-400 hover:text-white border border-slate-600 rounded-lg px-3 py-1.5 transition"
          >
            {lang === 'ar' ? 'English' : 'العربية'}
          </button>
        </div>

        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <img src="/logo.png" alt="نحلة" className="w-20 h-20 object-contain mb-3 drop-shadow-xl" />
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-white">{t(tr => tr.login.title)}</h1>
            <span className="inline-flex items-center px-2 py-0.5 rounded-md bg-amber-500/15 border border-amber-500/50 shadow-[0_0_10px_rgba(245,158,11,0.35)]">
              <span className="text-[11px] font-black text-amber-400 leading-none tracking-wide">AI</span>
            </span>
          </div>
          <p className="text-slate-400 text-sm mt-1">{t(tr => tr.login.subtitle)}</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          <h2 className="text-base font-semibold text-slate-800 text-center">
            {t(tr => tr.login.submitBtn)}
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
                {t(tr => tr.login.emailLabel)}
              </label>
              <input
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder={t(tr => tr.login.emailPlaceholder)}
                dir="ltr"
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Password */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                {t(tr => tr.login.passwordLabel)}
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
                >
                  {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <div className="flex justify-end">
              <Link to="/forgot-password" className="text-xs text-brand-600 hover:underline">
                {t(tr => tr.login.forgotPassword)}
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
              {loading ? t(tr => tr.login.submitting) : t(tr => tr.login.submitBtn)}
            </button>
          </form>
        </div>

        <p className="text-center text-slate-500 text-xs mt-4">
          {t(tr => tr.login.noAccount)}{' '}
          <Link to="/register" className="text-brand-400 font-medium hover:underline">
            {t(tr => tr.login.registerLink)}
          </Link>
        </p>

        {/* Footer */}
        <div className="mt-6 pb-4 flex flex-col items-center gap-2">
          {/* Made in Saudi Arabia */}
          <p className="text-slate-500 text-xs font-medium tracking-wide">
            {t(tr => tr.login.dev)}
          </p>
        </div>
      </div>
    </div>
  )
}
