// ── Manual coupon campaign — interim flow before Salla coupon API approval ──
//
// The merchant creates the coupon inside Salla themselves, then types the
// code + the storefront/product URL here. Nahla just renders an APPROVED
// WhatsApp template and ships it. No coupon is auto-created or validated
// against Salla, no automation flow consumes this template — it is strictly
// a "fill the form, preview, send a test" surface.
//
// Send pipeline:
//   1. Look up the seeded `manual_coupon_campaign_<lang>` template by name
//      via templatesApi.list().
//   2. POST /campaigns/test-send with the merchant's variables (numeric
//      placeholder map) plus `store_or_product_url` so the URL-button
//      suffix extractor can substitute the merchant URL into the {{1}}
//      slot of the URL button (test_send_urls.extract_button_suffix).
//
// All form validation runs CLIENT-side first (required fields, https://
// prefix on the URL, non-empty coupon code) so the merchant never burns
// a real WhatsApp send on a half-filled form.

import { useEffect, useMemo, useState } from 'react'
import {
  Send,
  Eye,
  Copy as CopyIcon,
  CheckCircle,
  AlertTriangle,
  Loader2,
  Tag,
  Info,
  Link2,
  Calendar,
  Store,
  MessageSquare,
} from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { templatesApi, type WhatsAppTemplateRecord } from '../api/templates'
import { campaignsApi } from '../api/campaigns'

// Canonical names of the seeded templates from
// backend/core/template_library.py::DEFAULT_AUTOMATION_TEMPLATES.
const TEMPLATE_NAMES = {
  ar: 'manual_coupon_campaign_ar',
  en: 'manual_coupon_campaign_en',
} as const

interface FormState {
  customer_name:         string
  merchant_name:         string
  offer_description:     string
  coupon_code:           string
  discount_text:         string
  expiry_text:           string
  store_or_product_url:  string
  test_phone:            string
}

const INITIAL_FORM: FormState = {
  customer_name:         '',
  merchant_name:         '',
  offer_description:     '',
  coupon_code:           '',
  discount_text:         '',
  expiry_text:           '',
  store_or_product_url:  '',
  test_phone:            '',
}

interface ValidationResult {
  ok: boolean
  errors: Partial<Record<keyof FormState, string>>
}

function validate(form: FormState, includeTestPhone: boolean): ValidationResult {
  const errors: Partial<Record<keyof FormState, string>> = {}

  if (!form.coupon_code.trim()) errors.coupon_code = 'كود الكوبون مطلوب.'
  if (!form.discount_text.trim()) errors.discount_text = 'نص/نسبة الخصم مطلوبة.'
  if (!form.offer_description.trim()) errors.offer_description = 'وصف العرض مطلوب.'
  if (!form.merchant_name.trim()) errors.merchant_name = 'اسم المتجر مطلوب.'

  const url = form.store_or_product_url.trim()
  if (!url) {
    errors.store_or_product_url = 'رابط المتجر أو المنتج مطلوب.'
  } else if (!/^https:\/\//i.test(url)) {
    errors.store_or_product_url = 'يجب أن يبدأ الرابط بـ https://'
  } else {
    try { new URL(url) } catch { errors.store_or_product_url = 'الرابط غير صالح.' }
  }

  if (includeTestPhone) {
    const phone = form.test_phone.trim()
    if (!phone) {
      errors.test_phone = 'رقم الجوال للاختبار مطلوب.'
    } else if (!/^\+?\d{8,15}$/.test(phone.replace(/\s|-/g, ''))) {
      errors.test_phone = 'رقم غير صالح. أدخل بالصيغة الدولية (مثال: +9665XXXXXXXX).'
    }
  }

  return { ok: Object.keys(errors).length === 0, errors }
}

function renderPreview(form: FormState): string {
  const name = form.customer_name.trim() || 'العميل'
  const merchant = form.merchant_name.trim() || 'متجرك'
  return [
    `مرحباً ${name} 👋`,
    '',
    `عرض خاص من ${merchant} 🍯`,
    '',
    form.offer_description.trim() || 'وصف العرض هنا',
    '',
    'استخدم كود الخصم:',
    form.coupon_code.trim() || 'COUPON',
    '',
    'الخصم:',
    form.discount_text.trim() || 'نسبة الخصم',
    '',
    form.expiry_text.trim() || 'العرض ساري لفترة محدودة',
    '',
    'اضغط الزر بالأسفل للطلب من المتجر.',
  ].join('\n')
}

function FieldRow({
  label,
  hint,
  error,
  required,
  children,
}: {
  label: string
  hint?: string
  error?: string
  required?: boolean
  children: React.ReactNode
}) {
  return (
    <div>
      <label className="block text-xs font-semibold text-slate-700 mb-1">
        {label}
        {required && <span className="text-red-500 ms-1">*</span>}
      </label>
      {children}
      {hint && !error && <p className="text-[11px] text-slate-400 mt-1">{hint}</p>}
      {error && (
        <p className="text-[11px] text-red-500 mt-1 flex items-center gap-1">
          <AlertTriangle className="w-3 h-3" /> {error}
        </p>
      )}
    </div>
  )
}

export default function ManualCouponCampaign() {
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [errors, setErrors] = useState<Partial<Record<keyof FormState, string>>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [templateAr, setTemplateAr] = useState<WhatsAppTemplateRecord | null>(null)
  const [templateEn, setTemplateEn] = useState<WhatsAppTemplateRecord | null>(null)
  const [showPreview, setShowPreview] = useState(false)
  const [sending, setSending] = useState(false)
  const [sendOk, setSendOk] = useState<string | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [copyHit, setCopyHit] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    templatesApi
      .list()
      .then(res => {
        if (cancelled) return
        const ar = res.templates.find(t => t.name === TEMPLATE_NAMES.ar) ?? null
        const en = res.templates.find(t => t.name === TEMPLATE_NAMES.en) ?? null
        setTemplateAr(ar)
        setTemplateEn(en)
        if (!ar && !en) {
          setLoadError(
            'لم يتم العثور على قالب «حملة كوبون يدوي» في مكتبة قوالبك. اضغط «تحديث القوالب» في صفحة قوالب واتساب لإعادة المزامنة.',
          )
        }
      })
      .catch(() => {
        if (cancelled) return
        setLoadError('تعذّر تحميل قوالب واتساب. تحقّق من اتصال نحلة بمزود واتساب ثم حاول مجدداً.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  const activeTemplate = useMemo<WhatsAppTemplateRecord | null>(
    () => templateAr ?? templateEn,
    [templateAr, templateEn],
  )

  const previewText = useMemo(() => renderPreview(form), [form])

  const update = (k: keyof FormState, v: string) => {
    setForm(prev => ({ ...prev, [k]: v }))
    setErrors(prev => ({ ...prev, [k]: undefined }))
    setSendOk(null)
    setSendError(null)
  }

  const handlePreview = () => {
    const v = validate(form, false)
    setErrors(v.errors)
    if (v.ok) setShowPreview(true)
  }

  const handleSendTest = async () => {
    setSendOk(null)
    setSendError(null)
    const v = validate(form, true)
    setErrors(v.errors)
    if (!v.ok) {
      setSendError('صحّح الحقول الناقصة قبل إرسال الاختبار.')
      return
    }
    if (!activeTemplate) {
      setSendError('لم يُحمَّل القالب بعد. حاول مرة أخرى.')
      return
    }
    if (activeTemplate.status !== 'APPROVED') {
      setSendError(
        `قالب «حملة كوبون يدوي» حالته «${activeTemplate.status}» وليست APPROVED. ` +
        `لا يمكن لـ Meta تسليم رسالة قالب غير معتمدة. أرسله للاعتماد من صفحة قوالب واتساب أولاً.`,
      )
      return
    }

    // Map our named fields onto the numeric placeholders Meta expects:
    // {{1}} customer_name, {{2}} merchant_name, {{3}} offer_description,
    // {{4}} coupon_code, {{5}} discount_text, {{6}} expiry_text.
    // The URL button gets resolved server-side from `store_or_product_url`
    // (or one of its aliases) via test_send_urls.resolve_test_button_url.
    const variables: Record<string, string> = {
      '1': form.customer_name.trim() || 'عميلنا',
      '2': form.merchant_name.trim(),
      '3': form.offer_description.trim(),
      '4': form.coupon_code.trim(),
      '5': form.discount_text.trim(),
      '6': form.expiry_text.trim() || 'العرض ساري لفترة محدودة',
      // Hand the URL to the URL-button resolver under every alias it walks
      // — so a future change to the resolver's preferred key keeps working.
      store_or_product_url: form.store_or_product_url.trim(),
      store_url:            form.store_or_product_url.trim(),
      product_url:          form.store_or_product_url.trim(),
    }

    setSending(true)
    try {
      const res = await campaignsApi.testSend(
        form.test_phone.trim(),
        String(activeTemplate.id),
        activeTemplate.name,
        activeTemplate.language || 'ar',
        variables,
      )
      if (res.success) {
        setSendOk(
          res.simulated
            ? 'تمت المحاكاة — لا يوجد اتصال واتساب نشط، لكن النموذج صحيح وسيُرسَل فعلياً عند تفعيل الاتصال.'
            : `تم إرسال رسالة الاختبار إلى ${form.test_phone.trim()} ✅`,
        )
      } else {
        setSendError(res.message || 'فشل إرسال الاختبار.')
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'فشل إرسال الاختبار.'
      setSendError(msg)
    } finally {
      setSending(false)
    }
  }

  const handleCopyCoupon = () => {
    const code = form.coupon_code.trim()
    if (!code) return
    navigator.clipboard.writeText(code).then(
      () => {
        setCopyHit(true)
        setTimeout(() => setCopyHit(false), 1500)
      },
      () => { /* ignore */ },
    )
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="حملة كوبون يدوي"
        subtitle="أنشئ الكوبون داخل سلة بنفسك ثم ضع الكود + الرابط هنا، ونحلة ترسلها للعميل عبر واتساب بزر للطلب."
      />

      {/* ── Disclaimer card ──────────────────────────────────────────────── */}
      <div className="card p-4 bg-amber-50 border-amber-200 border">
        <div className="flex items-start gap-3">
          <Info className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
          <div className="text-xs text-amber-800 leading-relaxed space-y-1">
            <p className="font-semibold">حل مؤقت قبل اعتماد سلة لـ API الكوبونات:</p>
            <ul className="list-disc ms-5 space-y-0.5 marker:text-amber-500">
              <li>لا يتم إنشاء الكوبون عبر API.</li>
              <li>لا يتم التحقق من الكوبون من سلة.</li>
              <li>لا ربط تلقائي بالمنتجات.</li>
              <li>التاجر مسؤول عن إنشاء الكوبون داخل سلة يدوياً.</li>
              <li>نحلة فقط تحفظ النص وترسل الكود والرابط الذي أدخلته.</li>
            </ul>
          </div>
        </div>
      </div>

      {/* ── Loading / error states ──────────────────────────────────────── */}
      {loading && (
        <div className="card p-6 flex items-center justify-center text-sm text-slate-500 gap-2">
          <Loader2 className="w-4 h-4 animate-spin text-brand-500" /> جاري تحميل قالب الحملة…
        </div>
      )}

      {!loading && loadError && (
        <div className="card p-4 bg-red-50 border-red-200 border">
          <div className="flex items-start gap-3 text-xs text-red-700">
            <AlertTriangle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
            <p>{loadError}</p>
          </div>
        </div>
      )}

      {!loading && activeTemplate && activeTemplate.status !== 'APPROVED' && (
        <div className="card p-4 bg-amber-50 border-amber-200 border">
          <div className="flex items-start gap-3 text-xs text-amber-800">
            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <p>
              قالب «حملة كوبون يدوي» موجود في مكتبتك لكنه ليس APPROVED بعد
              (الحالة الحالية: <span className="font-semibold">{activeTemplate.status}</span>).
              {' '}اذهب لصفحة قوالب واتساب وأرسله للاعتماد قبل إرسال أول حملة.
            </p>
          </div>
        </div>
      )}

      {/* ── Form ────────────────────────────────────────────────────────── */}
      <div className="card p-5 space-y-5">
        <div className="grid sm:grid-cols-2 gap-4">
          <FieldRow
            label="اسم المتجر"
            hint="يظهر في أول الرسالة كمصدر العرض."
            error={errors.merchant_name}
            required
          >
            <div className="relative">
              <Store className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
              <input
                className="input ps-3 pe-9"
                value={form.merchant_name}
                onChange={e => update('merchant_name', e.target.value)}
                placeholder="مثال: متجر نحلة"
                maxLength={60}
              />
            </div>
          </FieldRow>

          <FieldRow
            label="اسم العميل (اختياري)"
            hint="اتركه فارغاً ليظهر «عميلنا»."
          >
            <input
              className="input"
              value={form.customer_name}
              onChange={e => update('customer_name', e.target.value)}
              placeholder="مثال: أحمد"
              maxLength={60}
            />
          </FieldRow>

          <FieldRow
            label="كود الكوبون"
            hint="نفس الكود الذي أنشأته داخل سلة."
            error={errors.coupon_code}
            required
          >
            <div className="relative">
              <Tag className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
              <input
                className="input ps-3 pe-9 font-mono uppercase tracking-wider"
                value={form.coupon_code}
                onChange={e => update('coupon_code', e.target.value.toUpperCase())}
                placeholder="NAHLA10"
                maxLength={32}
              />
            </div>
          </FieldRow>

          <FieldRow
            label="نسبة/وصف الخصم"
            hint="نص حرّ يظهر تحت «الخصم:» في الرسالة."
            error={errors.discount_text}
            required
          >
            <input
              className="input"
              value={form.discount_text}
              onChange={e => update('discount_text', e.target.value)}
              placeholder="خصم 10%"
              maxLength={80}
            />
          </FieldRow>

          <div className="sm:col-span-2">
            <FieldRow
              label="وصف العرض"
              hint="جملة تسويقية قصيرة. تجنّب الأرقام السرية أو الأسعار التي قد تتغيّر."
              error={errors.offer_description}
              required
            >
              <textarea
                className="input min-h-[80px] resize-y"
                value={form.offer_description}
                onChange={e => update('offer_description', e.target.value)}
                placeholder="مثال: عرض اليوم على المنتجات المختارة فقط"
                maxLength={300}
              />
            </FieldRow>
          </div>

          <div className="sm:col-span-2">
            <FieldRow
              label="رابط المتجر أو المنتج"
              hint="رابط كامل يبدأ بـ https:// — هذا ما يفتحه العميل عند الضغط على زر «اطلب الآن»."
              error={errors.store_or_product_url}
              required
            >
              <div className="relative">
                <Link2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                <input
                  className="input ps-3 pe-9 ltr"
                  dir="ltr"
                  type="url"
                  value={form.store_or_product_url}
                  onChange={e => update('store_or_product_url', e.target.value)}
                  placeholder="https://store.example.com/products/special"
                />
              </div>
            </FieldRow>
          </div>

          <div className="sm:col-span-2">
            <FieldRow
              label="نص انتهاء العرض"
              hint="يظهر فوق زر الطلب — نص حرّ، لن نتحقق من تاريخ فعلي."
            >
              <div className="relative">
                <Calendar className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                <input
                  className="input ps-3 pe-9"
                  value={form.expiry_text}
                  onChange={e => update('expiry_text', e.target.value)}
                  placeholder="العرض ساري حتى نهاية الأسبوع"
                  maxLength={120}
                />
              </div>
            </FieldRow>
          </div>
        </div>

        <div className="border-t border-slate-100 pt-4 space-y-4">
          <FieldRow
            label="رقم الجوال للاختبار"
            hint="نرسل إليه رسالة اختبارية فقط للتأكد من الشكل قبل أي إطلاق."
            error={errors.test_phone}
          >
            <input
              className="input ltr"
              dir="ltr"
              value={form.test_phone}
              onChange={e => update('test_phone', e.target.value)}
              placeholder="+9665XXXXXXXX"
              maxLength={20}
            />
          </FieldRow>

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={handlePreview}
              className="btn-secondary text-sm flex items-center gap-2"
            >
              <Eye className="w-4 h-4" />
              معاينة الرسالة
            </button>
            <button
              type="button"
              onClick={handleSendTest}
              disabled={sending || loading || !activeTemplate}
              className="btn-primary text-sm flex items-center gap-2 disabled:opacity-60"
            >
              {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {sending ? 'جاري الإرسال…' : 'إرسال حملة اختبارية'}
            </button>
            {form.coupon_code.trim() && (
              <button
                type="button"
                onClick={handleCopyCoupon}
                className="btn-secondary text-sm flex items-center gap-2"
              >
                <CopyIcon className="w-4 h-4" />
                {copyHit ? 'تم النسخ' : 'نسخ الكود'}
              </button>
            )}
          </div>

          {sendOk && (
            <div className="flex items-start gap-2 text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg p-3">
              <CheckCircle className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
              <p>{sendOk}</p>
            </div>
          )}
          {sendError && (
            <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg p-3">
              <AlertTriangle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
              <p>{sendError}</p>
            </div>
          )}
        </div>
      </div>

      {/* ── Preview card ────────────────────────────────────────────────── */}
      {showPreview && (
        <div className="card p-5">
          <div className="flex items-center gap-2 mb-3">
            <MessageSquare className="w-4 h-4 text-brand-500" />
            <h3 className="text-sm font-semibold text-slate-900">معاينة الرسالة</h3>
            <span className="text-[10px] text-slate-400 ms-1">
              — هكذا تصل للعميل (دون احتساب الزر)
            </span>
          </div>
          <pre className="whitespace-pre-wrap text-sm leading-relaxed bg-slate-50 rounded-lg p-4 border border-slate-200 font-sans">
            {previewText}
          </pre>
          <div className="mt-3 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">
              زر URL: «اطلب الآن» → {form.store_or_product_url.trim() || 'https://…'}
            </span>
            <span className="text-[10px] text-slate-400">🐝 نحلة — مساعد متجرك</span>
          </div>
        </div>
      )}
    </div>
  )
}
