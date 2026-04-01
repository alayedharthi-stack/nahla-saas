import { useState, useEffect } from 'react'
import {
  CheckCircle, Zap, TrendingUp, Rocket, Loader2, AlertCircle,
  RefreshCw, Tag, MessageSquare, Star, ArrowUp,
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

// ── Plan card icons ───────────────────────────────────────────────────────────
const PLAN_ICONS: Record<string, React.ReactNode> = {
  starter: <Zap       className="w-5 h-5" />,
  growth:  <TrendingUp className="w-5 h-5" />,
  scale:   <Rocket    className="w-5 h-5" />,
}

const PLAN_COLORS: Record<string, string> = {
  starter: 'from-blue-500 to-blue-600',
  growth:  'from-brand-500 to-brand-600',
  scale:   'from-purple-500 to-purple-600',
}

// ── Components ────────────────────────────────────────────────────────────────

function PlanCard({
  plan,
  isCurrentPlan,
  onSelect,
  subscribing,
}: {
  plan:          BillingPlan
  isCurrentPlan: boolean
  onSelect:      (slug: string) => void
  subscribing:   string | null
}) {
  const isPopular = plan.slug === 'growth'
  const gradient  = PLAN_COLORS[plan.slug] ?? 'from-slate-500 to-slate-600'
  const isLoading = subscribing === plan.slug

  return (
    <div
      className={[
        'relative rounded-2xl border-2 flex flex-col transition-all duration-200',
        isCurrentPlan
          ? 'border-brand-500 shadow-lg shadow-brand-500/10'
          : 'border-slate-200 hover:border-slate-300 hover:shadow-md',
      ].join(' ')}
    >
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

      {/* Header */}
      <div className={`bg-gradient-to-br ${gradient} rounded-t-2xl p-5 text-white`}>
        <div className="flex items-center gap-2 mb-3">
          {PLAN_ICONS[plan.slug]}
          <span className="font-bold text-lg">{plan.name_ar}</span>
        </div>
        <p className="text-white/80 text-xs mb-4">{plan.description}</p>

        {/* Pricing */}
        <div className="flex items-end gap-2">
          <div>
            <span className="text-3xl font-black">{plan.launch_price_sar.toLocaleString('ar-SA')}</span>
            <span className="text-sm ms-1 font-medium">ر.س</span>
          </div>
          {plan.launch_price_sar < plan.price_sar && (
            <span className="line-through text-white/50 text-sm mb-1">
              {plan.price_sar.toLocaleString('ar-SA')}
            </span>
          )}
        </div>
        <p className="text-white/70 text-xs mt-1">شهرياً · أول شهرين بسعر الإطلاق</p>
        {plan.launch_price_sar < plan.price_sar && (
          <div className="mt-2 inline-flex items-center gap-1 bg-white/20 rounded-lg px-2 py-1 text-xs font-semibold">
            <Tag className="w-3 h-3" />
            خصم 50% لأول شهرين
          </div>
        )}
      </div>

      {/* Features */}
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
            onClick={() => onSelect(plan.slug)}
            disabled={!!subscribing}
            className={[
              'w-full py-2.5 rounded-xl text-white text-sm font-semibold transition-all flex items-center justify-center gap-2',
              `bg-gradient-to-br ${gradient}`,
              subscribing ? 'opacity-60 cursor-not-allowed' : 'hover:opacity-90 active:scale-95',
            ].join(' ')}
          >
            {isLoading && <Loader2 className="w-4 h-4 animate-spin" />}
            {isLoading ? 'جارٍ التفعيل...' : 'اختر هذه الخطة'}
          </button>
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
  const [subscribing, setSubscribing] = useState<string | null>(null)
  const [subSuccess,  setSubSuccess]  = useState<string | null>(null)
  const [subError,    setSubError]    = useState<string | null>(null)

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

  const handleSelect = async (slug: string) => {
    setSubscribing(slug)
    setSubSuccess(null)
    setSubError(null)
    try {
      const res = await billingApi.subscribe(slug)
      if (res.success) {
        setSubSuccess(`تم تفعيل الخطة بنجاح!`)
        await load()
      }
    } catch {
      setSubError('فشل تفعيل الخطة. يرجى المحاولة مجدداً.')
    } finally {
      setSubscribing(null)
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

  const pct = usagePercent(status?.conversations_used ?? 0, status?.conversations_limit ?? 0)

  return (
    <div className="space-y-6 max-w-5xl" dir="rtl">

      {/* Page header */}
      <div>
        <h1 className="text-xl font-bold text-slate-900">الاشتراك والفوترة</h1>
        <p className="text-sm text-slate-500 mt-1">إدارة خطة نهلة واستخدامك الشهري</p>
      </div>

      {/* Success / Error banners */}
      {subSuccess && (
        <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 text-emerald-800 rounded-xl px-4 py-3 text-sm">
          <CheckCircle className="w-4 h-4 shrink-0" />
          {subSuccess}
        </div>
      )}
      {subError && (
        <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-xl px-4 py-3 text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          {subError}
        </div>
      )}

      {/* Current status card */}
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
          ) : (
            <>
              <p className="text-base font-semibold text-slate-700">لا يوجد اشتراك</p>
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
            من {fmt(status?.conversations_limit ?? 0)}{status?.conversations_limit !== -1 ? ' محادثة' : ' (غير محدود)'}
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

      {/* No subscription alert */}
      {!status?.has_subscription && (
        <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">لم تختر خطة نهلة بعد</p>
            <p className="text-xs text-amber-700 mt-0.5">
              الردود الذكية والطيار الآلي والحملات محجوبة حتى تختار خطة.
            </p>
          </div>
        </div>
      )}

      {/* Upgrade prompt for active subscribers */}
      {status?.has_subscription && status.plan?.slug !== 'scale' && (
        <div className="flex items-center gap-3 bg-brand-50 border border-brand-100 rounded-xl p-4">
          <ArrowUp className="w-5 h-5 text-brand-500 shrink-0" />
          <p className="text-sm text-brand-800">
            ترقية الخطة تعني محادثات أكثر وأتمتات أقوى. الترقية مجانية حتى نهاية الشهر الحالي.
          </p>
        </div>
      )}

      {/* Plan cards */}
      <div>
        <h2 className="text-base font-bold text-slate-900 mb-4">خطط نهلة</h2>
        <div className="grid md:grid-cols-3 gap-6">
          {plans.map(plan => (
            <PlanCard
              key={plan.slug}
              plan={plan}
              isCurrentPlan={status?.plan?.slug === plan.slug}
              onSelect={handleSelect}
              subscribing={subscribing}
            />
          ))}
        </div>
      </div>

      {/* Pricing structure note */}
      <div className="card p-5 bg-slate-50">
        <h3 className="text-sm font-semibold text-slate-700 mb-3">هيكل الأسعار</h3>
        <div className="space-y-3">
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-slate-700 text-white text-xs font-bold flex items-center justify-center shrink-0">١</div>
            <div>
              <p className="text-sm font-medium text-slate-800">رسوم تكامل سلة/زد — {integFee} ر.س/شهر</p>
              <p className="text-xs text-slate-500 mt-0.5">تُدفع عبر المنصة · تشمل: ربط المتجر، مزامنة الطلبات والمنتجات، الإشعارات الآلية</p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <div className="w-6 h-6 rounded-full bg-brand-500 text-white text-xs font-bold flex items-center justify-center shrink-0">٢</div>
            <div>
              <p className="text-sm font-medium text-slate-800">خطة نهلة — من 449 ر.س/شهر</p>
              <p className="text-xs text-slate-500 mt-0.5">تُدفع مباشرة لنهلة · تشمل: الطيار الآلي، الردود الذكية، الحملات، وكيل المبيعات</p>
            </div>
          </div>
        </div>
      </div>

    </div>
  )
}
