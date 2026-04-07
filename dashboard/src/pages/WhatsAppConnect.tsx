/**
 * WhatsAppConnect.tsx
 * ────────────────────
 * Direct WhatsApp registration wizard (Shared-WABA model).
 *
 * Flow:
 *  Step 1 → Merchant enters phone number + display name
 *  Step 2 → Merchant enters OTP sent by Meta to their phone
 *  Step 3 → Success — number connected under Nahla's WABA
 */
import { useCallback, useEffect, useState } from 'react'
import {
  BadgeCheck,
  CheckCircle2,
  ChevronRight,
  Loader2,
  MessageCircle,
  Phone,
  RefreshCw,
  ShieldCheck,
  Unplug,
  XCircle,
} from 'lucide-react'
import { apiClient } from '../api/client'

// ── API helpers ───────────────────────────────────────────────────────────────

async function requestOtp(phoneNumber: string, displayName: string, method = 'SMS') {
  const res = await apiClient.post('/whatsapp/direct/request-otp', {
    phone_number: phoneNumber,
    display_name: displayName,
    method,
  })
  return res.data as { status: string; phone_number_id: string; message: string }
}

async function verifyOtp(phoneNumberId: string, code: string) {
  const res = await apiClient.post('/whatsapp/direct/verify-otp', {
    phone_number_id: phoneNumberId,
    code,
  })
  return res.data as { status: string; phone_number: string; display_name: string; message: string }
}

async function getDirectStatus() {
  const res = await apiClient.get('/whatsapp/direct/status')
  return res.data as {
    connected: boolean
    status: string
    phone_number?: string
    display_name?: string
    connected_at?: string
  }
}

async function disconnectWhatsApp() {
  await apiClient.post('/whatsapp/connection/disconnect')
}

// ── Step indicator ────────────────────────────────────────────────────────────

function StepDot({ n, current }: { n: number; current: number }) {
  const done   = n < current
  const active = n === current
  return (
    <div className="flex items-center gap-1">
      <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all
        ${done   ? 'bg-emerald-500 text-white'   : ''}
        ${active ? 'bg-violet-600 text-white ring-4 ring-violet-100' : ''}
        ${!done && !active ? 'bg-slate-100 text-slate-400' : ''}
      `}>
        {done ? <CheckCircle2 className="w-4 h-4" /> : n}
      </div>
    </div>
  )
}

function StepBar({ step }: { step: number }) {
  const labels = ['رقم الهاتف', 'التحقق', 'تم الربط']
  return (
    <div className="flex items-center justify-center gap-2 mb-8">
      {labels.map((label, i) => (
        <div key={i} className="flex items-center gap-2">
          <div className="flex flex-col items-center gap-1">
            <StepDot n={i + 1} current={step} />
            <span className={`text-[10px] font-medium ${i + 1 === step ? 'text-violet-600' : 'text-slate-400'}`}>
              {label}
            </span>
          </div>
          {i < labels.length - 1 && (
            <div className={`w-10 h-0.5 mb-4 rounded ${i + 1 < step ? 'bg-emerald-400' : 'bg-slate-200'}`} />
          )}
        </div>
      ))}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function WhatsAppConnect() {
  const [step, setStep]               = useState<1 | 2 | 3>(1)
  const [loading, setLoading]         = useState(true)
  const [busy, setBusy]               = useState(false)
  const [error, setError]             = useState('')

  // Step 1 fields
  const [phone, setPhone]             = useState('')
  const [displayName, setDisplayName] = useState('')
  const [otpMethod, setOtpMethod]     = useState<'SMS' | 'VOICE'>('SMS')

  // Step 2 fields
  const [otp, setOtp]                 = useState('')
  const [phoneNumberId, setPhoneNumberId] = useState('')
  const [sentTo, setSentTo]           = useState('')

  // Step 3 — connected info
  const [connectedPhone, setConnectedPhone] = useState('')
  const [connectedName, setConnectedName]   = useState('')
  const [connectedAt, setConnectedAt]       = useState('')

  // Load existing connection
  useEffect(() => {
    getDirectStatus()
      .then(s => {
        if (s.connected) {
          setConnectedPhone(s.phone_number ?? '')
          setConnectedName(s.display_name ?? '')
          setConnectedAt(s.connected_at ?? '')
          setStep(3)
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // ── Step 1: Request OTP ──────────────────────────────────────────────────

  const handleRequestOtp = useCallback(async () => {
    if (!phone.trim()) { setError('أدخل رقم الهاتف'); return }
    if (!displayName.trim()) { setError('أدخل اسم العرض على واتساب'); return }

    setBusy(true)
    setError('')
    try {
      const res = await requestOtp(phone.trim(), displayName.trim(), otpMethod)
      setPhoneNumberId(res.phone_number_id)
      setSentTo(res.message)
      setStep(2)
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? 'حدث خطأ. تحقق من الرقم وأعد المحاولة.'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }, [phone, displayName, otpMethod])

  // ── Step 2: Verify OTP ───────────────────────────────────────────────────

  const handleVerifyOtp = useCallback(async () => {
    if (otp.trim().length < 6) { setError('أدخل الرمز المكوّن من 6 أرقام'); return }

    setBusy(true)
    setError('')
    try {
      const res = await verifyOtp(phoneNumberId, otp.trim())
      setConnectedPhone(res.phone_number)
      setConnectedName(res.display_name)
      setConnectedAt(new Date().toISOString())
      setStep(3)
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? 'رمز التحقق غير صحيح. أعد المحاولة.'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }, [otp, phoneNumberId])

  // ── Disconnect ───────────────────────────────────────────────────────────

  const handleDisconnect = useCallback(async () => {
    if (!confirm('هل أنت متأكد من فصل واتساب؟ سيتوقف الرد التلقائي فوراً.')) return
    setBusy(true)
    try {
      await disconnectWhatsApp()
      setStep(1)
      setPhone('')
      setDisplayName('')
      setOtp('')
      setConnectedPhone('')
      setConnectedName('')
    } catch {
      setError('فشل في قطع الاتصال')
    } finally {
      setBusy(false)
    }
  }, [])

  // ─────────────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-violet-500" />
      </div>
    )
  }

  return (
    <div className="max-w-lg mx-auto space-y-2" dir="rtl">
      {/* Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
          <MessageCircle className="w-6 h-6 text-emerald-500" />
          ربط واتساب للأعمال
        </h1>
        <p className="text-slate-500 mt-1 text-sm">
          أضف رقم واتساب متجرك ليبدأ الرد التلقائي على عملائك
        </p>
      </div>

      {/* Step bar — only show for steps 1 and 2 */}
      {step < 3 && <StepBar step={step} />}

      {/* ── Step 1: Phone number ─────────────────────────────────────────── */}
      {step === 1 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-10 h-10 rounded-xl bg-violet-100 flex items-center justify-center">
              <Phone className="w-5 h-5 text-violet-600" />
            </div>
            <div>
              <p className="font-semibold text-slate-800">رقم الهاتف</p>
              <p className="text-xs text-slate-500">رقم لم يُسجَّل على واتساب من قبل</p>
            </div>
          </div>

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                رقم الهاتف <span className="text-red-500">*</span>
              </label>
              <input
                type="tel"
                value={phone}
                onChange={e => setPhone(e.target.value)}
                placeholder="+966 5X XXX XXXX"
                className="w-full border border-slate-300 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400 text-left"
                dir="ltr"
              />
              <p className="text-xs text-slate-400 mt-1">مثال: +966501234567 أو 0501234567</p>
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                الاسم على واتساب <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                placeholder="مثال: متجر النور"
                className="w-full border border-slate-300 rounded-xl px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-violet-400"
              />
              <p className="text-xs text-slate-400 mt-1">هذا الاسم سيظهر للعملاء على واتساب</p>
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-700 mb-2">
                طريقة التحقق
              </label>
              <div className="flex gap-3">
                {(['SMS', 'VOICE'] as const).map(m => (
                  <button
                    key={m}
                    onClick={() => setOtpMethod(m)}
                    className={`flex-1 py-2.5 rounded-xl text-sm font-medium border transition-all
                      ${otpMethod === m
                        ? 'bg-violet-600 text-white border-violet-600'
                        : 'bg-white text-slate-600 border-slate-300 hover:border-violet-300'
                      }`}
                  >
                    {m === 'SMS' ? '📱 رسالة نصية' : '📞 مكالمة هاتفية'}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3">
              <XCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}

          <button
            onClick={handleRequestOtp}
            disabled={busy}
            className="w-full flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-violet-600/20"
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> جاري الإرسال...</>
              : <>إرسال رمز التحقق <ChevronRight className="w-4 h-4" /></>
            }
          </button>
        </div>
      )}

      {/* ── Step 2: OTP ──────────────────────────────────────────────────── */}
      {step === 2 && (
        <div className="bg-white rounded-2xl border border-slate-200 p-6 space-y-5 shadow-sm">
          <div className="flex items-center gap-3 mb-1">
            <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center">
              <ShieldCheck className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <p className="font-semibold text-slate-800">رمز التحقق</p>
              <p className="text-xs text-slate-500 mt-0.5">{sentTo}</p>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              الرمز المُرسَل <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={otp}
              onChange={e => setOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
              placeholder="123456"
              maxLength={6}
              className="w-full border border-slate-300 rounded-xl px-4 py-3 text-center text-2xl font-mono tracking-widest focus:outline-none focus:ring-2 focus:ring-amber-400"
              dir="ltr"
              autoFocus
            />
            <p className="text-xs text-slate-400 mt-1 text-center">
              الرمز مكوّن من 6 أرقام
            </p>
          </div>

          {error && (
            <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl p-3">
              <XCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}

          <button
            onClick={handleVerifyOtp}
            disabled={busy || otp.length < 6}
            className="w-full flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-emerald-600/20"
          >
            {busy
              ? <><Loader2 className="w-4 h-4 animate-spin" /> جاري التحقق...</>
              : <>تأكيد الربط <CheckCircle2 className="w-4 h-4" /></>
            }
          </button>

          <button
            onClick={() => { setStep(1); setError(''); setOtp('') }}
            className="w-full text-sm text-slate-400 hover:text-slate-600 py-2"
          >
            ← تغيير رقم الهاتف
          </button>
        </div>
      )}

      {/* ── Step 3: Connected ─────────────────────────────────────────────── */}
      {step === 3 && (
        <div className="space-y-4">
          <div className="bg-emerald-50 border border-emerald-200 rounded-2xl p-6 text-center space-y-3">
            <div className="w-16 h-16 bg-emerald-100 rounded-full flex items-center justify-center mx-auto">
              <BadgeCheck className="w-9 h-9 text-emerald-600" />
            </div>
            <div>
              <p className="font-bold text-emerald-800 text-lg">واتساب مرتبط ✅</p>
              {connectedName && (
                <p className="font-semibold text-slate-700 mt-1">{connectedName}</p>
              )}
              {connectedPhone && (
                <p className="text-sm font-mono text-slate-500 mt-0.5">{connectedPhone}</p>
              )}
              {connectedAt && (
                <p className="text-xs text-slate-400 mt-2">
                  تم الربط: {new Date(connectedAt).toLocaleDateString('ar-SA')}
                </p>
              )}
            </div>

            <div className="bg-white rounded-xl p-4 text-right space-y-2 mt-2">
              <div className="flex items-center gap-2 text-sm text-emerald-700">
                <CheckCircle2 className="w-4 h-4 shrink-0" />
                الرد التلقائي على العملاء مفعّل
              </div>
              <div className="flex items-center gap-2 text-sm text-emerald-700">
                <CheckCircle2 className="w-4 h-4 shrink-0" />
                نحلة AI جاهز للمحادثات
              </div>
              <div className="flex items-center gap-2 text-sm text-emerald-700">
                <CheckCircle2 className="w-4 h-4 shrink-0" />
                الحملات التسويقية متاحة
              </div>
            </div>
          </div>

          <div className="flex gap-3">
            <button
              onClick={() => window.location.href = '/overview'}
              className="flex-1 flex items-center justify-center gap-2 bg-violet-600 hover:bg-violet-500 text-white font-bold py-3 rounded-xl transition-all"
            >
              <RefreshCw className="w-4 h-4" />
              لوحة التحكم
            </button>
            <button
              onClick={handleDisconnect}
              disabled={busy}
              className="flex items-center justify-center gap-2 border border-red-200 text-red-500 hover:bg-red-50 font-medium text-sm px-4 py-3 rounded-xl transition-all"
            >
              <Unplug className="w-4 h-4" />
              فصل
            </button>
          </div>
        </div>
      )}

      {/* Info box */}
      {step === 1 && (
        <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 space-y-2 text-xs text-blue-700">
          <p className="font-semibold text-blue-800">📋 متطلبات الرقم:</p>
          <ul className="space-y-1 list-disc list-inside">
            <li>رقم هاتف سعودي أو دولي غير مسجَّل على واتساب</li>
            <li>يجب أن تتمكن من استقبال رسائل SMS على هذا الرقم</li>
            <li>الرقم سيُستخدم فقط لخدمة عملاء متجرك</li>
          </ul>
        </div>
      )}
    </div>
  )
}
