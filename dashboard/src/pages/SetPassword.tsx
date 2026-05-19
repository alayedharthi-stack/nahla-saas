import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Sparkles, Eye, EyeOff, AlertCircle, Loader2, CheckCircle, Mail } from 'lucide-react'
import { API_BASE } from '../api/client'

/**
 * /set-password
 *
 * Lands here from the welcome email sent on Salla / Zid auto-create.
 * The link looks like:
 *
 *   https://app.nahlah.ai/set-password?token=<43-char-base64url>
 *
 * Flow:
 *   1. On mount → call GET /auth/set-password/verify?token=...
 *      - If valid: render the form with the email pre-filled (read-only).
 *      - If used / expired / invalid → render a tailored error UI with
 *        a CTA back to /login (the merchant can still log in via Salla
 *        and use forgot-password from there).
 *   2. On submit → POST /auth/set-password { token, password }.
 *      - Backend consumes the token + bcrypts the new password.
 *      - We bounce the user to /login so they prove they know it.
 *
 * The page intentionally does NOT issue a session — the merchant
 * authenticates the next time, which:
 *   - confirms they typed what they meant
 *   - keeps the auth surface uniform (login is the only path that
 *     mints a JWT outside OAuth)
 */
type VerifyState =
  | { status: 'loading' }
  | { status: 'valid';   email: string; expiresAt: string | null }
  | { status: 'invalid' | 'expired' | 'used' | 'missing'; reason?: string }

export default function SetPassword() {
  const navigate = useNavigate()
  const [token,    setToken]    = useState('')
  const [password, setPassword] = useState('')
  const [confirm,  setConfirm]  = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState('')
  const [done,     setDone]     = useState(false)
  const [verify,   setVerify]   = useState<VerifyState>({ status: 'loading' })

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const t = params.get('token') ?? ''
    setToken(t)

    if (!t) {
      setVerify({ status: 'missing' })
      return
    }

    let cancelled = false
    ;(async () => {
      try {
        const res = await fetch(
          `${API_BASE}/auth/set-password/verify?token=${encodeURIComponent(t)}`,
          { method: 'GET' },
        )
        const data = await res.json().catch(() => ({}))
        if (cancelled) return
        if (data?.valid) {
          setVerify({ status: 'valid', email: data.email, expiresAt: data.expires_at ?? null })
        } else {
          const r = (data?.reason as VerifyState['status']) ?? 'invalid'
          setVerify({ status: r === 'valid' ? 'invalid' : r })
        }
      } catch {
        if (!cancelled) setVerify({ status: 'invalid' })
      }
    })()
    return () => { cancelled = true }
  }, [])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    if (password !== confirm) { setError('كلمتا المرور غير متطابقتين'); return }
    if (password.length < 8)  { setError('كلمة المرور يجب أن تكون 8 أحرف على الأقل'); return }

    setLoading(true)
    try {
      const res  = await fetch(`${API_BASE}/auth/set-password`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ token, password }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        if (res.status === 410) {
          setError(data?.detail ?? 'انتهت صلاحية الرابط أو تم استخدامه. اطلب رابطاً جديداً.')
        } else {
          setError(data?.detail ?? 'فشلت عملية تعيين كلمة المرور')
        }
        return
      }
      setDone(true)
      setTimeout(() => navigate('/login', { replace: true }), 2500)
    } catch {
      setError('تعذّر الاتصال بالخادم. حاول مرة أخرى.')
    } finally {
      setLoading(false)
    }
  }

  // ── Error states ─────────────────────────────────────────────────────────
  if (verify.status === 'missing' || verify.status === 'invalid' ||
      verify.status === 'used'    || verify.status === 'expired') {
    const headline = verify.status === 'used'
      ? 'تم استخدام هذا الرابط من قبل'
      : verify.status === 'expired'
      ? 'انتهت صلاحية الرابط'
      : 'الرابط غير صالح'
    const body = verify.status === 'used'
      ? 'هذا الرابط لتعيين كلمة المرور استُخدم من قبل. إذا نسيت كلمة المرور استعدها من صفحة تسجيل الدخول.'
      : verify.status === 'expired'
      ? 'الرابط صالح لمدة محدودة فقط. يمكنك تسجيل الدخول من سلة كالمعتاد، أو طلب رابط استعادة كلمة المرور.'
      : 'تأكد أنك نسخت الرابط كاملاً من البريد. لا يزال بإمكانك الدخول من سلة بدون كلمة مرور.'
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir="rtl">
        <div className="w-full max-w-sm">
          <div className="flex flex-col items-center mb-8">
            <div className="w-14 h-14 bg-brand-500 rounded-2xl flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">
              <Sparkles className="w-7 h-7 text-white" />
            </div>
            <h1 className="text-2xl font-bold text-white">نحلة</h1>
          </div>
          <div className="bg-white rounded-2xl shadow-xl p-6 space-y-4 text-center">
            <AlertCircle className="w-10 h-10 text-amber-500 mx-auto" />
            <h2 className="font-bold text-slate-900">{headline}</h2>
            <p className="text-slate-600 text-sm leading-7">{body}</p>
            <div className="flex flex-col gap-2 pt-2">
              <Link to="/login"
                className="bg-brand-500 hover:bg-brand-600 text-white font-semibold py-2.5 rounded-lg text-sm">
                تسجيل الدخول
              </Link>
              <Link to="/forgot-password"
                className="text-brand-600 hover:underline text-xs">
                نسيت كلمة المرور؟
              </Link>
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ── Loading state ────────────────────────────────────────────────────────
  if (verify.status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir="rtl">
        <Loader2 className="w-8 h-8 text-white animate-spin" />
      </div>
    )
  }

  // ── Valid token — show the form ──────────────────────────────────────────
  // The early returns above narrowed `verify.status` to all error states +
  // 'loading'. TypeScript can't always carry that narrowing through to here
  // when the union has more than two non-valid variants chained with `||`,
  // so we capture the email in a local `const` after one explicit check.
  // Falling back to '' is purely a typescript-soothing default — control
  // flow guarantees we only reach this block when status === 'valid'.
  const accountEmail = verify.status === 'valid' ? verify.email : ''
  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-900 px-4" dir="rtl">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 bg-brand-500 rounded-2xl flex items-center justify-center mb-4 shadow-lg shadow-brand-500/30">
            <Sparkles className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">نحلة</h1>
          <p className="text-slate-400 text-sm mt-1">تعيين كلمة مرور لحسابك</p>
        </div>

        <div className="bg-white rounded-2xl shadow-xl p-6 space-y-5">
          {done ? (
            <div className="text-center space-y-4 py-2">
              <CheckCircle className="w-10 h-10 text-emerald-500 mx-auto" />
              <h2 className="font-bold text-slate-900">تم تعيين كلمة المرور ✅</h2>
              <p className="text-slate-500 text-sm">سيتم تحويلك لصفحة تسجيل الدخول...</p>
            </div>
          ) : (
            <>
              <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5 flex items-center gap-2">
                <Mail className="w-4 h-4 text-amber-600 shrink-0" />
                <span className="text-xs text-slate-700">
                  الحساب: <span className="font-mono text-slate-900">{accountEmail}</span>
                </span>
              </div>

              <p className="text-xs text-slate-500 leading-6">
                هذه كلمة مرور مستقلة لتسجيل الدخول المباشر إلى لوحة نحلة عبر
                <span className="mx-1 font-mono">app.nahlah.ai</span>.
                الدخول من سلة سيظل يعمل دائماً بدون كلمة مرور.
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
                    كلمة المرور
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
                  {loading ? 'جارٍ الحفظ...' : 'تعيين كلمة المرور'}
                </button>
              </form>

              <p className="text-center text-xs text-slate-500">
                <Link to="/login" className="text-brand-600 font-medium hover:underline">
                  أو ادخل باستخدام كلمة مرور موجودة
                </Link>
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
