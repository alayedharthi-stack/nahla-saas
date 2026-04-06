import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Eye, EyeOff, AlertCircle, Loader2, CheckCircle } from 'lucide-react'
import { register } from '../auth'
import { useLanguage } from '../i18n/context'

export default function Register() {
  const navigate = useNavigate()
  const { t, lang, setLang, dir } = useLanguage()
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

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('invite') ?? ''
    setInviteToken(token)
    if (token) {
      fetch(`/api/auth/invite/${token}`)
        .then(r => r.json())
        .then(data => {
          setInviteValid(!!data.valid)
          if (data.invited_email) setEmail(data.invited_email)
        })
        .catch(() => setInviteValid(false))
    } else {
      setInviteValid(null)
    }
  }, [])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')

    if (password !== confirm) {
      setError(lang === 'ar' ? 'كلمتا المرور غير متطابقتين' : 'Passwords do not match')
      return
    }
    if (password.length < 8) {
      setError(lang === 'ar' ? 'كلمة المرور يجب أن تكون 8 أحرف على الأقل' : 'Password must be at least 8 characters')
      return
    }

    setLoading(true)
    const result = await register(email, password, storeName, phone, inviteToken)
    if (result.ok) {
      navigate('/overview', { replace: true })
    } else {
      setError(result.error ?? (lang === 'ar' ? 'فشل التسجيل' : 'Registration failed'))
      setLoading(false)
    }
  }

  if (inviteToken && inviteValid === false) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir={dir}>
        <div className="w-full max-w-sm bg-white rounded-2xl shadow-xl p-8 text-center space-y-4">
          <AlertCircle className="w-10 h-10 text-red-500 mx-auto" />
          <h2 className="text-lg font-bold text-slate-900">
            {lang === 'ar' ? 'رابط الدعوة غير صالح' : 'Invalid Invitation Link'}
          </h2>
          <p className="text-slate-500 text-sm">
            {lang === 'ar'
              ? 'هذا الرابط منتهي الصلاحية أو غير صحيح. تواصل مع المالك للحصول على رابط جديد.'
              : 'This link has expired or is invalid. Contact the owner for a new link.'}
          </p>
          <Link to="/login" className="block text-brand-600 text-sm font-medium hover:underline">
            {t(tr => tr.login.submitBtn)}
          </Link>
        </div>
      </div>
    )
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
          <p className="text-slate-400 text-sm mt-1">{t(tr => tr.register.subtitle)}</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          <h2 className="text-base font-semibold text-slate-800 text-center">
            {t(tr => tr.register.title)}
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
                {t(tr => tr.register.storeNameLabel)}
              </label>
              <input
                type="text"
                required
                value={storeName}
                onChange={e => setStoreName(e.target.value)}
                placeholder={t(tr => tr.register.storeNamePh)}
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Email */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                {t(tr => tr.register.emailLabel)}
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

            {/* Phone */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                {t(tr => tr.register.phoneLabel)}
                <span className="text-slate-400 font-normal me-1">
                  ({t(tr => tr.common.optional)})
                </span>
              </label>
              <input
                type="tel"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder={t(tr => tr.register.phonePh)}
                dir="ltr"
                className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-lg
                           focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent
                           placeholder:text-slate-300"
              />
            </div>

            {/* Password */}
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1.5">
                {t(tr => tr.register.passwordLabel)}
              </label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  required
                  autoComplete="new-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder={lang === 'ar' ? '8 أحرف على الأقل' : 'At least 8 characters'}
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
                {lang === 'ar' ? 'تأكيد كلمة المرور' : 'Confirm Password'}
              </label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  required
                  autoComplete="new-password"
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                  placeholder={lang === 'ar' ? 'أعد إدخال كلمة المرور' : 'Re-enter password'}
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
              {loading ? t(tr => tr.register.submitting) : t(tr => tr.register.submitBtn)}
            </button>
          </form>

          <p className="text-center text-xs text-slate-500">
            {t(tr => tr.register.hasAccount)}{' '}
            <Link to="/login" className="text-brand-600 font-medium hover:underline">
              {t(tr => tr.register.loginLink)}
            </Link>
          </p>
        </div>

        {/* Footer */}
        <div className="mt-5 pb-4 flex flex-col items-center">
          <p className="text-slate-500 text-xs font-medium tracking-wide">
            {t(tr => tr.login.dev)}
          </p>
        </div>
      </div>
    </div>
  )
}
