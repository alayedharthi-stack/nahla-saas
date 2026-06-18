/**
 * SallaPricing.tsx — /app/pricing
 * -------------------------------------------------------
 * Salla Embedded pricing page — standalone layout for iframe use.
 * Post-payment merchants are redirected to /overview or /billing, not here.
 */
import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import {
  CheckCircle, Zap, TrendingUp, Rocket,
  Loader2, AlertCircle, RefreshCw,
  Tag, ShieldCheck, Sparkles, Phone, Clock, ArrowRight, Star, MessageSquare,
} from 'lucide-react'
import { billingApi, type BillingPlan, type BillingStatus } from '../api/billing'
import { pricingPageBackRoute } from '../lib/billingPostPayment'
import { displayPlanFeature } from '../lib/planFeatures'

const SUPPORT_WHATSAPP = '966555000000'

function buildSupportUrl(planNameAr: string): string {
  const storeName = (() => { try { return localStorage.getItem('nahla_store_name') || '' } catch { return '' } })()
  const text = `مرحباً، أرغب بتفعيل باقة «${planNameAr}» لمتجر ${storeName || '—'}.`
  return `https://wa.me/${SUPPORT_WHATSAPP}?text=${encodeURIComponent(text)}`
}

// ── Extra features per plan ───────────────────────────────────────────────────

interface ExtraFeature { text: string; badge?: string }

const EXTRA_FEATURES: Record<string, ExtraFeature[]> = {
  growth: [
    { text: 'مزامنة كاتالوج ميتا (Facebook / Instagram)' },
  ],
  scale: [
    { text: 'مزامنة كاتالوج ميتا (Facebook / Instagram)' },
    { text: 'مزامنة قوقل (Google Merchant)', badge: 'قريبًا' },
  ],
}

// ── Visual constants (identical to Billing.tsx) ───────────────────────────────

const PLAN_ICONS: Record<string, React.ReactNode> = {
  starter: <Zap        className="w-5 h-5" />,
  growth:  <TrendingUp className="w-5 h-5" />,
  scale:   <Rocket     className="w-5 h-5" />,
}

const PLAN_GRADIENTS: Record<string, string> = {
  starter: 'from-blue-500 to-blue-600',
  growth:  'from-brand-500 to-brand-600',
  scale:   'from-purple-500 to-purple-600',
}

// ── SallaPlanCard ─────────────────────────────────────────────────────────────

function SallaPlanCard({
  plan,
  billingStatus,
  onCheckout,
  checkingOut,
}: {
  plan:          BillingPlan
  billingStatus: BillingStatus | null
  onCheckout:    (slug: string) => void
  checkingOut:   string | null
}) {
  const isPopular      = plan.slug === 'growth'
  const gradient       = PLAN_GRADIENTS[plan.slug] ?? 'from-slate-500 to-slate-600'
  const hasDiscount    = plan.launch_price_sar < plan.price_sar
  const extras         = EXTRA_FEATURES[plan.slug] ?? []
  const isLoading      = checkingOut === plan.slug
  const isOtherLoading = checkingOut !== null && checkingOut !== plan.slug

  const isPaidActive = billingStatus?.lifecycle_status === 'paid_active'
    && billingStatus.plan?.slug === plan.slug

  const isTrialPlan = billingStatus?.lifecycle_status === 'trial_active'

  return (
    <div
      className={[
        'relative rounded-2xl border-2 flex flex-col transition-all duration-200',
        isPaidActive
          ? 'border-brand-500 shadow-lg shadow-brand-500/10'
          : isPopular
            ? 'border-brand-400 shadow-md shadow-brand-400/10'
            : 'border-slate-200 hover:border-slate-300 hover:shadow-md',
      ].join(' ')}
    >
      {/* Badge */}
      {isPopular && !isPaidActive && (
        <div className="absolute -top-3 start-1/2 -translate-x-1/2 rtl:translate-x-1/2">
          <span className="bg-brand-500 text-white text-[11px] font-bold px-3 py-1 rounded-full flex items-center gap-1">
            <Star className="w-3 h-3" /> الأكثر استخدامًا
          </span>
        </div>
      )}
      {isPaidActive && (
        <div className="absolute -top-3 start-1/2 -translate-x-1/2 rtl:translate-x-1/2">
          <span className="bg-emerald-500 text-white text-[11px] font-bold px-3 py-1 rounded-full flex items-center gap-1">
            <CheckCircle className="w-3 h-3" /> مشترك الآن
          </span>
        </div>
      )}

      {/* Header gradient */}
      <div className={`bg-gradient-to-br ${gradient} rounded-t-2xl p-5 text-white`}>
        <div className="flex items-center gap-2 mb-3">
          {PLAN_ICONS[plan.slug]}
          <span className="font-bold text-lg">{plan.name_ar}</span>
        </div>
        <p className="text-white/80 text-xs mb-4">{plan.description}</p>

        <div className="flex items-end gap-2">
          <div>
            <span className="text-3xl font-black">
              {plan.launch_price_sar.toLocaleString('ar-SA')}
            </span>
            <span className="text-sm ms-1 font-medium">ر.س</span>
          </div>
          {hasDiscount && (
            <span className="line-through text-white/50 text-sm mb-1">
              {plan.price_sar.toLocaleString('ar-SA')}
            </span>
          )}
        </div>
        <p className="text-white/70 text-xs mt-1">شهرياً</p>

        {hasDiscount && (
          <div className="mt-2 inline-flex items-center gap-1 bg-white/20 rounded-lg px-2 py-1 text-xs font-semibold">
            <Tag className="w-3 h-3" />
            خصم 50% — أول شهرين
          </div>
        )}
      </div>

      {/* Features list */}
      <div className="p-5 flex-1">
        <ul className="space-y-2.5">
          {plan.features.map((f, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-slate-700">
              <CheckCircle className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
              <span className="leading-snug">{displayPlanFeature(f)}</span>
            </li>
          ))}
          {extras.map((f, i) => (
            <li key={`extra-${i}`} className="flex items-start gap-2 text-sm text-slate-700">
              <CheckCircle className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
              <span className="flex items-center gap-1.5 flex-wrap">
                {f.text}
                {f.badge && (
                  <span className="text-[10px] font-bold bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded-full border border-amber-200 leading-none">
                    {f.badge}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>

      {/* CTA */}
      <div className="px-5 pb-5">
        {isPaidActive ? (
          <div className="w-full py-2.5 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm font-semibold text-center">
            مشترك الآن ✓
          </div>
        ) : (
          <>
            {isTrialPlan && (
              <div className="w-full py-2 rounded-xl bg-brand-50 border border-brand-200 text-brand-700 text-xs font-semibold text-center mb-2 flex items-center justify-center gap-1.5">
                <Clock className="w-3.5 h-3.5" />
                تجربة مجانية — متبقي {billingStatus?.trial_days_remaining ?? 0} يوم
              </div>
            )}
            <button
              type="button"
              onClick={() => onCheckout(plan.slug)}
              disabled={isOtherLoading || isLoading}
              className={[
                'w-full py-2.5 rounded-xl text-white text-sm font-semibold transition-all',
                'flex items-center justify-center gap-2',
                `bg-gradient-to-br ${gradient}`,
                (isOtherLoading || isLoading) ? 'opacity-60 cursor-not-allowed' : 'hover:opacity-90 active:scale-95',
              ].join(' ')}
            >
              {isLoading
                ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ التوجيه للدفع...</>
                : <>ادفع الآن — {plan.launch_price_sar.toLocaleString('ar-SA')} ر.س</>}
            </button>

            <a
              href={buildSupportUrl(plan.name_ar)}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 w-full py-2 rounded-xl bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <Phone className="w-3.5 h-3.5" />
              أو فعّل الباقة عبر واتساب الدعم
            </a>
          </>
        )}

        {!isPaidActive && (
          <p className="flex items-center justify-center gap-1 text-[10px] text-slate-400 mt-2">
            <ShieldCheck className="w-3 h-3" />
            دفع آمن عبر موى
          </p>
        )}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SallaPricing() {
  const [plans,       setPlans]       = useState<BillingPlan[]>([])
  const [status,      setStatus]      = useState<BillingStatus | null>(null)
  const [loading,     setLoading]     = useState(true)
  const [loadError,   setLoadError]   = useState<string | null>(null)
  const [checkingOut, setCheckingOut] = useState<string | null>(null)
  const [checkoutErr, setCheckoutErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      // Plans are always required — fail hard if unavailable
      const plansRes = await billingApi.getPlans()
      setPlans(plansRes.plans)
      // Status is optional — requires auth; skip gracefully if not logged in yet
      billingApi.getStatus()
        .then(setStatus)
        .catch(() => { /* not authenticated — plans still visible */ })
    } catch {
      setLoadError('تعذّر تحميل الباقات. تحقق من اتصالك وأعد المحاولة.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const handleCheckout = async (slug: string) => {
    setCheckingOut(slug)
    setCheckoutErr(null)
    try {
      const res = await billingApi.createCheckout(slug)

      if (res.checkout_url) {
        window.location.href = res.checkout_url
        return
      }

      if (res.demo_mode) {
        await load()
        setCheckingOut(null)
        return
      }

      setCheckoutErr('بوابة الدفع غير متاحة حالياً. يمكنك تفعيل الاشتراك يدوياً عبر واتساب الدعم.')
      setCheckingOut(null)
    } catch (err: any) {
      const msg = (err?.message as string | undefined) ?? 'خطأ غير معروف'
      setCheckoutErr(msg || 'تعذّر إنشاء جلسة الدفع. حاول مجدداً أو تواصل مع الدعم.')
      setCheckingOut(null)
    }
  }

  return (
    <div
      className="min-h-dvh px-4 py-8"
      dir="rtl"
      style={{ fontFamily: "'Cairo', system-ui, sans-serif", background: '#f8fafc' }}
    >
      {/* Checkout loading overlay */}
      {checkingOut && !checkoutErr && (
        <div className="fixed inset-0 bg-white/80 backdrop-blur-sm z-50 flex flex-col items-center justify-center gap-4">
          <Loader2 className="w-10 h-10 animate-spin text-brand-500" />
          <p className="text-sm font-semibold text-slate-700">جارٍ التحضير لصفحة الدفع...</p>
          <p className="text-xs text-slate-400">يرجى الانتظار بضع ثوانٍ</p>
          <button
            onClick={() => setCheckingOut(null)}
            className="mt-2 text-xs text-slate-500 hover:text-slate-700 underline"
          >
            إلغاء
          </button>
        </div>
      )}

      <div className="max-w-5xl mx-auto space-y-6">

        {/* ── Navigation ─────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between gap-3">
          <Link
            to={pricingPageBackRoute()}
            className="inline-flex items-center gap-1.5 text-sm font-semibold text-slate-600 hover:text-brand-600 transition-colors"
          >
            <ArrowRight className="w-4 h-4 rtl:rotate-180" />
            {pricingPageBackRoute() === '/app/entry' ? 'العودة للوحة التطبيق' : 'العودة للاشتراك والفوترة'}
          </Link>
          {status?.lifecycle_status === 'paid_active' && status.plan && (
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-full px-3 py-1">
              <CheckCircle className="w-3.5 h-3.5" />
              مشترك — {status.plan.name_ar}
            </span>
          )}
        </div>

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="text-center space-y-1">
          <div className="flex items-center justify-center gap-2">
            <img
              src="https://app.nahlah.ai/logo.png"
              alt="نحلة"
              className="w-8 h-8 object-contain"
              onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
            <h1 className="text-2xl font-black text-slate-900">اختر باقة نحلة</h1>
          </div>
          <p className="text-sm text-slate-500">
            موظف مبيعات ذكي يعمل 24/7 — يرد، يُكمل الطلبات، ويُرسل روابط الدفع
          </p>
        </div>

        {/* ── Checkout error ──────────────────────────────────────────────── */}
        {checkoutErr && (
          <div className="flex items-start gap-3 bg-red-50 border border-red-200 text-red-700 rounded-xl px-4 py-3 text-sm">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-red-500" />
            <div className="flex-1">
              <p className="font-semibold">تعذّر إنشاء جلسة الدفع</p>
              <p className="mt-0.5">{checkoutErr}</p>
              <button
                onClick={() => setCheckoutErr(null)}
                className="mt-2 text-xs text-slate-500 hover:text-slate-700 underline"
              >
                إغلاق
              </button>
            </div>
          </div>
        )}

        {/* ── Loading ─────────────────────────────────────────────────────── */}
        {loading && (
          <div className="flex justify-center py-16">
            <Loader2 className="w-7 h-7 animate-spin text-brand-500" />
          </div>
        )}

        {/* ── Load error ──────────────────────────────────────────────────── */}
        {loadError && (
          <div className="card p-6 flex flex-col items-center gap-3 text-center max-w-sm mx-auto">
            <AlertCircle className="w-7 h-7 text-red-400" />
            <p className="text-sm text-slate-700">{loadError}</p>
            <button onClick={load} className="btn-secondary text-sm flex items-center gap-2">
              <RefreshCw className="w-4 h-4" /> إعادة المحاولة
            </button>
          </div>
        )}

        {/* ── Plans grid ──────────────────────────────────────────────────── */}
        {!loading && !loadError && (
          <>
            <div className="flex items-end justify-between flex-wrap gap-2">
              <div>
                <h2 className="text-base font-bold text-slate-900">اختر خطتك</h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  جميع الخطط تشمل: واتساب الأعمال على الجوال · الذكاء الاصطناعي · الحملات · الطيار الآلي 24/7
                </p>
              </div>
              <span className="text-xs text-brand-600 font-medium flex items-center gap-1">
                <Sparkles className="w-3 h-3" />
                خصم 50% — أول شهرين
              </span>
            </div>

            <div className="grid md:grid-cols-3 gap-6">
              {plans.map(plan => (
                <SallaPlanCard
                  key={plan.slug}
                  plan={plan}
                  billingStatus={status}
                  onCheckout={handleCheckout}
                  checkingOut={checkingOut}
                />
              ))}
            </div>

            <div className="rounded-xl bg-slate-50 border border-slate-200 p-4 space-y-2 text-center">
              <div className="inline-flex items-center gap-2 text-xs font-semibold text-slate-700">
                <MessageSquare className="w-4 h-4 text-brand-500" />
                واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا
              </div>
              <p className="text-[11px] text-slate-500">
                لا حاجة لحذف واتساب الأعمال من جوالك — نحلة تعمل في الخلفية بدون أي تعارض
              </p>
            </div>

            {/* Security note */}
            <div className="flex items-center gap-3 bg-slate-50 rounded-xl p-4 border border-slate-200">
              <ShieldCheck className="w-5 h-5 text-slate-400 shrink-0" />
              <div>
                <p className="text-xs font-semibold text-slate-700">دفع آمن ومشفّر</p>
                <p className="text-xs text-slate-500 mt-0.5">
                  تتم معالجة جميع المدفوعات عبر بوابة موى (Moyasar) المرخّصة في المملكة العربية السعودية.
                  بيانات بطاقتك لا تُخزَّن على خوادم نحلة.
                </p>
              </div>
            </div>

            <p className="text-center text-xs text-slate-400 pb-4">
              يتم تفعيل الاشتراك وإدارة الدفع من خلال منصة نحلة
            </p>
          </>
        )}

      </div>
    </div>
  )
}
