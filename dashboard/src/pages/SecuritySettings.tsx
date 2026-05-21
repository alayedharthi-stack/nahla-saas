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
import { Shield, ShieldCheck, ShieldAlert, Copy, Download, AlertTriangle, Check, KeyRound, X, ChevronDown, ChevronUp, Smartphone, Apple, ExternalLink, Star } from 'lucide-react'

import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { apiCall } from '../api/client'
import {
  confirmTwoFactorSetup,
  disableTwoFactor,
  getTwoFactorStatus,
  refreshTwoFactorSetupCandidates,
  startTwoFactorSetup,
  type TotpCandidateCode,
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
  // Structured diagnostics surfaced by /setup/confirm when the OTP is
  // rejected. Carries server_unix, code_length, setup_age_sec, etc. —
  // populated only on failure so the user can see exactly what's off.
  const [confirmDiag, setConfirmDiag] = useState<{
    serverUnix?:    number
    codeLength?:    number
    setupAgeSec?:   number
    validWindow?:   number
    timeStepSec?:   number
    buildMarker?:   string
    clockSkewSec?:  number
    /** Set when the server reports a config issue (e.g. TOTP_ENC_KEY missing). */
    serverConfigCode?: string
    /** Operator-facing hint surfaced by the backend for admin-only errors. */
    operatorHint?:     string
  } | null>(null)
  // Soft clock-skew warning shown the moment the QR appears. We compare
  // the server timestamp returned by /setup/start with the local clock
  // and tell the user upfront if their phone clock is likely drifted —
  // this prevents the 6-digit code from ever being accepted otherwise.
  const [clockSkewSec, setClockSkewSec] = useState<number | null>(null)
  // Three-code picker: candidates from the server + a local ticking
  // clock used to compute remaining validity per button and to trigger
  // a refresh just before they expire.
  const [candidates, setCandidates]       = useState<TotpCandidateCode[]>([])
  const [refreshingCands, setRefreshingCands] = useState(false)
  const [nowUnix, setNowUnix]             = useState<number>(() => Math.floor(Date.now() / 1000))
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

  // ── Picker tick: drives countdowns + expiry detection ──────────────────
  // 1Hz is plenty — the visual changes per second (remaining countdown)
  // and the auto-refresh decision triggers off whole-second boundaries.
  // We only tick while the picker is actually visible to avoid an idle
  // setInterval running for the entire dashboard session.
  useEffect(() => {
    if (phase !== 'setup' || candidates.length === 0) return
    const id = window.setInterval(() => {
      setNowUnix(Math.floor(Date.now() / 1000))
    }, 1000)
    return () => window.clearInterval(id)
  }, [phase, candidates.length])

  // ── Auto-refresh candidates just before they all expire ─────────────────
  // The latest button (t+1) stays valid until valid_until_unix. Once we're
  // within 3 seconds of every candidate expiring, fetch a fresh triple so
  // the picker never goes dark in front of the user.
  useEffect(() => {
    if (phase !== 'setup' || !setup || candidates.length === 0) return
    const maxValid = Math.max(...candidates.map(c => c.valid_until_unix))
    if (nowUnix < maxValid - 3) return
    if (refreshingCands) return
    let alive = true
    ;(async () => {
      setRefreshingCands(true)
      try {
        const fresh = await refreshTwoFactorSetupCandidates(setup.setup_token)
        if (alive) setCandidates(fresh.candidate_codes || [])
      } catch {
        // Soft failure — user can still type the code manually. We log
        // to the console so an ops person can grep but don't surface
        // an error banner that would distract from the main flow.
        // eslint-disable-next-line no-console
        console.warn('[2fa] candidate refresh failed; manual entry still works')
      } finally {
        if (alive) setRefreshingCands(false)
      }
    })()
    return () => { alive = false }
  }, [nowUnix, candidates, phase, setup, refreshingCands])

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
    setCandidates([])
    setRefreshingCands(false)
    setConfirmDiag(null)
    setClockSkewSec(null)
  }

  async function onStartSetup() {
    setBusy(true); setErr(null); setInfo(null); setConfirmDiag(null); setClockSkewSec(null); setCandidates([])
    try {
      const data = await startTwoFactorSetup()
      setSetup(data)
      setPhase('setup')
      setCandidates(data.candidate_codes || [])
      setNowUnix(Math.floor(Date.now() / 1000))
      // Detect device-vs-server clock skew the moment we get the QR.
      // The server returned its own unix timestamp; anything > 25s of
      // drift means the 6-digit code is almost guaranteed to fail.
      if (typeof data.server_unix === 'number') {
        const localUnix = Math.floor(Date.now() / 1000)
        const skew = localUnix - data.server_unix
        setClockSkewSec(skew)
      }
    } catch (e: any) {
      setErr(e?.message || tr.errorGeneric)
    } finally {
      setBusy(false)
    }
  }

  async function onConfirmSetup(overrideCode?: string) {
    if (!setup) return
    const submittedCode = (overrideCode ?? otp).trim()
    if (submittedCode.length < 6) return
    setBusy(true); setErr(null); setConfirmDiag(null)
    try {
      const data = await confirmTwoFactorSetup({ setupToken: setup.setup_token, otp: submittedCode })
      setCodes(data.recovery_codes)
      setPhase('showCodes')
      setOtp('')
    } catch (e: any) {
      const msg = e?.message || tr.errorBadOtp
      setErr(msg)
      // Pull structured fields the server sent in `detail` — apiCall
      // copies scalar keys directly onto the Error object.
      const serverUnix = typeof e?.server_unix === 'number' ? e.server_unix : undefined
      const localUnix  = Math.floor(Date.now() / 1000)
      const skew       = typeof serverUnix === 'number' ? (localUnix - serverUnix) : undefined
      const isServerConfigError = e?.code === 'totp_enc_key_missing'
      if (
        isServerConfigError ||
        e?.code === 'totp_invalid' ||
        typeof e?.server_unix === 'number' ||
        typeof e?.setup_age_sec === 'number'
      ) {
        setConfirmDiag({
          serverUnix:   serverUnix,
          codeLength:   typeof e?.code_length === 'number' ? e.code_length : undefined,
          setupAgeSec:  typeof e?.setup_age_sec === 'number' ? e.setup_age_sec : undefined,
          validWindow:  typeof e?.valid_window === 'number' ? e.valid_window : undefined,
          timeStepSec:  typeof e?.time_step_sec === 'number' ? e.time_step_sec : undefined,
          buildMarker:  typeof e?.build_marker === 'string' ? e.build_marker : undefined,
          clockSkewSec: skew,
          serverConfigCode: isServerConfigError ? e.code : undefined,
          operatorHint:     typeof e?.operator_hint === 'string' ? e.operator_hint : undefined,
        })
      }
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
                <ul className="mt-4 grid gap-3 sm:grid-cols-3">
                  {[
                    {
                      key: 'google',
                      name: tr.setupStep1AppGoogle,
                      recommended: true,
                      initial: 'G',
                      iconBg: 'bg-gradient-to-br from-blue-500 via-emerald-500 to-amber-500',
                      ring: 'ring-2 ring-amber-400 dark:ring-amber-500',
                      ios: 'https://apps.apple.com/app/google-authenticator/id388497605',
                      android: 'https://play.google.com/store/apps/details?id=com.google.android.apps.authenticator2',
                    },
                    {
                      key: 'microsoft',
                      name: tr.setupStep1AppMicrosoft,
                      recommended: false,
                      initial: 'M',
                      iconBg: 'bg-gradient-to-br from-sky-500 to-blue-700',
                      ring: '',
                      ios: 'https://apps.apple.com/app/microsoft-authenticator/id983156458',
                      android: 'https://play.google.com/store/apps/details?id=com.azure.authenticator',
                    },
                    {
                      key: 'authy',
                      name: tr.setupStep1AppAuthy,
                      recommended: false,
                      initial: 'A',
                      iconBg: 'bg-gradient-to-br from-rose-500 to-red-700',
                      ring: '',
                      ios: 'https://apps.apple.com/app/twilio-authy/id494168017',
                      android: 'https://play.google.com/store/apps/details?id=com.authy.authy',
                    },
                  ].map((app) => (
                    <li
                      key={app.key}
                      className={`relative rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/40 p-4 shadow-sm hover:shadow-md hover:border-slate-300 dark:hover:border-slate-600 transition ${app.ring}`}
                    >
                      {app.recommended && (
                        <span className="absolute -top-2 start-3 inline-flex items-center gap-1 rounded-full bg-amber-500 text-white text-[10px] font-semibold px-2 py-0.5 shadow">
                          <Star className="w-3 h-3 fill-current" />
                          {isRTL ? 'الأنسب' : 'Recommended'}
                        </span>
                      )}

                      <div className="flex items-center gap-3 mb-3">
                        <div className={`shrink-0 w-10 h-10 rounded-xl ${app.iconBg} text-white font-bold text-lg flex items-center justify-center shadow-sm`}>
                          {app.initial}
                        </div>
                        <div className="min-w-0">
                          <p className="font-semibold text-slate-900 dark:text-slate-100 text-sm leading-tight truncate">
                            {app.name}
                          </p>
                          <p className="text-[11px] text-slate-500 dark:text-slate-400">
                            {isRTL ? 'تطبيق مصادقة' : 'Authenticator app'}
                          </p>
                        </div>
                      </div>

                      <div className="flex flex-col gap-1.5">
                        <a
                          href={app.ios}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center justify-between gap-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 px-3 py-2 text-xs font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-900/70 hover:border-slate-300 dark:hover:border-slate-600 transition"
                        >
                          <span className="inline-flex items-center gap-2">
                            <Apple className="w-4 h-4" />
                            {tr.setupStep1AppStoreIOS}
                          </span>
                          <ExternalLink className="w-3 h-3 text-slate-400" />
                        </a>
                        <a
                          href={app.android}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center justify-between gap-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/40 px-3 py-2 text-xs font-medium text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-900/70 hover:border-slate-300 dark:hover:border-slate-600 transition"
                        >
                          <span className="inline-flex items-center gap-2">
                            <Smartphone className="w-4 h-4" />
                            {tr.setupStep1AppStoreAndroid}
                          </span>
                          <ExternalLink className="w-3 h-3 text-slate-400" />
                        </a>
                      </div>
                    </li>
                  ))}
                </ul>
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

                {candidates.length > 0 && (
                  <div className="mt-3">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs text-slate-500 dark:text-slate-400">{tr.pickerHint}</p>
                      <button
                        type="button"
                        onClick={async () => {
                          if (!setup || refreshingCands) return
                          setRefreshingCands(true)
                          try {
                            const fresh = await refreshTwoFactorSetupCandidates(setup.setup_token)
                            setCandidates(fresh.candidate_codes || [])
                          } catch { /* picker stays as-is; manual entry still works */ }
                          finally { setRefreshingCands(false) }
                        }}
                        disabled={refreshingCands || busy}
                        className="text-[11px] text-brand-600 dark:text-brand-400 hover:text-brand-700 dark:hover:text-brand-300 disabled:opacity-60 font-medium"
                      >
                        {refreshingCands ? tr.pickerRefreshing : tr.pickerRefresh}
                      </button>
                    </div>

                    <div className="grid grid-cols-3 gap-2">
                      {candidates.map((c) => {
                        const remaining = c.valid_until_unix - nowUnix
                        const expired   = remaining <= 0
                        const isNow     = c.t_offset === 0
                        const badgeText =
                          c.t_offset === -1 ? tr.pickerBadgePrev :
                          c.t_offset === 0  ? tr.pickerBadgeNow  :
                                              tr.pickerBadgeNext
                        const pct = expired
                          ? 0
                          : Math.max(0, Math.min(100, Math.round(((c.valid_until_unix - nowUnix) / 30) * 100)))
                        return (
                          <button
                            key={c.t_offset}
                            type="button"
                            disabled={busy || expired}
                            onClick={() => {
                              setOtp(c.code)
                              onConfirmSetup(c.code)
                            }}
                            className={[
                              'group relative overflow-hidden rounded-xl border px-2 py-3 text-center transition',
                              expired
                                ? 'border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/40 text-slate-400 cursor-not-allowed'
                                : isNow
                                  ? 'border-brand-400 dark:border-brand-500 bg-brand-50 dark:bg-brand-900/30 text-slate-900 dark:text-slate-100 hover:border-brand-500 hover:shadow-sm'
                                  : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/60 text-slate-900 dark:text-slate-100 hover:border-brand-400 dark:hover:border-brand-500 hover:shadow-sm',
                            ].join(' ')}
                          >
                            <span
                              className={[
                                'absolute top-1 inline-flex items-center text-[9px] font-semibold uppercase tracking-wide rounded-full px-1.5 py-0.5',
                                isRTL ? 'end-1' : 'start-1',
                                isNow
                                  ? 'bg-brand-500 text-white'
                                  : 'bg-slate-200 dark:bg-slate-700 text-slate-600 dark:text-slate-300',
                              ].join(' ')}
                            >
                              {badgeText}
                            </span>

                            <p
                              className="font-mono text-base sm:text-lg font-bold tracking-[0.18em] mt-3 mb-1.5"
                              style={{ direction: 'ltr' }}
                            >
                              {expired ? '——————' : `${c.code.slice(0, 3)} ${c.code.slice(3)}`}
                            </p>

                            <div className="h-1 w-full bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                              <div
                                className={`h-full transition-all duration-1000 ease-linear ${
                                  isNow ? 'bg-brand-500' : 'bg-slate-400 dark:bg-slate-500'
                                }`}
                                style={{ width: `${pct}%` }}
                              />
                            </div>
                            <p className="mt-1 text-[10px] text-slate-500 dark:text-slate-400">
                              {expired
                                ? tr.pickerExpired
                                : `${Math.max(0, remaining)}s`}
                            </p>
                          </button>
                        )
                      })}
                    </div>

                    <p className="mt-2 text-[11px] text-slate-400 dark:text-slate-500 leading-relaxed">
                      {tr.pickerSecurityNote}
                    </p>
                  </div>
                )}

                <label className="block mt-4">
                  <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
                    {candidates.length > 0 ? tr.pickerOrType : tr.otpLabel}
                  </span>
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
                    onClick={() => onConfirmSetup()}
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

                {clockSkewSec !== null && Math.abs(clockSkewSec) > 25 && (
                  <div className="mt-4 rounded-xl border border-amber-300 dark:border-amber-700/60 bg-amber-50 dark:bg-amber-900/20 px-3 py-2.5">
                    <div className="flex items-start gap-2">
                      <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
                      <div className="text-xs text-amber-900 dark:text-amber-100 leading-relaxed">
                        <p className="font-semibold mb-0.5">
                          {isRTL
                            ? `يبدو أن ساعة جهازك مختلفة عن ساعة الخادم بـ ${Math.abs(clockSkewSec)} ثانية تقريباً.`
                            : `Your device clock is ~${Math.abs(clockSkewSec)} seconds off from the server.`}
                        </p>
                        <p className="text-amber-800 dark:text-amber-200">
                          {isRTL
                            ? 'لكي يقبل الخادم رمز المصادقة، فعّل "الوقت التلقائي" / Network time في إعدادات جوّالك، ثم أعد توليد رمز جديد من التطبيق.'
                            : 'Enable "Automatic / Network time" on your phone, then read a freshly generated code from the authenticator.'}
                        </p>
                      </div>
                    </div>
                  </div>
                )}

                {confirmDiag?.serverConfigCode === 'totp_enc_key_missing' && (
                  <div className="mt-4 rounded-xl border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/25 px-3 py-3 text-xs">
                    <div className="flex items-start gap-2 mb-2">
                      <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
                      <p className="font-semibold text-amber-900 dark:text-amber-100">
                        {isRTL ? 'إعداد الخادم ناقص (ليس خطأ منك)' : 'Server configuration missing (not your fault)'}
                      </p>
                    </div>
                    <p className="ms-6 text-amber-900 dark:text-amber-100 leading-relaxed mb-2">
                      {isRTL
                        ? 'الرمز الذي أدخلته صحيح، لكن الخادم لا يستطيع حفظ السر بأمان لأنّ مفتاح التشفير غير مضبوط.'
                        : 'Your code is correct, but the server can\'t store the secret because the encryption key isn\'t configured.'}
                    </p>
                    {confirmDiag.operatorHint && (
                      <pre
                        className="ms-6 mt-1 p-2 rounded-lg bg-slate-900 text-slate-100 dark:bg-slate-950 dark:text-slate-200 text-[11px] leading-relaxed overflow-auto whitespace-pre-wrap"
                        dir="ltr"
                      >
                        {confirmDiag.operatorHint}
                      </pre>
                    )}
                    {confirmDiag.buildMarker && (
                      <p className="ms-6 mt-2 text-[10px] text-amber-700 dark:text-amber-300 font-mono">
                        build: {confirmDiag.buildMarker}
                      </p>
                    )}
                  </div>
                )}

                {confirmDiag && confirmDiag.serverConfigCode !== 'totp_enc_key_missing' && (
                  <div className="mt-4 rounded-xl border border-rose-200 dark:border-rose-800/60 bg-rose-50 dark:bg-rose-900/20 px-3 py-2.5 text-xs">
                    <div className="flex items-start gap-2 mb-2">
                      <AlertTriangle className="w-4 h-4 text-rose-600 dark:text-rose-400 shrink-0 mt-0.5" />
                      <p className="font-semibold text-rose-900 dark:text-rose-100">
                        {isRTL ? 'تشخيص فشل التحقق' : 'Verification diagnostics'}
                      </p>
                    </div>
                    <ul className="ms-6 space-y-0.5 text-rose-900/90 dark:text-rose-100/90 font-mono">
                      {typeof confirmDiag.codeLength === 'number' && (
                        <li>
                          {isRTL ? 'طول الرمز المدخل: ' : 'Entered code length: '}
                          <span className="font-semibold">{confirmDiag.codeLength}</span>
                          {confirmDiag.codeLength !== 6 && (
                            <span className="text-rose-700 dark:text-rose-300">
                              {' '}{isRTL ? '← يجب أن يكون 6 أرقام' : '← must be 6 digits'}
                            </span>
                          )}
                        </li>
                      )}
                      {typeof confirmDiag.clockSkewSec === 'number' && (
                        <li>
                          {isRTL ? 'فرق الساعة (جهازك − الخادم): ' : 'Clock skew (device − server): '}
                          <span className={`font-semibold ${Math.abs(confirmDiag.clockSkewSec) > 25 ? 'text-rose-700 dark:text-rose-300' : ''}`}>
                            {confirmDiag.clockSkewSec >= 0 ? '+' : ''}{confirmDiag.clockSkewSec}s
                          </span>
                          {Math.abs(confirmDiag.clockSkewSec) > 25 && (
                            <span className="text-rose-700 dark:text-rose-300">
                              {' '}{isRTL ? '← الفرق كبير' : '← out of range'}
                            </span>
                          )}
                        </li>
                      )}
                      {typeof confirmDiag.setupAgeSec === 'number' && (
                        <li>
                          {isRTL ? 'عمر جلسة الإعداد: ' : 'Setup session age: '}
                          <span className="font-semibold">{confirmDiag.setupAgeSec}s</span>
                          {confirmDiag.setupAgeSec > 9 * 60 && (
                            <span className="text-rose-700 dark:text-rose-300">
                              {' '}{isRTL ? '← اقتربت من الانتهاء (10 دقائق)' : '← near 10 min limit'}
                            </span>
                          )}
                        </li>
                      )}
                      {typeof confirmDiag.validWindow === 'number' && typeof confirmDiag.timeStepSec === 'number' && (
                        <li>
                          {isRTL ? 'نافذة القبول: ' : 'Acceptance window: '}
                          <span className="font-semibold">
                            ±{confirmDiag.validWindow * confirmDiag.timeStepSec}s
                          </span>
                        </li>
                      )}
                      {confirmDiag.buildMarker && (
                        <li className="text-rose-700/80 dark:text-rose-300/80">
                          build: <span>{confirmDiag.buildMarker}</span>
                        </li>
                      )}
                    </ul>
                    <p className="mt-2 ms-6 text-rose-800/90 dark:text-rose-200/90">
                      {isRTL
                        ? 'الأكثر شيوعاً: حساب خاطئ داخل التطبيق (تأكد من اختيار "Nahla AI") أو رمز انتهى للتو — أعد قراءة الرمز الجديد من التطبيق ثم أدخله بسرعة.'
                        : 'Most common causes: wrong account in your authenticator (pick "Nahla AI") or a code that just rotated — read a fresh one and submit it quickly.'}
                    </p>
                  </div>
                )}
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
