import { useState, useEffect } from 'react'
import {
  CheckCircle, Zap, TrendingUp, Rocket, Loader2, AlertCircle,
  RefreshCw, Tag, MessageSquare, Star, ArrowUp, ExternalLink, ShieldCheck,
  Clock, Sparkles, Bot,
} from 'lucide-react'
import { billingApi, type BillingPlan, type BillingStatus } from '../api/billing'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(n: number) {
  return n === -1 ? '∞' : n.toLocaleString('ar-SA')
}

function usagePercent(used: number, limit: number) {
  if (limit === -1 || limit === 0) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

// ── Constants ─────────────────────────────────────────────────────────────────

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

// ── PlanCard ──────────────────────────────────────────────────────────────────

function PlanCard({
  plan,
  isCurrentPlan,
  onCheckout,
  checkingOut,
}: {
  plan:         BillingPlan
  isCurrentPlan: boolean
  onCheckout:   (slug: string) => void
  checkingOut:  string | null
}) {
  const isPopular  = plan.slug === 'growth'
  const gradient   = PLAN_GRADIENTS[plan.slug] ?? 'from-slate-500 to-slate-600'
  const isLoading  = checkingOut === plan.slug
  const hasDiscount = plan.launch_price_sar < plan.price_sar

  return (
    <div
      className={[
        'relative rounded-2xl border-2 flex flex-col transition-all duration-200',
        isCurrentPlan
          ? 'border-brand-500 shadow-lg shadow-brand-500/10'
          : 'border-slate-200 hover:border-slate-300 hover:shadow-md',
      ].join(' ')}
    >
      {/* Badge */}
      {isPopular && !isCurrentPlan && (
        <div className="absolute -top-3 start-1/2 -translate-x-1/2 rtl:translate-x-1/2">
          <span className="bg-brand-500 text-white text-[11px] font-bold px-3 py-1 rounded-full flex items-center gap-1">
            <Star className="w-3 h-3" /> الأكثر شيوعاً
          </span>
        </div>
      )}
      {isCurrentPlan && (
        <div className="absolute -top-3 start-1/2 -translate-x-1/2 rtl:translate-x-1/2">
          <span className="bg-emerald-500 text-white text-[11px] font-bold px-3 py-1 rounded-full flex items-center gap-1">
            <CheckCircle className="w-3 h-3" /> خطتك الحالية
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
              {f}
            </li>
          ))}
        </ul>
      </div>

      {/* CTA */}
      <div className="px-5 pb-5">
        {isCurrentPlan ? (
          <div className="w-full py-2.5 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm font-semibold text-center">
            مشترك الآن ✓
          </div>
        ) : (
          <button
            onClick={() => onCheckout(plan.slug)}
            disabled={!!checkingOut}
            className={[
              'w-full py-2.5 rounded-xl text-white text-sm font-semibold transition-all',
              'flex items-center justify-center gap-2',
              `bg-gradient-to-br ${gradient}`,
              checkingOut ? 'opacity-60 cursor-not-allowed' : 'hover:opacity-90 active:scale-95',
            ].join(' ')}
          >
            {isLoading
              ? <><Loader2 className="w-4 h-4 animate-spin" /> جارٍ التوجيه للدفع...</>
              : <><ExternalLink className="w-4 h-4" /> ادفع الآن — {plan.launch_price_sar.toLocaleString('ar-SA')} ر.س</>}
          </button>
        )}

        {/* Secure payment note */}
        {!isCurrentPlan && (
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

export default function Billing() {
  const [status,      setStatus]      = useState<BillingStatus | null>(null)
  const [plans,       setPlans]       = useState<BillingPlan[]>([])
  const [integFee,    setIntegFee]    = useState(59)
  const [loading,     setLoading]     = useState(true)
  const [loadError,   setLoadError]   = useState<string | null>(null)
  const [checkingOut, setCheckingOut] = useState<string | null>(null)
  const [checkoutMsg, setCheckoutMsg] = useState<string | null>(null)
  const [checkoutErr, setCheckoutErr] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [statusRes, plansRes] = await Promise.all([
        billingApi.getStatus(),
        billingApi.getPlans(),
      ])
      setStatus(statusRes)
      setPlans(plansRes.plans)
      setIntegFee(plansRes.integration_fee_sar)
    } catch {
      setLoadError('تعذّر تحميل بيانات الاشتراك')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleCheckout = async (slug: string) => {
    setCheckingOut(slug)
    setCheckoutMsg(null)
    setCheckoutErr(null)

    try {
      const res = await billingApi.createCheckout(slug)

      if (res.checkout_url) {
        // Real Moyasar payment — redirect to hosted payment page
        window.location.href = res.checkout_url
        // (page navigates away; no further state updates needed)
      } else if (res.demo_mode) {
        // No gateway configured — subscription activated immediately
        setCheckoutMsg('تم تفعيل الخطة بنجاح! (وضع تجريبي — بدون دفع)')
        await load()
        setCheckingOut(null)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'خطأ غير معروف'
      setCheckoutErr(`فشل إنشاء جلسة الدفع: ${msg}`)
      setCheckingOut(null)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
      </div>
    )
  }

  if (loadError) {
    return (
      <div className="card p-8 max-w-md mx-auto flex flex-col items-center gap-3 text-center">
        <AlertCircle className="w-8 h-8 text-red-400" />
        <p className="text-sm text-slate-700">{loadError}</p>
        <button onClick={load} className="btn-secondary text-sm flex items-center gap-2">
          <RefreshCw className="w-4 h-4" /> إعادة المحاولة
        </button>
      </div>
    )
  }

  const pct = usagePercent(
    status?.conversations_used ?? 0,
    status?.conversations_limit ?? 0,
  )

  return (
    <div className="space-y-6 max-w-5xl" dir="rtl">

      {/* Page header */}
      <div>
        <h1 className="text-xl font-bold text-slate-900">الاشتراك والفوترة</h1>
        <p className="text-sm text-slate-500 mt-1">إدارة خطة نحلة واستخدامك الشهري</p>
      </div>

      {/* Hero value proposition */}
      <div className="rounded-2xl bg-gradient-to-l from-brand-600 to-brand-400 p-5 text-white">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-white/20 flex items-center justify-center shrink-0">
            <Bot className="w-5 h-5" />
          </div>
          <div className="flex-1">
            <h2 className="font-bold text-base leading-snug">
              نحلة — موظف مبيعات يعمل 24/7
            </h2>
            <p className="text-white/80 text-xs mt-1 leading-relaxed">
              يرد على العملاء، يُكمل الطلبات، ويُرسل روابط الدفع — بشكل تلقائي، دون توقف.
              لا رواتب، لا إجازات، لا تأخير.
            </p>
          </div>
          <div className="hidden sm:flex flex-col items-end shrink-0">
            <div className="flex items-center gap-1 bg-white/20 rounded-lg px-2.5 py-1 text-xs font-semibold">
              <Sparkles className="w-3 h-3" />
              14 يوم مجاناً
            </div>
            <p className="text-white/60 text-[11px] mt-1">ثم من 449 ر.س/شهر</p>
          </div>
        </div>
      </div>

      {/* Trial status card — shown only during trial */}
      {status?.is_trial && (
        <div className="rounded-xl border-2 border-brand-300 bg-brand-50 p-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Clock className="w-5 h-5 text-brand-500 shrink-0" />
            <div>
              <p className="text-sm font-bold text-brand-900">
                التجربة المجانية — متبقي {status.trial_days_remaining} {status.trial_days_remaining === 1 ? 'يوم' : 'أيام'}
              </p>
              <p className="text-xs text-brand-700 mt-0.5">
                استمتع بجميع الميزات مجاناً · لا حاجة لبطاقة ائتمان الآن
              </p>
            </div>
          </div>
          <div className="shrink-0">
            <div className="flex items-center gap-1 text-xs text-brand-600 font-medium">
              {Array.from({ length: 14 }).map((_, i) => (
                <div
                  key={i}
                  className={`h-2 w-2 rounded-full ${
                    i < (14 - status.trial_days_remaining) ? 'bg-brand-500' : 'bg-brand-200'
                  }`}
                />
              ))}
            </div>
            <p className="text-[11px] text-brand-500 mt-1 text-center">
              {14 - status.trial_days_remaining} من 14 يوم
            </p>
          </div>
        </div>
      )}

      {/* Trial expired warning */}
      {status?.trial_expired && (
        <div className="flex items-start gap-3 bg-red-50 border-2 border-red-200 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold text-red-800">انتهت فترة التجربة المجانية</p>
            <p className="text-xs text-red-700 mt-1">
              ميزات الطيار الآلي والردود التلقائية موقوفة مؤقتاً. اختر خطة لإعادة تشغيل موظف المبيعات الذكي.
            </p>
          </div>
        </div>
      )}

      {/* Banners */}
      {checkoutMsg && (
        <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-xl px-4 py-3 text-sm">
          <CheckCircle className="w-4 h-4 shrink-0" />
          {checkoutMsg}
        </div>
      )}
      {checkoutErr && (
        <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-xl px-4 py-3 text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          {checkoutErr}
        </div>
      )}

      {/* Redirecting overlay */}
      {checkingOut && (
        <div className="fixed inset-0 bg-white/80 backdrop-blur-sm z-50 flex flex-col items-center justify-center gap-4">
          <Loader2 className="w-10 h-10 animate-spin text-brand-500" />
          <p className="text-sm font-semibold text-slate-700">جارٍ التوجيه إلى صفحة الدفع...</p>
          <p className="text-xs text-slate-400">يرجى الانتظار</p>
        </div>
      )}

      {/* Current status cards */}
      <div className="grid sm:grid-cols-3 gap-4">

        {/* Active plan */}
        <div className="card p-5">
          <p className="text-xs text-slate-500 mb-1">الخطة الحالية</p>
          {status?.has_subscription ? (
            <>
              <p className="text-lg font-bold text-slate-900">{status.plan?.name_ar}</p>
              <div className="flex items-baseline gap-1 mt-1">
                <span className="text-2xl font-black text-brand-600">
                  {status.current_price_sar.toLocaleString('ar-SA')}
                </span>
                <span className="text-xs text-slate-500">ر.س/شهر</span>
              </div>
              {status.launch_discount_active && (
                <span className="inline-flex items-center gap-1 mt-2 text-[11px] bg-amber-50 border border-amber-200 text-amber-700 px-2 py-0.5 rounded-full">
                  <Tag className="w-3 h-3" /> خصم الإطلاق فعّال
                </span>
              )}
            </>
          ) : status?.is_trial ? (
            <>
              <p className="text-base font-semibold text-brand-600">تجربة مجانية</p>
              <div className="flex items-baseline gap-1 mt-1">
                <span className="text-2xl font-black text-brand-600">
                  {status.trial_days_remaining}
                </span>
                <span className="text-xs text-slate-500">يوم متبقي</span>
              </div>
              <span className="inline-flex items-center gap-1 mt-2 text-[11px] bg-brand-50 border border-brand-200 text-brand-700 px-2 py-0.5 rounded-full">
                <Clock className="w-3 h-3" /> مجاني لمدة 14 يوم
              </span>
            </>
          ) : (
            <>
              <p className="text-base font-semibold text-red-600">التجربة منتهية</p>
              <p className="text-xs text-slate-400 mt-1">اختر خطة لتفعيل الطيار الآلي</p>
            </>
          )}
        </div>

        {/* Conversations usage */}
        <div className="card p-5">
          <p className="text-xs text-slate-500 mb-1 flex items-center gap-1">
            <MessageSquare className="w-3.5 h-3.5" /> المحادثات
          </p>
          <p className="text-2xl font-black text-slate-900">
            {(status?.conversations_used ?? 0).toLocaleString('ar-SA')}
          </p>
          <p className="text-xs text-slate-400 mt-0.5">
            من {fmt(status?.conversations_limit ?? 0)}
            {status?.conversations_limit !== -1 ? ' محادثة' : ' (غير محدود)'}
          </p>
          {status?.has_subscription && status.conversations_limit !== -1 && (
            <div className="mt-3">
              <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${pct > 85 ? 'bg-red-500' : 'bg-brand-500'}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <p className="text-[10px] text-slate-400 mt-1">{pct}% مستخدم</p>
            </div>
          )}
        </div>

        {/* Integration fee */}
        <div className="card p-5">
          <p className="text-xs text-slate-500 mb-1">رسوم التكامل (سلة/زد)</p>
          <div className="flex items-baseline gap-1 mt-1">
            <span className="text-2xl font-black text-slate-700">
              {integFee.toLocaleString('ar-SA')}
            </span>
            <span className="text-xs text-slate-500">ر.س/شهر</span>
          </div>
          <p className="text-[11px] text-slate-400 mt-2 leading-relaxed">
            تُدفع عبر سلة أو زد. تشمل ربط المتجر ومزامنة الطلبات.
          </p>
        </div>
      </div>

      {/* No subscription alert — only shown after trial ends */}
      {!status?.has_subscription && !status?.is_trial && (
        <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">لم تختر خطة نحلة بعد</p>
            <p className="text-xs text-amber-700 mt-0.5">
              الردود الذكية والطيار الآلي والحملات محجوبة حتى تختار خطة.
              اشترك الآن لإعادة تشغيل موظف المبيعات الذكي.
            </p>
          </div>
        </div>
      )}

      {/* Upgrade nudge */}
      {status?.has_subscription && status.plan?.slug !== 'scale' && (
        <div className="flex items-center gap-3 bg-brand-50 border border-brand-100 rounded-xl p-4">
          <ArrowUp className="w-5 h-5 text-brand-500 shrink-0" />
          <p className="text-sm text-brand-800">
            ترقية الخطة تعني محادثات أكثر وأتمتات أقوى.
          </p>
        </div>
      )}

      {/* Plans grid */}
      <div>
        <div className="flex items-end justify-between mb-1">
          <h2 className="text-base font-bold text-slate-900">اختر خطتك</h2>
          <span className="text-xs text-brand-600 font-medium flex items-center gap-1">
            <Sparkles className="w-3 h-3" />
            خصم 50% — أول شهرين
          </span>
        </div>
        <p className="text-xs text-slate-400 mb-4">
          جميع الخطط تشمل الطيار الآلي · الردود الذكية · وكيل المبيعات 24/7
        </p>
        <div className="grid md:grid-cols-3 gap-6">
          {plans.map(plan => (
            <PlanCard
              key={plan.slug}
              plan={plan}
              isCurrentPlan={status?.plan?.slug === plan.slug}
              onCheckout={handleCheckout}
              checkingOut={checkingOut}
            />
          ))}
        </div>
      </div>

      {/* Payment security note */}
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

      {/* Pricing structure */}
      <div className="card p-5 bg-slate-50">
        <h3 className="text-sm font-semibold text-slate-700 mb-3">هيكل الأسعار</h3>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-slate-700 text-white text-xs font-bold flex items-center justify-center shrink-0">١</div>
            <div>
              <p className="text-sm font-medium text-slate-800">رسوم تكامل سلة/زد — {integFee} ر.س/شهر</p>
              <p className="text-xs text-slate-500 mt-0.5">تُدفع عبر المنصة · ربط المتجر، مزامنة الطلبات والمنتجات</p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-brand-500 text-white text-xs font-bold flex items-center justify-center shrink-0">٢</div>
            <div>
              <p className="text-sm font-medium text-slate-800">خطة نحلة — من 449 ر.س/شهر</p>
              <p className="text-xs text-slate-500 mt-0.5">تُدفع عبر موى · الطيار الآلي، الردود الذكية، الحملات، وكيل المبيعات</p>
            </div>
          </div>
        </div>
      </div>

    </div>
  )
}
