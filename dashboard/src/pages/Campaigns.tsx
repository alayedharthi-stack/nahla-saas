import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  Plus, Send, Users, ShoppingCart, BarChart2, CheckCircle, XCircle,
  Megaphone, ChevronRight, ChevronLeft, Tag, Crown, Zap, Clock,
  Smartphone, AlertCircle, RefreshCw, X, MessageSquare, FileText,
  HandHeart, Repeat, Bell, Settings2, Sparkles, Moon, UserPlus, UserX,
  Calendar, ShoppingBag, TrendingUp, Star,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  campaignsApi, CampaignRecord, CreateCampaignPayload,
  CampaignGoal, CustomerSegmentMeta, RecommendedTemplate, TemplateRecommendation,
  extractVariables, renderTemplate, getTemplateBody, getTemplateHeader, getTemplateFooter,
} from '../api/campaigns'

// ── Types ─────────────────────────────────────────────────────────────────────

type ScheduleType = 'immediate' | 'scheduled' | 'delayed'

interface WizardState {
  step: number                                     // 1..8
  goalKey: string | null                           // step 1
  segmentKey: string | null                        // step 2
  template: RecommendedTemplate | null             // step 3
  variables: Record<string, string>                // step 4
  // step 5 = preview only (no state)
  testPhone: string                                // step 6
  testSent: boolean
  testSimulated: boolean
  testMessage: string
  testError: string
  // step 7 = review form
  campaignName: string
  scheduleType: ScheduleType
  scheduleTime: string
  delayMinutes: number
  couponCode: string
  autoCoupon: boolean
  discountPercent: number
}

const INITIAL_WIZARD: WizardState = {
  step: 1,
  goalKey: null,
  segmentKey: null,
  template: null,
  variables: {},
  testPhone: '',
  testSent: false,
  testSimulated: false,
  testMessage: '',
  testError: '',
  campaignName: '',
  scheduleType: 'immediate',
  scheduleTime: '',
  delayMinutes: 30,
  couponCode: '',
  autoCoupon: true,
  discountPercent: 10,
}

// ── Constants ─────────────────────────────────────────────────────────────────
//
// `STEP_LABELS` is the source of truth for both the breadcrumb and the
// progress bar — keep it 1-aligned with the `wiz.step` numbering.

const STEP_LABELS = [
  'هدف الحملة',         // 1
  'الشريحة المستهدفة',   // 2
  'اختيار القالب',       // 3
  'تعبئة المتغيرات',     // 4
  'المعاينة',            // 5
  'رسالة اختبار',        // 6
  'المراجعة النهائية',    // 7
  'إطلاق الحملة',         // 8
]

// Map lucide-react icon names emitted by the backend (goals/segments)
// to the actual React components. Using a registry keeps the page
// independent of any ad-hoc string-to-icon coupling.
const ICON_REGISTRY: Record<string, React.ComponentType<{ className?: string }>> = {
  HandHeart, Tag, RefreshCw, Repeat, Bell, Megaphone, Settings2,
  Users, UserPlus, UserX, Sparkles, Crown, Moon, ShoppingBag,
  ShoppingCart, TrendingUp, Calendar, Star,
}
function GoalIcon({ name, className }: { name: string; className?: string }) {
  const Cmp = ICON_REGISTRY[name] ?? Megaphone
  return <Cmp className={className} />
}

const STATUS_META: Record<string, { label: string; variant: 'green' | 'amber' | 'blue' | 'slate' | 'red' }> = {
  active:    { label: 'نشطة',    variant: 'green' },
  scheduled: { label: 'مجدولة',  variant: 'amber' },
  completed: { label: 'مكتملة',  variant: 'blue'  },
  paused:    { label: 'موقوفة',  variant: 'amber' },
  draft:     { label: 'مسودة',   variant: 'slate' },
  failed:    { label: 'فشلت',    variant: 'red'   },
}

// Map the new goal keys back to the legacy `campaign_type` enum the
// existing `Campaign` model + table column uses, so the list/table and
// any historical analytics keep working without a DB migration.
const GOAL_TO_LEGACY_TYPE: Record<string, string> = {
  welcome:      'new_arrivals',
  promotion:    'broadcast',
  reactivation: 'win_back',
  reorder:      'vip',
  reminder:     'abandoned_cart',
  broadcast:    'broadcast',
  custom:       'broadcast',
}

const TYPE_META: Record<string, { label: string; icon: React.ReactNode }> = {
  broadcast:     { label: 'بث جماعي',      icon: <Megaphone   className="w-3.5 h-3.5 text-blue-500" />  },
  abandoned_cart:{ label: 'عربة متروكة',   icon: <ShoppingCart className="w-3.5 h-3.5 text-amber-500" /> },
  vip:           { label: 'VIP',           icon: <Crown       className="w-3.5 h-3.5 text-purple-500" /> },
  new_arrivals:  { label: 'وصول جديد',     icon: <Zap         className="w-3.5 h-3.5 text-emerald-500" /> },
  win_back:      { label: 'استرجاع',       icon: <Users       className="w-3.5 h-3.5 text-rose-500" />   },
}

// ── WhatsApp preview bubble ───────────────────────────────────────────────────

function WaPreview({ header, body, footer }: { header: string; body: string; footer: string }) {
  return (
    <div className="bg-[#e5ddd5] rounded-xl p-4 min-h-32 flex items-end">
      <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs p-3 text-sm space-y-1" dir="rtl">
        {header && <p className="font-semibold text-slate-900 text-xs">{header}</p>}
        {body && (
          <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">
            {body}
          </p>
        )}
        {footer && <p className="text-slate-400 text-[10px] mt-1">{footer}</p>}
        <p className="text-[10px] text-slate-300 text-end">✓✓ الآن</p>
      </div>
    </div>
  )
}

// ── Step 1: Goal selection ────────────────────────────────────────────────────

function Step1Goal({
  wiz, setWiz, goals, loading,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  goals: CampaignGoal[]
  loading: boolean
}) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400">
        <RefreshCw className="w-5 h-5 animate-spin me-2" /> جارٍ تحميل أهداف الحملات…
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        ابدأ باختيار <span className="font-semibold text-slate-700">هدف الحملة</span>.
        نحلة ستقترح بعدها الشريحة والقالب الأنسب.
      </p>
      <div className="grid sm:grid-cols-2 gap-3">
        {goals.map(g => {
          const selected = wiz.goalKey === g.key
          return (
            <button
              key={g.key}
              onClick={() => setWiz(w => ({
                ...w,
                goalKey: g.key,
                // Auto-select the natural segment so step 2 has a default.
                segmentKey: w.segmentKey ?? g.default_segment_key,
                // Reset downstream selections — the template list will
                // re-rank for the new (goal, segment) context.
                template: null,
                variables: {},
              }))}
              className={`flex items-start gap-3 border rounded-xl p-4 text-start transition-all hover:shadow-md ${
                selected
                  ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                  : 'border-slate-200 bg-white hover:border-slate-300'
              }`}
            >
              <span className={`p-2 rounded-lg border ${
                selected ? 'border-brand-300 bg-white text-brand-600' : 'border-slate-200 bg-slate-50 text-slate-500'
              }`}>
                <GoalIcon name={g.icon} className="w-5 h-5" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-slate-900">{g.label_ar}</p>
                <p className="text-xs text-slate-500 mt-0.5 line-clamp-2">{g.description_ar}</p>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Step 2: Segment selection ─────────────────────────────────────────────────

function Step2Segment({
  wiz, setWiz, segments, loading, goals,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  segments: CustomerSegmentMeta[]
  loading: boolean
  goals: CampaignGoal[]
}) {
  // Reorder so segments "natural" to the chosen goal float to the top —
  // the merchant most often wants those first.
  const ordered = useMemo(() => {
    if (!wiz.goalKey) return segments
    const natural = segments.filter(s => s.natural_goals.includes(wiz.goalKey!))
    const rest    = segments.filter(s => !s.natural_goals.includes(wiz.goalKey!))
    return [...natural, ...rest]
  }, [segments, wiz.goalKey])

  const goalLabel = goals.find(g => g.key === wiz.goalKey)?.label_ar
  const selectedSeg = segments.find(s => s.key === wiz.segmentKey)

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400">
        <RefreshCw className="w-5 h-5 animate-spin me-2" /> جارٍ تحميل شرائح العملاء…
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        اختر الشريحة المستهدفة لحملة <span className="font-semibold text-slate-700">{goalLabel ?? '—'}</span>.
        الأرقام تعكس عدد العملاء القابل للوصول (لديهم رقم واتساب).
      </p>
      {selectedSeg && (
        <div className="bg-brand-50/50 border border-brand-100 rounded-lg p-3 text-xs text-slate-700 leading-relaxed">
          <span className="font-semibold text-brand-700">معنى «{selectedSeg.label_ar}»: </span>
          {selectedSeg.criteria_ar || selectedSeg.description_ar}
        </div>
      )}
      <div className="grid sm:grid-cols-2 gap-2 max-h-[26rem] overflow-y-auto pe-1">
        {ordered.map(s => {
          const selected = wiz.segmentKey === s.key
          const isNatural = wiz.goalKey ? s.natural_goals.includes(wiz.goalKey) : false
          return (
            <button
              key={s.key}
              onClick={() => setWiz(w => ({ ...w, segmentKey: s.key, template: null, variables: {} }))}
              title={s.criteria_ar || s.description_ar}
              className={`flex items-start gap-3 border rounded-xl p-3 text-start transition-all ${
                selected
                  ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                  : 'border-slate-200 bg-white hover:border-slate-300'
              }`}
            >
              <span className={`p-2 rounded-lg shrink-0 ${
                selected ? 'bg-brand-100 text-brand-600' : 'bg-slate-100 text-slate-500'
              }`}>
                <GoalIcon name={s.icon} className="w-4 h-4" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <p className="text-sm font-semibold text-slate-900 truncate">{s.label_ar}</p>
                  {isNatural && (
                    <span className="text-[9px] bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded-full font-medium shrink-0">
                      موصى به
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-slate-500 leading-snug line-clamp-2">
                  {s.description_ar}
                </p>
                <p className="text-[11px] text-slate-400 mt-0.5">
                  {s.customer_count.toLocaleString('ar-SA')} عميل قابل للوصول
                </p>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Step 3: Template selection (with badges + recommendation) ────────────────

function TemplateBadge({ label }: { label: string }) {
  // Map known labels to a colour. Unknown labels render in slate so a
  // future backend addition is safe even before we update this map.
  const map: Record<string, string> = {
    'الأفضل لهذه الحملة':   'bg-emerald-100 text-emerald-700 border-emerald-200',
    'معتمد من Meta':         'bg-blue-50 text-blue-700 border-blue-200',
    'متوافق':                 'bg-slate-100 text-slate-600 border-slate-200',
    'يحتاج مراجعة':           'bg-amber-50 text-amber-700 border-amber-200',
    'لغة مختلفة':              'bg-amber-50 text-amber-700 border-amber-200',
    'فئة لا تناسب الهدف':     'bg-rose-50 text-rose-700 border-rose-200',
  }
  const cls = map[label] ?? 'bg-slate-50 text-slate-500 border-slate-200'
  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${cls}`}>
      {label}
    </span>
  )
}

function RecommendedTemplateCard({
  tpl, selected, onClick,
}: { tpl: RecommendedTemplate; selected: boolean; onClick: () => void }) {
  const header = getTemplateHeader(tpl)
  const body   = getTemplateBody(tpl)
  const vars   = extractVariables(body)

  return (
    <button
      onClick={onClick}
      className={`text-start border rounded-xl p-4 transition-all hover:shadow-md w-full relative ${
        tpl.is_best
          ? 'border-emerald-400 bg-emerald-50/30 ring-1 ring-emerald-200'
          : selected
            ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
            : 'border-slate-200 bg-white hover:border-slate-300'
      }`}
    >
      {tpl.is_best && (
        <span className="absolute -top-2 start-3 bg-emerald-500 text-white text-[10px] px-2 py-0.5 rounded-full font-semibold flex items-center gap-1">
          <Star className="w-3 h-3" /> الأفضل لهذه الحملة
        </span>
      )}
      <div className="flex items-center justify-between mb-2 gap-2">
        <p className="text-xs font-semibold text-slate-800 truncate">
          {tpl.display_name_ar || tpl.name.replace(/_/g, ' ')}
        </p>
        <Badge label={tpl.category === 'MARKETING' ? 'تسويق' : tpl.category === 'UTILITY' ? 'خدمة' : 'مصادقة'}
               variant={tpl.category === 'MARKETING' ? 'amber' : 'blue'} />
      </div>
      {header && <p className="text-xs font-medium text-slate-700 mb-1">{header}</p>}
      <p className="text-xs text-slate-500 line-clamp-2 mb-2" dir="rtl">{body}</p>
      <div className="flex flex-wrap gap-1 mb-2">
        {tpl.badges.map(b => <TemplateBadge key={b} label={b} />)}
      </div>
      {vars.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {vars.map(v => (
            <span key={v} className="text-[10px] bg-amber-50 text-amber-700 px-1.5 py-0.5 rounded font-mono">{v}</span>
          ))}
        </div>
      )}
      <p className="text-[10px] text-slate-400 mt-2">{tpl.reason_ar}</p>
    </button>
  )
}

function Step3Template({
  wiz, setWiz, recommendation, loading,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  recommendation: TemplateRecommendation | null
  loading: boolean
}) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400">
        <RefreshCw className="w-5 h-5 animate-spin me-2" /> جارٍ ترتيب القوالب المناسبة…
      </div>
    )
  }
  if (!recommendation || recommendation.templates.length === 0) {
    const next = recommendation?.next_best_template
    const hint =
      recommendation?.suggestion_ar ||
      'لا توجد قوالب معتمدة مناسبة لهذا الهدف بعد.'
    return (
      <div className="py-12 text-center space-y-4">
        <FileText className="w-10 h-10 text-slate-200 mx-auto" />
        <p className="text-sm text-slate-500 max-w-md mx-auto leading-relaxed">{hint}</p>
        {next && (
          <div className="max-w-sm mx-auto bg-amber-50 border border-amber-200 rounded-lg p-3 text-start">
            <p className="text-xs font-semibold text-amber-800 mb-1">
              أقرب قالب لديك
            </p>
            <p className="text-sm text-amber-900 truncate">
              {next.display_name_ar || next.name}
            </p>
            <p className="text-[11px] text-amber-700 mt-1">
              الحالة: {next.status} · اللغة: {next.language || '—'} · الفئة: {next.category || '—'}
            </p>
          </div>
        )}
        <p className="text-xs text-slate-400">
          انتقل إلى{' '}
          <Link to="/templates" className="text-brand-500 underline font-medium">
            قوالب واتساب
          </Link>
          {' '}لإنشاء قالب وإرساله لـ Meta للاعتماد.
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        نحلة فلترت {recommendation.total} قالباً مناسباً ورتّبتها حسب الأنسب لحملتك.
      </p>
      <div className="grid sm:grid-cols-2 gap-3 max-h-[26rem] overflow-y-auto pe-1">
        {recommendation.templates.map(tpl => (
          <RecommendedTemplateCard
            key={tpl.id}
            tpl={tpl}
            selected={String(wiz.template?.id) === String(tpl.id)}
            onClick={() => setWiz(w => ({ ...w, template: tpl, variables: {} }))}
          />
        ))}
      </div>
    </div>
  )
}

// ── Step 4: Variables ─────────────────────────────────────────────────────────

// ALL numbered template variables ({{1}} through {{6}}) are resolved
// automatically by the backend at send time from customer data, store
// settings, cart/order context, and the coupon generator. The merchant
// should NEVER have to type any of them manually.
//
// The backend's `field_values` dict (routers/templates.py) and the
// automation engine's slot resolver (core/automation_engine.py) handle:
//   {{1}} → customer_name    (from Customer record)
//   {{2}} → product_name / store_name / cart_url  (context-dependent)
//   {{3}} → coupon_code / order_amount / discount_pct  (auto-generated)
//   {{4}} → store_name / tracking_url  (from settings)
//   {{5}} → coupon_code  (auto-generated)
//   {{6}} → store_url  (from settings)
const AUTO_RESOLVE_VARS: Record<string, { label: string; source: string }> = {
  '{{1}}': { label: 'اسم العميل',        source: 'من بيانات العميل تلقائياً' },
  '{{2}}': { label: 'رابط السلة / المنتج', source: 'رابط ديناميكي لكل عميل حسب السلة أو الطلب' },
  '{{3}}': { label: 'كود الخصم',          source: 'يُولّد تلقائياً من نظام الكوبونات لكل عميل' },
  '{{4}}': { label: 'اسم المتجر',         source: 'من إعدادات المتجر تلقائياً' },
  '{{5}}': { label: 'كوبون إضافي',        source: 'يُولّد تلقائياً من نظام الكوبونات' },
  '{{6}}': { label: 'رابط المتجر',        source: 'من إعدادات المتجر تلقائياً' },
}

// Fallback for any var beyond {{6}} that might appear in custom templates.
const MANUAL_VAR_HINTS: Record<string, string> = {
  '{{7}}': 'قيمة مخصصة',
  '{{8}}': 'قيمة مخصصة',
}

/** Returns true when ALL template body variables are auto-resolved. */
function allVarsAutoResolved(vars: string[]): boolean {
  return vars.length > 0 && vars.every(v => v in AUTO_RESOLVE_VARS)
}

function Step4Variables({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const body = getTemplateBody(wiz.template!)
  const vars = extractVariables(body)

  const autoVars   = vars.filter(v => v in AUTO_RESOLVE_VARS)
  const manualVars = vars.filter(v => !(v in AUTO_RESOLVE_VARS))

  if (vars.length === 0) {
    return (
      <div className="py-10 text-center text-sm text-slate-400 flex flex-col items-center gap-2">
        <CheckCircle className="w-8 h-8 text-emerald-400" />
        هذا القالب لا يحتوي على متغيرات — يمكنك المتابعة مباشرة.
      </div>
    )
  }

  // All variables are auto-resolved — show confirmation, no inputs
  if (manualVars.length === 0) {
    return (
      <div className="space-y-4">
        <div className="py-6 text-center flex flex-col items-center gap-3">
          <div className="w-12 h-12 rounded-full bg-emerald-50 flex items-center justify-center">
            <Sparkles className="w-6 h-6 text-emerald-500" />
          </div>
          <p className="text-sm font-semibold text-slate-800">جميع المتغيرات تُعبّأ تلقائياً</p>
          <p className="text-xs text-slate-500 max-w-sm">
            سيتم استبدال المتغيرات ببيانات كل عميل تلقائياً عند الإرسال — لا حاجة لإدخال أي شيء.
          </p>
        </div>
        <div className="space-y-2">
          {autoVars.map(v => {
            const info = AUTO_RESOLVE_VARS[v]!
            return (
              <div key={v} className="flex items-center gap-3 bg-emerald-50 border border-emerald-100 rounded-xl px-4 py-3">
                <span className="font-mono text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded text-[11px]">{v}</span>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-emerald-800">{info.label}</p>
                  <p className="text-[11px] text-emerald-600">{info.source}</p>
                </div>
                <CheckCircle className="w-4 h-4 text-emerald-500 ms-auto shrink-0" />
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  // Mixed: some auto, some manual
  return (
    <div className="space-y-4">
      {autoVars.length > 0 && (
        <div className="space-y-2 mb-2">
          <p className="text-xs text-emerald-700 font-medium">✓ يتم تعبئتها تلقائياً لكل عميل:</p>
          {autoVars.map(v => {
            const info = AUTO_RESOLVE_VARS[v]!
            return (
              <div key={v} className="flex items-center gap-2 bg-emerald-50 border border-emerald-100 rounded-lg px-3 py-2">
                <span className="font-mono text-emerald-700 bg-emerald-100 px-1.5 py-0.5 rounded text-[10px]">{v}</span>
                <span className="text-xs text-emerald-700">{info.label}</span>
                <CheckCircle className="w-3.5 h-3.5 text-emerald-500 ms-auto" />
              </div>
            )
          })}
        </div>
      )}
      <p className="text-xs text-slate-500">
        أدخل القيم للمتغيرات التالية — ستُستخدم نفس القيمة لجميع المستلمين في هذه الحملة.
      </p>
      {manualVars.map(v => (
        <div key={v}>
          <label className="label flex items-center gap-2">
            <span className="font-mono text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded text-[11px]">{v}</span>
            <span className="text-slate-600">{MANUAL_VAR_HINTS[v] ?? 'قيمة ديناميكية'}</span>
          </label>
          <input
            className="input text-sm"
            placeholder={`مثال: ${MANUAL_VAR_HINTS[v] ?? v}`}
            value={wiz.variables[v] ?? ''}
            onChange={e => setWiz(w => ({ ...w, variables: { ...w.variables, [v]: e.target.value } }))}
          />
        </div>
      ))}
    </div>
  )
}

// ── Step 5: Preview ───────────────────────────────────────────────────────────

function Step5Preview({ wiz }: { wiz: WizardState }) {
  const body   = renderTemplate(getTemplateBody(wiz.template!),   wiz.variables)
  const header = renderTemplate(getTemplateHeader(wiz.template!), wiz.variables)
  const footer = getTemplateFooter(wiz.template!)
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        هذا ما سيراه العميل عند فتح واتساب. أي قيم متغيرات تركتها فارغة ستظهر بـ <code className="font-mono">{'{{N}}'}</code>.
      </p>
      <div className="grid sm:grid-cols-2 gap-4">
        <WaPreview header={header} body={body} footer={footer} />
        <div className="space-y-2 text-xs">
          <div className="bg-slate-50 rounded-xl px-3 py-2">
            <p className="text-slate-400">القالب</p>
            <p className="font-medium text-slate-800">{wiz.template!.display_name_ar || wiz.template!.name.replace(/_/g, ' ')}</p>
          </div>
          <div className="bg-slate-50 rounded-xl px-3 py-2">
            <p className="text-slate-400">اللغة</p>
            <p className="font-medium text-slate-800">{wiz.template!.language}</p>
          </div>
          <div className="bg-slate-50 rounded-xl px-3 py-2">
            <p className="text-slate-400">الفئة</p>
            <p className="font-medium text-slate-800">{wiz.template!.category}</p>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Step 6: Test send ─────────────────────────────────────────────────────────

function Step6TestSend({
  wiz, setWiz, onTestSend, testLoading,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  onTestSend: () => void
  testLoading: boolean
}) {
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
        <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
        <p className="text-xs text-amber-800">
          سيتم إرسال رسالة اختبار <span className="font-semibold">حقيقية</span> إلى الرقم المُدخل عبر واتساب.
          استخدم رقمك أو رقم زميل لمراجعة الشكل النهائي قبل الإطلاق.
        </p>
      </div>

      <div>
        <label className="label">رقم الجوال للاختبار (صيغة دولية)</label>
        <div className="flex gap-2">
          <input
            className="input text-sm flex-1"
            placeholder="+966 50 000 0000"
            dir="ltr"
            value={wiz.testPhone}
            onChange={e => setWiz(w => ({
              ...w, testPhone: e.target.value,
              testSent: false, testMessage: '', testError: '', testSimulated: false,
            }))}
          />
          <button
            onClick={onTestSend}
            disabled={!wiz.testPhone || testLoading}
            className="btn-primary text-sm shrink-0"
          >
            {testLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            إرسال اختبار
          </button>
        </div>
        <p className="text-[11px] text-slate-400 mt-1">
          القيم الفارغة في المتغيرات سيتم تعبئتها ببيانات تجريبية لأغراض الاختبار فقط.
        </p>
      </div>

      {wiz.testSent && !wiz.testError && (
        <div className={`flex items-start gap-2 border rounded-xl p-3 ${
          wiz.testSimulated
            ? 'text-amber-800 bg-amber-50 border-amber-200'
            : 'text-emerald-700 bg-emerald-50 border-emerald-200'
        }`}>
          {wiz.testSimulated ? <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" /> : <CheckCircle className="w-4 h-4 shrink-0 mt-0.5" />}
          <p className="text-xs">{wiz.testMessage}</p>
        </div>
      )}
      {wiz.testError && (
        <div className="flex items-start gap-2 text-rose-700 bg-rose-50 border border-rose-200 rounded-xl p-3">
          <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <p className="text-xs">{wiz.testError}</p>
        </div>
      )}
    </div>
  )
}

// ── Step 7: Review (campaign name + schedule + coupon) ────────────────────────

function Step7Review({
  wiz, setWiz, segmentMeta, goalMeta,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  segmentMeta: CustomerSegmentMeta | undefined
  goalMeta: CampaignGoal | undefined
}) {
  return (
    <div className="space-y-5">
      <p className="text-xs text-slate-500">
        راجع تفاصيل الحملة وأكمل اسم الحملة، الجدولة، والكوبون قبل الإطلاق.
      </p>

      <div className="grid sm:grid-cols-2 gap-3 text-xs">
        {[
          ['الهدف',   goalMeta?.label_ar ?? '—'],
          ['الشريحة', segmentMeta ? `${segmentMeta.label_ar} (${segmentMeta.customer_count.toLocaleString('ar-SA')} عميل)` : '—'],
          ['القالب',  wiz.template?.display_name_ar || wiz.template?.name.replace(/_/g, ' ') || '—'],
          ['اللغة',   wiz.template?.language ?? '—'],
        ].map(([k, v]) => (
          <div key={k} className="bg-slate-50 rounded-xl px-3 py-2">
            <p className="text-slate-400">{k}</p>
            <p className="font-medium text-slate-800 truncate">{v}</p>
          </div>
        ))}
      </div>

      <div>
        <label className="label">اسم الحملة <span className="text-rose-500">*</span></label>
        <input
          className="input text-sm"
          placeholder="مثال: حملة رمضان 2026"
          value={wiz.campaignName}
          onChange={e => setWiz(w => ({ ...w, campaignName: e.target.value }))}
        />
      </div>

      <div className="space-y-2">
        <label className="label">الجدولة</label>
        <div className="grid sm:grid-cols-3 gap-2">
          {([
            { id: 'immediate' as ScheduleType, label: 'إرسال فوري',     icon: <Send className="w-3.5 h-3.5" /> },
            { id: 'scheduled' as ScheduleType, label: 'وقت محدد',        icon: <Clock className="w-3.5 h-3.5" /> },
            { id: 'delayed'   as ScheduleType, label: 'بعد تأخير',       icon: <RefreshCw className="w-3.5 h-3.5" /> },
          ] as const).map(opt => (
            <button
              key={opt.id}
              onClick={() => setWiz(w => ({ ...w, scheduleType: opt.id }))}
              className={`flex items-center gap-2 border rounded-xl px-3 py-2 text-xs transition-all ${
                wiz.scheduleType === opt.id
                  ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-200 text-brand-700'
                  : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
              }`}
            >
              {opt.icon} {opt.label}
            </button>
          ))}
        </div>
        {wiz.scheduleType === 'scheduled' && (
          <input
            type="datetime-local"
            className="input text-sm mt-2"
            value={wiz.scheduleTime}
            onChange={e => setWiz(w => ({ ...w, scheduleTime: e.target.value }))}
          />
        )}
        {wiz.scheduleType === 'delayed' && (
          <select
            className="input text-sm mt-2"
            value={wiz.delayMinutes}
            onChange={e => setWiz(w => ({ ...w, delayMinutes: Number(e.target.value) }))}
          >
            {[15, 30, 60, 120, 360, 720, 1440].map(m => (
              <option key={m} value={m}>{m < 60 ? `${m} دقيقة` : `${m / 60} ساعة`}</option>
            ))}
          </select>
        )}
      </div>

      {/* Coupon / Discount section — behavior depends on campaign goal */}
      {wiz.goalKey === 'reminder' ? (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 space-y-1">
          <p className="text-xs font-semibold text-emerald-700">الكوبونات والروابط تلقائية بالكامل</p>
          <p className="text-[11px] text-emerald-600 leading-relaxed">
            رابط السلة المتروكة يُرسل تلقائياً لكل عميل حسب سلته، والكوبون يُولّد فريداً لكل عميل من نظام الكوبونات في نحلة.
            لا تحتاج لكتابة أي شيء.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <label className="label mb-0">كوبون خصم تلقائي</label>
            <button
              type="button"
              onClick={() => setWiz(w => ({ ...w, autoCoupon: !w.autoCoupon }))}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors ${
                wiz.autoCoupon ? 'bg-brand-500' : 'bg-slate-300'
              }`}
            >
              <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${
                wiz.autoCoupon ? 'translate-x-[18px]' : 'translate-x-[3px]'
              }`} />
            </button>
          </div>

          {wiz.autoCoupon ? (
            <div className="space-y-2">
              <p className="text-[11px] text-slate-500">
                حدد نسبة الخصم فقط — النظام سيولّد كوبوناً فريداً لكل عميل تلقائياً ويوزعه عند الإرسال.
              </p>
              <div className="flex flex-wrap gap-2">
                {[5, 10, 15, 20, 25, 30].map(pct => (
                  <button
                    key={pct}
                    type="button"
                    onClick={() => setWiz(w => ({ ...w, discountPercent: pct }))}
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-all ${
                      wiz.discountPercent === pct
                        ? 'border-brand-500 bg-brand-50 text-brand-700 ring-1 ring-brand-200'
                        : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
                    }`}
                  >
                    {pct}%
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2 text-[11px] text-emerald-600 bg-emerald-50 border border-emerald-100 rounded-lg p-2">
                <CheckCircle className="w-3.5 h-3.5 shrink-0" />
                <span>سيتم توليد كوبون خصم {wiz.discountPercent}% فريد لكل عميل عند الإرسال</span>
              </div>
            </div>
          ) : (
            <p className="text-[11px] text-slate-400">
              لن يتم إرفاق كوبون مع هذه الحملة.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

// ── Step 8: Launch ────────────────────────────────────────────────────────────

function Step8Launch({
  wiz, segmentMeta, saving, onLaunch, error,
}: {
  wiz: WizardState
  segmentMeta: CustomerSegmentMeta | undefined
  saving: boolean
  onLaunch: () => void
  error: string
}) {
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3 bg-emerald-50 border border-emerald-200 rounded-xl p-4">
        <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-semibold text-emerald-800">جاهز للإطلاق</p>
          <p className="text-xs text-emerald-700 mt-0.5">
            ستُرسل الحملة إلى{' '}
            <span className="font-bold">
              {segmentMeta?.customer_count.toLocaleString('ar-SA') ?? '—'}
            </span>
            {' '}عميل من شريحة <span className="font-semibold">{segmentMeta?.label_ar ?? '—'}</span>.
          </p>
        </div>
      </div>

      <div className="bg-slate-50 rounded-xl p-4 space-y-1 text-xs">
        <div className="flex justify-between"><span className="text-slate-500">اسم الحملة</span><span className="font-semibold text-slate-800">{wiz.campaignName}</span></div>
        <div className="flex justify-between"><span className="text-slate-500">القالب</span><span className="text-slate-800">{wiz.template?.display_name_ar || wiz.template?.name}</span></div>
        <div className="flex justify-between"><span className="text-slate-500">الجدولة</span><span className="text-slate-800">
          {wiz.scheduleType === 'immediate' ? 'فوري'
            : wiz.scheduleType === 'scheduled' ? wiz.scheduleTime
            : `بعد ${wiz.delayMinutes} دقيقة`}
        </span></div>
        {wiz.goalKey === 'reminder' ? (
          <div className="flex justify-between"><span className="text-slate-500">الكوبون</span><span className="text-emerald-600">تلقائي لكل عميل</span></div>
        ) : wiz.autoCoupon ? (
          <div className="flex justify-between"><span className="text-slate-500">الكوبون</span><span className="text-emerald-600">خصم {wiz.discountPercent}% تلقائي لكل عميل</span></div>
        ) : null}
      </div>

      {error && (
        <div className="flex items-start gap-2 text-rose-700 bg-rose-50 border border-rose-200 rounded-xl p-3">
          <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <p className="text-xs">{error}</p>
        </div>
      )}

      <button onClick={onLaunch} disabled={saving} className="btn-primary text-sm w-full justify-center">
        {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
        {saving ? 'جارٍ الإطلاق…' : 'إطلاق الحملة الآن'}
      </button>
    </div>
  )
}

// ── Wizard modal ──────────────────────────────────────────────────────────────

function CampaignWizard({
  onClose, onCreated,
}: { onClose: () => void; onCreated: (c: CampaignRecord) => void }) {
  const [wiz, setWiz] = useState<WizardState>(INITIAL_WIZARD)
  const [goals, setGoals] = useState<CampaignGoal[]>([])
  const [goalsLoading, setGoalsLoading] = useState(true)
  const [segments, setSegments] = useState<CustomerSegmentMeta[]>([])
  const [segmentsLoading, setSegmentsLoading] = useState(true)
  const [recommendation, setRecommendation] = useState<TemplateRecommendation | null>(null)
  const [recoLoading, setRecoLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testLoading, setTestLoading] = useState(false)
  const [error, setError] = useState('')

  // Step 1: load goals on mount.
  useEffect(() => {
    campaignsApi.wizard.goals()
      .then(r => setGoals(r.goals))
      .catch(() => setGoals([]))
      .finally(() => setGoalsLoading(false))
  }, [])

  // Step 2: load segments lazily — first time the merchant lands on
  // step 2. Re-fetching on every step change would waste 13 queries.
  useEffect(() => {
    if (wiz.step >= 2 && segments.length === 0 && segmentsLoading) {
      campaignsApi.wizard.segments()
        .then(r => setSegments(r.segments))
        .catch(() => setSegments([]))
        .finally(() => setSegmentsLoading(false))
    }
  }, [wiz.step, segments.length, segmentsLoading])

  // Step 3: every time (goal, segment) changes, refresh the ranked
  // template list so the merchant always sees the right recommendations.
  useEffect(() => {
    if (wiz.step >= 3 && wiz.goalKey && wiz.segmentKey) {
      setRecoLoading(true)
      campaignsApi.wizard.templates(wiz.goalKey, wiz.segmentKey, 'ar')
        .then(r => setRecommendation(r))
        .catch(() => setRecommendation(null))
        .finally(() => setRecoLoading(false))
    }
  // We intentionally re-run whenever the wizard reaches step 3 OR
  // either selector changes underneath, so backtracking from step 4
  // and re-picking a segment refreshes the template list.
  }, [wiz.step, wiz.goalKey, wiz.segmentKey])

  const segmentMeta = segments.find(s => s.key === wiz.segmentKey)
  const goalMeta    = goals.find(g => g.key === wiz.goalKey)

  const canNext = (): boolean => {
    switch (wiz.step) {
      case 1: return !!wiz.goalKey
      case 2: return !!wiz.segmentKey
      case 3: return !!wiz.template
      case 4: return true   // variables are optional — empty ones use mock data
      case 5: return true
      case 6: return true   // test send is optional but recommended
      case 7: return wiz.campaignName.trim().length > 0
              && (wiz.scheduleType !== 'scheduled' || !!wiz.scheduleTime)
      default: return true
    }
  }

  // Auto-skip step 4 when all template body vars are auto-resolved from
  // CRM data (e.g. {{1}} = customer_name). The merchant shouldn't waste
  // time on a step that says "everything is automatic".
  const shouldSkipStep4 = useCallback((): boolean => {
    if (!wiz.template) return false
    const body = getTemplateBody(wiz.template)
    const vars = extractVariables(body)
    return allVarsAutoResolved(vars)
  }, [wiz.template])

  const next = () => setWiz(w => {
    let target = Math.min(w.step + 1, 8)
    if (target === 4 && shouldSkipStep4()) target = 5
    return { ...w, step: target }
  })
  const prev = () => setWiz(w => {
    let target = Math.max(w.step - 1, 1)
    if (target === 4 && shouldSkipStep4()) target = 3
    return { ...w, step: target }
  })

  const handleTestSend = async () => {
    if (!wiz.testPhone || !wiz.template) return
    setTestLoading(true)
    setWiz(w => ({ ...w, testSent: false, testError: '', testMessage: '', testSimulated: false }))
    try {
      const res = await campaignsApi.wizard.testSend(
        Number(wiz.template.id),
        wiz.testPhone,
        wiz.variables,
      )
      if (res.sent) {
        setWiz(w => ({
          ...w, testSent: true, testSimulated: res.simulated,
          testMessage: res.simulated
            ? (res.error_message || 'تمت محاكاة الإرسال — لا يوجد اتصال واتساب مفعّل.')
            : `تم إرسال رسالة اختبار إلى ${res.to}. تحقّق من واتساب الآن.`,
          testError: '',
        }))
      } else {
        setWiz(w => ({
          ...w, testSent: true, testSimulated: false,
          testMessage: '',
          testError: res.error_message || 'فشل إرسال رسالة الاختبار.',
        }))
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'حدث خطأ غير متوقع.'
      setWiz(w => ({ ...w, testSent: true, testError: msg }))
    } finally {
      setTestLoading(false)
    }
  }

  const handleLaunch = async () => {
    if (!wiz.template || !wiz.goalKey || !wiz.segmentKey) return
    setSaving(true)
    setError('')
    try {
      const payload: CreateCampaignPayload = {
        name: wiz.campaignName,
        // The legacy column accepts the existing enum — translate the
        // new goal key back so historical reports & filters keep working.
        campaign_type: GOAL_TO_LEGACY_TYPE[wiz.goalKey] || 'broadcast',
        template_id: String(wiz.template.id),
        template_name: wiz.template.name,
        template_language: wiz.template.language,
        template_category: wiz.template.category,
        template_body: getTemplateBody(wiz.template),
        template_variables: wiz.variables,
        audience_type: wiz.segmentKey,
        audience_count: segmentMeta?.customer_count ?? 0,
        schedule_type: wiz.scheduleType,
        schedule_time: wiz.scheduleType === 'scheduled' ? wiz.scheduleTime : undefined,
        delay_minutes: wiz.scheduleType === 'delayed' ? wiz.delayMinutes : undefined,
        coupon_code: (wiz.goalKey === 'reminder' || wiz.autoCoupon) ? 'auto' : '',
        discount_percent: (wiz.goalKey === 'reminder' || wiz.autoCoupon) ? wiz.discountPercent : undefined,
        auto_coupon: wiz.goalKey === 'reminder' || wiz.autoCoupon,
      }
      const created = await campaignsApi.create(payload)
      onCreated(created)
      onClose()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'حدث خطأ أثناء إنشاء الحملة. حاول مجدداً.'
      setError(msg)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl flex flex-col max-h-[92vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">إنشاء حملة واتساب ذكية</h2>
            <p className="text-xs text-slate-400 mt-0.5">
              {STEP_LABELS[wiz.step - 1]} — الخطوة {wiz.step} من {STEP_LABELS.length}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step progress bar */}
        <div className="px-6 pt-4">
          <div className="flex gap-1">
            {Array.from({ length: STEP_LABELS.length }, (_, i) => (
              <div
                key={i}
                className={`h-1 flex-1 rounded-full transition-colors ${
                  i + 1 < wiz.step ? 'bg-brand-500' : i + 1 === wiz.step ? 'bg-brand-300' : 'bg-slate-100'
                }`}
              />
            ))}
          </div>
        </div>

        {/* Step content */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {wiz.step === 1 && <Step1Goal     wiz={wiz} setWiz={setWiz} goals={goals} loading={goalsLoading} />}
          {wiz.step === 2 && <Step2Segment  wiz={wiz} setWiz={setWiz} segments={segments} loading={segmentsLoading} goals={goals} />}
          {wiz.step === 3 && <Step3Template wiz={wiz} setWiz={setWiz} recommendation={recommendation} loading={recoLoading} />}
          {wiz.step === 4 && <Step4Variables wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 5 && <Step5Preview  wiz={wiz} />}
          {wiz.step === 6 && <Step6TestSend wiz={wiz} setWiz={setWiz} onTestSend={handleTestSend} testLoading={testLoading} />}
          {wiz.step === 7 && <Step7Review   wiz={wiz} setWiz={setWiz} segmentMeta={segmentMeta} goalMeta={goalMeta} />}
          {wiz.step === 8 && <Step8Launch   wiz={wiz} segmentMeta={segmentMeta} saving={saving} onLaunch={handleLaunch} error={error} />}
        </div>

        {/* Footer nav */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-slate-100">
          <button
            onClick={prev}
            disabled={wiz.step === 1}
            className="btn-ghost text-sm disabled:opacity-30"
          >
            <ChevronRight className="w-4 h-4" /> السابق
          </button>

          {wiz.step < 8 && (
            <button
              onClick={next}
              disabled={!canNext()}
              className="btn-primary text-sm disabled:opacity-40"
            >
              التالي <ChevronLeft className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Debug template link (fetches from API and shows JSON) ─────────────────────

function DebugTemplateLink({ templateId }: { templateId: string }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)

  const handleClick = async () => {
    if (data) { setData(null); return }
    setLoading(true)
    try {
      const result = await campaignsApi.debugTemplate(templateId)
      setData(result)
    } catch { setData({ error: 'فشل جلب بيانات التشخيص' }) }
    finally { setLoading(false) }
  }

  return (
    <div className="mt-2">
      <button
        onClick={handleClick}
        className="text-[10px] text-blue-600 hover:underline flex items-center gap-1"
      >
        🔍 {loading ? 'جارٍ الفحص…' : data ? 'إخفاء التشخيص' : 'فحص القالب والحمولة المُرسلة'}
      </button>
      {data && (
        <pre className="mt-1.5 text-[9px] bg-slate-900 text-green-300 rounded p-2 overflow-x-auto max-h-60 leading-relaxed" dir="ltr">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  )
}

// ── Campaign list row ─────────────────────────────────────────────────────────

function CampaignRow({ campaign, onStatusChange }: { campaign: CampaignRecord; onStatusChange: (id: number, status: string) => void }) {
  const sm = STATUS_META[campaign.status] ?? STATUS_META['draft']
  const tm = TYPE_META[campaign.campaign_type] ?? TYPE_META['broadcast']
  const openRate = campaign.sent_count > 0 ? Math.round((campaign.read_count / campaign.sent_count) * 100) : 0
  const convRate = campaign.sent_count > 0 ? Math.round((campaign.converted_count / campaign.sent_count) * 100) : 0
  const [showErrors, setShowErrors] = useState(false)

  const isFailed = campaign.status === 'failed'
  const hasErrors = (campaign.dispatch_errors?.length ?? 0) > 0
  const failedCount = campaign.failed_count ?? 0

  return (
    <>
      <tr className={`hover:bg-slate-50 transition-colors ${isFailed ? 'bg-red-50/40' : ''}`}>
        <td className="px-5 py-3.5">
          <p className="text-xs font-semibold text-slate-900">{campaign.name}</p>
          <p className="text-[10px] text-slate-400 font-mono mt-0.5">{campaign.template_name?.replace(/_/g, ' ')}</p>
        </td>
        <td className="px-5 py-3.5">
          <span className="flex items-center gap-1.5 text-xs text-slate-600">{tm.icon} {tm.label}</span>
        </td>
        <td className="px-5 py-3.5">
          <Badge label={sm.label} variant={sm.variant} dot />
          {isFailed && hasErrors && (
            <button
              onClick={() => setShowErrors(v => !v)}
              className="flex items-center gap-0.5 text-[10px] text-red-500 hover:text-red-700 mt-1 cursor-pointer"
            >
              <AlertCircle className="w-3 h-3" />
              {showErrors ? 'إخفاء التفاصيل' : 'عرض سبب الفشل'}
            </button>
          )}
          {campaign.status === 'completed' && failedCount > 0 && (
            <button
              onClick={() => setShowErrors(v => !v)}
              className="flex items-center gap-0.5 text-[10px] text-amber-500 hover:text-amber-700 mt-1 cursor-pointer"
            >
              <AlertCircle className="w-3 h-3" />
              {failedCount} فشلت
            </button>
          )}
        </td>
        <td className="px-5 py-3.5 text-xs text-slate-700">{campaign.audience_count.toLocaleString('ar-SA')}</td>
        <td className="px-5 py-3.5">
          <span className="text-xs text-slate-700">{campaign.sent_count.toLocaleString('ar-SA')}</span>
          {failedCount > 0 && (
            <span className="text-[10px] text-red-500 block mt-0.5">
              {failedCount} فشلت
            </span>
          )}
        </td>
        <td className="px-5 py-3.5">
          <span className="text-xs text-slate-700">
            {campaign.sent_count > 0 ? `${campaign.read_count} (${openRate}%)` : '—'}
          </span>
        </td>
        <td className="px-5 py-3.5">
          <span className={`text-xs font-medium ${campaign.converted_count > 0 ? 'text-emerald-600' : 'text-slate-400'}`}>
            {campaign.sent_count > 0 ? `${campaign.converted_count} (${convRate}%)` : '—'}
          </span>
        </td>
        <td className="px-5 py-3.5">
          {campaign.status === 'active' && (
            <button
              onClick={() => onStatusChange(campaign.id, 'paused')}
              className="text-xs text-red-400 hover:text-red-600 transition-colors flex items-center gap-1"
            >
              <XCircle className="w-3.5 h-3.5" /> إيقاف
            </button>
          )}
          {campaign.status === 'paused' && (
            <button
              onClick={() => onStatusChange(campaign.id, 'active')}
              className="text-xs text-brand-500 hover:text-brand-700 transition-colors flex items-center gap-1"
            >
              <Send className="w-3.5 h-3.5" /> استئناف
            </button>
          )}
          {campaign.status === 'draft' && (
            <button
              onClick={() => onStatusChange(campaign.id, 'active')}
              className="text-xs text-brand-500 hover:text-brand-700 transition-colors flex items-center gap-1"
            >
              <Send className="w-3.5 h-3.5" /> إطلاق
            </button>
          )}
          {campaign.status === 'completed' && failedCount === 0 && (
            <span className="text-[10px] text-slate-400">مكتملة</span>
          )}
          {campaign.status === 'failed' && (
            <span className="text-[10px] text-red-500 font-medium">فشلت</span>
          )}
        </td>
      </tr>
      {showErrors && hasErrors && (
        <tr className="bg-red-50/60">
          <td colSpan={8} className="px-6 py-3">
            <div className="rounded-lg bg-red-100/80 border border-red-200 p-3">
              <p className="text-xs font-semibold text-red-700 mb-1.5 flex items-center gap-1">
                <AlertCircle className="w-3.5 h-3.5" />
                تفاصيل فشل الإرسال ({failedCount} من {campaign.audience_count})
              </p>
              <ul className="space-y-1">
                {campaign.dispatch_errors.map((err, i) => (
                  <li key={i} className="text-[11px] text-red-600 font-mono leading-relaxed">
                    • {err}
                  </li>
                ))}
              </ul>
              {campaign.template_id && (
                <DebugTemplateLink templateId={campaign.template_id} />
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

const TABLE_HEADERS = ['الحملة', 'النوع', 'الحالة', 'الجمهور', 'الإرسال', 'معدل القراءة', 'التحويل', '']

export default function Campaigns() {
  const [showWizard, setShowWizard] = useState(false)
  const [campaigns, setCampaigns] = useState<CampaignRecord[]>([])
  const [loading, setLoading] = useState(true)
  const { t } = useLanguage()

  const loadCampaigns = useCallback(() => {
    setLoading(true)
    campaignsApi.list()
      .then(r => setCampaigns(r.campaigns))
      .catch(() => setCampaigns([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { loadCampaigns() }, [loadCampaigns])

  const handleStatusChange = async (id: number, status: string) => {
    try {
      const updated = await campaignsApi.updateStatus(id, status)
      setCampaigns(cs => cs.map(c => c.id === updated.id ? updated : c))
    } catch { /* ignore */ }
  }

  const stats = useMemo(() => {
    const active = campaigns.filter(c => c.status === 'active').length
    const failedCampaigns = campaigns.filter(c => c.status === 'failed').length
    const totalSent = campaigns.reduce((s, c) => s + c.sent_count, 0)
    const totalFailed = campaigns.reduce((s, c) => s + (c.failed_count ?? 0), 0)
    const totalRead = campaigns.reduce((s, c) => s + c.read_count, 0)
    const totalConv = campaigns.reduce((s, c) => s + c.converted_count, 0)
    const openRate = totalSent > 0 ? Math.round((totalRead / totalSent) * 100) : 0
    const convRate = totalSent > 0 ? Math.round((totalConv / totalSent) * 100) : 0
    return { active, failedCampaigns, totalSent, totalFailed, openRate, convRate }
  }, [campaigns])

  return (
    <div className="space-y-6">
      <PageHeader
        title={t(tr => tr.pages.campaigns.title)}
        subtitle="حملات واتساب ذكية مبنية على شرائح نحلة وقوالب Meta المعتمدة"
        action={
          <button onClick={() => setShowWizard(true)} className="btn-primary text-sm">
            <Plus className="w-4 h-4" /> حملة جديدة
          </button>
        }
      />

      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="حملات نشطة" value={stats.active.toString()} icon={Megaphone} />
        <StatCard label="إجمالي المُرسَل" value={`${stats.totalSent.toLocaleString('ar-SA')}${stats.totalFailed > 0 ? ` / ${stats.totalFailed} فشلت` : ''}`} icon={Send} />
        <StatCard label="معدل القراءة" value={`${stats.openRate}%`} icon={BarChart2} />
        <StatCard label="معدل التحويل" value={`${stats.convRate}%`} icon={Smartphone} />
      </div>

      {stats.failedCampaigns > 0 && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-3 flex items-center gap-2">
          <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
          <p className="text-sm text-red-700">
            يوجد <strong>{stats.failedCampaigns}</strong> حملة فشلت في الإرسال.
            اضغط على "عرض سبب الفشل" في العمود لمعرفة التفاصيل.
          </p>
        </div>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-500 text-xs uppercase tracking-wider">
              <tr>
                {TABLE_HEADERS.map(h => (
                  <th key={h} className="px-5 py-3 text-start font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading && (
                <tr><td colSpan={TABLE_HEADERS.length} className="px-5 py-10 text-center text-slate-400">
                  <RefreshCw className="w-5 h-5 animate-spin mx-auto mb-2" /> جارٍ تحميل الحملات…
                </td></tr>
              )}
              {!loading && campaigns.length === 0 && (
                <tr><td colSpan={TABLE_HEADERS.length} className="px-5 py-12 text-center text-slate-400">
                  <Megaphone className="w-10 h-10 text-slate-200 mx-auto mb-3" />
                  <p className="text-sm">لا توجد حملات بعد.</p>
                  <p className="text-xs mt-1">ابدأ بإنشاء أول حملة واتساب لعملائك.</p>
                </td></tr>
              )}
              {!loading && campaigns.map(c => (
                <CampaignRow key={c.id} campaign={c} onStatusChange={handleStatusChange} />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {showWizard && (
        <CampaignWizard
          onClose={() => setShowWizard(false)}
          onCreated={(c) => {
            setCampaigns(cs => [c, ...cs])
          }}
        />
      )}
    </div>
  )
}
