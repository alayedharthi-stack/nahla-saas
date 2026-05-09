import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { Eye, EyeOff, AlertCircle, Loader2, ArrowRight } from 'lucide-react'
import {
  loginDetailed, getDefaultRoute, pingAuth,
  getApiBase, hasRuntimeApiBaseOverride, setApiBaseOverride, clearServiceWorkersAndCaches,
} from '../auth'
import { useLanguage } from '../i18n/context'
import LegalFooter from '../components/LegalFooter'
import TrustBlock from '../components/TrustBlock'

// Railway-generated domain — used as the fallback host when the
// custom domain edge (Cloudflare/Railway proxy in front of
// api.nahlah.ai) is dropping requests. Keep this in sync with the
// service name in Railway.
const RAILWAY_FALLBACK_BASE = 'https://nahla-saas-production.up.railway.app'

interface PingState {
  status:     number
  durationMs: number
  ok:         boolean
  error?:     string
}

export default function Login() {
  const navigate = useNavigate()
  const { t, lang, setLang, dir } = useLanguage()
  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [error,    setError]    = useState('')
  const [loading,  setLoading]  = useState(false)

  // ── Diagnostics state ───────────────────────────────────────────────────
  // Visible to the operator on the page itself (not just DevTools).
  // Refreshed on mount and after each user-triggered "Recheck".
  const [diagOpen, setDiagOpen] = useState(false)
  const apiBase = getApiBase()
  const usingOverride = hasRuntimeApiBaseOverride()
  const [ping,     setPing]     = useState<PingState | null>(null)
  const [pinging,  setPinging]  = useState(false)
  const [swStatus, setSwStatus] = useState<string>('')

  const runPing = async () => {
    setPinging(true)
    try {
      const r = await pingAuth()
      const next: PingState = {
        ok:         r.ok,
        status:     r.status,
        durationMs: r.durationMs,
        error:      r.error,
      }
      setPing(next)
      // eslint-disable-next-line no-console
      if (r.ok) console.info('[auth] /auth/ping OK', r)
      else      console.error('[auth] /auth/ping FAILED', r)
    } finally {
      setPinging(false)
    }
  }

  // ── Connectivity probe on mount ─────────────────────────────────────────
  // Runs once when the login page mounts. Tells us in DevTools AND on
  // the page itself whether the browser can even reach the API host
  // before the user types credentials. If this fails the issue is API
  // base URL / CORS / service-worker cache, NOT the password.
  useEffect(() => {
    let cancelled = false
    void pingAuth().then(r => {
      if (cancelled) return
      setPing({
        ok: r.ok, status: r.status, durationMs: r.durationMs, error: r.error,
      })
      // eslint-disable-next-line no-console
      if (r.ok) console.info('[auth] /auth/ping OK', r)
      else      console.error('[auth] /auth/ping FAILED', r)
    })
    return () => { cancelled = true }
  }, [])

  const switchToRailway = () => {
    setApiBaseOverride(RAILWAY_FALLBACK_BASE)
    window.location.reload()
  }

  const switchToCustomDomain = () => {
    setApiBaseOverride(null)
    window.location.reload()
  }

  const wipeServiceWorker = async () => {
    setSwStatus(lang === 'ar' ? 'جارٍ المسح…' : 'Clearing…')
    const r = await clearServiceWorkersAndCaches()
    setSwStatus(lang === 'ar'
      ? `تم المسح: ${r.swCount} SW + ${r.cacheCount} cache. أعد تحميل الصفحة.`
      : `Cleared ${r.swCount} SW + ${r.cacheCount} caches. Please reload.`)
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    // eslint-disable-next-line no-console
    console.info('[auth] login submit', { apiBase: getApiBase() })
    setError('')
    setLoading(true)
    try {
      const r = await loginDetailed(email, password)
      if (r.ok) {
        // Route strictly by role — owners never land on merchant /overview, which
        // would call merchant-scoped endpoints with the owner JWT.
        navigate(getDefaultRoute(), { replace: true })
        return
      }
      // Map the structured failure to a specific Arabic message so the
      // operator can tell credential-bad from network-down at a glance.
      switch (r.reason) {
        case 'unauthorized':
          setError(t(tr => tr.login.invalidCreds))
          break
        case 'timeout':
          setError(lang === 'ar'
            ? 'تأخر الخادم في الرد (٢٠ ثانية). تأكد من اتصال الإنترنت ثم حاول مرة أخرى.'
            : 'Server did not respond within 20s. Check your connection and retry.')
          break
        case 'network':
          setError(lang === 'ar'
            ? 'تعذّر الاتصال بالخادم. تحقق من الإنترنت أو حالة الخدمة.'
            : 'Could not reach the server. Check your network or service status.')
          break
        case 'http':
          setError(lang === 'ar'
            ? `فشل تسجيل الدخول (HTTP ${r.status ?? '??'}).`
            : `Login failed (HTTP ${r.status ?? '??'}).`)
          break
        case 'parse':
          setError(lang === 'ar'
            ? 'استجابة الخادم غير صحيحة. حاول مرة أخرى.'
            : 'Bad server response. Try again.')
          break
        default:
          setError(t(tr => tr.login.invalidCreds))
      }
    } catch (err) {
      // Defensive — loginDetailed already swallows everything, but a
      // try/finally here means setLoading(false) ALWAYS runs.
      // eslint-disable-next-line no-console
      console.error('[auth] handleSubmit unexpected error', err)
      setError(lang === 'ar'
        ? 'حدث خطأ غير متوقع. حاول مرة أخرى.'
        : 'An unexpected error occurred. Try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-slate-900" dir={dir}>
      <div
        className="min-h-dvh flex flex-col items-center justify-center px-4 pt-safe-extra pb-safe-bottom"
      >
        <div className="w-full max-w-sm">
        {/* Top bar: back to landing + language toggle */}
        <div className="flex items-center justify-between mb-4">
          <Link
            to="/landing"
            className="pub-top-btn gap-1.5 text-xs text-slate-400 hover:text-white transition rounded-lg"
          >
            <ArrowRight className="w-3.5 h-3.5 rtl:rotate-180" />
            {lang === 'ar' ? 'الرئيسية' : 'Home'}
          </Link>
          <button
            onClick={() => setLang(lang === 'ar' ? 'en' : 'ar')}
            className="pub-top-btn text-xs text-slate-400 hover:text-white border border-slate-600 rounded-lg transition"
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

        {/* ── Connection diagnostics ────────────────────────────────────
            Always visible (collapsed) so the operator can fix a stuck
            login WITHOUT opening DevTools. Shows the live API base,
            ping result, and gives one-click access to:
              - retry ping
              - switch to the Railway-generated domain (when the
                custom-domain edge is dropping OPTIONS/POSTs)
              - switch back to the custom domain
              - clear stale service workers + caches
        */}
        <div className="mt-4 text-xs text-slate-400">
          <button
            type="button"
            onClick={() => setDiagOpen(v => !v)}
            className="underline hover:text-slate-200"
          >
            {lang === 'ar' ? 'تشخيص الاتصال' : 'Connection diagnostics'}
            {ping && !ping.ok && (
              <span className="ms-2 text-red-400">
                ({lang === 'ar' ? 'فشل' : 'failing'})
              </span>
            )}
          </button>

          {diagOpen && (
            <div className="mt-2 rounded-lg border border-slate-700 bg-slate-800/60 p-3 space-y-2">
              <div className="font-mono break-all text-[11px] text-slate-300">
                <span className="text-slate-500">API: </span>{apiBase}
                {usingOverride && (
                  <span className="ms-1 text-amber-400">
                    ({lang === 'ar' ? 'تجاوز' : 'override'})
                  </span>
                )}
              </div>
              <div className="font-mono text-[11px]">
                <span className="text-slate-500">/auth/ping: </span>
                {!ping
                  ? <span className="text-slate-400">{pinging ? '…' : '—'}</span>
                  : ping.ok
                    ? <span className="text-emerald-400">OK {ping.status} · {ping.durationMs}ms</span>
                    : <span className="text-red-400">
                        {lang === 'ar' ? 'فشل' : 'FAIL'} {ping.status || ''} · {ping.durationMs}ms
                        {ping.error ? ` · ${ping.error}` : ''}
                      </span>}
              </div>
              <div className="flex flex-wrap gap-2 pt-1">
                <button
                  type="button"
                  onClick={runPing}
                  disabled={pinging}
                  className="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-100 disabled:opacity-50"
                >
                  {pinging
                    ? (lang === 'ar' ? 'جارٍ…' : 'Pinging…')
                    : (lang === 'ar' ? 'إعادة فحص' : 'Recheck')}
                </button>
                {!usingOverride && (
                  <button
                    type="button"
                    onClick={switchToRailway}
                    className="px-2 py-1 rounded bg-amber-700/50 hover:bg-amber-600/60 text-amber-100"
                    title={RAILWAY_FALLBACK_BASE}
                  >
                    {lang === 'ar' ? 'استخدم دومين Railway' : 'Use Railway domain'}
                  </button>
                )}
                {usingOverride && (
                  <button
                    type="button"
                    onClick={switchToCustomDomain}
                    className="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-100"
                  >
                    {lang === 'ar' ? 'العودة للدومين الأصلي' : 'Back to custom domain'}
                  </button>
                )}
                <button
                  type="button"
                  onClick={wipeServiceWorker}
                  className="px-2 py-1 rounded bg-slate-700 hover:bg-slate-600 text-slate-100"
                >
                  {lang === 'ar' ? 'مسح Service Worker والكاش' : 'Clear SW + caches'}
                </button>
              </div>
              {swStatus && (
                <div className="text-[11px] text-slate-300">{swStatus}</div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="mt-6 pb-4 flex flex-col items-center gap-2">
          <div className="flex items-center justify-center gap-2 text-slate-500 text-xs font-medium tracking-wide">
            <span>{t(tr => tr.login.dev)}</span>
            <img
              src="/flag-sa.png"
              alt="العلم السعودي"
              width={26}
              height={17}
              className="shrink-0 rounded-sm shadow-sm object-cover"
            />
          </div>
          <LegalFooter variant="light" />
        </div>
        </div>

        {/* Trust block — same width as the form (max-w-sm) so it looks
            like a natural continuation of the card on every screen size */}
        <div className="w-full max-w-sm mt-4 mb-8">
          <TrustBlock variant="light" compact />
        </div>
      </div>
    </div>
  )
}
