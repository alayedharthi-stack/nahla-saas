import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Sparkles, CheckCircle, ChevronLeft,
  Store, Bot, Zap, MessageSquare, TrendingUp, Rocket,
  Tag, Loader2, AlertCircle,
} from 'lucide-react'
import { billingApi, type BillingPlan } from '../api/billing'

// ── Step types ────────────────────────────────────────────────────────────────

type Step = 'welcome' | 'pricing' | 'plans' | 'done'

// ── Sub-components ────────────────────────────────────────────────────────────

function StepIndicator({ current, total }: { current: number; total: number }) {
  return (
    <div className="flex items-center gap-2">
      {Array.from({ length: total }, (_, i) => (
        <div
          key={i}
          className={[
            'h-1.5 rounded-full transition-all',
            i < current ? 'bg-brand-500 w-6' : i === current ? 'bg-brand-400 w-8' : 'bg-slate-200 w-4',
          ].join(' ')}
        />
      ))}
    </div>
  )
}

function PricingFeatureRow({ icon: Icon, text }: { icon: React.ElementType; text: string }) {
  return (
    <div className="flex items-center gap-3 py-2.5 border-b border-slate-100 last:border-0">
      <div className="w-7 h-7 bg-brand-50 rounded-lg flex items-center justify-center shrink-0">
        <Icon className="w-3.5 h-3.5 text-brand-600" />
      </div>
      <span className="text-sm text-slate-700">{text}</span>
    </div>
  )
}

const PLAN_ICONS: Record<string, React.ElementType> = {
  starter: Zap,
  growth:  TrendingUp,
  scale:   Rocket,
}

const PLAN_GRADIENTS: Record<string, string> = {
  starter: 'from-blue-500 to-blue-600',
  growth:  'from-brand-500 to-brand-600',
  scale:   'from-purple-500 to-purple-600',
}

// ── Welcome step ──────────────────────────────────────────────────────────────

function WelcomeStep({ onNext }: { onNext: () => void }) {
  return (
    <div className="text-center space-y-6">
      <div className="flex justify-center">
        <div className="w-20 h-20 bg-brand-500 rounded-3xl flex items-center justify-center shadow-xl shadow-brand-500/30">
          <Sparkles className="w-10 h-10 text-white" />
        </div>
      </div>
      <div>
        <h1 className="text-2xl font-black text-slate-900">أهلاً بك في نهلة!</h1>
        <p className="text-slate-500 mt-2 text-sm leading-relaxed max-w-xs mx-auto">
          منصة الطيار الآلي للمبيعات المدعومة بالذكاء الاصطناعي لمتجرك
        </p>
      </div>

      <div className="card p-4 text-start space-y-0">
        <PricingFeatureRow icon={Store}          text="ربط متجرك بسلة أو زد" />
        <PricingFeatureRow icon={Bot}            text="تفعيل الردود الذكية على واتساب" />
        <PricingFeatureRow icon={MessageSquare}  text="إدارة المحادثات تلقائياً" />
        <PricingFeatureRow icon={Zap}            text="أتمتة المبيعات والمتابعة" />
      </div>

      <button
        onClick={onNext}
        className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-3 rounded-xl text-sm transition-colors"
      >
        ابدأ الإعداد
      </button>
    </div>
  )
}

// ── Pricing explanation step ──────────────────────────────────────────────────

function PricingStep({ onNext, integFee }: { onNext: () => void; integFee: number }) {
  return (
    <div className="space-y-5">
      <div className="text-center">
        <h2 className="text-lg font-black text-slate-900">هيكل أسعار نهلة</h2>
        <p className="text-xs text-slate-500 mt-1">سعر شفاف بدون مفاجآت</p>
      </div>

      {/* Step 1: Integration fee */}
      <div className="card p-4 border-slate-200">
        <div className="flex items-start gap-3">
          <div className="w-8 h-8 bg-slate-700 text-white text-sm font-black rounded-xl flex items-center justify-center shrink-0">١</div>
          <div className="flex-1">
            <div className="flex items-center justify-between">
              <p className="font-semibold text-slate-900 text-sm">رسوم تكامل المتجر</p>
              <span className="text-sm font-black text-slate-800">{integFee} ر.س<span className="text-xs text-slate-400 font-normal">/شهر</span></span>
            </div>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">
              تُدفع عبر سلة أو زد مباشرةً. تشمل ربط متجرك، مزامنة الطلبات والمنتجات، وتحديثات الحالة.
            </p>
            <div className="mt-2 flex items-center gap-1 text-[11px] text-emerald-700 bg-emerald-50 border border-emerald-100 rounded-lg px-2 py-1 w-fit">
              <CheckCircle className="w-3 h-3" />
              هذه الرسوم لا تشمل الذكاء الاصطناعي
            </div>
          </div>
        </div>
      </div>

      {/* Step 2: Nahla plan */}
      <div className="card p-4 border-brand-200 bg-brand-50/40">
        <div className="flex items-start gap-3">
          <div className="w-8 h-8 bg-brand-500 text-white text-sm font-black rounded-xl flex items-center justify-center shrink-0">٢</div>
          <div className="flex-1">
            <div className="flex items-center justify-between">
              <p className="font-semibold text-slate-900 text-sm">خطة نهلة للذكاء الاصطناعي</p>
              <span className="text-sm font-black text-brand-600">من 449 ر.س<span className="text-xs text-slate-400 font-normal">/شهر</span></span>
            </div>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">
              تُدفع مباشرة لنهلة. تفعّل الطيار الآلي، الردود الذكية، الحملات التسويقية، ووكيل المبيعات.
            </p>
            <div className="mt-2 flex items-center gap-1 text-[11px] text-brand-700 bg-brand-100 border border-brand-200 rounded-lg px-2 py-1 w-fit">
              <Tag className="w-3 h-3" />
              خصم 50% على أول شهرين — عرض الإطلاق
            </div>
          </div>
        </div>
      </div>

      <div className="bg-slate-50 rounded-xl p-3 text-center">
        <p className="text-xs text-slate-500">
          الإجمالي المتوقع للبداية:{' '}
          <strong className="text-slate-800">{integFee + 449} ر.س/شهر</strong>
          {' '}(بعد انتهاء العرض: {integFee + 899} ر.س/شهر)
        </p>
      </div>

      <button
        onClick={onNext}
        className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-3 rounded-xl text-sm transition-colors"
      >
        اختر خطتك الآن
      </button>
    </div>
  )
}

// ── Plan selection step ───────────────────────────────────────────────────────

function PlansStep({
  plans,
  onSelect,
  subscribing,
}: {
  plans:       BillingPlan[]
  onSelect:    (slug: string) => void
  subscribing: string | null
}) {
  return (
    <div className="space-y-4">
      <div className="text-center">
        <h2 className="text-lg font-black text-slate-900">اختر خطة نهلة</h2>
        <p className="text-xs text-slate-500 mt-1">يمكنك الترقية أو التخفيض في أي وقت</p>
      </div>

      <div className="space-y-3">
        {plans.map(plan => {
          const Icon = PLAN_ICONS[plan.slug] ?? Zap
          const gradient = PLAN_GRADIENTS[plan.slug] ?? 'from-slate-500 to-slate-600'
          const isLoading = subscribing === plan.slug

          return (
            <button
              key={plan.slug}
              onClick={() => onSelect(plan.slug)}
              disabled={!!subscribing}
              className={[
                'w-full text-start card p-4 transition-all flex items-center gap-4',
                plan.slug === 'growth' ? 'border-brand-300 bg-brand-50/30' : 'border-slate-200',
                subscribing ? 'opacity-60 cursor-not-allowed' : 'hover:border-brand-300 hover:shadow-md active:scale-[0.99]',
              ].join(' ')}
            >
              <div className={`w-10 h-10 bg-gradient-to-br ${gradient} rounded-xl flex items-center justify-center shrink-0 text-white`}>
                {isLoading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Icon className="w-5 h-5" />}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <p className="font-bold text-slate-900 text-sm">{plan.name_ar}</p>
                  <div className="text-end">
                    <span className="text-base font-black text-brand-600">
                      {plan.launch_price_sar.toLocaleString('ar-SA')}
                    </span>
                    <span className="text-xs text-slate-400 ms-0.5">ر.س/شهر</span>
                    {plan.launch_price_sar < plan.price_sar && (
                      <p className="text-[10px] text-slate-400 line-through">{plan.price_sar.toLocaleString('ar-SA')} ر.س</p>
                    )}
                  </div>
                </div>
                <p className="text-xs text-slate-500 mt-0.5 truncate">{plan.description}</p>
              </div>
              <ChevronLeft className="w-4 h-4 text-slate-400 shrink-0 rtl:rotate-180" />
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Done step ─────────────────────────────────────────────────────────────────

function DoneStep({ planName, onGo }: { planName: string; onGo: () => void }) {
  return (
    <div className="text-center space-y-6 py-4">
      <div className="flex justify-center">
        <div className="w-20 h-20 bg-emerald-500 rounded-3xl flex items-center justify-center shadow-xl shadow-emerald-500/30">
          <CheckCircle className="w-10 h-10 text-white" />
        </div>
      </div>
      <div>
        <h2 className="text-xl font-black text-slate-900">تم تفعيل الطيار الآلي!</h2>
        <p className="text-slate-500 mt-2 text-sm">
          خطة <strong>{planName}</strong> فعّالة الآن. يمكنك البدء في إعداد واتساب والأتمتة.
        </p>
      </div>
      <div className="card p-4 text-start space-y-2">
        {['اذهب إلى الإعدادات واربط رقم واتساب', 'فعّل الطيار الآلي من صفحة الأتمتة', 'أضف منتجاتك وابدأ البيع!'].map((step, i) => (
          <div key={i} className="flex items-center gap-3 text-sm text-slate-700">
            <div className="w-5 h-5 bg-brand-500 text-white text-xs font-bold rounded-full flex items-center justify-center shrink-0">
              {i + 1}
            </div>
            {step}
          </div>
        ))}
      </div>
      <button
        onClick={onGo}
        className="w-full bg-brand-500 hover:bg-brand-600 text-white font-bold py-3 rounded-xl text-sm transition-colors"
      >
        الذهاب إلى لوحة التحكم
      </button>
    </div>
  )
}

// ── Main ──────────────────────────────────────────────────────────────────────

export default function Onboarding() {
  const navigate = useNavigate()
  const [step,        setStep]        = useState<Step>('welcome')
  const [plans,       setPlans]       = useState<BillingPlan[]>([])
  const [integFee,    setIntegFee]    = useState(59)
  const [plansLoaded, setPlansLoaded] = useState(false)
  const [subscribing, setSubscribing] = useState<string | null>(null)
  const [subError,    setSubError]    = useState<string | null>(null)
  const [chosenPlan,  setChosenPlan]  = useState('')

  const loadPlans = async () => {
    if (plansLoaded) return
    try {
      const res = await billingApi.getPlans()
      setPlans(res.plans)
      setIntegFee(res.integration_fee_sar)
      setPlansLoaded(true)
    } catch {/* use empty plans */}
  }

  const goToPricing = () => { setStep('pricing') }
  const goToPlans   = () => { loadPlans(); setStep('plans') }

  const handleSelect = async (slug: string) => {
    setSubscribing(slug)
    setSubError(null)
    try {
      const res = await billingApi.subscribe(slug)
      if (res.success) {
        const plan = plans.find(p => p.slug === slug)
        setChosenPlan(plan?.name_ar ?? slug)
        setStep('done')
      }
    } catch {
      setSubError('فشل تفعيل الخطة. يرجى المحاولة مجدداً.')
    } finally {
      setSubscribing(null)
    }
  }

  const STEP_NUM: Record<Step, number> = { welcome: 0, pricing: 1, plans: 2, done: 3 }

  return (
    <div className="min-h-screen bg-slate-50 flex items-center justify-center px-4 py-8" dir="rtl">
      <div className="w-full max-w-md">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 bg-brand-500 rounded-lg flex items-center justify-center">
              <Sparkles className="w-4 h-4 text-white" />
            </div>
            <span className="font-bold text-slate-800">نهلة</span>
          </div>
          <StepIndicator current={STEP_NUM[step]} total={4} />
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-xl p-6">
          {subError && (
            <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 rounded-xl px-3 py-2.5 text-sm mb-4">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {subError}
            </div>
          )}

          {step === 'welcome' && <WelcomeStep onNext={goToPricing} />}
          {step === 'pricing' && <PricingStep onNext={goToPlans} integFee={integFee} />}
          {step === 'plans'   && (
            <PlansStep
              plans={plans}
              onSelect={handleSelect}
              subscribing={subscribing}
            />
          )}
          {step === 'done' && (
            <DoneStep
              planName={chosenPlan}
              onGo={() => navigate('/overview')}
            />
          )}
        </div>

        {/* Skip link */}
        {step !== 'done' && (
          <p className="text-center mt-4">
            <button
              onClick={() => navigate('/overview')}
              className="text-xs text-slate-400 hover:text-slate-600 underline"
            >
              تخطي — سأختار لاحقاً
            </button>
          </p>
        )}
      </div>
    </div>
  )
}
