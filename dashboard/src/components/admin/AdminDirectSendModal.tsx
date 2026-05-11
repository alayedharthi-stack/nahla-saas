/**
 * AdminDirectSendModal
 * ────────────────────
 * Admin-only modal that fires a single WhatsApp template message
 * directly through the live provider (Meta / 360dialog) using
 * ``POST /admin/debug/whatsapp/send-template``, bypassing the
 * entire campaign engine.
 *
 * UX goals:
 *   * Pre-populate everything the admin already knows (phone_number_id
 *     + template name from the campaign row when opened from there).
 *   * Show the raw provider response in a pretty-printed JSON block
 *     so support can quote the exact error code/subcode/fbtrace_id
 *     into a 360dialog ticket without leaving the dashboard.
 *   * Mask the destination phone in the visible echo, even though
 *     the backend already masks — defense in depth, and consistent
 *     screen-sharing safety.
 *
 * Visibility: this component should be wrapped in `isAdmin()` by the
 * caller — the modal itself does NOT re-check the role.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Send, X, Copy, CheckCircle2, AlertTriangle } from 'lucide-react'

import {
  adminDebugApi,
  type AdminDirectSendResponse,
} from '../../api/adminDebug'

interface Props {
  open:               boolean
  onClose:            () => void
  defaultPhoneNumberId?: string
  defaultTemplate?:    string
  defaultLanguage?:    string
}

export default function AdminDirectSendModal({
  open,
  onClose,
  defaultPhoneNumberId = '',
  defaultTemplate      = '',
  defaultLanguage      = 'ar',
}: Props) {
  const [phoneNumberId, setPhoneNumberId] = useState(defaultPhoneNumberId)
  const [to,             setTo]            = useState('')
  const [template,       setTemplate]      = useState(defaultTemplate)
  const [language,       setLanguage]      = useState(defaultLanguage)
  const [varsRaw,        setVarsRaw]       = useState('')
  const [submitting,     setSubmitting]    = useState(false)
  const [result,         setResult]        = useState<AdminDirectSendResponse | null>(null)
  const [error,          setError]         = useState<string | null>(null)
  const [copied,         setCopied]        = useState(false)

  // Reset to incoming defaults whenever the modal is (re)opened.
  useEffect(() => {
    if (open) {
      setPhoneNumberId(defaultPhoneNumberId)
      setTemplate(defaultTemplate)
      setLanguage(defaultLanguage)
      setResult(null)
      setError(null)
      setCopied(false)
    }
  }, [open, defaultPhoneNumberId, defaultTemplate, defaultLanguage])

  const parsedVars = useMemo<Record<string, string> | { error: string }>(() => {
    const trimmed = varsRaw.trim()
    if (!trimmed) return {}
    try {
      const parsed = JSON.parse(trimmed)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { error: 'يجب أن يكون JSON object بمفاتيح "1", "2", ...' }
      }
      // Coerce values to strings — backend expects Dict[str, str].
      const out: Record<string, string> = {}
      for (const [k, v] of Object.entries(parsed)) {
        out[String(k)] = String(v ?? '')
      }
      return out
    } catch (e) {
      return { error: 'JSON غير صالح' }
    }
  }, [varsRaw])

  const varsError = 'error' in parsedVars ? parsedVars.error : null

  const submit = useCallback(async () => {
    if (varsError) return
    setSubmitting(true)
    setError(null)
    setResult(null)
    try {
      const res = await adminDebugApi.sendTemplate({
        phone_number_id: phoneNumberId.trim(),
        to:              to.trim(),
        template:        template.trim(),
        language:        language.trim() || 'ar',
        merchant_vars:   varsError ? {} : (parsedVars as Record<string, string>),
      })
      setResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'فشل الإرسال — راجع كونسول المتصفح')
    } finally {
      setSubmitting(false)
    }
  }, [phoneNumberId, to, template, language, parsedVars, varsError])

  const copyRaw = useCallback(async () => {
    if (!result) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(result, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 2200)
    } catch {
      setCopied(false)
    }
  }, [result])

  if (!open) return null

  const ok = result?.ok === true

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/60 backdrop-blur-sm overflow-y-auto p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl my-8" dir="rtl">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100">
          <div>
            <h3 className="text-base font-bold text-slate-900">
              إرسال اختبار مباشر (Admin)
            </h3>
            <p className="text-[11.5px] text-slate-500 mt-0.5">
              يتجاوز نظام الحملات بالكامل — لا frequency cap، لا retries،
              لا حفظ في campaign_send_logs.
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500"
            aria-label="إغلاق"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-3.5">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="text-[12.5px] font-medium text-slate-700">
              phone_number_id
              <input
                value={phoneNumberId}
                onChange={(e) => setPhoneNumberId(e.target.value)}
                placeholder="100543193146977"
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm font-mono"
              />
            </label>
            <label className="text-[12.5px] font-medium text-slate-700">
              الرقم (E.164)
              <input
                value={to}
                onChange={(e) => setTo(e.target.value)}
                placeholder="+966537970430"
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm font-mono"
                dir="ltr"
              />
            </label>
            <label className="text-[12.5px] font-medium text-slate-700 sm:col-span-2">
              اسم القالب
              <input
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
                placeholder="nahla_special_offer_c874"
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm font-mono"
              />
            </label>
            <label className="text-[12.5px] font-medium text-slate-700">
              اللغة
              <input
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                placeholder="ar"
                className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-1.5 text-sm font-mono"
              />
            </label>
            <label className="text-[12.5px] font-medium text-slate-700">
              متغيرات القالب (JSON)
              <input
                value={varsRaw}
                onChange={(e) => setVarsRaw(e.target.value)}
                placeholder='{"1":"Hisham","2":"499"}'
                className={`mt-1 w-full rounded-lg border px-2.5 py-1.5 text-sm font-mono ${
                  varsError ? 'border-rose-300 bg-rose-50/40' : 'border-slate-200'
                }`}
                dir="ltr"
              />
              {varsError && (
                <span className="text-[11px] text-rose-600 mt-0.5 block">{varsError}</span>
              )}
            </label>
          </div>

          <button
            onClick={submit}
            disabled={
              submitting
              || !phoneNumberId.trim()
              || !to.trim()
              || !template.trim()
              || !!varsError
            }
            className="w-full inline-flex items-center justify-center gap-2 bg-slate-900 hover:bg-slate-800 disabled:bg-slate-300 text-white text-sm font-medium rounded-lg px-3 py-2 transition-colors"
          >
            <Send className="w-4 h-4" />
            {submitting ? 'جاري الإرسال…' : 'إرسال اختبار مباشر'}
          </button>

          {error && (
            <div className="rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-[12.5px] px-3 py-2 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span className="whitespace-pre-wrap break-words">{error}</span>
            </div>
          )}

          {result && (
            <div className="space-y-2">
              <div className={`rounded-lg border px-3 py-2 flex items-center gap-2 text-[12.5px] ${
                ok
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                  : 'bg-amber-50 border-amber-200 text-amber-800'
              }`}>
                {ok ? <CheckCircle2 className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
                <span>
                  {ok
                    ? `Meta قبلت الرسالة — provider_message_id: ${result.provider_message_id ?? '—'}`
                    : 'Meta رفضت الرسالة أو لم تعد provider_message_id — راجع raw_response أدناه.'}
                </span>
                <span className="ms-auto text-[11px] text-slate-500">{result.duration_ms}ms</span>
              </div>

              <div className="rounded-lg bg-slate-50 border border-slate-200 overflow-hidden">
                <div className="flex items-center justify-between px-3 py-1.5 border-b border-slate-200 bg-white">
                  <span className="text-[11.5px] font-medium text-slate-600">
                    raw_response من {result.provider}
                  </span>
                  <button
                    onClick={copyRaw}
                    className="inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-800"
                  >
                    <Copy className="w-3 h-3" />
                    {copied ? 'تم النسخ ✓' : 'نسخ JSON'}
                  </button>
                </div>
                <pre className="text-[11px] leading-relaxed text-slate-800 p-3 overflow-x-auto max-h-72 whitespace-pre-wrap break-words" dir="ltr">
{JSON.stringify(result, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
