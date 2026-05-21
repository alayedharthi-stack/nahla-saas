// ── Security & 2FA settings page ─────────────────────────────────────────────
// Phase 2A Sprint 1 — enrol TOTP, show recovery codes once, disable 2FA.
//
// State machine
// ─────────────
//   idle         → first paint while we fetch /auth/2fa/status
//   notEnrolled  → user has no row in user_totp; show "Enable" CTA
//   setup        → /setup/start succeeded; show QR + manual secret + OTP input
//   showCodes    → /setup/confirm succeeded; show recovery codes ONCE
//   enabled      → 2FA is live; show status + Disable CTA
//   disabling    → modal asking for password + OTP, calls /disable
//
// All UI text routes through useLanguage().t(tr => tr.security.*) so the
// page is fully RTL/LTR + AR/EN out of the box.

import { useEffect, useMemo, useState } from 'react'
import { QRCodeSVG } from 'qrcode.react'
import { Shield, ShieldCheck, ShieldAlert, Copy, Download, AlertTriangle, Check, KeyRound, X, ChevronDown, ChevronUp } from 'lucide-react'

import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { apiCall } from '../api/client'
import {
  confirmTwoFactorSetup,
  disableTwoFactor,
  getTwoFactorStatus,
  startTwoFactorSetup,
  type TwoFactorSetupStart,
  type TwoFactorStatus,
} from '../api/twofa'

// Shape of the read-only diagnostic endpoint /auth/2fa/__diag. We
// intentionally don't import this from the api module so the rest of
// the app doesn't gain a `__diag` surface — it's only used here as a
// fallback when /status 500s, to show the operator what's actually
// going wrong without another deploy cycle.
type TwoFactorDiag = {
  build_marker?:      string
  jwt_claims?: {
    sub?:           string | null
    role?:          string | null
    has_user_id?:   boolean
    is_env_admin?:  boolean
  }
  admin_email_config?: string
  tables?:             Record<string, boolean | string>
  tenant_1_present?:   boolean | string
  env_admin_user?: {
    present?:           boolean | string
    id?:                number | null
    password_hash_null?: boolean | null
    role?:              string | null
    tenant_id?:         number | null
    username?:          string | null
  }
}

type Phase = 'loading' | 'notEnrolled' | 'setup' | 'showCodes' | 'enabled'

export default function SecuritySettings() {
  const { t, isRTL, lang } = useLanguage()
  const tr = t(tt => tt.security)

  const [phase, setPhase]       = useState<Phase>('loading')
  const [status, setStatus]     = useState<TwoFactorStatus | null>(null)
  const [setup, setSetup]       = useState<TwoFactorSetupStart | null>(null)
  const [codes, setCodes]       = useState<string[]>([])
  const [otp, setOtp]           = useState('')
  const [busy, setBusy]         = useState(false)
  const [err, setErr]           = useState<string | null>(null)
  const [info, setInfo]         = useState<string | null>(null)
  const [secretCopied, setSecretCopied] = useState(false)
  const [codesCopied, setCodesCopied]   = useState(false)
  const [disableOpen, setDisableOpen]   = useState(false)
  const [disablePwd, setDisablePwd]     = useState('')
  const [disableOtp, setDisableOtp]     = useState('')

  // Diagnostic surface — populated only when /status fails. Holds the
  // structured error fields the backend attaches to a 500 (via main.py's
  // global handler, twofa.py's wrapper, or middleware fallback) plus the
  // contents of the zero-side-effect /auth/2fa/__diag endpoint when we
  // can reach it. The collapsible panel is the operator's main signal
  // when "Internal server error" shows up.
  const [diag, setDiag]               = useState<{
    error?: {
      message?:     string
      code?:        string
      exc_class?:   string
      build_marker?: string
      incident_id?: string
      middleware?:  string
      path?:        string
      method?:      string
      status?:      number
    }
    probe?: TwoFactorDiag | null
    probeError?: string
  } | null>(null)
  const [showDiag, setShowDiag]       = useState(false)

  // ── Initial status load ──────────────────────────────────────────────────
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const s = await getTwoFactorStatus()
        if (!alive) return
        setStatus(s)
        setPhase(s.enabled ? 'enabled' : 'notEnrolled')
      } catch (e: any) {
        if (!alive) return
        // Surface diagnostic fields from the structured backend payload
        // so support / ops can copy them straight from the UI without
        // needing Railway access. The backend now attaches structured
        // fields on EVERY 500 path:
        //   * twofa.py /status route-level wrapper       → code/exc_class/build_marker
        //   * main.py global Exception handler           → incident_id + exc_class
        //   * middleware.py _safe_fallback_response      → middleware + exc_class
        // The api client (buildApiError) copies all scalar keys from
        // `detail` onto the Error object so `e?.exc_class` etc. work
        // without an extra `.detail` hop.
        const baseMsg = e?.message || tr.errorGeneric
        const diagBits: string[] = []
        if (e?.code) diagBits.push(`code=${e.code}`)
        if (e?.exc_class) diagBits.push(`exc=${e.exc_class}`)
        if (e?.incident_id) diagBits.push(`incident=${e.incident_id}`)
        if (e?.build_marker) diagBits.push(`build=${e.build_marker}`)
        if (typeof e?.status === 'number') diagBits.push(`http=${e.status}`)
        const composed = diagBits.length > 0
          ? `${baseMsg}  [${diagBits.join(' · ')}]`
          : baseMsg
        setErr(composed)
        setPhase('notEnrolled')

        // Auto-fall back to the zero-side-effect /__diag endpoint so the
        // operator can see (a) which tables exist on the deployed schema,
        // (b) whether the env-admin row is provisioned, and (c) the JWT
        // claims actually arriving at the route. This makes the failure
        // self-explanatory without another deploy.
        const errorBlock = {
          message:      e?.message,
          code:         e?.code,
          exc_class:    e?.exc_class,
          build_marker: e?.build_marker,
          incident_id:  e?.incident_id,
          middleware:   e?.middleware,
          path:         e?.path,
          method:       e?.method,
          status:       typeof e?.status === 'number' ? e.status : undefined,
        }
        let probe: TwoFactorDiag | null = null
        let probeError: string | undefined
        try {
          probe = await apiCall<TwoFactorDiag>('/auth/2fa/__diag', { method: 'GET' })
        } catch (pe: any) {
          probeError = pe?.message || 'فشل فحص التشخيص'
        }
        if (!alive) return
        setDiag({ error: errorBlock, probe, probeError })
        setShowDiag(true)

        // Also echo to console — devtools is the fastest paste-back for ops.
        // eslint-disable-next-line no-console
        console.error('[2fa] /status failed', {
          ...errorBlock,
          detail: e?.detail,
          probe,
          probeError,
        })
      }
    })()
    return () => { alive = false }
  }, [])

  // ── Helpers ──────────────────────────────────────────────────────────────
  const resetFlow = () => {
    setSetup(null)
    setCodes([])
    setOtp('')
    setErr(null)
    setInfo(null)
    setSecretCopied(false)
    setCodesCopied(false)
  }

  async function onStartSetup() {
    setBusy(true); setErr(null); setInfo(null)
    try {
      const data = await startTwoFactorSetup()
      setSetup(data)
      setPhase('setup')
    } catch (e: any) {
      setErr(e?.message || tr.errorGeneric)
    } finally {
      setBusy(false)
    }
  }

  async function onConfirmSetup() {
    if (!setup) return
    setBusy(true); setErr(null)
    try {
      const data = await confirmTwoFactorSetup({ setupToken: setup.setup_token, otp })
      setCodes(data.recovery_codes)
      setPhase('showCodes')
      setOtp('')
    } catch (e: any) {
      const msg = e?.message || tr.errorBadOtp
      setErr(msg)
    } finally {
      setBusy(false)
    }
  }

  async function onAfterShowCodes() {
    // Reload status, transition to enabled
    try {
      const s = await getTwoFactorStatus()
      setStatus(s)
    } catch { /* ignore — status reload is decorative */ }
    resetFlow()
    setPhase('enabled')
    setInfo(tr.successEnabled)
  }

  async function onDisable() {
    setBusy(true); setErr(null)
    try {
      await disableTwoFactor({ password: disablePwd, otp: disableOtp })
      setDisableOpen(false)
      setDisablePwd('')
      setDisableOtp('')
      const s = await getTwoFactorStatus().catch(() => null)
      if (s) setStatus(s)
      setPhase('notEnrolled')
      setInfo(tr.successDisabled)
    } catch (e: any) {
      setErr(e?.message || tr.errorGeneric)
    } finally {
      setBusy(false)
    }
  }

  function copyText(value: string, onOk: () => void) {
    if (!navigator.clipboard) return
    navigator.clipboard.writeText(value).then(() => {
      onOk()
      window.setTimeout(() => { onOk() }, 0) // satisfy linter
    }).catch(() => { /* ignore */ })
  }

  function downloadCodesTxt() {
    const account = setup?.account || status?.enrolled_at || 'nahla'
    const header = lang === 'ar'
      ? `أكواد استرداد Nahla AI — ${account}\nاحفظها في مكان آمن. كل كود يُستخدم مرة واحدة فقط.\n\n`
      : `Nahla AI recovery codes — ${account}\nKeep them somewhere safe. Each code works only once.\n\n`
    const body = codes.map((c, i) => `${String(i + 1).padStart(2, '0')}.  ${c}`).join('\n')
    const blob = new Blob([header + body + '\n'], { type: 'text/plain;charset=utf-8' })
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href = url
    a.download = 'nahla-recovery-codes.txt'
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const formattedEnrolled = useMemo(() => {
    if (!status?.enrolled_at) return null
    try {
      const d = new Date(status.enrolled_at)
      return d.toLocaleDateString(lang === 'ar' ? 'ar-SA' : 'en-US', {
        year: 'numeric', month: 'short', day: 'numeric',
      })
    } catch { return status.enrolled_at }
  }, [status?.enrolled_at, lang])

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6" dir={isRTL ? 'rtl' : 'ltr'}>
      <PageHeader title={tr.pageTitle} subtitle={tr.pageSubtitle} />

      {/* Generic flashes */}
      {err && (
        <div className="card p-4 border-red-200 bg-red-50 flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 text-red-600 mt-0.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="text-sm text-red-700">{err}</p>
            {diag && (
              <button
                type="button"
                onClick={() => setShowDiag(v => !v)}
                className="mt-2 inline-flex items-center gap-1 text-xs font-semibold text-red-700 hover:text-red-800"
              >
                {showDiag ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                {showDiag
                  ? (lang === 'ar' ? 'إخفاء التشخيص' : 'Hide diagnostics')
                  : (lang === 'ar' ? 'عرض التشخيص التفصيلي' : 'Show detailed diagnostics')}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Diagnostic panel — operator-facing details from the structured
          500 body + /auth/2fa/__diag probe. Renders ONLY when /status
          fails. Read-only; nothing here triggers a write. */}
      {diag && showDiag && (
        <div className="card p-4 border-amber-200 bg-amber-50/60 dark:bg-amber-950/20 space-y-3">
          <p className="text-xs font-semibold text-amber-800 dark:text-amber-200">
            {lang === 'ar' ? 'تشخيص الخطأ (للدعم الفني)' : 'Diagnostic snapshot (for support)'}
          </p>

          {/* Error block */}
          {diag.error && (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-xs">
              {Object.entries(diag.error).map(([k, v]) =>
                v == null || v === '' ? null : (
                  <div key={k} className="contents">
                    <span className="text-slate-500 dark:text-slate-400">{k}</span>
                    <code className="col-span-1 sm:col-span-2 break-all text-slate-800 dark:text-slate-100 font-mono">
                      {String(v)}
                    </code>
                  </div>
                )
              )}
            </div>
          )}

          {/* Probe block */}
          <div className="pt-2 border-t border-amber-200/60 dark:border-amber-800/40">
            <p className="text-xs font-semibold text-slate-700 dark:text-slate-300 mb-1">
              {lang === 'ar' ? 'نتيجة الفحص:' : 'Probe result:'}
            </p>
            {diag.probeError ? (
              <p className="text-xs text-red-700 dark:text-red-300 font-mono">
                {diag.probeError}
              </p>
            ) : diag.probe ? (
              <pre className="text-xs leading-relaxed bg-white/70 dark:bg-slate-900/40 border border-amber-200/60 dark:border-amber-800/40 rounded-lg p-2 overflow-auto max-h-72 text-slate-800 dark:text-slate-100 text-left" dir="ltr">
{JSON.stringify(diag.probe, null, 2)}
              </pre>
            ) : (
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {lang === 'ar' ? 'لم يصل أي تشخيص.' : 'No probe data.'}
              </p>
            )}
          </div>

          <p className="text-[11px] text-slate-500 dark:text-slate-400 leading-relaxed">
            {lang === 'ar'
              ? 'انسخ المحتوى أعلاه وأرفقه عند مراسلة الدعم — لا تحتوي هذه البيانات على أي معلومات سرية (لا توكنات ولا أسرار TOTP).'
              : 'Copy the block above when contacting support — it contains no secrets (no tokens, no TOTP material).'}
          </p>
        </div>
      )}
      {info && phase !== 'showCodes' && (
        <div className="card p-4 border-emerald-200 bg-emerald-50 flex items-start gap-2">
          <Check className="w-4 h-4 text-emerald-600 mt-0.5 shrink-0" />
          <p className="text-sm text-emerald-700">{info}</p>
        </div>
      )}

      {/* ── 2FA card ─────────────────────────────────────────────────────── */}
      <div className="card p-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-3 min-w-0">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${
              phase === 'enabled'
                ? 'bg-emerald-50 text-emerald-600'
                : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
            }`}>
              {phase === 'enabled' ? <ShieldCheck className="w-5 h-5" /> : <Shield className="w-5 h-5" />}
            </div>
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">{tr.twoFactorTitle}</h2>
              <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{tr.twoFactorDesc}</p>
              {phase === 'enabled' && (
                <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-slate-500 dark:text-slate-400">
                  <span className="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400 font-medium">
                    <ShieldCheck className="w-3.5 h-3.5" />
                    {tr.statusEnabled}
                  </span>
                  {formattedEnrolled && (
                    <span>{tr.enrolledAt}: <span className="font-medium text-slate-700 dark:text-slate-300">{formattedEnrolled}</span></span>
                  )}
                  {typeof status?.recovery_codes_remaining === 'number' && (
                    <span>{tr.recoveryRemaining}: <span className="font-medium text-slate-700 dark:text-slate-300">{status.recovery_codes_remaining}</span></span>
                  )}
                </div>
              )}
              {phase !== 'enabled' && phase !== 'setup' && phase !== 'showCodes' && (
                <p className="mt-3 text-xs text-slate-500 dark:text-slate-400 inline-flex items-center gap-1.5">
                  <ShieldAlert className="w-3.5 h-3.5" />
                  {tr.statusDisabled}
                </p>
              )}
            </div>
          </div>

          {/* Top-right action — context-sensitive */}
          {phase === 'notEnrolled' && (
            <button
              type="button"
              onClick={onStartSetup}
              disabled={busy}
              className="bg-brand-600 hover:bg-brand-500 disabled:opacity-60 text-white text-sm font-semibold px-4 py-2 rounded-xl transition-colors"
            >
              {busy ? '…' : tr.enableBtn}
            </button>
          )}
          {phase === 'enabled' && (
            <button
              type="button"
              onClick={() => { setDisableOpen(true); setErr(null) }}
              className="text-red-600 hover:text-red-500 dark:text-red-400 text-sm font-semibold px-4 py-2 rounded-xl border border-red-200 hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors"
            >
              {tr.disableBtn}
            </button>
          )}
        </div>

        {/* ── Phase: setup (QR + OTP) ──────────────────────────────────── */}
        {phase === 'setup' && setup && (
          <div className="mt-6 pt-6 border-t border-slate-200 dark:border-slate-700 space-y-5">
            <ol className="space-y-3 text-sm">
              <li>
                <p className="font-semibold text-slate-900 dark:text-slate-100">{tr.setupStep1}</p>
                <p className="text-slate-500 dark:text-slate-400 mt-0.5">{tr.setupStep1Desc}</p>
              </li>
              <li>
                <p className="font-semibold text-slate-900 dark:text-slate-100">{tr.setupStep2}</p>
                <p className="text-slate-500 dark:text-slate-400 mt-0.5">{tr.setupStep2Desc}</p>
              </li>
            </ol>

            <div className="flex flex-col sm:flex-row items-start gap-5">
              {/* QR */}
              <div className="bg-white p-3 rounded-2xl border border-slate-200 dark:border-slate-700 shrink-0">
                <QRCodeSVG value={setup.otpauth_url} size={180} includeMargin={false} level="M" />
              </div>

              {/* Manual secret */}
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-500 dark:text-slate-400 mb-1.5">{tr.cantScan}</p>
                <p className="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">{tr.manualSecretLabel}</p>
                <div className="flex items-center gap-2">
                  <code className="flex-1 min-w-0 break-all bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 text-sm font-mono text-slate-700 dark:text-slate-200">
                    {setup.secret_b32}
                  </code>
                  <button
                    type="button"
                    onClick={() => copyText(setup.secret_b32, () => {
                      setSecretCopied(true)
                      window.setTimeout(() => setSecretCopied(false), 2000)
                    })}
                    className="shrink-0 inline-flex items-center gap-1 text-xs font-medium text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 px-2.5 py-2 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800"
                    title={tr.copySecret}
                  >
                    {secretCopied ? <Check className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5" />}
                    {secretCopied ? tr.secretCopied : tr.copySecret}
                  </button>
                </div>

                <div className="mt-5">
                  <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">{tr.setupStep3}</p>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{tr.setupStep3Desc}</p>
                </div>

                <label className="block mt-3">
                  <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{tr.otpLabel}</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    value={otp}
                    onChange={e => setOtp(e.target.value.replace(/\D/g, '').slice(0, 8))}
                    placeholder={tr.otpPlaceholder}
                    className="mt-1 w-44 px-3 py-2 text-lg font-mono tracking-widest text-center border border-slate-200 dark:border-slate-700 rounded-xl bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
                  />
                </label>

                <div className="mt-4 flex items-center gap-3">
                  <button
                    type="button"
                    onClick={onConfirmSetup}
                    disabled={busy || otp.length < 6}
                    className="bg-brand-600 hover:bg-brand-500 disabled:opacity-60 text-white text-sm font-semibold px-4 py-2 rounded-xl"
                  >
                    {busy ? tr.verifying : tr.verifyBtn}
                  </button>
                  <button
                    type="button"
                    onClick={() => { resetFlow(); setPhase('notEnrolled') }}
                    disabled={busy}
                    className="text-sm text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
                  >
                    {tr.cancel}
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ── Phase: showCodes (one-time display) ──────────────────────── */}
        {phase === 'showCodes' && (
          <div className="mt-6 pt-6 border-t border-slate-200 dark:border-slate-700 space-y-4">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 bg-amber-50 rounded-xl flex items-center justify-center shrink-0">
                <KeyRound className="w-5 h-5 text-amber-600" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">{tr.recoveryTitle}</h3>
                <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">{tr.recoveryDesc}</p>
              </div>
            </div>

            <div className="bg-amber-50 border border-amber-200 rounded-xl p-3">
              <p className="text-xs font-semibold text-amber-800">{tr.recoveryWarning}</p>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
              {codes.map((c, i) => (
                <code
                  key={i}
                  className="bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg px-2.5 py-2 text-sm font-mono text-center text-slate-800 dark:text-slate-100 tracking-wider"
                >
                  {c}
                </code>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => copyText(codes.join('\n'), () => {
                  setCodesCopied(true)
                  window.setTimeout(() => setCodesCopied(false), 2000)
                })}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700 dark:text-slate-200 border border-slate-200 dark:border-slate-700 px-3 py-2 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800"
              >
                {codesCopied ? <Check className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5" />}
                {codesCopied ? tr.codesCopied : tr.copyAll}
              </button>
              <button
                type="button"
                onClick={downloadCodesTxt}
                className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700 dark:text-slate-200 border border-slate-200 dark:border-slate-700 px-3 py-2 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800"
              >
                <Download className="w-3.5 h-3.5" />
                {tr.downloadTxt}
              </button>
              <button
                type="button"
                onClick={onAfterShowCodes}
                className="bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold px-4 py-2 rounded-xl ms-auto"
              >
                {tr.iSavedThem}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── Disable modal ─────────────────────────────────────────────────── */}
      {disableOpen && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-2xl shadow-xl p-6">
            <div className="flex items-start justify-between gap-2">
              <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">{tr.disableTitle}</h3>
              <button
                type="button"
                onClick={() => { setDisableOpen(false); setErr(null); setDisablePwd(''); setDisableOtp('') }}
                className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                aria-label={tr.cancel}
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">{tr.disableDesc}</p>

            <label className="block mt-4">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{tr.currentPassword}</span>
              <input
                type="password"
                value={disablePwd}
                onChange={e => setDisablePwd(e.target.value)}
                autoComplete="current-password"
                className="mt-1 w-full px-3 py-2 border border-slate-200 dark:border-slate-700 rounded-xl bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </label>

            <label className="block mt-3">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-400">{tr.otpOrRecovery}</span>
              <input
                type="text"
                value={disableOtp}
                onChange={e => setDisableOtp(e.target.value)}
                autoComplete="one-time-code"
                className="mt-1 w-full px-3 py-2 font-mono border border-slate-200 dark:border-slate-700 rounded-xl bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
              />
            </label>

            <div className="mt-5 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => { setDisableOpen(false); setErr(null); setDisablePwd(''); setDisableOtp('') }}
                disabled={busy}
                className="text-sm text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100 px-3 py-2"
              >
                {tr.cancel}
              </button>
              <button
                type="button"
                onClick={onDisable}
                disabled={busy || !disablePwd || !disableOtp}
                className="bg-red-600 hover:bg-red-500 disabled:opacity-60 text-white text-sm font-semibold px-4 py-2 rounded-xl"
              >
                {busy ? tr.disabling : tr.confirmDisable}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
