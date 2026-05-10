/**
 * UpgradeModal — Plan Upgrade Prompt
 * ────────────────────────────────────
 * Shows when a locked feature is clicked.
 * Displays:
 *  - Feature name + benefit description
 *  - Which plan unlocks it
 *  - Comparison of current vs required plan
 *  - CTA to subscribe (Salla embedded or external)
 */
import { useEffect, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  FEATURE_LABELS_AR,
  FEATURE_REQUIRED_PLAN,
  PLAN_LABELS_AR,
  type PlanFeatures,
  type PlanSlug,
} from '../hooks/useEntitlements'

// ── Per-feature benefit descriptions ─────────────────────────────────────────

const FEATURE_BENEFITS_AR: Partial<Record<keyof PlanFeatures, string>> = {
  // Growth+
  advanced_coupon_types:         'أرسل كوبونات VIP للعملاء المميزين، واسترجع العملاء الخاملين بعروض مخصصة تلقائياً.',
  cart_recovery_stage_3:         'أضف مرحلة ثالثة لاسترجاع السلة المتروكة — زد فرص الإغلاق بنسبة تصل 40%.',
  cart_recovery_advanced_coupon: 'أرسل كوبون خصم ذكي في المرحلة الأخيرة من استرجاع السلة لتحويل التردد لشراء.',
  predictive_reorder:            'نحلة تتنبأ متى يحتاج عميلك للشراء مجدداً وترسل له رسالة في الوقت المثالي.',
  vip_rewards:                   'كافئ عملاءك المميزين تلقائياً بمكافآت حصرية لزيادة الولاء والإنفاق.',
  seasonal_smart_offers:         'استفد من اليوم الوطني، رمضان، عيد الفطر وغيرها بحملات ذكية تُرسل تلقائياً.',
  salary_offers:                 'أرسل عروضاً خاصة في أيام صرف الرواتب — وقت الشراء الذهبي.',
  smart_discount_popup:          'اعرض خصماً ذكياً في اللحظة المناسبة أثناء تصفح العميل لرفع معدل التحويل.',
  meta_catalog_sync:             'زامن منتجاتك تلقائياً مع Facebook وInstagram لحملات إعلانية أكثر دقة.',
  ai_performance_dashboard:      'تحليلات AI تفصيلية — الإيرادات المحولة، أفضل القوالب، أداء كل أتمتة.',
  campaign_ai_optimization:      'دع الذكاء الاصطناعي يختار أفضل وقت وقالب لكل حملة تلقائياً.',
  autopilot_full:                'أتمتة كاملة — استرجاع العملاء، COD، وإدارة المحادثات 24/7.',

  // Scale+
  advanced_ai_analytics:         'تحليلات متقدمة — تفصيل الإيرادات، أفضل المنتجات، مصادر الطلبات.',
  store_brain_advanced:          'ذكاء عميق بمنتجاتك وعملائك — ردود أدق وتوصيات أفضل.',
  team_handoff_queue:            'أدر طابور المحادثات بين فريقك مع أولويات ذكية وتسليم سلس.',
  advanced_discount_rules:       'بناء قواعد خصم مخصصة ومعقدة لكل سيناريو.',
  zid_integration:               'ربط متجرك على Zid بنحلة للتكامل الكامل.',
  future_integrations:           'وصول مبكر لجميع التكاملات القادمة (Google Ads، TikTok، وأكثر).',
}

// ── Plan feature highlights per plan ─────────────────────────────────────────

const PLAN_HIGHLIGHTS: Record<PlanSlug, string[]> = {
  starter: [
    '📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا',
    'حتى 5,000 محادثة/شهر',
    'مكتبة قوالب نحلة + مزامنة Meta',
    '3 أتمتات فعّالة',
    'حملات واتساب',
    'تحليلات أساسية',
  ],
  growth: [
    '📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا',
    'حتى 15,000 محادثة/شهر',
    'أتمتات غير محدودة',
    'حملات متقدمة',
    'إعادة الطلب التنبؤية + مكافآت VIP',
    'مزامنة كاتالوج ميتا (FB / IG)',
    'تحليلات متقدمة',
    'أولوية الدعم',
  ],
  scale: [
    '📱 واتساب الأعمال على الجوال + الذكاء الاصطناعي + الحملات معًا',
    'محادثات غير محدودة',
    'حملات غير محدودة',
    'تحليلات وتقارير مخصصة',
    'API كامل + فرق عمل وصلاحيات',
    'ذكاء المتجر المتقدم + تخصيص كامل',
    'دعم مخصص 24/7',
  ],
  none:   [],
  failed: [],
}

const PLAN_PRICES: Record<PlanSlug, string> = {
  starter: '979 ر.س / شهر',
  growth:  '1,899 ر.س / شهر',
  scale:   '3,199 ر.س / شهر',
  none:    '',
  failed:  '',
}

// ── Salla store URL helper ────────────────────────────────────────────────────

function getSallaAppStoreUrl(): string {
  try {
    return localStorage.getItem('nahla_salla_app_store_url') || 'https://s.salla.sa/apps'
  } catch {
    return 'https://s.salla.sa/apps'
  }
}

function isSallaEmbedded(): boolean {
  try {
    return localStorage.getItem('nahla_salla_embedded') === '1'
  } catch {
    return false
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

interface UpgradeModalProps {
  open:          boolean
  onClose:       () => void
  feature?:      keyof PlanFeatures
  requiredPlan?: PlanSlug
  title?:        string
  message?:      string
}

export function UpgradeModal({
  open,
  onClose,
  feature,
  requiredPlan = 'growth',
  title,
  message,
}: UpgradeModalProps) {
  const navigate   = useNavigate()
  const inSalla    = isSallaEmbedded()

  // Close on Escape
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null

  const featureLabel  = feature ? (FEATURE_LABELS_AR[feature] ?? String(feature)) : null
  const benefit       = feature ? FEATURE_BENEFITS_AR[feature] : null
  const planLabel     = PLAN_LABELS_AR[requiredPlan] ?? requiredPlan
  const planHighlights = PLAN_HIGHLIGHTS[requiredPlan] ?? []
  const planPrice      = PLAN_PRICES[requiredPlan] ?? ''
  const planColor      = requiredPlan === 'scale' ? '#a78bfa' : '#f59e0b'
  const planColorBg    = requiredPlan === 'scale' ? 'rgba(167,139,250,0.08)' : 'rgba(245,158,11,0.08)'
  const planColorBorder = requiredPlan === 'scale' ? 'rgba(167,139,250,0.2)' : 'rgba(245,158,11,0.2)'

  const handleSubscribe = () => {
    if (inSalla) {
      // In Salla iframe → navigate to plans page within iframe
      navigate('/app/entry')
    } else {
      // External dashboard → go to billing page
      window.location.href = '/billing'
    }
    onClose()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)' }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
      dir="rtl"
    >
      <div
        className="w-full max-w-sm rounded-3xl p-6 relative overflow-hidden"
        style={{
          background: '#0f172a',
          border:     `1px solid ${planColorBorder}`,
          boxShadow:  `0 24px 60px rgba(0,0,0,0.6), 0 0 0 1px ${planColorBorder}`,
        }}
      >
        {/* Close button */}
        <button
          type="button"
          onClick={onClose}
          className="absolute top-4 left-4 w-7 h-7 flex items-center justify-center rounded-full text-slate-500 hover:text-slate-300"
          style={{ background: 'rgba(255,255,255,0.05)' }}
        >
          ✕
        </button>

        {/* Plan badge */}
        <div className="flex justify-center mb-4">
          <div
            className="flex items-center gap-2 px-4 py-2 rounded-full"
            style={{ background: planColorBg, border: `1px solid ${planColorBorder}` }}
          >
            <span style={{ color: planColor, fontSize: 20 }}>🚀</span>
            <span className="font-black text-sm" style={{ color: planColor }}>
              باقة {planLabel}
            </span>
          </div>
        </div>

        {/* Title */}
        <div className="text-center mb-4">
          <h3 className="text-lg font-black text-white">
            {title ?? (featureLabel ? `رقِّ لتفعيل ${featureLabel}` : 'ترقية الباقة')}
          </h3>
          {(message ?? benefit) && (
            <p className="text-sm text-slate-400 mt-2 leading-relaxed">
              {message ?? benefit}
            </p>
          )}
        </div>

        {/* Plan price */}
        {planPrice && (
          <div
            className="text-center py-3 rounded-xl mb-4"
            style={{ background: planColorBg }}
          >
            <p className="text-xl font-black" style={{ color: planColor }}>{planPrice}</p>
            <p className="text-xs text-slate-500 mt-0.5">تجربة مجانية 14 يوم</p>
          </div>
        )}

        {/* Highlights */}
        {planHighlights.length > 0 && (
          <ul className="space-y-1.5 mb-5">
            {planHighlights.map((h, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="text-xs mt-0.5" style={{ color: planColor }}>✓</span>
                <span className="text-xs text-slate-300">{h}</span>
              </li>
            ))}
          </ul>
        )}

        {/* CTA */}
        <button
          type="button"
          onClick={handleSubscribe}
          className="w-full py-3.5 rounded-xl font-black text-sm"
          style={{ background: planColor, color: '#0f172a' }}
        >
          {inSalla ? 'عرض الباقات والاشتراك' : 'ترقية الباقة الآن'}
        </button>

        <p className="text-center text-[10px] text-slate-600 mt-3">
          {inSalla
            ? 'الاشتراك والدفع عبر منصة سلة'
            : 'بمجرد الترقية تُفعَّل الميزة فوراً'}
        </p>
      </div>
    </div>
  )
}
