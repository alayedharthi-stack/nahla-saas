/**
 * WhatsAppConnect.tsx
 * ────────────────────
 * Primary: Meta Embedded Signup (merchant's own WABA)
 * Fallback: Direct add (platform WABA — requires BSP permissions)
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  BadgeCheck,
  Building2,
  CheckCircle2,
  ChevronRight,
  Globe,
  Loader2,
  Mail,
  MessageCircle,
  Phone,
  RefreshCw,
  ShieldCheck,
  Unplug,
  XCircle,
} from 'lucide-react'
import { apiCall } from '../api/client'
import { whatsappConnectApi, type WaConnection } from '../api/whatsappConnect'
import { useLanguage } from '../i18n/context'
import type { Translations } from '../i18n/types'

/** Map stored Arabic connLabel values to localized display — logic keys unchanged. */
function displayConnLabel(raw: string, wc: Translations['whatsappConnect']): string {
  if (raw === 'ربط عبر Meta') return wc.connLabels.viaMeta
  if (raw === 'واتساب الجوال + الذكاء الاصطناعي') return wc.connLabels.coexistence
  if (raw === 'واتساب الأعمال') return wc.connLabels.business
  if (raw.startsWith('واتساب يدوي — ID:')) {
    return `${wc.connLabels.manualPrefix} ${raw.replace('واتساب يدوي — ID:', '').trim()}`
  }
  return raw
}

function isCoexistenceConnLabel(raw: string): boolean {
  return raw === 'واتساب الجوال + الذكاء الاصطناعي'
}

// ── Facebook SDK types ────────────────────────────────────────────────────────
declare global {
  interface Window {
    FB: any
    fbAsyncInit: () => void
  }
}

// ── Embedded Signup Component ─────────────────────────────────────────────────

interface EmbeddedPhone { id: string; number: string; name: string; verified: boolean }

interface EmbeddedStatusPayload {
  connected: boolean
  status: string
  connection_status?: string
  phone_number?: string
  display_name?: string
  connected_at?: string
  phone_number_id?: string
  waba_id?: string
  sending_enabled?: boolean
  verification_status?: string | null
  name_status?: string | null
  meta_phone_status?: string | null
  message?: string | null
  last_error?: string | null
  oauth_session_status?: string | null
  oauth_session_message?: string | null
  oauth_session_needs_reauth?: boolean
  provider?: string | null
  provider_label?: string | null
  merchant_channel_label?: string | null
  connection_type?: string | null
  coexistence_status?: string | null
  action_required_message?: string | null
  request_submitted_at?: string | null
  coexistence_available?: boolean
  phones?: EmbeddedPhone[]
}

function CoexistenceFlow({
  status,
  onConnected,
}: {
  status: WaConnection | null
  onConnected: (payload?: { phone_number?: string; display_name?: string; connected_at?: string }) => void
}) {
  const { t, lang } = useLanguage()
  const c = t(tr => tr.whatsappConnect.coexistence)
  const [phone, setPhone] = useState(status?.phone_number ?? '')
  const [displayName, setDisplayName] = useState(status?.display_name ?? '')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [localStatus, setLocalStatus] = useState<WaConnection | null>(status)

  useEffect(() => { setLocalStatus(status) }, [status])

  useEffect(() => {
    if (localStatus?.status === 'connected') {
      onConnected({
        phone_number: localStatus.phone_number ?? undefined,
        display_name: localStatus.display_name ?? undefined,
        connected_at: localStatus.connected_at ?? undefined,
      })
    }
  }, [localStatus, onConnected])

  // Poll while waiting for team activation
  useEffect(() => {
    if (!localStatus || !['request_submitted', 'pending_activation', 'action_required'].includes(localStatus.status)) return
    let cancelled = false
    let timer: number | undefined
    const poll = async () => {
      try {
        const next = await whatsappConnectApi.getCoexistenceStatus()
        if (!cancelled) {
          setLocalStatus(next)
          if (next.status === 'connected') return
        }
      } catch { /* keep last known state */ }
      if (!cancelled) timer = window.setTimeout(poll, 8000)
    }
    timer = window.setTimeout(poll, 4000)
    return () => { cancelled = true; if (timer) window.clearTimeout(timer) }
  }, [localStatus?.status])

  const submitRequest = async () => {
    if (!phone.trim()) { setError(c.phoneRequired); return }
    setBusy(true); setError('')
    try {
      const result = await whatsappConnectApi.requestCoexistence({
        phone_number: phone.trim(),
        display_name: displayName.trim() || undefined,
        has_whatsapp_business_app: true,
        understands_keep_app_installed: true,
        understands_open_every_13_days: true,
        notes: notes.trim() || undefined,
      })
      setLocalStatus(result)
    } catch (e) {
      setError(e instanceof Error ? e.message : c.submitFailed)
    } finally {
      setBusy(false)
    }
  }

  const current = localStatus
  const tipsBlock = (
    <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-600 space-y-2">
      {[c.tipKeepApp, c.tipDontDelete, c.tipOpenPeriodically].map(tip => (
        <div key={tip} className="flex items-center gap-2">
          <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" /> {tip}
        </div>
      ))}
    </div>
  )

  // ── Connected ────────────────────────────────────────────────────────────
  if (current?.status === 'connected') {
    return (
      <div className="space-y-4">
        <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5">
          <p className="text-lg font-bold text-emerald-800">{c.connectedTitle}</p>
          <p className="mt-2 text-sm text-emerald-700">
            {c.connectedBody}
          </p>
          {current.phone_number && <p className="mt-3 text-sm font-mono text-emerald-800">{current.phone_number}</p>}
        </div>
        {tipsBlock}
      </div>
    )
  }

  // ── Submitted / pending ──────────────────────────────────────────────────
  if (current?.status === 'request_submitted' || current?.status === 'pending_activation' || current?.status === 'action_required') {
    const pendingTitle =
      current.status === 'request_submitted' ? c.statusRequestSubmitted
      : current.status === 'pending_activation' ? c.statusPendingActivation
      : c.statusActionRequired
    return (
      <div className="space-y-4">
        <div className={`rounded-2xl border p-5 ${current.status === 'action_required' ? 'border-amber-200 bg-amber-50' : 'border-blue-200 bg-blue-50'}`}>
          <p className="text-lg font-bold text-slate-800">
            {pendingTitle}
          </p>
          <p className="mt-2 text-sm text-slate-700">
            {current.action_required_message
              || current.last_error
              || c.defaultPendingMessage}
          </p>
          {current.request_submitted_at && (
            <p className="mt-3 text-xs text-slate-500">
              {c.requestTimeLabel} {new Date(current.request_submitted_at).toLocaleString(lang === 'ar' ? 'ar-SA' : 'en-US')}
            </p>
          )}
        </div>
        {tipsBlock}
      </div>
    )
  }

  // ── Request form ─────────────────────────────────────────────────────────
  return (
    <div className="space-y-5">
      <div className="text-center">
        <p className="text-lg font-bold text-slate-800">{c.formTitle}</p>
        <p className="mt-1 text-sm text-slate-500">
          {c.formSubtitle}
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600 space-y-2">
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" /> {c.benefitSameNumber}</div>
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" /> {c.benefitAiReplies}</div>
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" /> {c.benefitActivationTime}</div>
      </div>

      <div className="space-y-3">
        <input
          value={phone}
          onChange={e => setPhone(e.target.value)}
          placeholder={c.phonePlaceholder}
          className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm"
          dir="ltr"
        />
        <input
          value={displayName}
          onChange={e => setDisplayName(e.target.value)}
          placeholder={c.displayNamePlaceholder}
          className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm"
        />
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          placeholder={c.notesPlaceholder}
          rows={3}
          className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm"
        />
      </div>

      {error && <ErrorBox msg={error} />}

      <button
        onClick={submitRequest}
        disabled={busy}
        className="w-full rounded-xl bg-violet-600 py-3.5 text-sm font-bold text-white transition-all hover:bg-violet-500 disabled:opacity-60"
      >
        {busy ? c.submitting : c.submitBtn}
      </button>
    </div>
  )
}

function explainWhatsAppError(msg: unknown): string {
  const raw = typeof msg === 'string' ? msg.trim() : ''
  const m = raw.toLowerCase()

  if (!raw) return 'حدث خطأ غير متوقع أثناء ربط واتساب.'
  if (m.includes('131000') || m.includes('something went wrong')) {
    return 'حدث خلل مؤقت من Meta أثناء جلب حالة الرقم. إذا كان رمز التحقق قد وصل أو تم قبوله، انتظر قليلًا ثم اضغط تحديث الآن.'
  }
  if (m.includes('cors') || m.includes('failed to fetch') || m.includes('تعذر الوصول إلى الخادم')) {
    return 'تعذر الاتصال بـ API. السبب المرجّح: CORS أو انقطاع الشبكة أو خطأ مؤقت في الخادم.'
  }
  if (m.includes('انتهت جلسة meta') || m.includes('انتهت صلاحية الجلسة') || m.includes('token') || m.includes('authentication required') || m.includes('missing_token')) {
    return 'انتهت جلسة Meta الإدارية في نحلة. إذا كان الرقم ما زال ظاهرًا في Meta فالربط نفسه غالبًا مستمر، وقد تحتاج فقط إلى إعادة التفويض.'
  }
  if (m.includes('review') || m.includes('مراجعة') || m.includes('name') || m.includes('اسم العرض')) {
    return raw
  }
  if (m.includes('pending') || m.includes('تفعيل') || m.includes('cloud api')) {
    return raw
  }
  return raw
}

function EmbeddedSignupFlow({
  onConnected,
}: {
  onConnected: (payload?: { phone_number?: string; display_name?: string; connected_at?: string }) => void
}) {
  const { t } = useLanguage()
  const emb = t(tr => tr.whatsappConnect.embedded)
  const [stage, setStage]       = useState<'init'|'loading-sdk'|'ready'|'exchanging'|'select-phone'|'add-phone'|'requesting-code'|'verify-phone'|'syncing-phone'|'done'>('init')
  const [error, setError]       = useState('')
  const [phones, setPhones]     = useState<EmbeddedPhone[]>([])
  const [wabaId, setWabaId]     = useState('')
  const [busy, setBusy]         = useState(false)
  const sdkLoaded               = useRef(false)
  const embeddedStatusChecked   = useRef(false)

  const [configId, setConfigId] = useState('')
  // Whether the merchant's Meta app actually has the FB Login for
  // Business / WhatsApp Embedded Signup entitlement. Read from the
  // backend ``/embedded/config`` payload. When false we render a
  // "قريباً" state and DO NOT open the FB.login popup (Meta would
  // reject it with the BSP/TP entitlement error anyway).
  const [signupEnabled, setSignupEnabled] = useState<boolean | null>(null)
  const [disabledReason, setDisabledReason] = useState('')

  // Add-phone form state
  const [newPhone, setNewPhone]         = useState('')
  const [countryCode, setCountryCode]   = useState('966')
  const [displayName, setDisplayName]   = useState('')
  const [otpCode, setOtpCode]           = useState('')
  const [newPhoneId, setNewPhoneId]     = useState('')
  const [statusHint, setStatusHint]     = useState('')

  // Load Meta config + FB SDK on mount
  useEffect(() => {
    let cancelled = false
    async function loadSdk() {
      setStage('loading-sdk')
      try {
        const cfg = await apiCall<{
          app_id: string
          config_id: string
          embedded_signup_config_id?: string
          graph_version: string
          embedded_signup_enabled?: boolean
          disabled_reason?: string
          oauth_start_path?: string | null
        }>('/whatsapp/embedded/config')
        if (cancelled) return
        const cfgId = cfg.embedded_signup_config_id || cfg.config_id || ''
        if (cfgId) setConfigId(cfgId)

        // Gate the entire flow on the backend's enablement flag. When
        // disabled we render a "قريباً / قيد التفعيل" state — we do
        // NOT load the FB SDK because the popup would just bounce
        // back with the BSP/TP entitlement error.
        const isEnabled = cfg.embedded_signup_enabled !== false && !!cfg.app_id && !!cfgId
        setSignupEnabled(isEnabled)
        setDisabledReason(cfg.disabled_reason || '')
        if (!isEnabled) {
          setStage('ready')
          return
        }

        window.fbAsyncInit = () => {
          window.FB.init({ appId: cfg.app_id, version: cfg.graph_version, xfbml: false, cookie: true })
          if (!cancelled) { sdkLoaded.current = true; setStage('ready') }
        }
        if (!document.getElementById('facebook-jssdk')) {
          const s  = document.createElement('script')
          s.id     = 'facebook-jssdk'
          s.src    = 'https://connect.facebook.net/ar_AR/sdk.js'
          s.async  = true
          s.defer  = true
          document.body.appendChild(s)
        } else {
          if (window.FB) { sdkLoaded.current = true; setStage('ready') }
        }
      } catch (err) {
        if (!cancelled) setError(explainWhatsAppError(err instanceof Error ? err.message : emb.loadConfigFailed))
      }
    }
    loadSdk()
    return () => { cancelled = true }
  }, [emb.loadConfigFailed])

  const applyEmbeddedStatus = useCallback((res: EmbeddedStatusPayload) => {
    if (res.waba_id) setWabaId(res.waba_id)
    if (res.phone_number_id) setNewPhoneId(res.phone_number_id)
    if (Array.isArray(res.phones)) setPhones(res.phones)

    const message = res.message || res.oauth_session_message || res.last_error || ''
    setStatusHint(message)
    if (res.status !== 'error') setError('')

    if (res.connected && res.sending_enabled) {
      setStage('done')
      setTimeout(() => onConnected({
        phone_number: res.phone_number,
        display_name: res.display_name,
        connected_at: res.connected_at,
      }), 1200)
      return
    }

    if (res.oauth_session_needs_reauth && res.connection_status === 'connected') {
      setStage('done')
      return
    }

    if (res.status === 'otp_pending') {
      setStage('verify-phone')
      return
    }

    if (res.status === 'review_pending' || res.status === 'activation_pending') {
      setStage('syncing-phone')
      return
    }

    if (res.status === 'error') {
      setError(message || emb.activateFailed)
      setStage('select-phone')
      return
    }

    if (res.status === 'pending') {
      setStage('select-phone')
    }
  }, [onConnected, emb.activateFailed])

  const refreshEmbeddedStatus = useCallback(async () => {
    const res = await apiCall<EmbeddedStatusPayload>('/whatsapp/embedded/status')
    applyEmbeddedStatus(res)
    return res
  }, [applyEmbeddedStatus])

  useEffect(() => {
    if (stage !== 'syncing-phone') return
    let cancelled = false
    let timer: number | undefined

    const poll = async () => {
      try {
        const res = await refreshEmbeddedStatus()
        if (cancelled) return
        if (res.connected || !['review_pending', 'activation_pending', 'syncing-phone'].includes(res.status)) {
          return
        }
      } catch (err) {
        if (!cancelled) {
          setError(explainWhatsAppError(err instanceof Error ? err.message : emb.syncStatusFailed))
        }
      }

      if (!cancelled) {
        timer = window.setTimeout(poll, 5000)
      }
    }

    timer = window.setTimeout(poll, 3000)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [stage, refreshEmbeddedStatus, emb.syncStatusFailed])

  useEffect(() => {
    if (stage !== 'ready' || embeddedStatusChecked.current) return
    embeddedStatusChecked.current = true
    refreshEmbeddedStatus().catch(() => {})
  }, [stage, refreshEmbeddedStatus])

  const handleExchange = useCallback((code?: string, accessToken?: string) => {
    setBusy(true); setStage('exchanging')
    const payload: Record<string, string> = {}
    if (code) payload.code = code
    if (accessToken) payload.access_token = accessToken
    apiCall<{ waba_id: string; phones: EmbeddedPhone[]; message: string }>(
      '/whatsapp/embedded/exchange',
      { method: 'POST', body: JSON.stringify(payload) }
    ).then(result => {
      setStatusHint(result.message || '')
      setWabaId(result.waba_id)
      setPhones(result.phones)
      setStage('select-phone')
    }).catch(err => {
      const raw = err instanceof Error ? err.message : emb.exchangeFailed
      // Catch the Meta BSP/Tech Provider entitlement error here too —
      // the backend already maps it, but a direct upstream 4xx may
      // leak through with the raw English copy. Show the
      // "use 360dialog" fallback so the merchant never sees raw Meta
      // text in the dashboard.
      const lower = String(raw).toLowerCase()
      const isBspTp =
        (lower.includes('embedded signup') || lower.includes('embedded sign up'))
        && (lower.includes('bsp') || lower.includes(' tp') || lower.includes('tech provider'))
      setError(isBspTp
        ? emb.bspNotEnabled
        : explainWhatsAppError(raw))
      setStage('ready')
    }).finally(() => setBusy(false))
  }, [emb.bspNotEnabled, emb.exchangeFailed])

  const launchSignup = useCallback(() => {
    if (signupEnabled === false) {
      setError(disabledReason || emb.directNotEnabled)
      return
    }
    if (!window.FB || !sdkLoaded.current) { setError(emb.sdkNotReady); return }
    setError('')
    window.FB.login((response: any) => {
      if (!response?.authResponse) { setError(emb.linkCancelled); return }
      const auth = response.authResponse
      // Prefer accessToken if present — avoids redirect_uri mismatch on code exchange.
      // 'code,token' response_type makes Meta JS SDK return both; backend uses
      // access_token directly and skips the problematic code-exchange step.
      handleExchange(
        auth.accessToken ? undefined : auth.code,
        auth.accessToken || undefined,
      )
    }, {
      config_id: configId,
      response_type: 'code,token',
      override_default_response_type: true,
      extras: {
        setup: {},
        feature: 'whatsapp_embedded_signup',
        sessionInfoVersion: '3',
      },
    })
  }, [handleExchange, configId, signupEnabled, disabledReason, emb.directNotEnabled, emb.sdkNotReady, emb.linkCancelled])

  const selectPhone = useCallback(async (phoneId: string) => {
    setBusy(true); setError('')
    setStatusHint(emb.preparingVerify)
    setStage('requesting-code')
    try {
      const res = await apiCall<EmbeddedStatusPayload>('/whatsapp/embedded/select-phone', {
        method: 'POST',
        body: JSON.stringify({ phone_number_id: phoneId }),
      })
      setNewPhoneId(res.phone_number_id || phoneId)
      applyEmbeddedStatus(res)
    } catch (err) {
      setError(explainWhatsAppError(err instanceof Error ? err.message : emb.selectPhoneFailed))
      setStage('select-phone')
    } finally { setBusy(false) }
  }, [applyEmbeddedStatus, emb.preparingVerify, emb.selectPhoneFailed])

  // ── Render ────────────────────────────────────────────────────────────────
  if (stage === 'done') {
    return (
      <div className="flex flex-col items-center gap-4 py-10">
        <div className="w-16 h-16 rounded-full bg-emerald-100 flex items-center justify-center">
          <CheckCircle2 className="w-8 h-8 text-emerald-600" />
        </div>
        <p className="font-bold text-slate-800 text-lg">{emb.successTitle}</p>
      </div>
    )
  }

  if (stage === 'select-phone') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
            <Phone className="w-5 h-5 text-emerald-600" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">{emb.selectPhoneTitle}</p>
            <p className="text-xs text-slate-500">WABA: {wabaId}</p>
          </div>
        </div>
        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-start">
              <p className="text-sm font-medium text-slate-800">{emb.addNewTitle}</p>
              <p className="text-xs text-slate-500">
                {emb.addNewHint}
              </p>
            </div>
            <button
              onClick={() => { setError(''); setStage('add-phone') }}
              disabled={busy}
              className="shrink-0 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-700 transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Phone className="w-4 h-4" />
              {emb.addNewBtn}
            </button>
          </div>
          {phones.length === 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-700">
              {emb.noPhones}
            </div>
          )}
          {phones.map(p => (
            <button
              key={p.id}
              onClick={() => selectPhone(p.id)}
              disabled={busy}
              className="w-full flex items-center justify-between p-4 border border-slate-200 rounded-xl hover:border-violet-400 hover:bg-violet-50 transition-all disabled:opacity-50"
            >
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-full bg-slate-100 flex items-center justify-center">
                  <Phone className="w-4 h-4 text-slate-500" />
                </div>
                <div className="text-start">
                  <p className="font-medium text-slate-800 text-sm">{p.number || p.id}</p>
                  {p.name && <p className="text-xs text-slate-500">{p.name}</p>}
                </div>
              </div>
              {p.verified
                ? <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full">{emb.verified}</span>
                : <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">{emb.unverified}</span>
              }
            </button>
          ))}
        </div>
      </div>
    )
  }

  // ── Add phone stage ──────────────────────────────────────────────────────────
  if (stage === 'add-phone') {
    const submitPhone = async () => {
      if (!newPhone || !displayName) { setError(emb.phoneNameRequired); return }
      setBusy(true); setError('')
      try {
        // Normalize phone: remove spaces, dashes, dots, parentheses, leading zeros
        const cleanPhone = newPhone.replace(/[\s\-().+]/g, '').replace(/^0+/, '')
        const cleanCC    = countryCode.replace(/\D/g, '')
        if (!cleanPhone) { setError(emb.phoneInvalid); setBusy(false); return }
        if (!displayName.trim()) { setError(emb.displayNameRequired); setBusy(false); return }
        setStatusHint(emb.sendingOtp)
        setStage('requesting-code')
        const res = await apiCall<{ phone_number_id: string; message?: string }>('/whatsapp/embedded/add-phone', {
          method: 'POST',
          body: JSON.stringify({
            country_code:  cleanCC,
            phone_number:  cleanPhone,
            verified_name: displayName.trim(),
            code_method:   'SMS',
          }),
        })
        setNewPhoneId(res.phone_number_id)
        setStatusHint(res.message || '')
        setStage('verify-phone')
      } catch (err) {
        setError(explainWhatsAppError(err instanceof Error ? err.message : emb.addPhoneFailed))
        setStage('add-phone')
      } finally { setBusy(false) }
    }
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center">
            <Phone className="w-5 h-5 text-violet-600" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">{emb.addPhoneTitle}</p>
            <p className="text-xs text-slate-500">{emb.addPhoneSubtitle}</p>
          </div>
        </div>
        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-slate-500 mb-1 text-start">{emb.phoneLabel}</label>
            <div className="flex gap-2">
              <div className="relative">
                <input
                  value={countryCode}
                  onChange={e => setCountryCode(e.target.value.replace(/\D/g, ''))}
                  placeholder="966"
                  maxLength={4}
                  className="w-20 px-3 py-3 border border-slate-200 rounded-xl text-sm text-center focus:outline-none focus:border-violet-400"
                />
                <span className="absolute -top-2 right-2 text-xs text-slate-400 bg-white px-1">+</span>
              </div>
              <input
                value={newPhone}
                onChange={e => setNewPhone(e.target.value)}
                placeholder="512345678"
                className="flex-1 px-4 py-3 border border-slate-200 rounded-xl text-sm focus:outline-none focus:border-violet-400"
                dir="ltr"
              />
            </div>
            <p className="text-xs text-slate-400 mt-1 text-start">{emb.phoneExampleHint}</p>
          </div>
          <div>
            <label className="block text-xs text-slate-500 mb-1 text-start">{emb.businessNameLabel}</label>
            <input
              value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder={emb.businessNamePlaceholder}
              className="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:outline-none focus:border-violet-400"
            />
            <p className="text-xs text-slate-400 mt-1 text-start">{emb.businessNameHint}</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setStage('select-phone')}
            className="flex-1 py-3 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition-colors"
          >
            {emb.back}
          </button>
          <button
            onClick={submitPhone}
            disabled={busy}
            className="flex-1 py-3 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ChevronRight className="w-4 h-4" />}
            {emb.sendOtp}
          </button>
        </div>
      </div>
    )
  }

  if (stage === 'requesting-code') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center">
            <Loader2 className="w-5 h-5 text-violet-600 animate-spin" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">{emb.preparingCodeTitle}</p>
            <p className="text-xs text-slate-500">{emb.preparingCodeSubtitle}</p>
          </div>
        </div>

        <div className="bg-violet-50 border border-violet-200 rounded-xl p-4 text-sm text-violet-800">
          {statusHint || emb.requestingCodeDefault}
        </div>

        <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          {emb.requestingCodeTip}
        </div>
      </div>
    )
  }

  // ── Verify phone stage ────────────────────────────────────────────────────────
  if (stage === 'verify-phone') {
    const submitOtp = async () => {
      if (!otpCode) { setError(emb.otpRequired); return }
      setBusy(true); setError('')
      try {
        const res = await apiCall<EmbeddedStatusPayload>('/whatsapp/embedded/verify-phone', {
          method: 'POST',
          body: JSON.stringify({ phone_number_id: newPhoneId, code: otpCode }),
        })
        applyEmbeddedStatus(res)
      } catch (err) {
        setError(explainWhatsAppError(err instanceof Error ? err.message : emb.otpInvalid))
      } finally { setBusy(false) }
    }
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
            <ShieldCheck className="w-5 h-5 text-emerald-600" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">{emb.verifyTitle}</p>
            <p className="text-xs text-slate-500">{emb.verifySubtitle}</p>
          </div>
        </div>
        {statusHint && !error && <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 text-sm text-blue-700">{statusHint}</div>}
        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}
        <input
          value={otpCode}
          onChange={e => setOtpCode(e.target.value)}
          placeholder="- - - - - -"
          maxLength={6}
          className="w-full px-4 py-4 border border-slate-200 rounded-xl text-center text-2xl font-mono tracking-widest focus:outline-none focus:border-emerald-400"
          dir="ltr"
        />
        <div className="flex gap-2">
          <button
            onClick={() => setStage('add-phone')}
            className="flex-1 py-3 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition-colors"
          >
            {emb.back}
          </button>
          <button
            onClick={submitOtp}
            disabled={busy}
            className="flex-1 py-3 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <BadgeCheck className="w-4 h-4" />}
            {emb.confirm}
          </button>
        </div>
      </div>
    )
  }

  if (stage === 'syncing-phone') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center">
            <Loader2 className="w-5 h-5 text-amber-600 animate-spin" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">{emb.syncingTitle}</p>
            <p className="text-xs text-slate-500">{emb.syncingSubtitle}</p>
          </div>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-800">
          {statusHint || emb.syncingDefault}
        </div>

        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}

        <div className="flex gap-2">
          <button
            onClick={() => refreshEmbeddedStatus().catch(err => setError(explainWhatsAppError(err instanceof Error ? err.message : emb.refreshFailed)))}
            className="flex-1 py-3 bg-violet-600 hover:bg-violet-700 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            {emb.refreshNow}
          </button>
          <button
            onClick={() => setStage('select-phone')}
            className="flex-1 py-3 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition-colors"
          >
            {emb.backToPhones}
          </button>
        </div>
      </div>
    )
  }

  // ── Disabled state (May 2026) ────────────────────────────────────
  // When the backend reports ``embedded_signup_enabled=false`` we
  // render a "قريباً / قيد التفعيل" card instead of mounting the FB
  // SDK and showing the "ربط مع Meta" button. The backend reason
  // string is shown verbatim so changes to the disabled copy live
  // in one place (``core/config.meta_embedded_disabled_reason``).
  // The card guides the merchant toward 360dialog as the working
  // path; clicking the secondary button switches the parent tab
  // back to manual/360dialog via a custom event the parent listens
  // to.
  if (signupEnabled === false) {
    return (
      <div className="space-y-5">
        <div className="flex flex-col items-center gap-3 py-4">
          <div className="w-16 h-16 rounded-2xl bg-slate-100 flex items-center justify-center">
            <AlertCircle className="w-8 h-8 text-slate-500" />
          </div>
          <div className="text-center">
            <p className="font-bold text-slate-800 text-lg">{emb.disabledTitle}</p>
            <p className="text-sm text-slate-500 mt-1">
              {emb.disabledSubtitle}
            </p>
          </div>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-900 leading-relaxed">
          {disabledReason || emb.disabledReasonFallback}
        </div>

        <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 text-xs text-slate-600 space-y-1.5">
          <p className="font-semibold text-slate-700">{emb.disabledExplainTitle}</p>
          <p>{emb.disabledExplainBody}</p>
        </div>

        <p className="text-center text-xs text-slate-400">
          {emb.disabledFooter}
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      {/* Logo + Title */}
      <div className="flex flex-col items-center gap-3 py-4">
        <div className="w-16 h-16 rounded-2xl bg-[#25D366]/10 flex items-center justify-center">
          <MessageCircle className="w-8 h-8 text-[#25D366]" />
        </div>
        <div className="text-center">
          <p className="font-bold text-slate-800 text-lg">{emb.initTitle}</p>
          <p className="text-sm text-slate-500 mt-1">{emb.initSubtitle}</p>
        </div>
      </div>

      {/* Steps */}
      <div className="bg-slate-50 rounded-xl p-4 space-y-2">
        {[
          { n: 1, text: emb.step1 },
          { n: 2, text: emb.step2 },
          { n: 3, text: emb.step3 },
          { n: 4, text: emb.step4 },
        ].map(s => (
          <div key={s.n} className="flex items-center gap-3">
            <div className="w-6 h-6 rounded-full bg-violet-100 text-violet-700 flex items-center justify-center text-xs font-bold shrink-0">{s.n}</div>
            <p className="text-sm text-slate-600">{s.text}</p>
          </div>
        ))}
      </div>

      {/* Informational hint — not blocking */}
      <div className="bg-blue-50 border border-blue-100 rounded-xl p-3 text-xs text-blue-700">
        {emb.initHint}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>
      )}

      {/* Main CTA */}
      <button
        onClick={launchSignup}
        disabled={stage !== 'ready' || busy}
        className="w-full flex items-center justify-center gap-3 bg-[#1877F2] hover:bg-[#166FE5] text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-blue-600/20"
      >
        {(stage === 'loading-sdk' || stage === 'exchanging' || busy)
          ? <><Loader2 className="w-5 h-5 animate-spin" />{emb.loading}</>
          : <>
              <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/>
              </svg>
              {emb.connectBtn}
            </>
        }
      </button>

      <p className="text-center text-xs text-slate-400">
        {emb.initFooter}
      </p>
    </div>
  )
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface OtpResponse   { status: string; phone_number_id: string; message: string }
interface VerifyResponse { status: string; phone_number: string; display_name: string; message: string }
interface StatusResponse {
  connected: boolean; status: string
  phone_number?: string; display_name?: string; connected_at?: string
  phone_number_id?: string; last_attempt_at?: string
  sending_enabled?: boolean
  verification_status?: string | null
  name_status?: string | null
  meta_phone_status?: string | null
  message?: string | null
  last_error?: string | null
  provider?: string | null
  merchant_channel_label?: string | null
  connection_type?: string | null
  coexistence_status?: string | null
  action_required_message?: string | null
  request_submitted_at?: string | null
  coexistence_available?: boolean
}

// ── Meta business verticals ───────────────────────────────────────────────────

const VERTICAL_VALUES = [
  'RETAIL',
  'APPAREL',
  'BEAUTY_SPA_SALON',
  'FOOD_AND_GROCERY',
  'RESTAURANT',
  'HEALTH_AND_MEDICAL',
  'EDUCATION',
  'HOTEL_AND_LODGING',
  'TRAVEL_AND_TRANSPORTATION',
  'AUTOMOTIVE',
  'ENTERTAINMENT',
  'PROFESSIONAL_SERVICES',
  'NONPROFIT',
  'OTHER',
] as const

// ── Phone normalizer (frontend) ───────────────────────────────────────────────
// Mirrors backend _normalize_phone so the user sees the normalized value live.

function normalizePhone(raw: string): string {
  // Convert Arabic-Indic digits
  const ar = '٠١٢٣٤٥٦٧٨٩'
  let s = raw.split('').map(c => {
    const i = ar.indexOf(c); return i >= 0 ? String(i) : c
  }).join('')
  // Strip whitespace, dashes, dots, parens
  s = s.replace(/[\s\-.()\u00A0]+/g, '')
  // Remove leading + or 00
  if (s.startsWith('+'))  s = s.slice(1)
  if (s.startsWith('00')) s = s.slice(2)
  // Normalize to full international (966XXXXXXXXX)
  if (s.startsWith('966'))       return s          // already full
  if (s.startsWith('0'))         return '966' + s.slice(1)
  if (/^5\d{8}$/.test(s))        return '966' + s  // bare 9-digit Saudi
  return s
}

function isValidSaudiPhone(normalized: string): boolean {
  return /^9665\d{8}$/.test(normalized)
}

// ── Meta message sanitizer ────────────────────────────────────────────────────
// Raw Meta messages (escaped unicode, HTML entities, provider text) must NEVER
// be shown to merchants. This is a last-resort guard on the frontend side.

const FALLBACK_MSG = 'تمت معالجة الطلب، ولكن تعذر عرض تفاصيل الرسالة بشكل صحيح.'

function sanitizeMessage(msg: unknown): string {
  if (typeof msg !== 'string' || !msg.trim()) return FALLBACK_MSG
  const raw = msg.trim()
  // Detect raw escaped unicode sequences
  if (/\\u[0-9a-fA-F]{4}/.test(raw)) return FALLBACK_MSG
  // Detect HTML-escaped content
  if (/^html:/i.test(raw) || /&[a-z]+;/.test(raw)) return FALLBACK_MSG
  // Detect obvious raw Meta provider messages (English technical text)
  if (/\(#\d+\)/.test(raw)) return FALLBACK_MSG
  if (/^unsupported|^object with id|^invalid oauth/i.test(raw)) return FALLBACK_MSG
  return raw
}

// ── API helpers ───────────────────────────────────────────────────────────────

const post = <T,>(path: string, body: unknown) =>
  apiCall<T>(path, { method: 'POST', body: JSON.stringify(body) })

async function requestOtp(phone: string, displayName: string, method: string) {
  return post<OtpResponse>('/whatsapp/direct/request-otp', {
    phone_number: phone, display_name: displayName, method,
  })
}
async function verifyOtp(phoneNumberId: string, code: string) {
  return post<VerifyResponse>('/whatsapp/direct/verify-otp', { phone_number_id: phoneNumberId, code })
}
async function resendOtp(phoneNumberId: string) {
  return post<OtpResponse>('/whatsapp/direct/resend-otp', { phone_number_id: phoneNumberId, code: '' })
}
async function saveProfile(phoneNumberId: string, profile: Record<string, string>) {
  return post('/whatsapp/direct/save-profile', { phone_number_id: phoneNumberId, ...profile })
    .catch(() => {}) // non-fatal
}
async function getStatus() {
  return apiCall<StatusResponse>('/whatsapp/status')
}
async function disconnect() {
  return post('/whatsapp/connection/disconnect', {})
}

// ── Step indicator ────────────────────────────────────────────────────────────

function StepBar({ step, labels }: { step: number; labels: readonly [string, string, string, string] }) {
  return (
    <div className="flex items-center justify-center gap-1 mb-7">
      {labels.map((label, i) => {
        const n    = i + 1
        const done = n < step
        const active = n === step
        return (
          <div key={i} className="flex items-center gap-1">
            <div className="flex flex-col items-center gap-1">
              <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all
                ${done   ? 'bg-emerald-500 text-white' : ''}
                ${active ? 'bg-violet-600 text-white ring-4 ring-violet-100' : ''}
                ${!done && !active ? 'bg-slate-100 text-slate-400' : ''}`}>
                {done ? <CheckCircle2 className="w-4 h-4" /> : n}
              </div>
              <span className={`text-[10px] font-medium whitespace-nowrap
                ${active ? 'text-violet-600' : 'text-slate-400'}`}>
                {label}
              </span>
            </div>
            {i < labels.length - 1 && (
              <div className={`w-8 h-0.5 mb-4 rounded ${n < step ? 'bg-emerald-400' : 'bg-slate-200'}`} />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Field component ───────────────────────────────────────────────────────────

function Field({
  label, hint, required, children,
}: { label: string; hint?: string; required?: boolean; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="block text-sm font-medium text-slate-700">
        {label} {required && <span className="text-red-500">*</span>}
      </label>
      {children}
      {hint && <p className="text-xs text-slate-400">{hint}</p>}
    </div>
  )
}

const inputCls = "w-full border border-slate-300 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400 bg-white"

// ── Manual Connect Component ──────────────────────────────────────────────────

interface ConnectReadiness {
  credentials_saved:          boolean
  phone_registered:           boolean
  webhook_subscribed:         boolean
  inbound_usable:             boolean
  phone_registration_error:   string | null
  webhook_error:              string | null
  readiness:                  string
  phone_number_id:            string
  waba_id:                    string
}

function ReadinessBadge({ ok, label, detail }: { ok: boolean; label: string; detail?: string | null }) {
  return (
    <div className={`flex items-start gap-2.5 p-3 rounded-xl border ${ok ? 'bg-emerald-50 border-emerald-200' : 'bg-amber-50 border-amber-200'}`}>
      {ok
        ? <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
        : <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
      }
      <div className="flex-1 min-w-0">
        <p className={`text-sm font-semibold ${ok ? 'text-emerald-800' : 'text-amber-800'}`}>{label}</p>
        {detail && <p className="text-xs mt-0.5 text-slate-500 break-words">{detail}</p>}
      </div>
    </div>
  )
}

function ManualConnectForm({ onConnected }: { onConnected: (r: { phone_number_id: string; waba_id: string; connected_at: string }) => void }) {
  const { t } = useLanguage()
  const m = t(tr => tr.whatsappConnect.manual)
  const [phoneNumberId, setPhoneNumberId]   = useState('')
  const [wabaId, setWabaId]                 = useState('')
  const [accessToken, setAccessToken]       = useState('')
  const [showToken, setShowToken]           = useState(false)
  const [busy, setBusy]                     = useState(false)
  const [resolvingWaba, setResolvingWaba]   = useState(false)
  const [wabaResolved, setWabaResolved]     = useState(false)
  const [error, setError]                   = useState('')
  const [readiness, setReadiness]           = useState<ConnectReadiness | null>(null)

  const handleResolveWaba = async () => {
    if (!phoneNumberId.trim() || !accessToken.trim()) {
      setError(m.resolveWabaNeedCreds)
      return
    }
    setResolvingWaba(true); setError(''); setWabaResolved(false)
    try {
      const r = await apiCall<{ ok: boolean; resolved_waba_id: string | null; error?: string; message: string }>(
        '/whatsapp/connection/resolve-waba',
        { method: 'POST', body: JSON.stringify({ phone_number_id: phoneNumberId.trim(), access_token: accessToken.trim() }) }
      )
      if (r.ok && r.resolved_waba_id) {
        setWabaId(r.resolved_waba_id)
        setWabaResolved(true)
      } else {
        setError(r.message || r.error || m.wabaResolveFailed)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : m.wabaResolveError)
    } finally {
      setResolvingWaba(false)
    }
  }

  const validate = (): string => {
    if (!phoneNumberId.trim())           return m.validatePhoneIdRequired
    if (!/^\d+$/.test(phoneNumberId.trim())) return m.validatePhoneIdDigits
    if (!wabaId.trim())                  return m.validateWabaRequired
    if (!/^\d+$/.test(wabaId.trim()))    return m.validateWabaDigits
    if (!accessToken.trim())             return m.validateTokenRequired
    return ''
  }

  const handleConnect = async () => {
    const err = validate()
    if (err) { setError(err); return }
    setBusy(true); setError(''); setReadiness(null)
    try {
      const r = await apiCall<ConnectReadiness & { connected_at?: string | null }>('/whatsapp/connection/manual-connect', {
        method: 'POST',
        body: JSON.stringify({
          phone_number_id: phoneNumberId.trim(),
          waba_id: wabaId.trim(),
          access_token: accessToken.trim(),
        }),
      })
      setReadiness(r)
      // If fully ready, advance immediately
      if (r.inbound_usable) {
        onConnected({
          phone_number_id: r.phone_number_id,
          waba_id: r.waba_id,
          connected_at: r.connected_at ?? new Date().toISOString(),
        })
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : m.connectError)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-emerald-200 shadow-sm p-6 space-y-5">
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
          <MessageCircle className="w-5 h-5 text-emerald-600" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <p className="font-bold text-slate-800">{m.title}</p>
            <span className="text-xs bg-emerald-100 text-emerald-700 font-semibold px-2 py-0.5 rounded-full">{m.badge}</span>
          </div>
          <p className="text-xs text-slate-500">{m.subtitle}</p>
        </div>
      </div>

      {/* Notice banner */}
      <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
        <div className="text-xs text-amber-800 space-y-1">
          <p className="font-semibold">{m.noticeTitle}</p>
          <p>{m.noticeBody}</p>
        </div>
      </div>

      {/* Fields */}
      <Field label="Phone Number ID" hint={m.phoneNumberIdHint} required>
        <div className="relative">
          <Phone className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text" inputMode="numeric"
            value={phoneNumberId}
            onChange={e => { setPhoneNumberId(e.target.value.replace(/\D/g, '')); setError('') }}
            placeholder="123456789012345"
            className={`${inputCls} pr-9`} dir="ltr"
          />
        </div>
        {phoneNumberId && !/^\d+$/.test(phoneNumberId) && (
          <p className="text-xs text-red-500">{m.digitsOnly}</p>
        )}
      </Field>

      <Field label="WABA ID (WhatsApp Business Account ID)" hint={m.wabaHint} required>
        <div className="flex gap-2 items-center">
          <div className="relative flex-1">
            <Building2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              type="text" inputMode="numeric"
              value={wabaId}
              onChange={e => { setWabaId(e.target.value.replace(/\D/g, '')); setError(''); setWabaResolved(false) }}
              placeholder="987654321098765"
              className={`${inputCls} pr-9 ${wabaResolved ? 'border-emerald-400 bg-emerald-50' : ''}`} dir="ltr"
            />
          </div>
          <button
            type="button"
            onClick={handleResolveWaba}
            disabled={resolvingWaba || !phoneNumberId || !accessToken}
            title={m.resolveWabaTitle}
            className="shrink-0 px-3 py-2.5 rounded-xl text-xs font-semibold border border-violet-300 bg-violet-50 hover:bg-violet-100 text-violet-700 transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
          >
            {resolvingWaba
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> {m.resolving}</>
              : wabaResolved
                ? <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" /> {m.resolved}</>
                : <><RefreshCw className="w-3.5 h-3.5" /> {m.discover}</>
            }
          </button>
        </div>
        {wabaResolved && (
          <p className="text-xs text-emerald-600 mt-1">{m.wabaAutoResolved}</p>
        )}
        {wabaId && !/^\d+$/.test(wabaId) && (
          <p className="text-xs text-red-500">{m.digitsOnly}</p>
        )}
      </Field>

      <Field label="Permanent Access Token" hint={m.tokenHint} required>
        <div className="relative">
          <ShieldCheck className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type={showToken ? 'text' : 'password'}
            value={accessToken}
            onChange={e => { setAccessToken(e.target.value); setError('') }}
            placeholder="EAAxxxxxxxx..."
            className={`${inputCls} pr-9 pl-10`} dir="ltr"
          />
          <button
            type="button"
            onClick={() => setShowToken(p => !p)}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
            tabIndex={-1}
          >
            <ChevronRight className={`w-4 h-4 transition ${showToken ? 'rotate-90' : ''}`} />
          </button>
        </div>
      </Field>

      {error && <ErrorBox msg={error} />}

      {/* ── Readiness panel (shown after connect attempt) ───────────────────── */}
      {readiness && (
        <div className="space-y-2">
          <p className="text-xs font-bold text-slate-600 uppercase tracking-wide">{m.readinessTitle}</p>
          <ReadinessBadge
            ok={readiness.credentials_saved}
            label={m.credSaved}
            detail={readiness.credentials_saved ? m.credSavedOk : m.credSavedFail}
          />
          <ReadinessBadge
            ok={readiness.phone_registered}
            label={m.phoneRegistered}
            detail={
              readiness.phone_registered
                ? m.phoneRegisteredOk
                : readiness.phone_registration_error
                  ? `${m.phoneRegisteredFailPrefix} ${readiness.phone_registration_error}`
                  : m.phoneRegisteredPending
            }
          />
          <ReadinessBadge
            ok={readiness.webhook_subscribed}
            label={m.webhookSub}
            detail={
              readiness.webhook_subscribed
                ? m.webhookSubOk
                : readiness.webhook_error
                  ? `${m.webhookSubFailPrefix} ${readiness.webhook_error}`
                  : m.webhookSubPending
            }
          />
          <ReadinessBadge
            ok={readiness.inbound_usable}
            label={m.inboundReady}
            detail={
              readiness.inbound_usable
                ? m.inboundReadyOk
                : m.inboundReadyPartial
            }
          />
          {!readiness.inbound_usable && readiness.credentials_saved && (
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => onConnected({
                  phone_number_id: readiness.phone_number_id,
                  waba_id: readiness.waba_id,
                  connected_at: new Date().toISOString(),
                })}
                className="flex-1 py-2.5 rounded-xl text-sm font-semibold bg-amber-500 hover:bg-amber-400 text-white transition-all"
              >
                {m.continueAnyway}
              </button>
              <button
                onClick={() => { setReadiness(null); setError('') }}
                className="px-4 py-2.5 rounded-xl text-sm border border-slate-300 text-slate-600 hover:bg-slate-50 transition-all"
              >
                {m.retry}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Help link */}
      <p className="text-xs text-slate-400 text-center">
        {m.helpPrefix}{' '}
        <a href="/help/whatsapp-manual-setup" target="_blank" rel="noreferrer"
          className="text-emerald-600 hover:text-emerald-700 font-medium underline">
          {m.helpLink}
        </a>
      </p>

      {!readiness && (
        <button
          onClick={handleConnect}
          disabled={busy || !phoneNumberId || !wabaId || !accessToken}
          className="w-full flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-50 shadow-lg shadow-emerald-600/20"
        >
          {busy
            ? <><Loader2 className="w-4 h-4 animate-spin" /> {m.connecting}</>
            : <><MessageCircle className="w-4 h-4" /> {m.connectBtn}</>
          }
        </button>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function WhatsAppConnect() {
  const { t, dir, lang } = useLanguage()
  const wc = t(tr => tr.whatsappConnect)
  const d = t(tr => tr.whatsappConnect.direct)
  // 'manual' = Manual connect (current) | 'embedded' = Meta Embedded Signup | 'direct' = OTP flow
  const [mode, setMode]       = useState<'manual'|'embedded'|'direct'|'coexistence'>('manual')
  const [step, setStep]       = useState<1|2|3|4>(1)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy]       = useState(false)
  const [error, setError]     = useState('')
  const [status, setStatus]   = useState<WaConnection | null>(null)
  const [showDisconnectModal, setShowDisconnectModal] = useState(false)

  // Step 1
  const [phone, setPhone]             = useState('')
  const [displayName, setDisplayName] = useState('')
  const [otpMethod, setOtpMethod]     = useState<'SMS'|'VOICE'>('SMS')

  // Step 2
  const [otp, setOtp]                     = useState('')
  const [phoneNumberId, setPhoneNumberId] = useState('')
  const [sentMsg, setSentMsg]             = useState('')
  const [resendCooldown, setResendCooldown] = useState(0)

  // Step 3 — business profile
  const [vertical, setVertical]     = useState('RETAIL')
  const [about, setAbout]           = useState('')
  const [address, setAddress]       = useState('')
  const [email, setEmail]           = useState('')
  const [website, setWebsite]       = useState('')

  // Step 4 — connected
  const [connPhone, setConnPhone]   = useState('')
  const [connName, setConnName]     = useState('')
  const [connAt, setConnAt]         = useState('')
  const [connLabel, setConnLabel]   = useState('واتساب الأعمال')

  // ── Server-side Meta OAuth callback result (May 2026) ────────────
  // The backend ``/whatsapp/embedded/oauth/callback`` redirects the
  // browser here with a result fragment (``#meta=ok`` or
  // ``#meta=error&reason=...``). We read it once on mount, surface
  // a banner, then scrub the hash so a page refresh doesn't replay
  // the same toast.
  const [metaCallbackBanner, setMetaCallbackBanner] = useState<{
    ok: boolean
    text: string
  } | null>(null)
  useEffect(() => {
    const hash = window.location.hash || ''
    if (!hash.startsWith('#meta=')) return
    const params = new URLSearchParams(hash.slice(1))
    const result = params.get('meta')
    const reason = params.get('reason') || ''
    if (result === 'ok') {
      setMetaCallbackBanner({
        ok: true,
        text: wc.metaBanner.success,
      })
      setMode('embedded')
    } else {
      setMetaCallbackBanner({
        ok: false,
        text: reason ? decodeURIComponent(reason) : wc.metaBanner.failure,
      })
    }
    // Scrub the hash so a refresh doesn't re-fire the banner.
    try {
      const u = new URL(window.location.href)
      u.hash = ''
      window.history.replaceState({}, '', u.toString())
    } catch { /* noop */ }
  }, [])
  useEffect(() => {
    if (!metaCallbackBanner) return
    const t = setTimeout(() => setMetaCallbackBanner(null), 8000)
    return () => clearTimeout(t)
  }, [metaCallbackBanner])

  // Live verification (real provider probe)
  const [liveVerify, setLiveVerify] = useState<{
    truly_connected: boolean
    soft_warning?:   boolean
    webhook_active?: boolean
    reason_code:     string | null
    reason_message:  string
    db_status:       string | null
    provider:        string | null
    checks:          Array<{ name: string; ok: boolean; status_code?: number | null; detail?: string | null }>
    provider_probe?: unknown
  } | null>(null)
  const [liveVerifying, setLiveVerifying] = useState(false)

  const runLiveVerify = async () => {
    setLiveVerifying(true)
    try {
      const res = await apiCall<typeof liveVerify>('/whatsapp/connection/live-verify')
      console.info('[WhatsApp] live-verify result', res)
      setLiveVerify(res)
    } catch (e) {
      console.warn('[WhatsApp] live-verify failed', e)
      setLiveVerify(null)
    } finally {
      setLiveVerifying(false)
    }
  }

  useEffect(() => {
    getStatus()
      .then(s => {
        setStatus(s as WaConnection)
        if ((s as StatusResponse).coexistence_available && (s.provider === 'dialog360' || s.connection_type === 'coexistence')) {
          setMode('coexistence')
        }
        if (s.connected && s.sending_enabled !== false) {
          setConnPhone(s.phone_number ?? '')
          setConnName(s.display_name ?? '')
          setConnAt(s.connected_at ?? '')
          setConnLabel(s.merchant_channel_label ?? 'واتساب الأعمال')
          setStep(4)
          // Probe the provider in the background — DB record alone isn't proof.
          void runLiveVerify()
        } else if (s.connection_type === 'coexistence' || s.provider === 'dialog360') {
          setMode('coexistence')
        } else if ((s.status === 'pending' || s.status === 'otp_pending') && s.phone_number_id) {
          // Resume from Step 2 — OTP was already sent, pending verification
          setPhoneNumberId(s.phone_number_id)
          setSentMsg(d.resumeOtpSent)
          setStep(2)
          // Calculate remaining cooldown from last_attempt_at
          if (s.last_attempt_at) {
            const elapsed = Math.floor((Date.now() - new Date(s.last_attempt_at).getTime()) / 1000)
            const remaining = Math.max(0, 60 - elapsed)
            if (remaining > 0) setResendCooldown(remaining)
          }
        } else if (s.status === 'activation_pending' || s.status === 'review_pending') {
          setMode('embedded')
        }
      })
      .catch(()=>{})
      .finally(()=>setLoading(false))
  }, [])

  // Resend cooldown countdown
  useEffect(() => {
    if (resendCooldown <= 0) return
    const t = setTimeout(() => setResendCooldown(c => c - 1), 1000)
    return () => clearTimeout(t)
  }, [resendCooldown])

  // ── Step 1 → 2 ──────────────────────────────────────────────────────────

  const handleRequestOtp = useCallback(async () => {
    if (!phone.trim())       { setError(d.errPhoneRequired); return }
    if (!displayName.trim()) { setError(d.errDisplayNameRequired); return }

    const original   = phone.trim()
    const normalized = normalizePhone(original)
    const valid      = isValidSaudiPhone(normalized)

    console.log('[Nahla/OTP] original_input=', original,
      '| normalized=', normalized, '| valid=', valid)

    if (!valid) {
      // PHONE_VALIDATION_ERROR — do not proceed
      setError(d.errPhoneInvalid)
      return
    }

    setBusy(true); setError('')
    try {
      const payload = { phone_number: normalized, display_name: displayName.trim(), method: otpMethod }
      console.log('[Nahla/OTP] payload_sent_to_backend=', JSON.stringify(payload))

      const r = await requestOtp(normalized, displayName.trim(), otpMethod)
      console.log('[Nahla/OTP] api_response=', r)

      setPhoneNumberId(r.phone_number_id)
      setSentMsg(sanitizeMessage(r.message))
      setStep(2)
      // Start 60-second resend cooldown
      setResendCooldown(60)
    } catch (e) {
      const raw = e instanceof Error ? e.message : ''
      console.error('[Nahla/OTP] api_error=', raw)
      const isRateLimit = /انتظار|rate.limit|OTP_RATE_LIMITED|حاولت عدة مرات/i.test(raw)
      if (isRateLimit) {
        setError('⏳ ' + sanitizeMessage(raw) + d.errRateLimitSuffix)
      } else {
        const isPhoneFormatMsg = /صيغة رقم الهاتف|phone.*format|invalid.*phone/i.test(raw)
        if (isPhoneFormatMsg && valid) {
          setError(d.errSendOtpFailed)
        } else {
          setError(sanitizeMessage(raw))
        }
      }
    }
    finally { setBusy(false) }
  }, [phone, displayName, otpMethod, d])

  // ── Step 2 → 3 ──────────────────────────────────────────────────────────

  const handleVerifyOtp = useCallback(async () => {
    if (otp.trim().length < 6) { setError(d.errOtpIncomplete); return }
    setBusy(true); setError('')
    try {
      const r = await verifyOtp(phoneNumberId, otp.trim()) as VerifyResponse & { sending_enabled?: boolean; status?: string }
      setConnPhone(r.phone_number)
      setConnName(r.display_name)
      if (r.sending_enabled === false || (r.status && r.status !== 'connected')) {
        setError(explainWhatsAppError(r.message || d.errVerifiedPendingMeta))
        return
      }
      setStep(3)
    } catch (e) { setError(explainWhatsAppError(sanitizeMessage(e instanceof Error ? e.message : ''))) }
    finally { setBusy(false) }
  }, [otp, phoneNumberId, d])

  // ── Step 3 → 4 ──────────────────────────────────────────────────────────

  const handleSaveProfile = useCallback(async () => {
    setBusy(true); setError('')
    try {
      await saveProfile(phoneNumberId, {
        vertical, about, address, email,
        ...(website ? { websites: website } : {}),
      })
      setConnAt(new Date().toISOString())
      setStep(4)
    } catch (e) { setError(sanitizeMessage(e instanceof Error ? e.message : '')) }
    finally { setBusy(false) }
  }, [phoneNumberId, vertical, about, address, email, website])

  const handleDisconnect = useCallback(() => {
    const managedByOps =
      status?.provider === 'dialog360' ||
      status?.connection_type === 'coexistence' ||
      isCoexistenceConnLabel(connLabel)
    if (managedByOps) {
      setError(wc.disconnect.opsOnlyError)
      return
    }
    setShowDisconnectModal(true)
  }, [connLabel, status, wc.disconnect.opsOnlyError])

  const confirmDisconnect = useCallback(async () => {
    setBusy(true)
    try {
      await disconnect()
      setShowDisconnectModal(false)
      setStep(1); setPhone(''); setDisplayName(''); setOtp('')
      setConnPhone(''); setConnName('')
    } catch { setError(wc.disconnect.failedError) }
    finally { setBusy(false) }
  }, [])

  // ─────────────────────────────────────────────────────────────────────────
  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <Loader2 className="w-6 h-6 animate-spin text-violet-500" />
    </div>
  )

  return (
    <>
    <DisconnectModal
      open={showDisconnectModal}
      busy={busy}
      onConfirm={confirmDisconnect}
      onCancel={() => setShowDisconnectModal(false)}
    />
    <div className="max-w-lg mx-auto space-y-4" dir={dir}>

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
          <MessageCircle className="w-6 h-6 text-emerald-500" />
          {wc.page.headerTitle}
        </h1>
        <p className="text-slate-500 mt-1 text-sm">
          {wc.page.headerSubtitle}
        </p>
      </div>

      {/* ── Mode switcher (only when not connected) ─────────────────────── */}
      {step < 4 && !loading && (
        <div className="space-y-2">
          {/* Main tabs */}
          <div className="flex gap-2 bg-slate-100 rounded-xl p-1">
            <button
              onClick={() => { setMode('manual'); setStep(1); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all flex items-center justify-center gap-1.5 ${
                mode === 'manual'
                  ? 'bg-white shadow text-emerald-700'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {wc.page.modes.manual}
              {mode === 'manual' && (
                <span className="text-[10px] bg-emerald-100 text-emerald-700 font-semibold px-1.5 py-0.5 rounded-full">
                  {wc.page.modes.manualBadge}
                </span>
              )}
            </button>
            <button
              onClick={() => { setMode('embedded'); setStep(1); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                mode === 'embedded'
                  ? 'bg-white shadow text-violet-700'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {wc.page.modes.embedded}
            </button>
            <button
              onClick={() => { setMode('direct'); setStep(1); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                mode === 'direct'
                  ? 'bg-white shadow text-violet-700'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {wc.page.modes.otp}
            </button>
            {status?.coexistence_available && (
              <button
                onClick={() => { setMode('coexistence'); setStep(1); setError('') }}
                className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                  mode === 'coexistence'
                    ? 'bg-white shadow text-violet-700'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                {wc.page.modes.coexistence}
              </button>
            )}
          </div>

          {/* Context hint per mode */}
          {mode === 'manual' && (
            <p className="text-xs text-emerald-700 text-center bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-1.5">
              {wc.page.modeHints.manual}
            </p>
          )}
          {mode === 'embedded' && (
            <p className="text-xs text-slate-500 text-center bg-slate-50 border border-slate-200 rounded-lg px-3 py-1.5">
              {wc.page.modeHints.embedded}
            </p>
          )}
        </div>
      )}

      {/* ── Manual connect mode ──────────────────────────────────────────── */}
      {mode === 'manual' && step < 4 && !loading && (
        <ManualConnectForm
          onConnected={(r) => {
            setConnPhone('')
            setConnName('')
            setConnAt(r.connected_at)
            setConnLabel(`واتساب يدوي — ID: ${r.phone_number_id}`)
            setStep(4)
          }}
        />
      )}

      {/* ── Server-side Meta OAuth callback banner ───────────────────────── */}
      {metaCallbackBanner && (
        <div
          className={
            'rounded-xl p-3 text-sm font-medium border ' +
            (metaCallbackBanner.ok
              ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
              : 'bg-rose-50 border-rose-200 text-rose-800')
          }
        >
          {metaCallbackBanner.text}
        </div>
      )}

      {/* ── Embedded Signup mode ─────────────────────────────────────────── */}
      {mode === 'embedded' && step < 4 && !loading && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-sm">
          <EmbeddedSignupFlow onConnected={(payload) => {
            setConnPhone(payload?.phone_number ?? '')
            setConnName(payload?.display_name ?? '')
            setConnAt(payload?.connected_at ?? new Date().toISOString())
            setConnLabel('ربط عبر Meta')
            setStep(4)
          }} />
        </div>
      )}

      {mode === 'coexistence' && step < 4 && !loading && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-sm">
          <CoexistenceFlow
            status={status}
            onConnected={(payload) => {
              setConnPhone(payload?.phone_number ?? '')
              setConnName(payload?.display_name ?? '')
              setConnAt(payload?.connected_at ?? new Date().toISOString())
              setConnLabel('واتساب الجوال + الذكاء الاصطناعي')
              setStep(4)
            }}
          />
        </div>
      )}

      {/* ── Direct mode step bar ─────────────────────────────────────────── */}
      {mode === 'direct' && step < 4 && (
        <StepBar step={step} labels={[d.stepIdentity, d.stepVerify, d.stepProfile, d.stepDone]} />
      )}

      {/* ── Direct mode steps (1-3) ──────────────────────────────────────── */}
      {/* ── Step 1: Identity ─────────────────────────────────────────────── */}
      {mode === 'direct' && step === 1 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center">
              <Phone className="w-5 h-5 text-violet-600" />
            </div>
            <div>
              <p className="font-semibold text-slate-800">{d.step1Title}</p>
              <p className="text-xs text-slate-500">{d.step1Subtitle}</p>
            </div>
          </div>

          <Field label={d.phoneLabel} hint={d.phoneHint} required>
            <input
              type="tel" value={phone}
              onChange={e => {
                setPhone(e.target.value)
                // Clear any previous error immediately when user edits the field
                setError('')
              }}
              placeholder="+9665XXXXXXXX"
              className={inputCls} dir="ltr"
            />
            {phone.trim() && (() => {
              const n = normalizePhone(phone.trim())
              return isValidSaudiPhone(n)
                ? <p className="text-xs text-emerald-600 mt-1">✓ {d.phoneNormalizedOk} {n}</p>
                : <p className="text-xs text-amber-500 mt-1">{d.phoneFormatHint}</p>
            })()}
          </Field>

          <Field
            label={d.displayNameLabel}
            hint={d.displayNameHint}
            required
          >
            <input
              type="text" value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder={d.displayNamePlaceholder}
              className={inputCls}
            />
            <p className="text-xs text-amber-600 mt-1">
              {d.displayNameWarning}
            </p>
          </Field>

          <Field label={d.otpMethodLabel}>
            <div className="flex gap-3">
              {(['SMS','VOICE'] as const).map(m => (
                <button key={m} onClick={() => setOtpMethod(m)}
                  className={`flex-1 py-2.5 rounded-xl text-sm font-medium border transition-all
                    ${otpMethod===m ? 'bg-violet-600 text-white border-violet-600' : 'bg-white text-slate-600 border-slate-300 hover:border-violet-300'}`}>
                  {m==='SMS' ? d.otpMethodSms : d.otpMethodVoice}
                </button>
              ))}
            </div>
          </Field>

          {error && <ErrorBox msg={error} />}

          <button onClick={handleRequestOtp} disabled={busy}
            className="w-full flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-violet-600/20">
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin"/>{d.sending}</>
              : <>{d.sendOtpBtn} <ChevronRight className="w-4 h-4"/></>}
          </button>

          {/* Info */}
          <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-700 space-y-1">
            <p className="font-semibold text-blue-800">{d.requirementsTitle}</p>
            <ul className="space-y-1 list-disc list-inside">
              <li>{d.requirement1}</li>
              <li>{d.requirement2}</li>
              <li>{d.requirement3}</li>
            </ul>
          </div>
        </div>
      )}

      {/* ── Step 2: OTP ──────────────────────────────────────────────────── */}
      {mode === 'direct' && step === 2 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center">
              <ShieldCheck className="w-5 h-5 text-amber-600"/>
            </div>
            <div>
              <p className="font-semibold text-slate-800">{d.step2Title}</p>
              <p className="text-xs text-slate-500 mt-0.5">{sentMsg}</p>
            </div>
          </div>

          <Field label={d.otpFieldLabel} required>
            <input
              type="text" value={otp}
              onChange={e => setOtp(e.target.value.replace(/\D/g,'').slice(0,6))}
              placeholder="• • • • • •"
              maxLength={6} autoFocus
              className={`${inputCls} text-center text-2xl font-mono tracking-[0.5em]`}
              dir="ltr"
            />
          </Field>

          {error && <ErrorBox msg={error} />}

          <button onClick={handleVerifyOtp} disabled={busy||otp.length<6}
            className="w-full flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-emerald-600/20">
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin"/>{d.verifying}</>
              : <>{d.confirmPhoneBtn} <CheckCircle2 className="w-4 h-4"/></>}
          </button>

          {/* Already verified in Meta? refresh button */}
          <div className="flex items-center gap-2 p-3 bg-blue-50 border border-blue-200 rounded-xl text-xs text-blue-700">
            <ShieldCheck className="w-4 h-4 shrink-0 text-blue-500"/>
            <span>{d.metaVerifiedPrompt}</span>
            <button
              onClick={async () => {
                setBusy(true); setError('')
                try {
                  const r = await post<{ updated: boolean; connected?: boolean; message: string }>(
                    '/whatsapp/direct/refresh-from-meta', {}
                  )
                  if (r.updated || r.connected) {
                    setStep(3)
                    setSentMsg(d.refreshSuccess)
                  } else {
                    setError(sanitizeMessage(r.message))
                  }
                } catch(e) {
                  setError(sanitizeMessage(e instanceof Error ? e.message : ''))
                } finally { setBusy(false) }
              }}
              disabled={busy}
              className="mr-auto font-semibold underline hover:text-blue-900 disabled:opacity-50 whitespace-nowrap">
              {busy ? d.refreshStatusBusy : d.refreshStatusBtn}
            </button>
          </div>

          {/* Resend code */}
          <div className="flex items-center justify-between text-sm pt-1">
            <button onClick={()=>{setStep(1);setError('');setOtp('')}}
              className="text-slate-400 hover:text-slate-600">
              {d.changePhone}
            </button>
            <button
              onClick={async () => {
                setOtp(''); setError(''); setBusy(true)
                try {
                  const r = await resendOtp(phoneNumberId)
                  setSentMsg(sanitizeMessage(r.message))
                  setResendCooldown(60)
                } catch(e) {
                  const msg = sanitizeMessage(e instanceof Error ? e.message : '')
                  // Stale phone_number_id — reset to step 1 so user can re-add
                  if (msg.includes('الخطوة الأولى') || msg.includes('STALE_PHONE')) {
                    setStep(1); setPhone(''); setOtp(''); setPhoneNumberId('')
                  }
                  setError(msg)
                } finally { setBusy(false) }
              }}
              disabled={resendCooldown > 0 || busy}
              className="text-violet-600 hover:text-violet-800 disabled:text-slate-400 disabled:cursor-not-allowed font-medium">
              {resendCooldown > 0
                ? `${d.resendLabel} (${resendCooldown}${d.resendCooldownUnit})`
                : d.resendBtn}
            </button>
          </div>
        </div>
      )}

      {/* ── Step 3: Business Profile ─────────────────────────────────────── */}
      {mode === 'direct' && step === 3 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
              <Building2 className="w-5 h-5 text-emerald-600"/>
            </div>
            <div>
              <p className="font-semibold text-slate-800">{d.step3Title}</p>
              <p className="text-xs text-slate-500">{d.step3Subtitle}</p>
            </div>
          </div>

          <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-3 flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0"/>
            <p className="text-xs text-emerald-700">
              {d.verifiedBanner}
            </p>
          </div>

          <Field label={d.verticalLabel} required>
            <select value={vertical} onChange={e=>setVertical(e.target.value)} className={inputCls}>
              {VERTICAL_VALUES.map(v => (
                <option key={v} value={v}>{d.verticals[v]}</option>
              ))}
            </select>
          </Field>

          <Field label={d.aboutLabel} hint={d.aboutHint}>
            <textarea
              value={about} onChange={e=>setAbout(e.target.value.slice(0,512))}
              placeholder={d.aboutPlaceholder}
              rows={3} className={`${inputCls} resize-none`}
            />
            <p className="text-xs text-slate-400 text-start">{about.length}/512</p>
          </Field>

          <Field label={d.addressLabel} hint={d.addressHint}>
            <input type="text" value={address} onChange={e=>setAddress(e.target.value)}
              placeholder={d.addressPlaceholder}
              className={inputCls}/>
          </Field>

          <div className="grid grid-cols-2 gap-4">
            <Field label={d.emailLabel} hint={d.emailHint}>
              <div className="relative">
                <Mail className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400"/>
                <input type="email" value={email} onChange={e=>setEmail(e.target.value)}
                  placeholder="info@store.com"
                  className={`${inputCls} pr-9`} dir="ltr"/>
              </div>
            </Field>

            <Field label={d.websiteLabel}>
              <div className="relative">
                <Globe className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400"/>
                <input type="url" value={website} onChange={e=>setWebsite(e.target.value)}
                  placeholder="https://store.com"
                  className={`${inputCls} pr-9`} dir="ltr"/>
              </div>
            </Field>
          </div>

          {error && <ErrorBox msg={error}/>}

          <div className="flex gap-3">
            <button onClick={handleSaveProfile} disabled={busy}
              className="flex-1 flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-violet-600/20">
              {busy
                ? <><Loader2 className="w-4 h-4 animate-spin"/>{d.saving}</>
                : <>{d.saveBtn} <CheckCircle2 className="w-4 h-4"/></>}
            </button>
            <button onClick={()=>{setConnAt(new Date().toISOString());setStep(4)}}
              className="px-4 border border-slate-300 text-slate-500 hover:bg-slate-50 rounded-xl text-sm transition-all">
              {d.skipBtn}
            </button>
          </div>
        </div>
      )}

      {/* ── Step 4: Connected ─────────────────────────────────────────────── */}
      {step === 4 && (() => {
        // Honest verdict: DB says connected, but the live probe is the truth.
        // While the probe is running we show a neutral "checking" badge,
        // not a false green.
        // Soft-warning state: integration IS routing webhooks but a non-
        // blocking field (typically WABA ID for template sending) is still
        // pending. We show amber + a softer message so merchants don't see
        // "غير متصل فعليًا" while their messages are flowing.
        const verifyKnown   = liveVerify !== null
        const trulyOk       = verifyKnown && liveVerify!.truly_connected
        const softWarning   = verifyKnown && Boolean(liveVerify!.soft_warning)
        const trulyBroken   = verifyKnown && !liveVerify!.truly_connected
        const palette = !verifyKnown
          ? { wrap: 'bg-slate-50 border-slate-200',  iconWrap: 'bg-slate-100',   iconColor: 'text-slate-500', title: 'text-slate-700' }
          : softWarning
            ? { wrap: 'bg-amber-50 border-amber-200', iconWrap: 'bg-amber-100', iconColor: 'text-amber-600', title: 'text-amber-800' }
            : trulyOk
              ? { wrap: 'bg-emerald-50 border-emerald-200', iconWrap: 'bg-emerald-100', iconColor: 'text-emerald-600', title: 'text-emerald-800' }
              : { wrap: 'bg-red-50 border-red-200',         iconWrap: 'bg-red-100',     iconColor: 'text-red-600',     title: 'text-red-800' }

        return (
        <div className="space-y-4">
          <div className={`border rounded-2xl p-6 text-center space-y-3 ${palette.wrap}`}>
            <div className={`w-16 h-16 rounded-full flex items-center justify-center mx-auto ${palette.iconWrap}`}>
              {trulyBroken
                ? <AlertCircle className={`w-9 h-9 ${palette.iconColor}`}/>
                : <BadgeCheck className={`w-9 h-9 ${palette.iconColor}`}/>}
            </div>
            <div>
              <p className={`font-bold text-lg ${palette.title}`}>
                {!verifyKnown && (liveVerifying ? wc.connected.verifying : wc.connected.linkedUnverified)}
                {softWarning  && wc.connected.softWarning}
                {trulyOk && !softWarning && wc.connected.verified}
                {trulyBroken  && wc.connected.broken}
              </p>
              {softWarning && liveVerify?.reason_message && (
                <p className="text-xs text-amber-800 mt-2 leading-relaxed max-w-md mx-auto">
                  {liveVerify.reason_message}
                </p>
              )}
              {connName && <p className="font-semibold text-slate-700 mt-1">{connName}</p>}
              {connPhone && <p className="text-sm font-mono text-slate-500 mt-0.5">{connPhone}</p>}
              {connAt && (
                <p className="text-xs text-slate-400 mt-2">
                  {wc.connected.linkedAt} {new Date(connAt).toLocaleDateString(lang === 'ar' ? 'ar-SA' : 'en-US')}
                </p>
              )}
              {connLabel && (
                <p className="text-xs text-slate-500 mt-1">{displayConnLabel(connLabel, wc)}</p>
              )}
            </div>

            {/* Provider checklist — shows the real state from the live probe */}
            {verifyKnown && (
              <div className="bg-white rounded-xl p-4 text-start space-y-1.5">
                {liveVerify!.checks.map(c => (
                  <div key={c.name} className={`flex items-center gap-2 text-xs ${c.ok ? 'text-emerald-700' : 'text-red-600'}`}>
                    {c.ok
                      ? <CheckCircle2 className="w-3.5 h-3.5 shrink-0"/>
                      : <AlertCircle className="w-3.5 h-3.5 shrink-0"/>}
                    <span>
                      {({
                        has_record:          wc.connected.checkHasRecord,
                        status_ok:           wc.connected.checkStatusOk,
                        has_waba_id:         wc.connected.checkWabaId,
                        has_phone_id:        wc.connected.checkPhoneId,
                        has_token:           wc.connected.checkToken,
                        provider_reachable:  `${wc.connected.checkProvider}${c.name === 'provider_reachable' && liveVerify!.provider ? ` (${liveVerify!.provider})` : ''}`,
                      } as Record<string,string>)[c.name] || c.name}
                      {c.status_code != null && ` (${c.status_code})`}
                      {c.detail && !c.ok && <span className="text-slate-500"> — {c.detail}</span>}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {trulyBroken && (
              <div className="bg-white rounded-xl p-3 text-sm text-red-700 text-start border border-red-200">
                <p className="font-semibold">{wc.connected.reason}</p>
                <p className="mt-1">{liveVerify!.reason_message}</p>
                {liveVerify!.reason_code && (
                  <p className="text-[11px] text-slate-400 mt-1 font-mono">code: {liveVerify!.reason_code}</p>
                )}
              </div>
            )}

            {softWarning && (
              <div className="bg-white rounded-xl p-3 text-sm text-amber-800 text-start border border-amber-200">
                <p className="font-semibold">{wc.connected.note}</p>
                <p className="mt-1">{liveVerify!.reason_message}</p>
                <p className="text-xs text-slate-500 mt-2">
                  {wc.connected.softWarningDetail}
                </p>
                {liveVerify!.reason_code && (
                  <p className="text-[11px] text-slate-400 mt-1 font-mono">code: {liveVerify!.reason_code}</p>
                )}
              </div>
            )}

            {trulyOk && (
              <div className="bg-white rounded-xl p-4 text-start space-y-2">
                {[
                  wc.connected.featureAutoReply,
                  wc.connected.featureAiReady,
                  wc.connected.featureCampaigns,
                ].map(line => (
                  <div key={line} className="flex items-center gap-2 text-sm text-emerald-700">
                    <CheckCircle2 className="w-4 h-4 shrink-0"/>{line}
                  </div>
                ))}
              </div>
            )}

            <button
              type="button"
              onClick={runLiveVerify}
              disabled={liveVerifying}
              className="text-xs font-medium text-slate-500 hover:text-slate-700 underline inline-flex items-center gap-1 disabled:opacity-50"
            >
              {liveVerifying
                ? <><Loader2 className="w-3 h-3 animate-spin"/> {wc.connected.rechecking}</>
                : <><RefreshCw className="w-3 h-3"/> {wc.connected.recheckLive}</>}
            </button>
          </div>

          <div className="flex gap-3">
            <button onClick={()=>window.location.href='/overview'}
              className="flex-1 flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3 rounded-xl transition-all">
              <RefreshCw className="w-4 h-4"/>{wc.connected.dashboard}
            </button>
            {isCoexistenceConnLabel(connLabel) ? (
              <div className="flex items-center justify-center rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
                {wc.disconnect.managedByTeam}
              </div>
            ) : (
              <button onClick={handleDisconnect} disabled={busy}
                className="flex items-center justify-center gap-2 border border-red-200 text-red-500 hover:bg-red-50 font-medium text-sm px-4 py-3 rounded-xl transition-all">
                <Unplug className="w-4 h-4"/>{wc.connected.disconnect}
              </button>
            )}
          </div>
        </div>
        )
      })()}
    </div>
    </>
  )
}

// ── Disconnect Confirmation Modal ─────────────────────────────────────────

function DisconnectModal({
  open,
  busy,
  onConfirm,
  onCancel,
}: {
  open:      boolean
  busy:      boolean
  onConfirm: () => void
  onCancel:  () => void
}) {
  const { t, dir } = useLanguage()
  const d = t(tr => tr.whatsappConnect.disconnect)
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6 space-y-5" dir={dir}>
        {/* Icon + title */}
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="w-14 h-14 rounded-full bg-red-100 flex items-center justify-center">
            <AlertTriangle className="w-7 h-7 text-red-500" />
          </div>
          <div>
            <p className="text-base font-black text-slate-800">{d.title}</p>
            <p className="text-sm text-slate-500 mt-1">
              {d.subtitle}
            </p>
          </div>
        </div>

        {/* Checklist of consequences */}
        <ul className="space-y-2 text-sm text-slate-600 bg-slate-50 rounded-xl p-4">
          {[d.consequence1, d.consequence2, d.consequence3].map(line => (
            <li key={line} className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0 text-red-400">•</span>
              {line}
            </li>
          ))}
        </ul>

        {/* Actions */}
        <div className="flex gap-3">
          <button
            onClick={onCancel}
            disabled={busy}
            className="flex-1 border border-slate-200 rounded-xl py-2.5 text-sm font-semibold text-slate-600 hover:bg-slate-50 transition disabled:opacity-60"
          >
            {d.cancel}
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="flex-1 bg-red-500 hover:bg-red-600 rounded-xl py-2.5 text-sm font-bold text-white transition disabled:opacity-60 flex items-center justify-center gap-2"
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> {d.confirming}</>
              : <><Unplug className="w-4 h-4" /> {d.confirm}</>
            }
          </button>
        </div>
      </div>
    </div>
  )
}

function ErrorBox({ msg }: { msg: string }) {
  // Last-resort sanitization: never render raw Meta/provider text
  const safe = sanitizeMessage(msg)
  return (
    <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3">
      <XCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5"/>
      <p className="text-sm text-red-700">{safe}</p>
    </div>
  )
}
