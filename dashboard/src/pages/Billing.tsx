import { useState, useEffect } from 'react'
import {
  CheckCircle, Zap, TrendingUp, Rocket, Loader2, AlertCircle,
  RefreshCw, Tag, MessageSquare, Star, ArrowUp, ExternalLink, ShieldCheck,
  Clock, Sparkles, Bot, Phone, Info, ArrowUpRight,
} from 'lucide-react'
import { billingApi, type BillingPlan, type BillingStatus } from '../api/billing'

// ── Salla app URL ─────────────────────────────────────────────────────────────
// Single source of truth for "subscribe / manage your plan via Salla".
// Override per-environment with VITE_SALLA_APP_URL (Railway / Vercel / .env).
const SALLA_APP_URL: string =
  (import.meta.env.VITE_SALLA_APP_URL as string | undefined) ||
  'https://s.salla.sa/apps/nahla'

// Open the Salla app subscription page at the TOP level so it breaks out of
// the iframe when the merchant is viewing this page inside Salla's dashboard.
function openSallaApp(): void {
  try {
    if (window.top) {
      window.top.location.href = SALLA_APP_URL
      return
    }
  } catch { /* cross-origin top access blocked — fall through */ }
  window.location.href = SALLA_APP_URL
}

// ── Analytics ─────────────────────────────────────────────────────────────────
// Lightweight event tracker that forwards to whichever analytics platform is
// loaded on the page (posthog, GA4 via gtag) and always logs to console so
// tracking still works in dev / before any tool is wired.
type TrackPayload = Record<string, string | number | boolean | null | undefined>

function trackEvent(name: string, payload: TrackPayload = {}): void {
  try {
    console.info(`[track] ${name}`, payload)

    // PostHog — if loaded (posthog.com)
    const ph = (window as unknown as { posthog?: { capture: (n: string, p: TrackPayload) => void } }).posthog
    if (ph && typeof ph.capture === 'function') {
      ph.capture(name, payload)
    }

    // Google Analytics 4 / GTM — if loaded (gtag('event', ...))
    const gtag = (window as unknown as { gtag?: (cmd: string, eventName: string, p: TrackPayload) => void }).gtag
    if (typeof gtag === 'function') {
      gtag('event', name, payload)
    }
  } catch (e) {
    console.warn('[track] failed:', e)
  }
}

// Detect whether the merchant is browsing this billing page from inside the
// Salla embedded experience.  Used to render a Salla-specific notice that
// directs the merchant to subscribe via Salla's billing UI (required by
// Salla's app distribution policy).
function isSallaMerchant(): boolean {
  try {
    if (localStorage.getItem('nahla_salla_embedded') === '1') return true
    if (localStorage.getItem('nahla_salla_store_id'))         return true
  } catch { /* localStorage blocked */ }
  return false
}

// ── Manual fallback (used when payment gateway is down / not configured) ──────
// ⚠️ Update this number if Nahla support contact changes.
const SUPPORT_WHATSAPP = '966555000000'   // +966 5 55 00 00 00 (placeholder)

function buildSupportUrl(planNameAr: string, storeName: string): string {
  const text = `مرحباً، أرغب بتفعيل باقة «${planNameAr}» لمتجر ${storeName || '—'}.`
  return `https://wa.me/${SUPPORT_WHATSAPP}?text=${encodeURIComponent(text)}`
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(n: number) {
  return n === -1 ? '∞' : n.toLocaleString('ar-SA')
}

function usagePercent(used: number, limit: number) {
  if (limit === -1 || limit === 0) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

function fmtDate(iso?: string | null) {
  if (!iso) return '—'
  return iso.slice(0, 10)
}

function paymentProviderLabel(provider?: string) {
  if (!provider || provider === 'unknown') return 'غير معروف'
  if (provider === 'moyasar') return 'Moyasar'
  if (provider === 'manual') return 'يدوي'
  return provider
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
  billingStatus,
  onCheckout,
  checkingOut,
  storeName,
  isSalla,
}: {
  plan:          BillingPlan
  billingStatus: BillingStatus | null
  onCheckout:    (slug: string) => void
  checkingOut:   string | null
  storeName:     string
  isSalla:       boolean
}) {
  const isPopular  = plan.slug === 'growth'
  const gradient   = PLAN_GRADIENTS[plan.slug] ?? 'from-slate-500 to-slate-600'
  // Only THIS card is loading — other cards remain clickable.
  const isLoading  = checkingOut === plan.slug
  const isOtherLoading = checkingOut !== null && checkingOut !== plan.slug
  const hasDiscount = plan.launch_price_sar < plan.price_sar

  const isPaidActive = billingStatus?.lifecycle_status === 'paid_active'
    && billingStatus.plan?.slug === plan.slug
  const isTrialPlan = billingStatus?.lifecycle_status === 'trial_active'
  const isHighlighted = isPaidActive

  return (
    <div
      className={[
        'relative rounded-2xl border-2 flex flex-col transition-all duration-200',
        isHighlighted
          ? 'border-brand-500 shadow-lg shadow-brand-500/10'
          : 'border-slate-200 hover:border-slate-300 hover:shadow-md',
      ].join(' ')}
    >
      {/* Badge */}
      {isPopular && !isPaidActive && (
        <div className="absolute -top-3 start-1/2 -translate-x-1/2 rtl:translate-x-1/2">
          <span className="bg-brand-500 text-white text-[11px] font-bold px-3 py-1 rounded-full flex items-center gap-1">
            <Star className="w-3 h-3" /> الأكثر شيوعاً
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
            i === 0 ? (
              /* First feature = killer feature — gets a prominent
                 visual treatment to draw the eye immediately. */
              <li key={i} className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2 -mx-1">
                <span className="text-base shrink-0 mt-0.5">📱</span>
                <span className="text-xs font-bold text-amber-900 leading-snug">{f}</span>
              </li>
            ) : (
              <li key={i} className="flex items-start gap-2 text-sm text-slate-700">
                <CheckCircle className="w-4 h-4 text-emerald-500 shrink-0 mt-0.5" />
                {f}
              </li>
            )
          ))}
        </ul>
      </div>

      {/* CTA */}
      <div className="px-5 pb-5">
        {isPaidActive ? (
          <div className="w-full py-2.5 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm font-semibold text-center">
            مشترك الآن ✓
          </div>
        ) : isSalla ? (
          // ── Salla merchants — subscription must happen inside Salla's
          // billing UI (per Salla App Store policy).  We disable our own
          // payment flow and direct the merchant to Salla's app subscription
          // page instead.  Hides the WhatsApp manual-fallback as well so we
          // present a single, unambiguous path.
          <>
            {isTrialPlan && (
              <div className="w-full py-2 rounded-xl bg-brand-50 border border-brand-200 text-brand-700 text-xs font-semibold text-center mb-2 flex items-center justify-center gap-1.5">
                <Clock className="w-3.5 h-3.5" />
                تجربة مجانية — متبقي {billingStatus?.trial_days_remaining ?? 0} يوم
              </div>
            )}
            <a
              href={SALLA_APP_URL}
              target="_top"
              rel="noopener noreferrer"
              onClick={() => trackEvent('salla_redirect_clicked', {
                source:   'plan_card',
                plan:     plan.slug,
                is_salla: true,
              })}
              className={[
                'w-full py-2.5 rounded-xl text-white text-sm font-semibold transition-all',
                'flex items-center justify-center gap-2 cursor-alias',
                `bg-gradient-to-br ${gradient}`,
                'hover:opacity-90 active:scale-95',
              ].join(' ')}
            >
              <ArrowUpRight className="w-4 h-4" />
              الاشتراك عبر سلة
            </a>
            <p className="mt-2 text-[11px] text-amber-700 text-center leading-relaxed">
              يتم الاشتراك من داخل منصة سلة لضمان الربط الصحيح بمتجرك
            </p>
          </>
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
              onClick={() => {
                console.info('[Billing] plan clicked', {
                  plan_slug: plan.slug,
                  plan_name: plan.name_ar,
                  price_sar: plan.launch_price_sar,
                })
                onCheckout(plan.slug)
              }}
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
                : <><ExternalLink className="w-4 h-4" /> ادفع الآن — {plan.launch_price_sar.toLocaleString('ar-SA')} ر.س</>}
            </button>

            {/* Manual fallback — always visible so the merchant is never stuck */}
            <a
              href={buildSupportUrl(plan.name_ar, storeName)}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 w-full py-2 rounded-xl bg-emerald-50 hover:bg-emerald-100 text-emerald-700 text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <Phone className="w-3.5 h-3.5" />
              أو فعّل الباقة عبر واتساب الدعم
            </a>
          </>
        )}

        {/* Secure payment note (only for non-Salla payment flow) */}
        {!isPaidActive && !isSalla && (
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
  const [checkoutErrCode, setCheckoutErrCode] = useState<string>('')

  // Detected once per render — Salla merchants must subscribe via Salla.
  const isSalla = isSallaMerchant()

  // Inactivity hint for Salla merchants — appears 8 seconds after the page
  // loads if they haven't started a checkout yet.  Helps merchants who don't
  // realise the subscription is completed via Salla's billing UI.
  const [showSallaHint, setShowSallaHint] = useState(false)
  useEffect(() => {
    if (!isSalla) return
    const t = setTimeout(() => {
      // Skip hint if a checkout already started or the page already navigated.
      if (!checkingOut) setShowSallaHint(true)
    }, 8000)
    return () => clearTimeout(t)
  }, [isSalla, checkingOut])

  // "Returned from Salla without subscribing" banner.  Triggered when the
  // merchant lands on /billing with ?from=salla in the URL — i.e. they
  // bounced back without completing the subscription on Salla's side.
  // Cleans the query param immediately so a browser refresh doesn't re-show
  // the banner forever.
  const [returnedFromSalla, setReturnedFromSalla] = useState(false)
  useEffect(() => {
    if (!isSalla) return
    try {
      const params = new URLSearchParams(window.location.search)
      if (params.get('from') === 'salla') {
        setReturnedFromSalla(true)
        params.delete('from')
        const cleanSearch = params.toString()
        const cleanUrl    = window.location.pathname + (cleanSearch ? `?${cleanSearch}` : '')
        window.history.replaceState(null, '', cleanUrl)
        trackEvent('salla_returned_without_subscription', { is_salla: true })
      }
    } catch { /* noop */ }
  }, [isSalla])

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
    // ── Salla merchants are NEVER allowed to use our checkout — they must
    // subscribe through Salla's billing UI.  Defensive guard in addition to
    // the disabled button: if the click somehow reaches us, redirect to the
    // Salla app subscription page at the top level instead of opening Moyasar.
    if (isSalla) {
      console.info('[Billing] Salla merchant — redirecting to Salla subscription instead of Moyasar')
      openSallaApp()
      return
    }

    const tenantId = (() => {
      try { return localStorage.getItem('nahla_tenant_id') || '?' } catch { return '?' }
    })()
    console.info('[Billing] → /billing/checkout', { plan_slug: slug, tenant_id: tenantId })

    setCheckingOut(slug)
    setCheckoutMsg(null)
    setCheckoutErr(null)
    setCheckoutErrCode('')

    try {
      const res = await billingApi.createCheckout(slug)
      console.info('[Billing] checkout response', {
        gateway:        res.gateway,
        has_url:        !!res.checkout_url,
        demo_mode:      res.demo_mode,
        subscription_id: res.subscription_id,
      })

      if (res.checkout_url) {
        // Real Moyasar payment — redirect to hosted payment page
        console.info('[Billing] redirecting to payment gateway:', res.checkout_url)
        window.location.href = res.checkout_url
        return
      }

      if (res.demo_mode) {
        setCheckoutMsg('تم تفعيل الخطة بنجاح! (وضع تجريبي — بدون دفع)')
        await load()
        setCheckingOut(null)
        return
      }

      // ── Gateway responded successfully but with NO usable url ──────────
      // Don't leave the merchant stuck on a spinner — show actionable error.
      console.warn('[Billing] checkout returned no checkout_url and no demo_mode', res)
      setCheckoutErr(
        'بوابة الدفع قيد المراجعة حاليًا. يمكنك تفعيل الاشتراك يدويًا عبر الدعم.',
      )
      setCheckoutErrCode('payment_provider_not_ready')
      setCheckingOut(null)
    } catch (err: any) {
      const code = (err?.code as string | undefined) ?? ''
      const msg  = (err?.message as string | undefined) ?? 'خطأ غير معروف'
      console.error('[Billing] checkout failed', { code, msg, status: err?.status, plan_slug: slug })

      let friendly: string
      switch (code) {
        case 'payment_provider_not_ready':
          // Moyasar account under review / not approved yet.
          friendly = msg || 'بوابة الدفع قيد المراجعة حاليًا. يمكنك تفعيل الاشتراك يدويًا عبر الدعم.'
          break
        case 'payment_gateway_error':
          friendly = msg || 'تعذّر الاتصال ببوابة الدفع. حاول لاحقاً أو فعّل الاشتراك يدويًا عبر الدعم.'
          break
        case 'subscription_inactive':
          friendly = msg
          break
        default:
          friendly = `تعذّر إنشاء جلسة الدفع (${msg}). يمكنك تفعيل الاشتراك يدويًا عبر واتساب الدعم.`
      }
      setCheckoutErr(friendly)
      setCheckoutErrCode(code)
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

  if (!status) {
    return (
      <div className="card p-8 max-w-md mx-auto flex flex-col items-center gap-3 text-center">
        <AlertCircle className="w-8 h-8 text-red-400" />
        <p className="text-sm text-slate-700">تعذّر تحميل بيانات الاشتراك</p>
        <button onClick={load} className="btn-secondary text-sm flex items-center gap-2">
          <RefreshCw className="w-4 h-4" /> إعادة المحاولة
        </button>
      </div>
    )
  }

  const pct = usagePercent(
    status.conversations_used,
    status.conversations_limit,
  )

  const lifecycle = status.lifecycle_status ?? (
    status.trial_pending_whatsapp ? 'trial_pending_whatsapp'
    : status.is_trial ? 'trial_active'
    : status.subscription_expired ? 'paid_expired'
    : status.trial_expired ? 'trial_expired'
    : status.has_subscription ? 'paid_active'
    : 'trial_expired'
  )

  const daysRemainingLabel = (() => {
    if (lifecycle === 'trial_pending_whatsapp') return '—'
    if (status.days_remaining != null && status.days_remaining > 0) {
      return String(status.days_remaining)
    }
    if (lifecycle === 'trial_active') return String(status.trial_days_remaining)
    if (lifecycle === 'trial_expired' || lifecycle === 'paid_expired') return '٠'
    return '—'
  })()

  const warningLevel = status.warning_level ?? 'none'
  const showExpiryWarning = ['7d', '3d', '1d', 'expired'].includes(warningLevel)
  const expiryWarningStyle =
    warningLevel === 'expired' ? 'bg-red-50 border-red-200 text-red-800'
    : warningLevel === '1d'    ? 'bg-red-50 border-red-200 text-red-800'
    : warningLevel === '3d'    ? 'bg-amber-50 border-amber-200 text-amber-900'
    : 'bg-amber-50 border-amber-200 text-amber-900'

  const lifecycleHeadline = status.headline_ar || status.status_reason_ar || '—'
  const planDisplayName = status.plan_name || status.plan?.name_ar || '—'

  return (
    <div className="space-y-6 mx-auto px-5 py-10" style={{maxWidth: '1100px'}} dir="rtl">

      {/* Page header */}
      <div>
        <h1 className="text-xl font-bold text-slate-900">الاشتراك والفوترة</h1>
        <p className="text-sm text-slate-500 mt-1">إدارة خطة نحلة واستخدامك الشهري</p>
      </div>

      {/* Returned from Salla without subscribing — only for Salla merchants
          who bounced back via ?from=salla query param. */}
      {isSalla && returnedFromSalla && (
        <div className="flex items-center justify-between gap-3 bg-amber-50 border-2 border-amber-300 rounded-xl px-4 py-3">
          <div className="flex items-start gap-2.5 flex-1">
            <AlertCircle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-sm font-semibold text-amber-900 leading-relaxed">
              لم يتم إتمام الاشتراك بعد — يمكنك المتابعة عبر سلة 👆
            </p>
          </div>
          <a
            href={SALLA_APP_URL}
            target="_top"
            rel="noopener noreferrer"
            onClick={() => {
              setReturnedFromSalla(false)
              trackEvent('salla_redirect_clicked', {
                source:   'returned_banner',
                plan:     null,
                is_salla: true,
              })
            }}
            className={[
              'shrink-0 inline-flex items-center gap-1.5',
              'bg-amber-600 hover:bg-amber-700 text-white',
              'rounded-lg px-3 py-1.5 text-xs font-bold',
              'transition-colors cursor-alias',
            ].join(' ')}
          >
            الذهاب إلى سلة
            <ArrowUpRight className="w-3.5 h-3.5" />
          </a>
        </div>
      )}

      {/* Subscription lifecycle summary */}
      <div className="card p-5 space-y-3">
          <h2 className="text-sm font-bold text-slate-900">حالة الاشتراك</h2>
          <p className="text-sm font-semibold text-slate-800 leading-relaxed">{lifecycleHeadline}</p>
          <div className="grid sm:grid-cols-2 gap-3 text-sm">
            <div>
              <p className="text-xs text-slate-500">الحالة</p>
              <p className="font-semibold text-slate-800">
                {status.lifecycle_status_label_ar || status.status_reason_ar || '—'}
              </p>
            </div>
            <div>
              <p className="text-xs text-slate-500">الأيام المتبقية</p>
              <p className="font-semibold text-slate-800">{daysRemainingLabel}</p>
            </div>
            {lifecycle === 'paid_active' && status.subscription_started_at && (
              <div>
                <p className="text-xs text-slate-500">بدأ الاشتراك</p>
                <p className="font-medium text-slate-700">{fmtDate(status.subscription_started_at)}</p>
              </div>
            )}
            {lifecycle === 'paid_active' && status.subscription_ends_at && (
              <div>
                <p className="text-xs text-slate-500">ينتهي الاشتراك</p>
                <p className="font-medium text-slate-700">{fmtDate(status.subscription_ends_at)}</p>
              </div>
            )}
            {lifecycle === 'trial_active' && status.trial_ends_at && (
              <div>
                <p className="text-xs text-slate-500">تنتهي التجربة</p>
                <p className="font-medium text-slate-700">{fmtDate(status.trial_ends_at)}</p>
              </div>
            )}
            {(lifecycle === 'trial_expired' || lifecycle === 'paid_expired') && (
              <div>
                <p className="text-xs text-slate-500">تاريخ الانتهاء</p>
                <p className="font-medium text-slate-700">
                  {lifecycle === 'paid_expired'
                    ? fmtDate(status.subscription_ends_at)
                    : fmtDate(status.trial_ends_at)}
                </p>
              </div>
            )}
          </div>
        </div>

      {/* Subscription details */}
      <div className="card p-5 space-y-4">
          <h2 className="text-sm font-bold text-slate-900">تفاصيل الاشتراك</h2>
          <div className="grid sm:grid-cols-2 gap-3 text-sm">
            <div>
              <p className="text-xs text-slate-500">الخطة الحالية / آخر خطة</p>
              <p className="font-medium text-slate-800">{planDisplayName}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">حالة الاشتراك</p>
              <p className="font-medium text-slate-800">
                {status.lifecycle_status_label_ar || '—'}
              </p>
            </div>
            <div>
              <p className="text-xs text-slate-500">تاريخ بداية التجربة</p>
              <p className="font-medium text-slate-700">{fmtDate(status.trial_started_at)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">تاريخ نهاية التجربة</p>
              <p className="font-medium text-slate-700">{fmtDate(status.trial_ends_at)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">تاريخ بداية الاشتراك المدفوع</p>
              <p className="font-medium text-slate-700">{fmtDate(status.subscription_started_at)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">تاريخ نهاية الاشتراك المدفوع</p>
              <p className="font-medium text-slate-700">{fmtDate(status.subscription_ends_at)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">آخر دفعة</p>
              <p className="font-medium text-slate-700">{fmtDate(status.last_payment_at)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">مبلغ آخر دفعة</p>
              <p className="font-medium text-slate-700">
                {status.last_payment_amount
                  ? `${status.last_payment_amount.toLocaleString('ar-SA')} ر.س`
                  : '—'}
              </p>
            </div>
            <div>
              <p className="text-xs text-slate-500">طريقة الدفع</p>
              <p className="font-medium text-slate-700">{paymentProviderLabel(status.payment_provider)}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">ربط واتساب</p>
              <p className="font-medium text-slate-700">
                {status.whatsapp_connected ? 'متصل' : 'غير متصل'}
              </p>
            </div>
          </div>

          {status.payment_history && status.payment_history.length > 0 && (
            <div className="pt-2 border-t border-slate-100">
              <h3 className="text-xs font-bold text-slate-700 mb-2">سجل الدفعات</h3>
              <div className="space-y-2">
                {status.payment_history.map((row, i) => (
                  <div
                    key={i}
                    className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600 bg-slate-50 rounded-lg px-3 py-2"
                  >
                    <span className="font-medium">{fmtDate(row.paid_at)}</span>
                    <span>—</span>
                    <span>{row.plan_name}</span>
                    <span>—</span>
                    <span>{row.amount_sar.toLocaleString('ar-SA')} ر.س</span>
                    <span>—</span>
                    <span className={
                      row.status === 'paid' ? 'text-emerald-600 font-semibold' : 'text-amber-600'
                    }>
                      {row.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

      {/* Trial not started — WhatsApp not connected */}
      {lifecycle === 'trial_pending_whatsapp' && (
        <div className="flex items-start gap-3 bg-sky-50 border-2 border-sky-200 rounded-xl p-4">
          <Info className="w-5 h-5 text-sky-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold text-sky-900">تجربتك المجانية لم تبدأ بعد</p>
            <p className="text-xs text-sky-800 mt-1">
              اربط واتساب لبدء التجربة المجانية · يمكنك إعداد المتجر الآن دون احتساب أيام
            </p>
            <button
              onClick={() => window.location.href = '/whatsapp-connect'}
              className="mt-2 inline-flex items-center gap-1.5 text-xs font-bold bg-sky-600 text-white px-3 py-1.5 rounded-lg hover:bg-sky-700"
            >
              <Phone className="w-3.5 h-3.5" />
              اربط واتساب
            </button>
          </div>
        </div>
      )}

      {/* Expiry warnings: 7d / 3d / 1d / expired */}
      {showExpiryWarning && (
        <div className={`flex items-start gap-3 border-2 rounded-xl p-4 ${expiryWarningStyle}`}>
          <AlertCircle className="w-5 h-5 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold">
              {warningLevel === 'expired'
                ? (lifecycle === 'paid_expired'
                    ? `انتهى اشتراكك في باقة ${planDisplayName}`
                    : lifecycle === 'trial_expired'
                      ? 'انتهت تجربتك المجانية'
                      : lifecycleHeadline)
                : warningLevel === '1d'
                  ? 'يتبقى يوم واحد على انتهاء اشتراكك'
                  : warningLevel === '3d'
                    ? 'يتبقى 3 أيام على انتهاء اشتراكك'
                    : 'يتبقى 7 أيام على انتهاء اشتراكك'}
            </p>
            <p className="text-xs mt-1 opacity-90">{lifecycleHeadline}</p>
          </div>
        </div>
      )}

      {/* Hero value proposition */}
      <div className="rounded-2xl bg-gradient-to-l from-brand-600 to-brand-400 p-5 text-white">
        {/* Killer feature banner */}
        <div className="mb-4 bg-white/15 border border-white/25 rounded-xl px-3 py-2.5 flex items-center gap-2.5">
          <span className="text-xl shrink-0">📱</span>
          <div>
            <p className="text-sm font-bold leading-snug">
              واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا
            </p>
            <p className="text-white/70 text-[11px] mt-0.5">
              استخدم تطبيق واتساب الأعمال على جوالك كالمعتاد — نحلة تعمل في الخلفية بدون أي تعارض. لا حاجة لحذف التطبيق.
            </p>
          </div>
        </div>
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
      {lifecycle === 'trial_active' && (
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

      {/* Paid subscription active */}
      {lifecycle === 'paid_active' && (
        <div className="flex items-start gap-3 bg-emerald-50 border-2 border-emerald-200 rounded-xl p-4">
          <CheckCircle className="w-5 h-5 text-emerald-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold text-emerald-900">
              اشتراكك في باقة {planDisplayName} نشط
            </p>
            <p className="text-xs text-emerald-800 mt-1">
              بدأ الاشتراك بتاريخ: {fmtDate(status.subscription_started_at)} ·
              ينتهي بتاريخ: {fmtDate(status.subscription_ends_at)} ·
              الأيام المتبقية: {daysRemainingLabel}
            </p>
          </div>
        </div>
      )}

      {/* Trial expired — no paid subscription */}
      {lifecycle === 'trial_expired' && (
        <div className="flex items-start gap-3 bg-red-50 border-2 border-red-200 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold text-red-800">
              انتهت تجربتك المجانية بتاريخ: {fmtDate(status.trial_ends_at)}
            </p>
            <p className="text-xs text-red-700 mt-1">
              اختر خطة للاشتراك ومتابعة تشغيل موظف المبيعات الذكي.
            </p>
          </div>
        </div>
      )}

      {/* Paid subscription expired */}
      {lifecycle === 'paid_expired' && (
        <div className="flex items-start gap-3 bg-orange-50 border-2 border-orange-300 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-orange-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-bold text-orange-900">
              انتهى اشتراكك في باقة {planDisplayName} بتاريخ: {fmtDate(status.subscription_ends_at)}
            </p>
            <p className="text-xs text-orange-800 mt-1">
              يرجى التجديد لاستمرار الردود الذكية وموظف المبيعات الذكي.
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
      {checkoutErr && (() => {
        // Provider-not-ready is an "info" state, not a hard error.
        const isProviderNotReady = checkoutErrCode === 'payment_provider_not_ready'
        const palette = isProviderNotReady
          ? { bg: 'bg-amber-50', border: 'border-amber-200', text: 'text-amber-800', icon: 'text-amber-500' }
          : { bg: 'bg-red-50',   border: 'border-red-200',   text: 'text-red-700',   icon: 'text-red-500'   }
        const title = isProviderNotReady
          ? 'بوابة الدفع قيد المراجعة'
          : 'تعذّر إنشاء جلسة الدفع'
        return (
          <div className={`flex items-start gap-3 ${palette.bg} border ${palette.border} ${palette.text} rounded-xl px-4 py-3 text-sm`}>
            <AlertCircle className={`w-4 h-4 shrink-0 mt-0.5 ${palette.icon}`} />
            <div className="flex-1">
              <p className="font-semibold">{title}</p>
              <p className="mt-0.5">{checkoutErr}</p>
              <div className="flex items-center gap-2 mt-2 flex-wrap">
                <a
                  href={buildSupportUrl('—', (() => {
                    try { return localStorage.getItem('nahla_store_name') || '' } catch { return '' }
                  })())}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs font-bold bg-emerald-600 text-white px-3 py-1.5 rounded-lg hover:bg-emerald-700 transition-colors"
                >
                  <Phone className="w-3.5 h-3.5" />
                  تفعيل الاشتراك يدوياً عبر واتساب
                </a>
                <button
                  onClick={() => { setCheckoutErr(null); setCheckoutErrCode('') }}
                  className="text-xs text-slate-500 hover:text-slate-700 underline"
                >
                  إغلاق
                </button>
              </div>
            </div>
          </div>
        )
      })()}

      {/* Redirecting overlay — only while waiting for backend response.
          Auto-dismisses on error so the merchant can act. */}
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

      {/* Current status cards */}
      <div className="grid sm:grid-cols-3 gap-4">

        {/* Active plan */}
        <div className="card p-5">
          <p className="text-xs text-slate-500 mb-1">الخطة الحالية</p>
          {lifecycle === 'paid_active' ? (
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
          ) : lifecycle === 'trial_pending_whatsapp' ? (
            <>
              <p className="text-base font-semibold text-sky-600">في انتظار ربط واتساب</p>
              <p className="text-xs text-slate-400 mt-1">التجربة المجانية تبدأ بعد الربط</p>
            </>
          ) : lifecycle === 'trial_active' ? (
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
          ) : lifecycle === 'paid_expired' ? (
            <>
              <p className="text-base font-semibold text-orange-700">{planDisplayName}</p>
              <p className="text-xs text-orange-600 mt-1">اشتراك منتهي — يرجى التجديد</p>
            </>
          ) : lifecycle === 'trial_expired' ? (
            <>
              <p className="text-base font-semibold text-red-600">التجربة منتهية</p>
              <p className="text-xs text-slate-400 mt-1">اختر خطة لتفعيل الطيار الآلي</p>
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
            {(status.conversations_used).toLocaleString('ar-SA')}
          </p>
          <p className="text-xs text-slate-400 mt-0.5">
            من {fmt(status.conversations_limit)}
            {status.conversations_limit !== -1 ? ' محادثة' : ' (غير محدود)'}
          </p>
          {lifecycle === 'paid_active' && status.conversations_limit !== -1 && (
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

      {/* No subscription alert — trial expired without payment */}
      {lifecycle === 'trial_expired' && (
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

      {/* Paid subscription expired — renewal nudge */}
      {lifecycle === 'paid_expired' && (
        <div className="flex items-start gap-3 bg-orange-50 border border-orange-200 rounded-xl p-4">
          <AlertCircle className="w-5 h-5 text-orange-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-orange-800">اشتراكك المدفوع منتهٍ</p>
            <p className="text-xs text-orange-700 mt-0.5">
              الردود الذكية والطيار الآلي والحملات محجوبة حتى تجدد الاشتراك.
            </p>
          </div>
        </div>
      )}

      {/* Upgrade nudge */}
      {lifecycle === 'paid_active' && status.plan?.slug !== 'scale' && (
        <div className="flex items-center gap-3 bg-brand-50 border border-brand-100 rounded-xl p-4">
          <ArrowUp className="w-5 h-5 text-brand-500 shrink-0" />
          <p className="text-sm text-brand-800">
            ترقية الخطة تعني محادثات أكثر وأتمتات أقوى.
          </p>
        </div>
      )}

      {/* Salla-merchant subscription notice — appears ONLY for merchants who
          opened this page via Salla embedded session. Required by Salla's
          policy: subscriptions for Salla merchants must go through Salla's
          billing UI to ensure correct activation + linking with their store. */}
      {isSalla && (
        <div className="space-y-3">
          {/* Policy notice */}
          <div className="flex items-start gap-3 bg-amber-50 border-2 border-amber-300 rounded-xl p-4">
            <Info className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <div className="flex-1">
              <p className="text-sm font-bold text-amber-900">
                تنبيه لتجار سلة
              </p>
              <p className="text-xs text-amber-800 mt-1 leading-relaxed">
                يتم الاشتراك وتجديد الباقة من داخل منصة سلة لضمان تفعيل الخدمة وربطها بمتجرك بشكل صحيح.
              </p>
            </div>
          </div>

          {/* Primary CTA — always available so the merchant can jump to Salla
              at any time, even before scrolling through the plans below. */}
          <a
            href={SALLA_APP_URL}
            target="_top"
            rel="noopener noreferrer"
            onClick={() => {
              setShowSallaHint(false)
              setReturnedFromSalla(false)
              trackEvent('salla_redirect_clicked', {
                source:   'billing_cta',
                plan:     null,
                is_salla: true,
              })
            }}
            className={[
              'group flex items-center justify-between gap-3',
              'rounded-2xl px-5 py-4',
              'bg-gradient-to-l from-brand-600 to-brand-500 text-white',
              // Stronger glow — this is THE primary action on the page for
              // Salla merchants.  Brand-500 ring + lifted shadow on hover.
              'shadow-xl shadow-brand-500/30 ring-1 ring-brand-400/40',
              'hover:shadow-2xl hover:shadow-brand-500/40 hover:ring-brand-300/60',
              'transition-all hover:-translate-y-0.5 active:translate-y-0',
              'cursor-alias',
            ].join(' ')}
            aria-label="افتح صفحة الاشتراك على سلة"
          >
            <div className="flex items-center gap-3">
              <div className="w-11 h-11 rounded-xl bg-white/20 flex items-center justify-center shrink-0">
                <ArrowUpRight className="w-5 h-5" />
              </div>
              <div className="text-right">
                <p className="text-base font-bold leading-tight">
                  ابدأ اشتراكك أو أدر باقتك عبر سلة
                </p>
                <p className="text-xs text-white/80 mt-0.5">
                  سيتم فتح صفحة التطبيق على منصة سلة في تبويب علوي
                </p>
              </div>
            </div>
            <span className="hidden sm:inline-flex items-center gap-1 bg-white/15 rounded-lg px-3 py-1.5 text-xs font-semibold group-hover:bg-white/25 transition-colors">
              فتح سلة
              <ExternalLink className="w-3.5 h-3.5" />
            </span>
          </a>

          {/* Inactivity hint — shows after 8s if the merchant hasn't acted */}
          {showSallaHint && (
            <div className="flex items-center justify-between gap-3 bg-brand-50 border border-brand-200 text-brand-800 rounded-xl px-4 py-2.5 text-xs animate-pulse">
              <div className="flex items-center gap-2">
                <span>✨ لإتمام الاشتراك، توجه إلى سلة من هنا 👆</span>
              </div>
              <button
                onClick={() => setShowSallaHint(false)}
                className="text-brand-500 hover:text-brand-700 text-xs underline"
                aria-label="إغلاق التنبيه"
              >
                إغلاق
              </button>
            </div>
          )}
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
              billingStatus={status}
              onCheckout={handleCheckout}
              checkingOut={checkingOut}
              storeName={(() => {
                try { return localStorage.getItem('nahla_store_name') || '' } catch { return '' }
              })()}
              isSalla={isSalla}
            />
          ))}
        </div>
      </div>

      {/* Payment security note — only shown for non-Salla merchants since
          Salla merchants pay via Salla's own checkout. */}
      {!isSalla && (
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
      )}

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
