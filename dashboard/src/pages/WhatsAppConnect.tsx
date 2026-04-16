/**
 * WhatsAppConnect.tsx
 * ────────────────────
 * Primary: Meta Embedded Signup (merchant's own WABA)
 * Fallback: Direct add (platform WABA — requires BSP permissions)
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
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
    if (!phone.trim()) { setError('أدخل رقم واتساب المرتبط على الجوال'); return }
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
      setError(e instanceof Error ? e.message : 'تعذر إرسال طلب التفعيل')
    } finally {
      setBusy(false)
    }
  }

  const current = localStatus

  // ── Connected ────────────────────────────────────────────────────────────
  if (current?.status === 'connected') {
    return (
      <div className="space-y-4">
        <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-5">
          <p className="text-lg font-bold text-emerald-800">واتساب الجوال + الذكاء الاصطناعي مرتبط ✅</p>
          <p className="mt-2 text-sm text-emerald-700">
            يمكنك الاستمرار في استخدام تطبيق واتساب للأعمال من الجوال، ونحلة ستستقبل الرسائل وترد وتنفّذ الأتمتة.
          </p>
          {current.phone_number && <p className="mt-3 text-sm font-mono text-emerald-800">{current.phone_number}</p>}
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-600 space-y-2">
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> استمر في استخدام تطبيق واتساب للأعمال على الجوال</div>
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> لا تحذف التطبيق ولا تفصل الرقم</div>
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> افتح التطبيق دوريًا للحفاظ على الجاهزية</div>
        </div>
      </div>
    )
  }

  // ── Submitted / pending ──────────────────────────────────────────────────
  if (current?.status === 'request_submitted' || current?.status === 'pending_activation' || current?.status === 'action_required') {
    return (
      <div className="space-y-4">
        <div className={`rounded-2xl border p-5 ${current.status === 'action_required' ? 'border-amber-200 bg-amber-50' : 'border-blue-200 bg-blue-50'}`}>
          <p className="text-lg font-bold text-slate-800">
            {current.status === 'request_submitted' && 'تم استلام طلبك'}
            {current.status === 'pending_activation' && 'جارٍ التفعيل من فريق نحلة'}
            {current.status === 'action_required' && 'مطلوب إجراء منك لإكمال الربط'}
          </p>
          <p className="mt-2 text-sm text-slate-700">
            {current.action_required_message
              || current.last_error
              || 'سيتواصل معك فريق نحلة لإتمام ربط واتساب الجوال + الذكاء الاصطناعي.'}
          </p>
          {current.request_submitted_at && (
            <p className="mt-3 text-xs text-slate-500">
              وقت الطلب: {new Date(current.request_submitted_at).toLocaleString('ar-SA')}
            </p>
          )}
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-600 space-y-2">
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> استمر في استخدام تطبيق واتساب للأعمال على الجوال</div>
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> لا تحذف التطبيق ولا تفصل الرقم</div>
          <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> افتح التطبيق دوريًا للحفاظ على الجاهزية</div>
        </div>
      </div>
    )
  }

  // ── Request form ─────────────────────────────────────────────────────────
  return (
    <div className="space-y-5">
      <div className="text-center">
        <p className="text-lg font-bold text-slate-800">واتساب الجوال + الذكاء الاصطناعي</p>
        <p className="mt-1 text-sm text-slate-500">
          احتفظ برقم واتساب الأعمال الموجود على جوالك، واستمر في استخدام التطبيق، ودع نحلة ترد وتنفّذ الأتمتة.
        </p>
      </div>

      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-600 space-y-2">
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> نفس الرقم الموجود على جوالك</div>
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> نحلة ترد على العملاء بالذكاء الاصطناعي</div>
        <div className="flex items-center gap-2"><CheckCircle2 className="w-4 h-4 text-emerald-600" /> يُفعّله فريق نحلة خلال 24 ساعة</div>
      </div>

      <div className="space-y-3">
        <input
          value={phone}
          onChange={e => setPhone(e.target.value)}
          placeholder="رقم واتساب الأعمال على الجوال"
          className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm"
          dir="ltr"
        />
        <input
          value={displayName}
          onChange={e => setDisplayName(e.target.value)}
          placeholder="اسم النشاط التجاري"
          className="w-full rounded-xl border border-slate-200 px-4 py-3 text-sm"
        />
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          placeholder="ملاحظات إضافية (اختياري)"
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
        {busy ? 'جارٍ إرسال الطلب...' : 'طلب التفعيل'}
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
  const [stage, setStage]       = useState<'init'|'loading-sdk'|'ready'|'exchanging'|'select-phone'|'add-phone'|'requesting-code'|'verify-phone'|'syncing-phone'|'done'>('init')
  const [error, setError]       = useState('')
  const [phones, setPhones]     = useState<EmbeddedPhone[]>([])
  const [wabaId, setWabaId]     = useState('')
  const [busy, setBusy]         = useState(false)
  const sdkLoaded               = useRef(false)
  const embeddedStatusChecked   = useRef(false)

  const [configId, setConfigId] = useState('')

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
        const cfg = await apiCall<{ app_id: string; config_id: string; graph_version: string }>(
          '/whatsapp/embedded/config'
        )
        if (cancelled) return
        if (cfg.config_id) setConfigId(cfg.config_id)
        // Init FB SDK
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
      } catch (e) {
        if (!cancelled) setError(explainWhatsAppError(e instanceof Error ? e.message : 'تعذر تحميل إعدادات Meta'))
      }
    }
    loadSdk()
    return () => { cancelled = true }
  }, [])

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
      setError(message || 'تعذر إكمال تفعيل الرقم في Meta')
      setStage('select-phone')
      return
    }

    if (res.status === 'pending') {
      setStage('select-phone')
    }
  }, [onConnected])

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
      } catch (e) {
        if (!cancelled) {
          setError(explainWhatsAppError(e instanceof Error ? e.message : 'تعذر مزامنة حالة الرقم مع Meta'))
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
  }, [stage, refreshEmbeddedStatus])

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
    }).catch(e => {
      setError(explainWhatsAppError(e instanceof Error ? e.message : 'حدث خطأ أثناء الربط'))
      setStage('ready')
    }).finally(() => setBusy(false))
  }, [])

  const launchSignup = useCallback(() => {
    if (!window.FB || !sdkLoaded.current) { setError('SDK غير جاهز، انتظر لحظة'); return }
    setError('')
    window.FB.login((response: any) => {
      if (!response?.authResponse) { setError('تم إلغاء عملية الربط'); return }
      handleExchange(response.authResponse.code, response.authResponse.accessToken)
    }, {
      config_id: configId,
      response_type: 'code',
      override_default_response_type: true,
      extras: {
        setup: {},
        feature: 'whatsapp_embedded_signup',
        sessionInfoVersion: '3',
      },
    })
  }, [handleExchange, configId])

  const selectPhone = useCallback(async (phoneId: string) => {
    setBusy(true); setError('')
    setStatusHint('جارٍ تجهيز خطوة التحقق... قد تصلك رسالة الكود قبل أن تظهر شاشة إدخاله. انتظر قليلًا.')
    setStage('requesting-code')
    try {
      const res = await apiCall<EmbeddedStatusPayload>('/whatsapp/embedded/select-phone', {
        method: 'POST',
        body: JSON.stringify({ phone_number_id: phoneId }),
      })
      setNewPhoneId(res.phone_number_id || phoneId)
      applyEmbeddedStatus(res)
    } catch (e) {
      setError(explainWhatsAppError(e instanceof Error ? e.message : 'تعذر اختيار الرقم'))
      setStage('select-phone')
    } finally { setBusy(false) }
  }, [applyEmbeddedStatus])

  // ── Render ────────────────────────────────────────────────────────────────
  if (stage === 'done') {
    return (
      <div className="flex flex-col items-center gap-4 py-10">
        <div className="w-16 h-16 rounded-full bg-emerald-100 flex items-center justify-center">
          <CheckCircle2 className="w-8 h-8 text-emerald-600" />
        </div>
        <p className="font-bold text-slate-800 text-lg">تم ربط واتساب بنجاح!</p>
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
            <p className="font-semibold text-slate-800">اختر رقم الهاتف</p>
            <p className="text-xs text-slate-500">WABA: {wabaId}</p>
          </div>
        </div>
        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-right">
              <p className="text-sm font-medium text-slate-800">إضافة رقم جديد</p>
              <p className="text-xs text-slate-500">
                إذا ظهر لك رقم قديم أو تجريبي، أضف رقم نشاطك الجديد من هنا
              </p>
            </div>
            <button
              onClick={() => { setError(''); setStage('add-phone') }}
              disabled={busy}
              className="shrink-0 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white hover:bg-violet-700 transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Phone className="w-4 h-4" />
              إضافة رقم
            </button>
          </div>
          {phones.length === 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-700">
              لا توجد أرقام هاتف في حساب واتساب للأعمال.
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
                <div className="text-right">
                  <p className="font-medium text-slate-800 text-sm">{p.number || p.id}</p>
                  {p.name && <p className="text-xs text-slate-500">{p.name}</p>}
                </div>
              </div>
              {p.verified
                ? <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full">موثّق</span>
                : <span className="text-xs bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">غير موثّق</span>
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
      if (!newPhone || !displayName) { setError('أدخل رقم الهاتف والاسم التجاري'); return }
      setBusy(true); setError('')
      try {
        // Normalize phone: remove spaces, dashes, dots, parentheses, leading zeros
        const cleanPhone = newPhone.replace(/[\s\-().+]/g, '').replace(/^0+/, '')
        const cleanCC    = countryCode.replace(/\D/g, '')
        if (!cleanPhone) { setError('أدخل رقم الهاتف بشكل صحيح'); setBusy(false); return }
        if (!displayName.trim()) { setError('الاسم التجاري مطلوب'); setBusy(false); return }
        setStatusHint('جارٍ إرسال رمز التحقق... قد تصلك رسالة الكود قبل أن تظهر شاشة إدخاله. لا تغادر هذه الخطوة.')
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
      } catch (e) {
        setError(explainWhatsAppError(e instanceof Error ? e.message : 'فشل إضافة الرقم'))
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
            <p className="font-semibold text-slate-800">إضافة رقم هاتف</p>
            <p className="text-xs text-slate-500">سيصلك رمز تحقق عبر SMS</p>
          </div>
        </div>
        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-slate-500 mb-1 text-right">رقم الهاتف</label>
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
            <p className="text-xs text-slate-400 mt-1 text-right">مثال: كود الدولة <span className="font-mono">966</span> ورقم الهاتف <span className="font-mono">512345678</span> (بدون الصفر الأول)</p>
          </div>
          <div>
            <label className="block text-xs text-slate-500 mb-1 text-right">الاسم التجاري</label>
            <input
              value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder="مثال: متجر نحلة للعطور"
              className="w-full px-4 py-3 border border-slate-200 rounded-xl text-sm focus:outline-none focus:border-violet-400"
            />
            <p className="text-xs text-slate-400 mt-1 text-right">اسم نشاطك التجاري كما سيظهر للعملاء في واتساب</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setStage('select-phone')}
            className="flex-1 py-3 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition-colors"
          >
            رجوع
          </button>
          <button
            onClick={submitPhone}
            disabled={busy}
            className="flex-1 py-3 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ChevronRight className="w-4 h-4" />}
            إرسال رمز التحقق
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
            <p className="font-semibold text-slate-800">جارٍ تجهيز رمز التحقق</p>
            <p className="text-xs text-slate-500">قد تصلك الرسالة النصية أولًا ثم تظهر شاشة إدخال الكود بعد لحظات</p>
          </div>
        </div>

        <div className="bg-violet-50 border border-violet-200 rounded-xl p-4 text-sm text-violet-800">
          {statusHint || 'يتم الآن طلب رمز التحقق من Meta. لا تنتقل إلى خطوة أخرى حتى تظهر شاشة إدخال الكود.'}
        </div>

        <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          إذا وصلتك الرسالة الآن فهذا طبيعي. انتظر قليلًا وسيتم فتح نافذة إدخال الكود تلقائيًا.
        </div>
      </div>
    )
  }

  // ── Verify phone stage ────────────────────────────────────────────────────────
  if (stage === 'verify-phone') {
    const submitOtp = async () => {
      if (!otpCode) { setError('أدخل رمز التحقق'); return }
      setBusy(true); setError('')
      try {
        const res = await apiCall<EmbeddedStatusPayload>('/whatsapp/embedded/verify-phone', {
          method: 'POST',
          body: JSON.stringify({ phone_number_id: newPhoneId, code: otpCode }),
        })
        applyEmbeddedStatus(res)
      } catch (e) {
        setError(explainWhatsAppError(e instanceof Error ? e.message : 'رمز التحقق غير صحيح'))
      } finally { setBusy(false) }
    }
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-emerald-100 flex items-center justify-center">
            <ShieldCheck className="w-5 h-5 text-emerald-600" />
          </div>
          <div>
            <p className="font-semibold text-slate-800">تحقق من رقم الهاتف</p>
            <p className="text-xs text-slate-500">أدخل الرمز المرسل عبر SMS</p>
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
            رجوع
          </button>
          <button
            onClick={submitOtp}
            disabled={busy}
            className="flex-1 py-3 bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <BadgeCheck className="w-4 h-4" />}
            تأكيد
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
            <p className="font-semibold text-slate-800">جارٍ مزامنة الرقم مع Meta</p>
            <p className="text-xs text-slate-500">لن يظهر كمرتبط إلا بعد أن يصبح جاهزًا فعليًا للإرسال</p>
          </div>
        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-800">
          {statusHint || 'Meta ما زالت تُراجع أو تُفعّل الرقم. يتم التحديث تلقائيًا كل بضع ثوانٍ.'}
        </div>

        {error && <div className="bg-red-50 border border-red-200 rounded-xl p-3 text-sm text-red-700">{error}</div>}

        <div className="flex gap-2">
          <button
            onClick={() => refreshEmbeddedStatus().catch(e => setError(explainWhatsAppError(e instanceof Error ? e.message : 'تعذر تحديث الحالة')))}
            className="flex-1 py-3 bg-violet-600 hover:bg-violet-700 text-white rounded-xl font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            <RefreshCw className="w-4 h-4" />
            تحديث الآن
          </button>
          <button
            onClick={() => setStage('select-phone')}
            className="flex-1 py-3 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition-colors"
          >
            العودة للأرقام
          </button>
        </div>
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
          <p className="font-bold text-slate-800 text-lg">ربط واتساب للأعمال</p>
          <p className="text-sm text-slate-500 mt-1">اربط حساب واتساب الخاص بمتجرك مباشرةً عبر Meta</p>
        </div>
      </div>

      {/* Steps */}
      <div className="bg-slate-50 rounded-xl p-4 space-y-2">
        {[
          { n: 1, text: 'اضغط "ربط مع Meta" أدناه' },
          { n: 2, text: 'سجّل دخولك بحساب Facebook' },
          { n: 3, text: 'ستنشئ Meta حساب واتساب للأعمال تلقائيًا إذا لم يكن لديك' },
          { n: 4, text: 'اختر أو أضف رقم هاتف نشاطك التجاري' },
        ].map(s => (
          <div key={s.n} className="flex items-center gap-3">
            <div className="w-6 h-6 rounded-full bg-violet-100 text-violet-700 flex items-center justify-center text-xs font-bold shrink-0">{s.n}</div>
            <p className="text-sm text-slate-600">{s.text}</p>
          </div>
        ))}
      </div>

      {/* Informational hint — not blocking */}
      <div className="bg-blue-50 border border-blue-100 rounded-xl p-3 text-xs text-blue-700">
        إذا لم يكن لديك حساب واتساب للأعمال بعد، ستقوم Meta بإنشائه تلقائيًا أثناء الربط — لا تحتاج إعداد أي شيء مسبقًا.
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
          ? <><Loader2 className="w-5 h-5 animate-spin" />جاري التحميل...</>
          : <>
              <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
                <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/>
              </svg>
              ربط مع Meta
            </>
        }
      </button>

      <p className="text-center text-xs text-slate-400">
        ستُفتح نافذة Meta الرسمية — كل بياناتك آمنة ومشفّرة. ستتكفل Meta بجميع الإعدادات التقنية نيابةً عنك.
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

const VERTICALS = [
  { value: 'RETAIL',                    label: 'تجزئة وتسوق'           },
  { value: 'APPAREL',                   label: 'ملابس وأزياء'           },
  { value: 'BEAUTY_SPA_SALON',          label: 'تجميل وعناية'           },
  { value: 'FOOD_AND_GROCERY',          label: 'طعام وبقالة'            },
  { value: 'RESTAURANT',               label: 'مطعم وكافيه'            },
  { value: 'HEALTH_AND_MEDICAL',        label: 'صحة وطب'               },
  { value: 'EDUCATION',                label: 'تعليم وتدريب'           },
  { value: 'HOTEL_AND_LODGING',         label: 'فنادق وضيافة'           },
  { value: 'TRAVEL_AND_TRANSPORTATION', label: 'سفر ونقل'              },
  { value: 'AUTOMOTIVE',               label: 'سيارات'                },
  { value: 'ENTERTAINMENT',            label: 'ترفيه وفعاليات'         },
  { value: 'PROFESSIONAL_SERVICES',     label: 'خدمات مهنية'            },
  { value: 'NONPROFIT',                label: 'منظمة غير ربحية'        },
  { value: 'OTHER',                    label: 'أخرى'                  },
]

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

const STEPS = ['الهوية', 'التحقق', 'الملف التجاري', 'تم']

function StepBar({ step }: { step: number }) {
  return (
    <div className="flex items-center justify-center gap-1 mb-7">
      {STEPS.map((label, i) => {
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
            {i < STEPS.length - 1 && (
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
      setError('أدخل Phone Number ID و Access Token أولاً ثم اضغط "اكتشاف WABA"')
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
        setError(r.message || r.error || 'تعذر اكتشاف الـ WABA تلقائياً')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'خطأ أثناء اكتشاف WABA')
    } finally {
      setResolvingWaba(false)
    }
  }

  const validate = (): string => {
    if (!phoneNumberId.trim())           return 'Phone Number ID مطلوب'
    if (!/^\d+$/.test(phoneNumberId.trim())) return 'Phone Number ID يجب أن يحتوي على أرقام فقط'
    if (!wabaId.trim())                  return 'WABA ID مطلوب'
    if (!/^\d+$/.test(wabaId.trim()))    return 'WABA ID يجب أن يحتوي على أرقام فقط'
    if (!accessToken.trim())             return 'Access Token مطلوب'
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
      setError(e instanceof Error ? e.message : 'حدث خطأ أثناء الربط')
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
            <p className="font-bold text-slate-800">الربط اليدوي</p>
            <span className="text-xs bg-emerald-100 text-emerald-700 font-semibold px-2 py-0.5 rounded-full">المعتمد حاليًا</span>
          </div>
          <p className="text-xs text-slate-500">أدخل بيانات حسابك على Meta مباشرةً</p>
        </div>
      </div>

      {/* Notice banner */}
      <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
        <div className="text-xs text-amber-800 space-y-1">
          <p className="font-semibold">هذه الطريقة اليدوية هي المعتمدة حاليًا</p>
          <p>سيتم تفعيل الربط السهل عبر Meta لاحقًا بعد اكتمال عملية الاعتماد.</p>
        </div>
      </div>

      {/* Fields */}
      <Field label="Phone Number ID" hint="الرقم التعريفي للهاتف من Meta Business — أرقام فقط" required>
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
          <p className="text-xs text-red-500">أرقام فقط</p>
        )}
      </Field>

      <Field label="WABA ID (WhatsApp Business Account ID)" hint="معرّف حساب واتساب للأعمال — أرقام فقط" required>
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
            title="اكتشاف WABA تلقائياً من Phone Number ID والتوكن"
            className="shrink-0 px-3 py-2.5 rounded-xl text-xs font-semibold border border-violet-300 bg-violet-50 hover:bg-violet-100 text-violet-700 transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5"
          >
            {resolvingWaba
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> جارٍ...</>
              : wabaResolved
                ? <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" /> تم</>
                : <><RefreshCw className="w-3.5 h-3.5" /> اكتشاف</>
            }
          </button>
        </div>
        {wabaResolved && (
          <p className="text-xs text-emerald-600 mt-1">✓ تم اكتشاف الـ WABA تلقائياً من Meta</p>
        )}
        {wabaId && !/^\d+$/.test(wabaId) && (
          <p className="text-xs text-red-500">أرقام فقط</p>
        )}
      </Field>

      <Field label="Permanent Access Token" hint="رمز الوصول الدائم من Meta — لا تشاركه مع أي أحد" required>
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
          <p className="text-xs font-bold text-slate-600 uppercase tracking-wide">حالة الربط</p>
          <ReadinessBadge
            ok={readiness.credentials_saved}
            label="البيانات محفوظة"
            detail={readiness.credentials_saved ? 'تم حفظ بيانات الاعتماد في النظام بنجاح' : 'فشل حفظ بيانات الاعتماد'}
          />
          <ReadinessBadge
            ok={readiness.phone_registered}
            label="تسجيل الرقم في Meta Cloud API"
            detail={
              readiness.phone_registered
                ? 'الرقم مُسجَّل ونشط في Meta — لا توجد حالة Pending'
                : readiness.phone_registration_error
                  ? `فشل تسجيل الرقم: ${readiness.phone_registration_error}`
                  : 'الرقم لم يُسجَّل — قد يبقى في حالة Pending في Meta'
            }
          />
          <ReadinessBadge
            ok={readiness.webhook_subscribed}
            label="اشتراك Webhook في Meta"
            detail={
              readiness.webhook_subscribed
                ? 'نحلة مشترك في أحداث WABA — الرسائل الواردة ستصل'
                : readiness.webhook_error
                  ? `فشل الاشتراك: ${readiness.webhook_error}`
                  : 'لم يتم الاشتراك في webhook — قد لا تصل الرسائل الواردة'
            }
          />
          <ReadinessBadge
            ok={readiness.inbound_usable}
            label="جاهز للرسائل الواردة"
            detail={
              readiness.inbound_usable
                ? 'الربط مكتمل — الرسائل الواردة ستُوجَّه بشكل صحيح'
                : 'الربط جزئي — تحقق من تسجيل الرقم واشتراك webhook أعلاه'
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
                المتابعة رغم ذلك (إرسال فقط)
              </button>
              <button
                onClick={() => { setReadiness(null); setError('') }}
                className="px-4 py-2.5 rounded-xl text-sm border border-slate-300 text-slate-600 hover:bg-slate-50 transition-all"
              >
                إعادة المحاولة
              </button>
            </div>
          )}
        </div>
      )}

      {/* Help link */}
      <p className="text-xs text-slate-400 text-center">
        لا تعرف كيف تستخرج هذه البيانات؟{' '}
        <a href="/help/whatsapp-manual-setup" target="_blank" rel="noreferrer"
          className="text-emerald-600 hover:text-emerald-700 font-medium underline">
          راجع الشرح التفصيلي خطوة بخطوة ←
        </a>
      </p>

      {!readiness && (
        <button
          onClick={handleConnect}
          disabled={busy || !phoneNumberId || !wabaId || !accessToken}
          className="w-full flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-50 shadow-lg shadow-emerald-600/20"
        >
          {busy
            ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ الربط...</>
            : <><MessageCircle className="w-4 h-4" /> ربط واتساب يدويًا</>
          }
        </button>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function WhatsAppConnect() {
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
        } else if (s.connection_type === 'coexistence' || s.provider === 'dialog360') {
          setMode('coexistence')
        } else if ((s.status === 'pending' || s.status === 'otp_pending') && s.phone_number_id) {
          // Resume from Step 2 — OTP was already sent, pending verification
          setPhoneNumberId(s.phone_number_id)
          setSentMsg('تم إرسال رمز التحقق مسبقاً — أدخل الرمز الذي وصلك.')
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
    if (!phone.trim())       { setError('أدخل رقم الهاتف'); return }
    if (!displayName.trim()) { setError('أدخل اسم العرض'); return }

    const original   = phone.trim()
    const normalized = normalizePhone(original)
    const valid      = isValidSaudiPhone(normalized)

    console.log('[Nahla/OTP] original_input=', original,
      '| normalized=', normalized, '| valid=', valid)

    if (!valid) {
      // PHONE_VALIDATION_ERROR — do not proceed
      setError('رقم الهاتف غير صحيح. أدخل رقماً سعودياً مثل: +966542878717 أو 0542878717')
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
        setError('⏳ ' + sanitizeMessage(raw) + ' — جرّب مرة أخرى بعد بضع ساعات أو استخدم رقماً مختلفاً.')
      } else {
        const isPhoneFormatMsg = /صيغة رقم الهاتف|phone.*format|invalid.*phone/i.test(raw)
        if (isPhoneFormatMsg && valid) {
          setError('تعذر إرسال رمز التحقق. تأكد من الرقم أو حاول مرة أخرى.')
        } else {
          setError(sanitizeMessage(raw))
        }
      }
    }
    finally { setBusy(false) }
  }, [phone, displayName, otpMethod])

  // ── Step 2 → 3 ──────────────────────────────────────────────────────────

  const handleVerifyOtp = useCallback(async () => {
    if (otp.trim().length < 6) { setError('أدخل الرمز كاملاً (6 أرقام)'); return }
    setBusy(true); setError('')
    try {
      const r = await verifyOtp(phoneNumberId, otp.trim()) as VerifyResponse & { sending_enabled?: boolean; status?: string }
      setConnPhone(r.phone_number)
      setConnName(r.display_name)
      if (r.sending_enabled === false || (r.status && r.status !== 'connected')) {
        setError(explainWhatsAppError(r.message || 'تم التحقق من الرمز، لكن الرقم ما زال بانتظار تفعيل Meta.'))
        return
      }
      setStep(3)
    } catch (e) { setError(explainWhatsAppError(sanitizeMessage(e instanceof Error ? e.message : ''))) }
    finally { setBusy(false) }
  }, [otp, phoneNumberId])

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
      connLabel === 'واتساب الجوال + الذكاء الاصطناعي'
    if (managedByOps) {
      setError('فصل هذا النوع من الربط يتم عبر فريق نحلة حفاظًا على استقرار القناة.')
      return
    }
    setShowDisconnectModal(true)
  }, [connLabel, status])

  const confirmDisconnect = useCallback(async () => {
    setBusy(true)
    try {
      await disconnect()
      setShowDisconnectModal(false)
      setStep(1); setPhone(''); setDisplayName(''); setOtp('')
      setConnPhone(''); setConnName('')
    } catch { setError('تعذّر فصل واتساب — حاول مرة أخرى') }
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
    <div className="max-w-lg mx-auto space-y-4" dir="rtl">

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
          <MessageCircle className="w-6 h-6 text-emerald-500" />
          ربط واتساب للأعمال
        </h1>
        <p className="text-slate-500 mt-1 text-sm">
          أضف رقم واتساب متجرك ليبدأ نحلة AI بالرد على عملائك
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
              🔑 الربط اليدوي
              {mode === 'manual' && (
                <span className="text-[10px] bg-emerald-100 text-emerald-700 font-semibold px-1.5 py-0.5 rounded-full">
                  معتمد حاليًا
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
              🔗 ربط عبر Meta
            </button>
            <button
              onClick={() => { setMode('direct'); setStep(1); setError('') }}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                mode === 'direct'
                  ? 'bg-white shadow text-violet-700'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              📱 OTP
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
                ✨ واتساب الجوال
              </button>
            )}
          </div>

          {/* Context hint per mode */}
          {mode === 'manual' && (
            <p className="text-xs text-emerald-700 text-center bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-1.5">
              أدخل بياناتك من Meta مباشرةً — هذه الطريقة هي الطريقة المعتمدة حاليًا قبل اكتمال اعتماد Meta
            </p>
          )}
          {mode === 'embedded' && (
            <p className="text-xs text-slate-500 text-center bg-slate-50 border border-slate-200 rounded-lg px-3 py-1.5">
              الربط السهل عبر Meta — سيتم تفعيله بعد اكتمال اعتماد Meta الرسمي
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
      {mode === 'direct' && step < 4 && <StepBar step={step} />}

      {/* ── Direct mode steps (1-3) ──────────────────────────────────────── */}
      {/* ── Step 1: Identity ─────────────────────────────────────────────── */}
      {mode === 'direct' && step === 1 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center">
              <Phone className="w-5 h-5 text-violet-600" />
            </div>
            <div>
              <p className="font-semibold text-slate-800">بيانات هوية النشاط التجاري</p>
              <p className="text-xs text-slate-500">تُستخدم لتسجيل الرقم في Meta</p>
            </div>
          </div>

          <Field label="رقم الهاتف" hint="رقم لم يُسجَّل على واتساب من قبل" required>
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
                ? <p className="text-xs text-emerald-600 mt-1">✓ الرقم المُرسَل: {n}</p>
                : <p className="text-xs text-amber-500 mt-1">الصيغ المقبولة: +966542878717 أو 0542878717 أو 542878717</p>
            })()}
          </Field>

          <Field
            label="الاسم المعروض (Verified Name)"
            hint="الاسم الذي سيظهر للعملاء على واتساب — يجب أن يطابق اسم نشاطك التجاري الرسمي"
            required
          >
            <input
              type="text" value={displayName}
              onChange={e => setDisplayName(e.target.value)}
              placeholder="مثال: متجر النور للإلكترونيات"
              className={inputCls}
            />
            <p className="text-xs text-amber-600 mt-1">
              ⚠️ يجب أن يتطابق مع اسم نشاطك في السجل التجاري
            </p>
          </Field>

          <Field label="طريقة استقبال رمز التحقق">
            <div className="flex gap-3">
              {(['SMS','VOICE'] as const).map(m => (
                <button key={m} onClick={() => setOtpMethod(m)}
                  className={`flex-1 py-2.5 rounded-xl text-sm font-medium border transition-all
                    ${otpMethod===m ? 'bg-violet-600 text-white border-violet-600' : 'bg-white text-slate-600 border-slate-300 hover:border-violet-300'}`}>
                  {m==='SMS' ? '📱 رسالة نصية' : '📞 مكالمة هاتفية'}
                </button>
              ))}
            </div>
          </Field>

          {error && <ErrorBox msg={error} />}

          <button onClick={handleRequestOtp} disabled={busy}
            className="w-full flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-violet-600/20">
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin"/>جاري الإرسال...</>
              : <>إرسال رمز التحقق <ChevronRight className="w-4 h-4"/></>}
          </button>

          {/* Info */}
          <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 text-xs text-blue-700 space-y-1">
            <p className="font-semibold text-blue-800">📋 متطلبات الرقم:</p>
            <ul className="space-y-1 list-disc list-inside">
              <li>رقم غير مسجَّل على واتساب الشخصي أو للأعمال</li>
              <li>يجب استقبال SMS أو مكالمة على هذا الرقم</li>
              <li>رقم سعودي (+966) أو دولي</li>
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
              <p className="font-semibold text-slate-800">التحقق من الرقم</p>
              <p className="text-xs text-slate-500 mt-0.5">{sentMsg}</p>
            </div>
          </div>

          <Field label="رمز التحقق (6 أرقام)" required>
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
              ? <><Loader2 className="w-4 h-4 animate-spin"/>جاري التحقق...</>
              : <>تأكيد الرقم <CheckCircle2 className="w-4 h-4"/></>}
          </button>

          {/* Already verified in Meta? refresh button */}
          <div className="flex items-center gap-2 p-3 bg-blue-50 border border-blue-200 rounded-xl text-xs text-blue-700">
            <ShieldCheck className="w-4 h-4 shrink-0 text-blue-500"/>
            <span>هل تحققت من الرقم مسبقاً في Meta؟</span>
            <button
              onClick={async () => {
                setBusy(true); setError('')
                try {
                  const r = await post<{ updated: boolean; connected?: boolean; message: string }>(
                    '/whatsapp/direct/refresh-from-meta', {}
                  )
                  if (r.updated || r.connected) {
                    setStep(3)
                    setSentMsg('✅ تم التحقق من حالة الرقم في Meta بنجاح')
                  } else {
                    setError(sanitizeMessage(r.message))
                  }
                } catch(e) {
                  setError(sanitizeMessage(e instanceof Error ? e.message : ''))
                } finally { setBusy(false) }
              }}
              disabled={busy}
              className="mr-auto font-semibold underline hover:text-blue-900 disabled:opacity-50 whitespace-nowrap">
              {busy ? 'جاري التحقق...' : 'تحقق من حالة الربط'}
            </button>
          </div>

          {/* Resend code */}
          <div className="flex items-center justify-between text-sm pt-1">
            <button onClick={()=>{setStep(1);setError('');setOtp('')}}
              className="text-slate-400 hover:text-slate-600">
              ← تغيير رقم الهاتف
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
                ? `إعادة الإرسال (${resendCooldown}ث)`
                : 'إعادة إرسال الرمز'}
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
              <p className="font-semibold text-slate-800">ملف النشاط التجاري</p>
              <p className="text-xs text-slate-500">يظهر للعملاء في صفحة نشاطك على واتساب</p>
            </div>
          </div>

          <div className="bg-emerald-50 border border-emerald-200 rounded-xl p-3 flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0"/>
            <p className="text-xs text-emerald-700">
              تم التحقق من الرقم بنجاح — أكمل بيانات نشاطك التجاري
            </p>
          </div>

          <Field label="نوع النشاط التجاري" required>
            <select value={vertical} onChange={e=>setVertical(e.target.value)} className={inputCls}>
              {VERTICALS.map(v => <option key={v.value} value={v.value}>{v.label}</option>)}
            </select>
          </Field>

          <Field label="وصف النشاط التجاري" hint="ما يظهر في قسم 'نبذة' على واتساب للأعمال — حد أقصى 512 حرف">
            <textarea
              value={about} onChange={e=>setAbout(e.target.value.slice(0,512))}
              placeholder="مثال: نوفر أفضل منتجات الإلكترونيات بأسعار منافسة مع توصيل سريع"
              rows={3} className={`${inputCls} resize-none`}
            />
            <p className="text-xs text-slate-400 text-left">{about.length}/512</p>
          </Field>

          <Field label="عنوان النشاط التجاري" hint="العنوان الذي سيظهر في الملف التجاري">
            <input type="text" value={address} onChange={e=>setAddress(e.target.value)}
              placeholder="مثال: الرياض، حي العليا، شارع التحلية"
              className={inputCls}/>
          </Field>

          <div className="grid grid-cols-2 gap-4">
            <Field label="البريد الإلكتروني" hint="للتواصل التجاري">
              <div className="relative">
                <Mail className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400"/>
                <input type="email" value={email} onChange={e=>setEmail(e.target.value)}
                  placeholder="info@store.com"
                  className={`${inputCls} pr-9`} dir="ltr"/>
              </div>
            </Field>

            <Field label="الموقع الإلكتروني">
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
                ? <><Loader2 className="w-4 h-4 animate-spin"/>جاري الحفظ...</>
                : <>حفظ وإكمال الربط <CheckCircle2 className="w-4 h-4"/></>}
            </button>
            <button onClick={()=>{setConnAt(new Date().toISOString());setStep(4)}}
              className="px-4 border border-slate-300 text-slate-500 hover:bg-slate-50 rounded-xl text-sm transition-all">
              تخطي
            </button>
          </div>
        </div>
      )}

      {/* ── Step 4: Connected ─────────────────────────────────────────────── */}
      {step === 4 && (
        <div className="space-y-4">
          <div className="bg-emerald-50 border border-emerald-200 rounded-2xl p-6 text-center space-y-3">
            <div className="w-16 h-16 bg-emerald-100 rounded-full flex items-center justify-center mx-auto">
              <BadgeCheck className="w-9 h-9 text-emerald-600"/>
            </div>
            <div>
              <p className="font-bold text-emerald-800 text-lg">واتساب مرتبط ✅</p>
              {connName && <p className="font-semibold text-slate-700 mt-1">{connName}</p>}
              {connPhone && <p className="text-sm font-mono text-slate-500 mt-0.5">{connPhone}</p>}
              {connAt && (
                <p className="text-xs text-slate-400 mt-2">
                  تم الربط: {new Date(connAt).toLocaleDateString('ar-SA')}
                </p>
              )}
              {connLabel && (
                <p className="text-xs text-slate-500 mt-1">{connLabel}</p>
              )}
            </div>
            <div className="bg-white rounded-xl p-4 text-right space-y-2">
              {[
                'الرد التلقائي على العملاء مفعّل',
                'نحلة AI جاهز للمحادثات',
                'الحملات التسويقية متاحة',
              ].map(t => (
                <div key={t} className="flex items-center gap-2 text-sm text-emerald-700">
                  <CheckCircle2 className="w-4 h-4 shrink-0"/>{t}
                </div>
              ))}
            </div>
          </div>

          <div className="flex gap-3">
            <button onClick={()=>window.location.href='/overview'}
              className="flex-1 flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3 rounded-xl transition-all">
              <RefreshCw className="w-4 h-4"/>لوحة التحكم
            </button>
            {connLabel === 'واتساب الجوال + الذكاء الاصطناعي' ? (
              <div className="flex items-center justify-center rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
                تتم إدارة هذا الربط عبر فريق نحلة
              </div>
            ) : (
              <button onClick={handleDisconnect} disabled={busy}
                className="flex items-center justify-center gap-2 border border-red-200 text-red-500 hover:bg-red-50 font-medium text-sm px-4 py-3 rounded-xl transition-all">
                <Unplug className="w-4 h-4"/>فصل
              </button>
            )}
          </div>
        </div>
      )}
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
  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6 space-y-5" dir="rtl">
        {/* Icon + title */}
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="w-14 h-14 rounded-full bg-red-100 flex items-center justify-center">
            <AlertTriangle className="w-7 h-7 text-red-500" />
          </div>
          <div>
            <p className="text-base font-black text-slate-800">فصل واتساب؟</p>
            <p className="text-sm text-slate-500 mt-1">
              سيتوقف الرد التلقائي على العملاء فور الفصل.
              يمكنك إعادة الربط في أي وقت.
            </p>
          </div>
        </div>

        {/* Checklist of consequences */}
        <ul className="space-y-2 text-sm text-slate-600 bg-slate-50 rounded-xl p-4">
          {[
            'سيتوقف نحلة AI عن الرد على رسائل واتساب',
            'لن تُرسل الحملات التسويقية عبر واتساب',
            'تبقى بيانات المتجر والمحادثات السابقة محفوظة',
          ].map(line => (
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
            إلغاء
          </button>
          <button
            onClick={onConfirm}
            disabled={busy}
            className="flex-1 bg-red-500 hover:bg-red-600 rounded-xl py-2.5 text-sm font-bold text-white transition disabled:opacity-60 flex items-center justify-center gap-2"
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ الفصل...</>
              : <><Unplug className="w-4 h-4" /> تأكيد الفصل</>
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
