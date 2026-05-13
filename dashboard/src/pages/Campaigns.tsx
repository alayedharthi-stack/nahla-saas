import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  Plus, Send, Users, ShoppingCart, BarChart2, CheckCircle, XCircle,
  Megaphone, ChevronRight, ChevronLeft, Tag, Crown, Zap, Clock,
  Smartphone, AlertCircle, RefreshCw, X, MessageSquare, FileText,
  HandHeart, Repeat, Bell, Settings2, Sparkles, Moon, UserPlus, UserX,
  Calendar, ShoppingBag, TrendingUp, Star, Trash2, CheckSquare, Square,
  Shield, Beaker, ShieldOff, Copy, LifeBuoy, AlertTriangle,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import AdminDirectSendModal from '../components/admin/AdminDirectSendModal'
import MediaEnvModal from '../components/admin/MediaEnvModal'
import { canUseInternalDebug } from '../auth'
import { useLanguage } from '../i18n/context'
import {
  campaignsApi, CampaignRecord, CreateCampaignPayload,
  CampaignGoal, CustomerSegmentMeta, RecommendedTemplate, TemplateRecommendation,
  CampaignProtectionInfo, CampaignDebugSnapshot,
  PreflightStrategyResponse, CampaignWavesResponse, CampaignWaveRow,
  extractVariables, renderTemplate, getTemplateBody, getTemplateHeader, getTemplateFooter,
} from '../api/campaigns'
import { useDashboardPoll } from '../lib/dashboardPolling'

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
  /** Manual segment keys to *exclude* from this campaign. Snapshot
   *  marks these recipients as ``skipped_manual_exclusion`` so the
   *  report shows exactly who was filtered out and why. */
  excludeSegments: string[]
  /** Wave / Batch sending strategy (Meta-aware pacing).
   *
   *  - `immediate` — default; the small-campaign default that
   *    skips all wave UI.
   *  - `adaptive` — Nahla picks `batch_size` + delay from the
   *    current Quality Score / Meta tier (recommended for
   *    large broadcasts).
   *  - `batched`  — merchant supplies their own `batch_size` +
   *    `delayBetweenBatchesSec`. */
  sendStrategy: 'immediate' | 'batched' | 'adaptive'
  batchSize: number
  delayBetweenBatchesSec: number
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
  excludeSegments: [],
  sendStrategy: 'immediate',
  batchSize: 1000,
  delayBetweenBatchesSec: 3600,
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

/**
 * Granular lifecycle labels surfaced on top of the raw ``status``
 * column. The backend computes the lifecycle verb from status +
 * recipient counters (see ``_classify_campaign_lifecycle``), so the
 * UI just renders.
 *
 * IMPORTANT: a campaign whose async dispatch task died silently shows
 * up as ``status='active'`` but ``lifecycle='pending_dispatch'`` — we
 * render the lifecycle label so the merchant doesn't trust a "نشطة"
 * pill on an inert campaign.
 */
const LIFECYCLE_META: Record<string, { label: string; variant: 'green' | 'amber' | 'blue' | 'slate' | 'red' }> = {
  draft:                   { label: 'مسودة',                  variant: 'slate' },
  waiting_scheduler:       { label: 'بانتظار المُجدول',        variant: 'amber' },
  pending_dispatch:        { label: 'ينتظر بدء الإرسال',       variant: 'amber' },
  sending:                 { label: 'جاري الإرسال',            variant: 'green' },
  sent:                    { label: 'تم الإرسال',              variant: 'blue'  },
  partial:                 { label: 'أُرسل جزئياً',             variant: 'amber' },
  // ``partial_minor`` = sent>0 with only minor failures (e.g. some
  // customers don't have WhatsApp). The campaign worked — we just
  // didn't reach 100% — so we render it green, not amber.
  partial_minor:           { label: 'أُرسل بنجاح',              variant: 'blue'  },
  // ``no_whatsapp_recipients`` = sent==0 but every failure is minor.
  // Not the campaign's fault; surface it with a calm slate badge so
  // the merchant doesn't think the platform broke.
  no_whatsapp_recipients:  { label: 'لا يوجد عملاء على واتساب', variant: 'slate' },
  // ``excluded_before_send`` = audience matched > 0 customers but every
  // one was filtered out before we even wrote a send-log row (no phone,
  // opted-out, etc.). Different from ``completed_empty`` (zero
  // audience) — the merchant needs the explicit breakdown.
  excluded_before_send:    { label: 'استبعد كل العملاء قبل الإرسال', variant: 'amber' },
  // ``orphaned_materialized_rows`` = the audience funnel claims rows
  // were created but campaign_send_logs is empty now. Almost always a
  // data inconsistency that needs a manual dispatch-now.
  orphaned_materialized_rows: { label: 'صفوف مفقودة من السجل', variant: 'amber' },
  // ``unknown_status`` = rows exist but their status values aren't
  // recognised (legacy / hand-edited). We surface this distinctly so
  // the merchant doesn't read it as "no recipients".
  unknown_status:          { label: 'حالة إرسال غير معروفة',   variant: 'amber' },
  completed_empty:         { label: 'اكتملت بلا مستلمين',       variant: 'slate' },
  failed:                  { label: 'فشل الإرسال',              variant: 'red'   },
  failed_all:              { label: 'فشل الإرسال للجميع',       variant: 'red'   },
  unknown:                 { label: 'غير معروفة',              variant: 'slate' },
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
        {/* Quick action: target the merchant's internal "test list" —
            customers flipped via the drawer toggle. NOT a Nahla
            segment by design, so it's surfaced as a separate card. */}
        <button
          onClick={() => setWiz(w => ({ ...w, segmentKey: 'test_recipients', template: null, variables: {} }))}
          title="أرسل لعدد محدود من العملاء الذين فعّلت لهم زر «إضافة إلى قائمة اختبار الحملات» داخل بطاقة العميل."
          className={`flex items-start gap-3 border rounded-xl p-3 text-start transition-all ${
            wiz.segmentKey === 'test_recipients'
              ? 'border-emerald-500 bg-emerald-50 ring-2 ring-emerald-200'
              : 'border-emerald-200 bg-emerald-50/40 hover:border-emerald-300'
          }`}
        >
          <span className="p-2 rounded-lg shrink-0 bg-emerald-100 text-emerald-700">
            <Beaker className="w-4 h-4" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <p className="text-sm font-semibold text-emerald-900 truncate">قائمة اختبار الحملات</p>
              <span className="text-[9px] bg-white text-emerald-700 px-1.5 py-0.5 rounded-full font-medium border border-emerald-200 shrink-0">
                داخلي
              </span>
            </div>
            <p className="text-[11px] text-emerald-700/80 leading-snug line-clamp-2">
              أرسل تجريبياً لمجموعة صغيرة قبل الإطلاق على القاعدة كاملة.
            </p>
          </div>
        </button>

        {/* Auto Nahla segments */}
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

        {/* Manual segments — same registry, but targets customers
            with a *manually* set tag rather than the auto classifier.
            Distinct in the audience type as ``manual:<key>`` so the
            dispatcher knows to query `customer_segments_manual`. */}
        {ordered.filter(s => s.key !== 'all').map(s => {
          const audKey = `manual:${s.key}`
          const selected = wiz.segmentKey === audKey
          return (
            <button
              key={audKey}
              onClick={() => setWiz(w => ({ ...w, segmentKey: audKey, template: null, variables: {} }))}
              title={`فقط العملاء الذين قمت بتصنيفهم يدوياً كـ «${s.label_ar}»`}
              className={`flex items-start gap-3 border rounded-xl p-3 text-start transition-all ${
                selected
                  ? 'border-amber-500 bg-amber-50 ring-2 ring-amber-200'
                  : 'border-amber-100 bg-amber-50/30 hover:border-amber-200'
              }`}
            >
              <span className={`p-2 rounded-lg shrink-0 ${
                selected ? 'bg-amber-100 text-amber-700' : 'bg-amber-50 text-amber-600'
              }`}>
                <Tag className="w-4 h-4" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <p className="text-sm font-semibold text-amber-900 truncate">{s.label_ar}</p>
                  <span className="text-[9px] bg-white text-amber-700 px-1.5 py-0.5 rounded-full font-medium border border-amber-200 shrink-0">
                    تصنيف يدوي
                  </span>
                </div>
                <p className="text-[11px] text-amber-700/80 leading-snug line-clamp-2">
                  أرسل فقط للعملاء الذين قمت بتصنيفهم يدوياً.
                </p>
              </div>
            </button>
          )
        })}
      </div>

      {/* Exclude segments — applied AFTER audience resolution. The
          dispatcher writes a `skipped_manual_exclusion` row for each
          excluded recipient so the report shows exactly who was
          dropped and why. */}
      <div className="border-t border-slate-100 pt-3 space-y-2">
        <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
          <ShieldOff className="w-3.5 h-3.5 text-slate-400" />
          استبعد التصنيفات (اختياري)
        </p>
        <p className="text-[11px] text-slate-500">
          أي عميل يحمل أحد هذه التصنيفات يدوياً لن يستلم الحملة، حتى لو كان ضمن الشريحة المختارة.
        </p>
        <div className="flex flex-wrap gap-1.5">
          {segments.filter(s => s.key !== 'all').map(s => {
            const active = wiz.excludeSegments.includes(s.key)
            return (
              <button
                key={`excl-${s.key}`}
                type="button"
                onClick={() => setWiz(w => ({
                  ...w,
                  excludeSegments: active
                    ? w.excludeSegments.filter(k => k !== s.key)
                    : [...w.excludeSegments, s.key],
                }))}
                className={`text-[11px] px-2 py-1 rounded-full border transition-colors ${
                  active
                    ? 'bg-red-50 text-red-700 border-red-200'
                    : 'bg-white text-slate-600 border-slate-200 hover:border-slate-300'
                }`}
              >
                {active ? '✕ ' : '+ '}{s.label_ar}
              </button>
            )
          })}
        </div>
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
  const manual = isManualTemplate(tpl)
  // Prefer the merchant-set display name, then the library's labelled
  // suggestion (always contains "يدوي" / "تلقائي"), and only fall
  // back to the raw template name as a last resort.
  const displayName =
    tpl.display_name_ar ||
    tpl.library_label_ar ||
    tpl.name.replace(/_/g, ' ')

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
          {displayName}
        </p>
        <div className="flex items-center gap-1 shrink-0">
          {/* Mode pill — the single most important signal on the
              card. Orange = merchant types every value; green = Nahla
              fills values from system data. */}
          <span
            className={`inline-flex items-center gap-1 text-[10px] font-bold px-2 py-0.5 rounded-full border ${
              manual
                ? 'bg-orange-50 text-orange-700 border-orange-200'
                : 'bg-emerald-50 text-emerald-700 border-emerald-200'
            }`}
            title={manual
              ? 'قالب يدوي — أنت تكتب الكوبون والرابط بنفسك.'
              : 'قالب تلقائي — نحلة تُعبّئ القيم تلقائياً حسب الإعدادات.'}
          >
            {manual ? '✋ يدوي' : '⚡ تلقائي'}
          </span>
          <Badge label={tpl.category === 'MARKETING' ? 'تسويق' : tpl.category === 'UTILITY' ? 'خدمة' : 'مصادقة'}
                 variant={tpl.category === 'MARKETING' ? 'amber' : 'blue'} />
        </div>
      </div>
      {header && <p className="text-xs font-medium text-slate-700 mb-1">{header}</p>}
      <p className="text-xs text-slate-500 line-clamp-2 mb-2" dir="rtl">{body}</p>
      <div className="flex flex-wrap gap-1 mb-2">
        {tpl.badges
          // Drop the inline mode emoji-badge — the dedicated pill above
          // is louder; keeping both clutters the card.
          .filter(b => b !== '✋ يدوي' && b !== '🟠 يدوي' && b !== '⚡ تلقائي')
          .map(b => <TemplateBadge key={b} label={b} />)}
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
  // Split templates into auto / manual groups so the merchant never
  // confuses an auto template (Nahla fills values) with a manual one
  // (the merchant types values). Both groups stay sorted by the
  // recommender's score so "الأفضل لهذه الحملة" still surfaces inside
  // its own group.
  const autoTpls   = recommendation.templates.filter(t => !isManualTemplate(t))
  const manualTpls = recommendation.templates.filter(t =>  isManualTemplate(t))
  const renderGroup = (tpls: RecommendedTemplate[]) => (
    <div className="grid sm:grid-cols-2 gap-3">
      {tpls.map(tpl => (
        <RecommendedTemplateCard
          key={tpl.id}
          tpl={tpl}
          selected={String(wiz.template?.id) === String(tpl.id)}
          onClick={() => setWiz(w => ({ ...w, template: tpl, variables: {} }))}
        />
      ))}
    </div>
  )
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        نحلة فلترت {recommendation.total} قالباً مناسباً ورتّبتها حسب الأنسب لحملتك.
      </p>

      {/* Quick legend so the merchant understands the badge before
          they even click a card. Stays visible above both groups. */}
      <div className="flex flex-wrap gap-3 text-[11px] bg-slate-50 border border-slate-200 rounded-xl px-3 py-2">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block px-2 py-0.5 rounded-full bg-orange-50 text-orange-700 border border-orange-200 font-bold text-[10px]">
            ✋ يدوي
          </span>
          <span className="text-slate-600">أنت تكتب الكوبون والرابط بنفسك</span>
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 font-bold text-[10px]">
            ⚡ تلقائي
          </span>
          <span className="text-slate-600">نحلة تُعبّئ القيم تلقائياً حسب الإعدادات</span>
        </span>
      </div>

      <div className="max-h-[26rem] overflow-y-auto pe-1 space-y-5">
        {autoTpls.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-[11px] font-bold text-emerald-700 inline-flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5" />
              قوالب تلقائية ({autoTpls.length})
            </h3>
            {renderGroup(autoTpls)}
          </section>
        )}
        {manualTpls.length > 0 && (
          <section className="space-y-2">
            <h3 className="text-[11px] font-bold text-orange-700 inline-flex items-center gap-1.5">
              <FileText className="w-3.5 h-3.5" />
              قوالب يدوية ({manualTpls.length})
            </h3>
            {renderGroup(manualTpls)}
          </section>
        )}
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

// Goals where the merchant is sending a one-off marketing message and
// every input must come from THEM, not from a service binding. The
// wizard treats these as fully manual:
//   - no auto-resolved variables (every {{N}} is a free-text input)
//   - no automatic coupon generation (only an optional manual code)
//   - no service_key / automation_type carried into the campaign row
//
// Today's two manual goals are `broadcast` and `custom`. If we add a
// new manual-style goal in `services.campaign_wizard.goals` (e.g.
// `announcement`), extend this set in lock-step.
const MANUAL_GOAL_KEYS: ReadonlySet<string> = new Set(['broadcast', 'custom'])

function isManualGoal(goalKey: string | null): boolean {
  return !!goalKey && MANUAL_GOAL_KEYS.has(goalKey)
}

/** True when the chosen template itself is a manual library template.
 *  This is independent of the goal — a merchant can pick a manual
 *  template under any goal and the wizard must treat it as fully
 *  manual (no auto-coupon, no service binding, every variable typed
 *  by the merchant). The library labels these explicitly with
 *  ``mode === 'manual'`` and a display label containing "يدوي". */
function isManualTemplate(tpl: { mode?: 'manual' | 'auto' } | null | undefined): boolean {
  return !!tpl && tpl.mode === 'manual'
}

/** Convenience: true if either the goal OR the chosen template forces
 *  manual mode. This is the single predicate every step should
 *  consult — never check ``isManualGoal`` alone. */
function isManualMode(
  goalKey: string | null,
  tpl: { mode?: 'manual' | 'auto' } | null | undefined,
): boolean {
  return isManualGoal(goalKey) || isManualTemplate(tpl)
}

/** Returns true when ALL template body variables are auto-resolved.
 *  Manual goals/templates always force this to false — the merchant
 *  must type every variable themselves so the campaign carries no
 *  service binding. */
function allVarsAutoResolved(
  vars: string[],
  goalKey: string | null = null,
  tpl: { mode?: 'manual' | 'auto' } | null | undefined = null,
): boolean {
  if (isManualMode(goalKey, tpl)) return false
  return vars.length > 0 && vars.every(v => v in AUTO_RESOLVE_VARS)
}

function Step4Variables({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const body = getTemplateBody(wiz.template!)
  const vars = extractVariables(body)

  // Manual mode is forced by EITHER a manual goal (broadcast / custom)
  // OR a manual template (library entries flagged ``mode === 'manual'``).
  // In manual mode we bypass the auto-resolve map so the merchant types
  // every dynamic value themselves — no coupon code, cart URL, or
  // service-bound value is silently inherited.
  const manualMode = isManualMode(wiz.goalKey, wiz.template)
  const autoVars   = manualMode ? [] : vars.filter(v => v in AUTO_RESOLVE_VARS)
  const manualVars = manualMode ? vars : vars.filter(v => !(v in AUTO_RESOLVE_VARS))

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
        {manualMode
          ? 'أدخل القيم يدوياً لكل متغير. هذه الحملة مستقلة تماماً — لن يتم ربطها بأي خدمة أو كوبون تلقائي.'
          : 'أدخل القيم للمتغيرات التالية — ستُستخدم نفس القيمة لجميع المستلمين في هذه الحملة.'}
      </p>
      {manualVars.map(v => {
        const hint = manualMode
          ? AUTO_RESOLVE_VARS[v]?.label ?? MANUAL_VAR_HINTS[v] ?? 'قيمة ديناميكية'
          : MANUAL_VAR_HINTS[v] ?? 'قيمة ديناميكية'
        return (
          <div key={v}>
            <label className="label flex items-center gap-2">
              <span className="font-mono text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded text-[11px]">{v}</span>
              <span className="text-slate-600">{hint}</span>
            </label>
            <input
              className="input text-sm"
              placeholder={`مثال: ${hint}`}
              value={wiz.variables[v] ?? ''}
              onChange={e => setWiz(w => ({ ...w, variables: { ...w.variables, [v]: e.target.value } }))}
            />
          </div>
        )
      })}
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

// ── Wave timeline (used inside the campaign-detail drawer) ──────────────────
//
// Renders the persisted ``campaign_waves`` rows for a wave-mode
// campaign as a vertical timeline. Each wave shows: index of
// total, scheduled time, status pill, planned vs sent counters,
// and a thin progress bar. Auto-refreshes every 15s while at
// least one wave is still ``pending`` or ``dispatching`` so the
// merchant sees progress live without a manual reload.
//
// Read-only — no actions exposed here yet (pause/cancel will
// come in a follow-up once the backend exposes the mutators).

function WavesPanel({ campaignId }: { campaignId: number }) {
  const [data, setData] = useState<CampaignWavesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchWaves = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const r = await campaignsApi.waves(campaignId, signal ? { signal } : undefined)
        setData(r)
        setError(null)
      } catch (e: unknown) {
        if (signal?.aborted) return
        const message = e instanceof Error ? e.message : 'تعذر تحميل الدفعات'
        setError(message)
      } finally {
        setLoading(false)
      }
    },
    [campaignId],
  )

  useEffect(() => {
    void fetchWaves()
  }, [fetchWaves])

  const wavesStillRunning =
    !!data?.waves.some((w) => w.status === 'pending' || w.status === 'dispatching')

  useDashboardPoll({
    pollKey: `GET:/campaigns/${campaignId}/waves`,
    intervalMs: 15_000,
    enabled: wavesStillRunning && data != null,
    leading: false,
    run: async (signal) => fetchWaves(signal),
  })

  if (loading) return null
  if (error) {
    return (
      <div className="mt-3 text-[11px] text-slate-500">{error}</div>
    )
  }
  if (!data || data.send_strategy === 'immediate' || data.waves.length === 0) {
    return null
  }

  const totalPlanned = data.waves.reduce((acc, w) => acc + w.planned_recipients, 0)
  const totalSent = data.waves.reduce((acc, w) => acc + w.sent_count, 0)
  const totalFailed = data.waves.reduce((acc, w) => acc + w.failed_count, 0)

  const statusMeta: Record<
    CampaignWaveRow['status'],
    { label: string; variant: 'green' | 'amber' | 'blue' | 'slate' | 'red' | 'purple' }
  > = {
    pending:     { label: 'بانتظار الإطلاق', variant: 'slate'  },
    dispatching: { label: 'جارٍ الإرسال',    variant: 'blue'   },
    completed:   { label: 'مكتملة',          variant: 'green'  },
    failed:      { label: 'فشلت',           variant: 'red'    },
    paused:      { label: 'موقوفة',          variant: 'amber'  },
    cancelled:   { label: 'ملغية',          variant: 'slate'  },
  }

  return (
    <div className="mt-3 bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div className="px-4 py-3 bg-gradient-to-l from-purple-50 to-white border-b border-slate-200">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <p className="text-sm font-semibold text-slate-800 flex items-center gap-2">
              <Repeat className="w-4 h-4 text-purple-500" />
              جدول الدفعات
            </p>
            <p className="text-[11px] text-slate-500 mt-0.5">
              استراتيجية {data.send_strategy === 'adaptive' ? 'تلقائية' : 'يدوية'}
              {' • '}
              {data.total_waves} دفعة
              {data.batch_size ? ` • ${data.batch_size.toLocaleString('ar-SA')}/دفعة` : ''}
            </p>
          </div>
          <div className="text-[11px] text-slate-600">
            <span className="text-emerald-600 font-semibold">{totalSent.toLocaleString('ar-SA')}</span>
            <span className="text-slate-400 mx-1">/</span>
            <span>{totalPlanned.toLocaleString('ar-SA')}</span>
            {totalFailed > 0 && (
              <span className="text-rose-500 mr-2"> • فشل {totalFailed.toLocaleString('ar-SA')}</span>
            )}
          </div>
        </div>
      </div>

      <div className="divide-y divide-slate-100">
        {data.waves.map((w) => {
          const sm = statusMeta[w.status]
          const pct = w.planned_recipients > 0
            ? Math.min(100, Math.round((w.sent_count / w.planned_recipients) * 100))
            : 0
          const scheduledLabel = w.scheduled_at
            ? new Date(w.scheduled_at).toLocaleString('ar-SA', {
                day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
              })
            : '—'
          return (
            <div key={w.id} className="px-4 py-3">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                  <div className="w-7 h-7 rounded-full bg-purple-100 text-purple-700 flex items-center justify-center text-[11px] font-semibold">
                    {w.wave_index}
                  </div>
                  <div>
                    <p className="text-xs font-medium text-slate-800">
                      دفعة {w.wave_index} من {w.total_waves}
                    </p>
                    <p className="text-[10px] text-slate-500 flex items-center gap-1">
                      <Clock className="w-3 h-3" /> {scheduledLabel}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[10.5px] text-slate-500">
                    {w.sent_count.toLocaleString('ar-SA')} / {w.planned_recipients.toLocaleString('ar-SA')}
                  </span>
                  <Badge label={sm.label} variant={sm.variant} />
                </div>
              </div>
              <div className="mt-2 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all ${
                    w.status === 'failed' ? 'bg-rose-400' :
                    w.status === 'completed' ? 'bg-emerald-400' :
                    w.status === 'dispatching' ? 'bg-blue-400' :
                    'bg-slate-200'
                  }`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}


// ── Send Strategy preview (used inside Step7) ────────────────────────────────
//
// Calls the backend's /campaigns/preflight-strategy endpoint every
// time the merchant changes audience size or strategy. Displays
// what the adaptive engine would pick, plus a sanity check on the
// merchant's explicit choice. Read-only — never persists anything.

function SendStrategyPicker({
  wiz, setWiz, audienceCount,
}: {
  wiz: WizardState
  setWiz: React.Dispatch<React.SetStateAction<WizardState>>
  audienceCount: number
}) {
  const [preview, setPreview] = useState<PreflightStrategyResponse | null>(null)
  const [loading, setLoading] = useState(false)

  // Refresh preview when the strategy, batch size, or audience changes.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    campaignsApi
      .preflightStrategy({
        audience_count: audienceCount,
        proposed_strategy: wiz.sendStrategy,
        proposed_batch_size:
          wiz.sendStrategy === 'batched' ? wiz.batchSize : undefined,
        proposed_delay_between_batches_sec:
          wiz.sendStrategy === 'batched' ? wiz.delayBetweenBatchesSec : undefined,
      })
      .then((r) => { if (!cancelled) setPreview(r) })
      .catch(() => { if (!cancelled) setPreview(null) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [audienceCount, wiz.sendStrategy, wiz.batchSize, wiz.delayBetweenBatchesSec])

  const threshold = preview?.threshold_recipients_for_waves ?? 500
  const tooSmall = audienceCount > 0 && audienceCount < threshold

  const formatDelay = (seconds: number): string => {
    if (seconds <= 0) return 'بدون فاصل'
    if (seconds < 60) return `${seconds} ثانية`
    const minutes = Math.round(seconds / 60)
    if (minutes < 60) return `${minutes} دقيقة`
    const hours = (seconds / 3600).toFixed(1).replace(/\.0$/, '')
    if (Number(hours) < 24) return `${hours} ساعة`
    const days = (seconds / 86400).toFixed(1).replace(/\.0$/, '')
    return `${days} يوم`
  }

  const options: Array<{
    id: WizardState['sendStrategy']
    label: string
    desc: string
    icon: React.ReactNode
  }> = [
    {
      id: 'immediate',
      label: 'إرسال فوري',
      desc: 'دفعة واحدة الآن. مناسب للحملات الصغيرة.',
      icon: <Send className="w-3.5 h-3.5" />,
    },
    {
      id: 'adaptive',
      label: 'تلقائي حسب جودة الرقم',
      desc: 'نحلة تختار حجم الدفعة وفترات الإرسال تلقائياً.',
      icon: <Sparkles className="w-3.5 h-3.5" />,
    },
    {
      id: 'batched',
      label: 'إرسال على دفعات يدوياً',
      desc: 'حدد حجم الدفعة والفاصل الزمني بنفسك.',
      icon: <Repeat className="w-3.5 h-3.5" />,
    },
  ]

  const proposed = preview?.proposed
  const adaptive = preview?.suggested_adaptive

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <label className="label mb-0">استراتيجية الإرسال</label>
        {preview?.current_quality?.nahla_tier && (
          <Badge
            label={`جودة الرقم: ${preview.current_quality.nahla_tier}`}
            variant="blue"
          />
        )}
      </div>

      <div className="grid sm:grid-cols-3 gap-2">
        {options.map((opt) => {
          const disabled = tooSmall && opt.id !== 'immediate'
          const active = wiz.sendStrategy === opt.id
          return (
            <button
              key={opt.id}
              type="button"
              disabled={disabled}
              onClick={() => setWiz((w) => ({ ...w, sendStrategy: opt.id }))}
              className={`flex flex-col items-start gap-1 border rounded-xl px-3 py-2 text-xs text-right transition-all ${
                active
                  ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-200 text-brand-700'
                  : disabled
                    ? 'border-slate-100 bg-slate-50 text-slate-300 cursor-not-allowed'
                    : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300'
              }`}
            >
              <div className="flex items-center gap-1.5 font-medium">
                {opt.icon} {opt.label}
              </div>
              <p className="text-[10.5px] text-slate-500 leading-relaxed">{opt.desc}</p>
            </button>
          )
        })}
      </div>

      {tooSmall && (
        <div className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600">
          <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <p>
            الجمهور الحالي ({audienceCount.toLocaleString('ar-SA')} مستلم) أقل من الحد الأدنى
            لتقسيم الحملات ({threshold.toLocaleString('ar-SA')}). سيتم الإرسال مباشرة بدون دفعات.
          </p>
        </div>
      )}

      {wiz.sendStrategy === 'batched' && !tooSmall && (
        <div className="grid sm:grid-cols-2 gap-2">
          <div>
            <label className="label text-[11px]">حجم الدفعة</label>
            <select
              className="input text-sm"
              value={wiz.batchSize}
              onChange={(e) => setWiz((w) => ({ ...w, batchSize: Number(e.target.value) }))}
            >
              {[100, 250, 500, 1000, 2000, 3000, 5000].map((n) => (
                <option key={n} value={n}>{n.toLocaleString('ar-SA')} مستلم</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label text-[11px]">الفاصل بين الدفعات</label>
            <select
              className="input text-sm"
              value={wiz.delayBetweenBatchesSec}
              onChange={(e) => setWiz((w) => ({ ...w, delayBetweenBatchesSec: Number(e.target.value) }))}
            >
              <option value={900}>15 دقيقة</option>
              <option value={1800}>30 دقيقة</option>
              <option value={3600}>1 ساعة</option>
              <option value={7200}>2 ساعة</option>
              <option value={14400}>4 ساعات</option>
              <option value={21600}>6 ساعات</option>
              <option value={43200}>12 ساعة</option>
              <option value={86400}>24 ساعة</option>
            </select>
          </div>
        </div>
      )}

      {!loading && wiz.sendStrategy !== 'immediate' && !tooSmall && (proposed || adaptive) && (
        <div className="rounded-xl border border-brand-200 bg-brand-50/60 p-3 space-y-1.5">
          <p className="text-xs font-semibold text-brand-700 flex items-center gap-1.5">
            <BarChart2 className="w-3.5 h-3.5" />
            خطة الإرسال
          </p>
          {(() => {
            const p = proposed || {
              total_waves: adaptive!.total_waves,
              batch_size: adaptive!.batch_size,
              delay_between_batches_sec: adaptive!.delay_between_batches_sec,
              reason: adaptive!.rationale,
              strategy: adaptive!.strategy,
              downgraded_to_immediate: false,
            }
            return (
              <>
                <p className="text-[11px] text-slate-700 leading-relaxed">
                  <strong>{p.total_waves.toLocaleString('ar-SA')}</strong> دفعة ×{' '}
                  <strong>{p.batch_size.toLocaleString('ar-SA')}</strong> مستلم،
                  فاصل <strong>{formatDelay(p.delay_between_batches_sec)}</strong>.
                </p>
                <p className="text-[10.5px] text-slate-500 leading-relaxed">{p.reason}</p>
              </>
            )
          })()}
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

      {/* Wave / Batch sending — the merchant picks how the audience
          is paced over time. Only shown for `immediate` schedule;
          scheduled/delayed campaigns currently use the legacy
          single-shot path. */}
      {wiz.scheduleType === 'immediate' && (
        <SendStrategyPicker
          wiz={wiz}
          setWiz={setWiz}
          audienceCount={segmentMeta?.customer_count ?? 0}
        />
      )}

      {/* Coupon / Discount section — behavior depends on campaign goal.
          Three flavours:
            1. `reminder` (cart-recovery, automation-style)  → fully auto.
            2. `broadcast` / `custom`  → manual-only: optional plain code.
            3. everything else  → opt-in auto-coupon toggle. */}
      {wiz.goalKey === 'reminder' ? (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 space-y-1">
          <p className="text-xs font-semibold text-emerald-700">الكوبونات والروابط تلقائية بالكامل</p>
          <p className="text-[11px] text-emerald-600 leading-relaxed">
            رابط السلة المتروكة يُرسل تلقائياً لكل عميل حسب سلته، والكوبون يُولّد فريداً لكل عميل من نظام الكوبونات في نحلة.
            لا تحتاج لكتابة أي شيء.
          </p>
        </div>
      ) : isManualMode(wiz.goalKey, wiz.template) ? (
        <div className="space-y-2">
          <label className="label mb-0">كود خصم يدوي (اختياري)</label>
          <input
            className="input text-sm"
            placeholder="مثال: WELCOME10"
            value={wiz.couponCode}
            onChange={e => setWiz(w => ({
              ...w,
              couponCode: e.target.value,
              autoCoupon: false,   // manual mode: never auto-bind a coupon
            }))}
          />
          <p className="text-[11px] text-slate-500 leading-relaxed">
            {isManualTemplate(wiz.template)
              ? 'القالب الذي اخترته يدوي بطبيعته — التاجر يكتب الكوبون والرابط بنفسه ولا يولّد نظام الكوبونات قيماً تلقائية.'
              : 'هذه الحملة مستقلة. لن يقوم نظام الكوبونات بتوليد كوبون لكل عميل — سيظهر الكود كما كتبتَه (أو يبقى الحقل فارغاً) إذا لم تدخل أي قيمة.'}
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
  wiz, segmentMeta, protection, saving, onLaunch, error,
}: {
  wiz: WizardState
  segmentMeta: CustomerSegmentMeta | undefined
  protection: CampaignProtectionInfo
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

      {/* Anti-duplicate trust card — surfaces the back-end protection
       *  guarantee (idempotent send + N-day frequency cap) right
       *  before the merchant clicks "إطلاق الحملة الآن". This is the
       *  single most important psychological signal before sending
       *  thousands of marketing messages. */}
      <div className="rounded-xl border border-blue-200 bg-blue-50 p-4 space-y-2">
        <div className="flex items-start gap-3">
          <Shield className="w-5 h-5 text-blue-600 shrink-0 mt-0.5" />
          <div className="flex-1 space-y-1.5">
            <p className="text-sm font-semibold text-blue-900">🛡️ حماية ذكية من التكرار</p>
            <p className="text-xs text-blue-800 leading-relaxed">
              اطمئن، نحلة تمنع تكرار إرسال الحملات التسويقية لنفس العميل حتى في حال:
            </p>
            <ul className="text-[11px] text-blue-800 leading-relaxed space-y-0.5 list-none">
              <li>• توقّف الإرسال بسبب خطأ مؤقت</li>
              <li>• انقطاع الاتصال أو إعادة تشغيل الحملة</li>
              <li>• إعادة محاولة الإرسال لاحقاً من نفس الحملة</li>
            </ul>
            <p className="text-[11px] text-blue-700 leading-relaxed pt-1">
              لن نرسل الرسالة لنفس العميل مرة أخرى إذا تم تسجيل النجاح من قبل،
              ولن نرسلها إذا استلم حملة تسويقية أخرى خلال آخر{' '}
              <span className="font-semibold">{protection.frequency_cap_days}</span> يوم.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2 pt-1 border-t border-blue-200/60">
          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-blue-700 bg-white px-2 py-1 rounded-full border border-blue-200">
            <Clock className="w-3 h-3" />
            مدة الحماية: {protection.frequency_cap_days} يوم
          </span>
          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700 bg-white px-2 py-1 rounded-full border border-emerald-200">
            <CheckCircle className="w-3 h-3" />
            استكمال آمن للحملة
          </span>
          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-blue-700 bg-white px-2 py-1 rounded-full border border-blue-200">
            <RefreshCw className="w-3 h-3" />
            إرسال مقاوم للتكرار (idempotent)
          </span>
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
        ) : isManualMode(wiz.goalKey, wiz.template) ? (
          wiz.couponCode.trim() ? (
            <div className="flex justify-between"><span className="text-slate-500">الكوبون</span><span className="text-slate-800 font-mono">{wiz.couponCode.trim()}</span></div>
          ) : (
            <div className="flex justify-between"><span className="text-slate-500">الكوبون</span><span className="text-slate-400">بدون</span></div>
          )
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
  // Stable per-wizard-session idempotency key. The backend dedupes
  // ``POST /campaigns`` on this UUID within a 10-minute window so
  // even if the wizard's POST times out (frontend ``signal timed
  // out`` at 25 s on a large audience) the merchant can safely
  // click "Launch" again — the second POST will return the SAME
  // campaign that's already running, never spawn a second dispatch.
  // Re-generated when the wizard remounts so a brand-new campaign
  // gets a brand-new key.
  const idemKeyRef = useRef<string>(
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `wiz-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  )
  // Anti-spam protection metadata for the launch trust card. Falls
  // back to a sane default (14 days) if the endpoint is unavailable so
  // the merchant never sees a missing badge.
  const [protection, setProtection] = useState<CampaignProtectionInfo>({
    frequency_cap_days: 14,
    idempotent_resend_protected: true,
  })

  // Step 1: load goals on mount.
  useEffect(() => {
    campaignsApi.wizard.goals()
      .then(r => setGoals(r.goals))
      .catch(() => setGoals([]))
      .finally(() => setGoalsLoading(false))

    // Fire the protection-info call alongside goals so the trust card
    // is ready by the time the merchant reaches step 8.
    campaignsApi.protectionInfo()
      .then(setProtection)
      .catch(() => { /* keep the 14-day fallback */ })
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

  // Synthesise meta for the two non-registry audience types
  // (`manual:<key>` + `test_recipients`) so the review screen still
  // shows a meaningful label instead of "—".
  const segmentMeta: CustomerSegmentMeta | undefined = useMemo(() => {
    const key = wiz.segmentKey || ''
    if (!key) return undefined
    if (key === 'test_recipients') {
      return {
        key: 'test_recipients',
        label_ar: 'قائمة اختبار الحملات',
        label_en: 'Campaign test list',
        description_ar: 'مجموعة داخلية صغيرة لاختبار الحملة قبل الإطلاق.',
        criteria_ar: 'كل العملاء الذين فعّلت لهم زر «قائمة اختبار الحملات» داخل بطاقة العميل.',
        icon: 'Beaker',
        natural_goals: [],
        crm_statuses: [],
        rfm_buckets: [],
        customer_count: 0,
      }
    }
    if (key.startsWith('manual:')) {
      const baseKey = key.slice('manual:'.length)
      const base = segments.find(s => s.key === baseKey)
      if (base) {
        return {
          ...base,
          key,
          label_ar: `${base.label_ar} (تصنيف يدوي)`,
          description_ar: 'العملاء الذين قمت بتصنيفهم يدوياً ضمن هذه الفئة.',
        }
      }
    }
    return segments.find(s => s.key === key)
  }, [segments, wiz.segmentKey])
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
  // time on a step that says "everything is automatic". Manual mode
  // (manual goal OR manual template) deliberately disables this skip
  // so every variable goes through the explicit input UI.
  const shouldSkipStep4 = useCallback((): boolean => {
    if (!wiz.template) return false
    if (isManualMode(wiz.goalKey, wiz.template)) return false
    const body = getTemplateBody(wiz.template)
    const vars = extractVariables(body)
    return allVarsAutoResolved(vars, wiz.goalKey, wiz.template)
  }, [wiz.template, wiz.goalKey])

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
      // Manual mode (manual goal OR manual template) MUST NOT carry
      // an auto_coupon flag, a service binding, or the magic "auto"
      // coupon_code — those would re-attach the campaign to the
      // coupon-generator + service-templates pipeline, which is
      // exactly what the merchant opted out of when picking a manual
      // template / manual goal. We send an empty coupon string when
      // the merchant left the manual code blank so the backend column
      // stays NULL.
      const manualMode = isManualMode(wiz.goalKey, wiz.template)
      const wantsAutoCoupon = !manualMode && (wiz.goalKey === 'reminder' || wiz.autoCoupon)
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
        // Stash the wizard-level exclude list under a reserved key
        // so the dispatcher can read it without a new column. Empty
        // arrays are still serialised so a re-launch that *removes*
        // exclusions wins over a previous one that had them.
        template_variables: {
          ...wiz.variables,
          _exclude_segments: wiz.excludeSegments,
        } as Record<string, unknown> as Record<string, string>,
        audience_type: wiz.segmentKey,
        audience_count: segmentMeta?.customer_count ?? 0,
        schedule_type: wiz.scheduleType,
        schedule_time: wiz.scheduleType === 'scheduled' ? wiz.scheduleTime : undefined,
        delay_minutes: wiz.scheduleType === 'delayed' ? wiz.delayMinutes : undefined,
        coupon_code: wantsAutoCoupon
          ? 'auto'
          : (manualMode ? (wiz.couponCode.trim() || '') : ''),
        discount_percent: wantsAutoCoupon ? wiz.discountPercent : undefined,
        auto_coupon: wantsAutoCoupon,
        // Stable per-wizard idempotency key — the backend uses this
        // to dedupe retries within a 10-minute window so a second
        // click on "Launch" after a 25 s ``signal timed out``
        // returns the already-running campaign instead of spawning
        // a second dispatch.
        idempotency_key: idemKeyRef.current,
        // Wave/Batch — the backend silently downgrades to
        // ``immediate`` for small audiences (see
        // ``WAVE_THRESHOLD_RECIPIENTS``), so we always send the
        // merchant's chosen strategy verbatim and trust the
        // server to apply the threshold rule.
        send_strategy: wiz.scheduleType === 'immediate' ? wiz.sendStrategy : 'immediate',
        batch_size: wiz.sendStrategy === 'batched' ? wiz.batchSize : undefined,
        delay_between_batches_sec:
          wiz.sendStrategy === 'batched' ? wiz.delayBetweenBatchesSec : undefined,
      }
      const created = await campaignsApi.create(payload)
      onCreated(created)
      onClose()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'حدث خطأ أثناء إنشاء الحملة. حاول مجدداً.'
      // Treat a frontend timeout as a *soft* error: the campaign was
      // almost certainly created (POST /campaigns returns in <1 s
      // now that dispatch runs on a background thread, but
      // entitlement checks + slow DB still occasionally tip past
      // 25 s). Don't leave the merchant stuck on the launch screen
      // — surface a clear "click again to recover" message. The
      // idempotency key above guarantees the retry will NOT
      // duplicate the dispatch.
      const isTimeout = /انتهت مهلة الطلب|signal timed out|aborted/i.test(msg)
      if (isTimeout) {
        setError(
          'انتهت مهلة الطلب قبل وصول رد الخادم، لكن الحملة على الأرجح '
          + 'تم إنشاؤها وبدأ إرسالها في الخلفية. اضغط «إطلاق الحملة الآن» '
          + 'مرة أخرى — حماية التكرار في الخادم ستعيد نفس الحملة بدلاً '
          + 'من إنشاء حملة جديدة، أو أغلق هذه النافذة وافتح الحملة من '
          + 'القائمة لرؤية حالة الإرسال.'
        )
      } else {
        setError(msg)
      }
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
          {wiz.step === 8 && <Step8Launch   wiz={wiz} segmentMeta={segmentMeta} protection={protection} saving={saving} onLaunch={handleLaunch} error={error} />}
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

// ── Per-customer exclusion drill-down ────────────────────────────────────────
//
// Shown below the diagnostic <pre> when a campaign is in
// ``excluded_before_send`` (or any other state where rows were dropped).
// Renders the first 10 excluded recipients as compact cards with
// coloured field flags so support can spot the pattern at a glance:
//
//   • "all 4 have phone but no normalized_phone"  → import didn't normalise
//   • "all 4 have is_unsubscribed=true"           → bulk opt-out / data bug
//   • "all 4 have has_whatsapp=null"              → NOT a blocker (we'd
//                                                    have tried to send)
//
// Tri-state ``has_whatsapp`` is the key UX detail — null renders
// neutral grey ("غير معروف") so the merchant doesn't blame Nahla for
// excluding people we'd actually have tried to reach via Meta.

// ── Provider-side billing/account block banner ─────────────────────
// Rendered above the diagnostic dump when the campaign hit a Meta /
// 360dialog provider restriction. The merchant cannot fix this from
// the dashboard — the workflow is:
//   1. Show the rose banner with the fixed Arabic copy
//      ("مشكلة من مزود واتساب أو الدفع — تواصل مع 360dialog").
//   2. Hide the auto-retry CTA (handled in the caller via the
//      ``providerBlocked`` flag — retrying just produces the same
//      restriction and burns attempts).
//   3. Surface the "نسخ تقرير الدعم" button which calls
//      ``GET /campaigns/{id}/support-bundle`` and copies the full
//      JSON to the clipboard, ready for a 360dialog ticket.
function ProviderBlockBanner({
  block,
  onCopyBundle,
  bundleStatus,
}: {
  block: NonNullable<CampaignDebugSnapshot['provider_block']>
  onCopyBundle: () => void
  bundleStatus: 'idle' | 'loading' | 'copied' | 'error'
}) {
  const bundleLabel =
    bundleStatus === 'loading' ? 'جاري التحضير…'
    : bundleStatus === 'copied'  ? '✓ تم النسخ — الصق في تذكرة 360dialog'
    : bundleStatus === 'error'   ? 'تعذر النسخ — حاول مجدداً'
    : 'نسخ تقرير الدعم'

  return (
    <div className="mb-3 rounded-xl border-2 border-rose-200 bg-rose-50/80 p-4 shadow-sm">
      <div className="flex items-start gap-3">
        <div className="shrink-0 rounded-full bg-rose-100 p-2">
          <AlertTriangle className="w-5 h-5 text-rose-600" />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-bold text-rose-900 leading-snug">
            مشكلة من مزود واتساب أو الدفع — تواصل مع 360dialog
          </p>
          <p className="mt-1 text-xs text-rose-800 leading-relaxed">
            {block.support_message_ar ||
              'هذه الحالة من جانب المزود (Meta / 360dialog) ولا يمكن استعادتها من جانبنا. تم إيقاف إعادة الإرسال التلقائي لهذه الحملة.'}
          </p>

          {/* Per-error breakdown — surfaces the exact reasons so the
              support engineer has the context up front. */}
          {block.error_keys && block.error_keys.length > 0 && (
            <ul className="mt-2 space-y-1">
              {block.error_keys.map(k => (
                <li
                  key={k.key}
                  className="flex items-center gap-2 text-[11px] text-rose-900"
                >
                  <span className="inline-flex items-center rounded-md bg-rose-100 px-1.5 py-0.5 font-mono text-[10px] text-rose-700 border border-rose-200">
                    {k.key}
                  </span>
                  <span className="flex-1">{k.label_ar}</span>
                  <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-rose-700 border border-rose-200">
                    {k.count}
                  </span>
                </li>
              ))}
            </ul>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onCopyBundle}
              disabled={bundleStatus === 'loading'}
              className={[
                'inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-semibold shadow-sm transition-colors',
                bundleStatus === 'copied'
                  ? 'bg-emerald-600 text-white border-emerald-600 hover:bg-emerald-700'
                  : bundleStatus === 'error'
                  ? 'bg-amber-100 text-amber-900 border-amber-300 hover:bg-amber-200'
                  : 'bg-rose-600 text-white border-rose-600 hover:bg-rose-700 disabled:opacity-50 disabled:cursor-wait',
              ].join(' ')}
            >
              <LifeBuoy className="w-3.5 h-3.5" />
              {bundleLabel}
            </button>
            <span className="text-[10px] text-rose-700/80">
              التقرير يتضمن phone_number_id واسم القالب وعيّنة من ردّ Meta الخام.
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}


function ExcludedCustomersDrillDown({
  rows,
}: {
  rows: NonNullable<CampaignDebugSnapshot['sample_excluded_before_send']>
}) {
  // Map reason_key → small badge colour. ``unknown`` is amber (data
  // smell — we should know why) rather than red.
  const reasonVariant: Record<string, string> = {
    no_phone:              'bg-rose-100 text-rose-700 border-rose-200',
    phone_not_normalized:  'bg-amber-100 text-amber-700 border-amber-200',
    unsubscribed:          'bg-slate-200 text-slate-700 border-slate-300',
    pending_unsubscribe:   'bg-slate-100 text-slate-600 border-slate-200',
    marketing_opt_out:     'bg-violet-100 text-violet-700 border-violet-200',
    no_whatsapp_confirmed: 'bg-orange-100 text-orange-700 border-orange-200',
    unknown:               'bg-amber-100 text-amber-700 border-amber-200',
  }
  return (
    <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-[11px] font-semibold text-slate-700 mb-2">
        تفاصيل المستبعدين (أول {rows.length}):
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {rows.map(row => (
          <div
            key={row.customer_id}
            className="border border-slate-200 rounded-md p-2 bg-slate-50/40"
          >
            <div className="flex items-center justify-between gap-2 mb-1.5">
              <p className="text-[11px] font-semibold text-slate-800 truncate">
                {row.name} <span className="text-slate-400 font-normal">#{row.customer_id}</span>
              </p>
              <span
                className={`text-[9.5px] font-semibold px-1.5 py-0.5 rounded border ${
                  reasonVariant[row.reason_key] || reasonVariant.unknown
                }`}
              >
                {row.reason_label_ar}
              </span>
            </div>
            <p className="text-[10px] text-slate-500 font-mono mb-1.5" dir="ltr">
              {row.phone_masked || '— بدون رقم —'}
            </p>
            <div className="flex flex-wrap gap-1">
              <FieldFlag label="رقم" value={row.fields.has_phone} />
              <FieldFlag label="مُطبَّع" value={row.fields.phone_normalized_valid} />
              <FieldFlag
                label="ألغى الاشتراك"
                value={row.fields.is_unsubscribed}
                invert
              />
              <FieldFlag
                label="قيد الإلغاء"
                value={row.fields.pending_unsubscribe}
                invert
              />
              <FieldFlag
                label="إلغاء تسويق"
                value={row.fields.marketing_opt_out}
                invert
              />
              {/* Tri-state has_whatsapp — null renders neutral grey
                  (we haven't been told yet, so we'd have TRIED to send
                  via Meta and let it tell us). Only explicit false
                  blocks the send. */}
              <FieldFlag
                label="واتساب"
                value={row.fields.has_whatsapp}
                triState
              />
            </div>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-slate-500 mt-2 leading-relaxed">
        ملاحظة: <strong>واتساب=غير معروف</strong> ليست سبب استبعاد —
        نُرسل عبر Meta وهو من يؤكّد. فقط <strong>واتساب=لا</strong>
        المؤكَّد من فشل سابق هو الحاجز.
      </p>
    </div>
  )
}

function FieldFlag({
  label, value, invert, triState,
}: {
  label: string
  value: boolean | null
  /** When true, ``value=true`` is the BAD state (red). Otherwise
   *  ``value=true`` is the GOOD state (green). */
  invert?: boolean
  /** Three-valued: null renders neutral grey ("غير معروف"). */
  triState?: boolean
}) {
  let color: string
  let text: string
  if (triState && value === null) {
    color = 'bg-slate-100 text-slate-500 border-slate-200'
    text = 'غير معروف'
  } else if (value === true) {
    color = invert
      ? 'bg-rose-100 text-rose-700 border-rose-200'
      : 'bg-emerald-100 text-emerald-700 border-emerald-200'
    text = 'نعم'
  } else {
    color = invert
      ? 'bg-emerald-100 text-emerald-700 border-emerald-200'
      : 'bg-rose-100 text-rose-700 border-rose-200'
    text = 'لا'
  }
  return (
    <span className={`text-[9.5px] px-1.5 py-0.5 rounded border ${color}`}>
      {label}: {text}
    </span>
  )
}

// ── Unknown-Meta error fingerprint panels ────────────────────────────────────
//
// Shown only when at least one failed recipient was classified as
// ``unknown`` (the classifier hasn't fingerprinted the Meta code yet).
// Renders the parsed code/subcode/type/message prominently — no more
// "خطأ غير معروف" with nothing underneath — and copy-to-clipboard the
// full technical line so support can add the code to
// ``backend/services/meta_errors._CODE_MAP``.

function UnknownMetaErrorsPanel({
  rows,
}: {
  rows: NonNullable<CampaignDebugSnapshot['sample_failed']>
}) {
  return (
    <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50/60 p-3">
      <p className="text-[11px] font-semibold text-amber-800 mb-2 flex items-center gap-1">
        <AlertCircle className="w-3.5 h-3.5" />
        Meta أعادت خطأ غير مصنّف بعد — افحص الرد الخام أدناه ({rows.length})
      </p>
      <p className="text-[10px] text-amber-700 leading-relaxed mb-2">
        كل عينة فشل صنّفها النظام كـ «خطأ غير مصنّف». افحص قسم
        «العيّنات الخام من Meta» في الأسفل — يحوي ردّ Meta الكامل لكل
        محاولة (request + response + code + subcode + type + message).
        أرسل لقطة منها للدعم لإضافة الكود إلى المُصنِّف.
      </p>
      <div className="space-y-2">
        {rows.map((r, i) => (
          <div
            key={i}
            className="border border-amber-200 rounded-md p-2 bg-white"
          >
            <div className="flex items-center justify-between gap-2 mb-1">
              <span className="text-[10px] text-slate-500 font-mono" dir="ltr">
                {r.phone}
              </span>
              <button
                type="button"
                onClick={() =>
                  navigator.clipboard
                    .writeText(r.error_technical || '')
                    .catch(() => {})
                }
                className="text-[10px] text-amber-700 hover:text-amber-900 flex items-center gap-1"
                title="نسخ السطر التقني الكامل"
              >
                <Copy className="w-3 h-3" /> نسخ
              </button>
            </div>
            <div className="grid grid-cols-2 gap-1 text-[10px] mb-1.5">
              <MetaField label="code"    value={r.meta_error_code} />
              <MetaField label="subcode" value={r.meta_error_subcode} />
              <MetaField label="type"    value={r.meta_error_type} />
            </div>
            <p
              className="text-[10.5px] text-slate-700 leading-relaxed border-t border-amber-100 pt-1.5"
              dir="ltr"
            >
              {r.meta_error_message || r.error_technical || '—'}
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}

function MetaField({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-slate-500 font-mono">{label}:</span>
      <span
        className={`font-mono px-1 rounded ${
          value
            ? 'bg-slate-100 text-slate-800'
            : 'bg-slate-50 text-slate-400'
        }`}
        dir="ltr"
      >
        {value || '—'}
      </span>
    </div>
  )
}

/** Expandable raw Meta request / response payloads. Lets support
 *  drill into the EXACT bytes Meta saw — template name, language
 *  code, component count, parameter list — to debug ``unknown``
 *  errors caused by template-name mismatch, language-code mismatch,
 *  parameter-count mismatch, or sandbox/test-number restrictions.
 */
function RawMetaSamplesPanel({
  samples,
}: {
  samples: NonNullable<CampaignDebugSnapshot['raw_meta_error_samples']>
}) {
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  const copyAll = () => {
    const payload = JSON.stringify(samples, null, 2)
    navigator.clipboard.writeText(payload).catch(() => {})
  }
  return (
    <div className="mt-3 rounded-lg border border-slate-300 bg-slate-50/70 p-3">
      <div className="flex items-center justify-between mb-2 gap-2">
        <p className="text-[11px] font-semibold text-slate-700 flex items-center gap-1">
          🔬 العيّنات الخام من Meta ({samples.length})
        </p>
        <button
          type="button"
          onClick={copyAll}
          className="text-[10px] text-slate-600 hover:text-slate-900 flex items-center gap-1 border border-slate-200 rounded px-1.5 py-0.5 bg-white"
          title="نسخ كل العيّنات كـ JSON"
        >
          <Copy className="w-3 h-3" /> نسخ الكل
        </button>
      </div>
      <p className="text-[10px] text-slate-600 leading-relaxed mb-2">
        كل عينة تحوي الطلب والاستجابة الكاملين — مفيد للتأكد من
        ``template.name`` و``language.code`` وعدد المتغيّرات قبل
        تقديم تذكرة للدعم. عند اختلاف هيكل القالب عن البايلود
        تظهر شارة "اختلاف القالب" مع التفاصيل.
      </p>
      <div className="space-y-2">
        {samples.map((s, i) => {
          const open = openIdx === i
          const diff = s.component_diff || []
          const summary = s.template_summary || {}
          const technical = `[code=${s.meta_error_code ?? ''} subcode=${
            s.meta_error_subcode ?? ''
          } type=${s.meta_error_type ?? ''}] ${s.meta_error_message ?? ''}`
          const copyOne = () => {
            navigator.clipboard.writeText(technical).catch(() => {})
          }
          return (
            <div
              key={i}
              className="border border-slate-200 rounded-md bg-white"
            >
              <button
                type="button"
                onClick={() => setOpenIdx(open ? null : i)}
                className="w-full flex items-center justify-between gap-2 px-2 py-1.5 text-[10.5px] hover:bg-slate-50"
              >
                <span className="text-slate-700 font-mono flex items-center gap-2" dir="ltr">
                  {s.ts.slice(0, 19).replace('T', ' ')} • {s.recipient}
                  {diff.length > 0 && (
                    <span className="bg-rose-100 text-rose-700 border border-rose-200 rounded px-1.5 py-0.5 font-sans" dir="rtl">
                      اختلاف القالب ({diff.length})
                    </span>
                  )}
                </span>
                <span className="flex items-center gap-2">
                  <span
                    className={`font-mono px-1.5 py-0.5 rounded border ${
                      s.classified_key === 'unknown'
                        ? 'bg-amber-100 text-amber-700 border-amber-200'
                        : 'bg-slate-100 text-slate-700 border-slate-200'
                    }`}
                  >
                    {s.classified_key}
                  </span>
                  <span className="text-slate-400">{open ? '▲' : '▼'}</span>
                </span>
              </button>
              {open && (
                <div className="border-t border-slate-200 p-2 space-y-2">
                  <div className="flex items-center justify-end">
                    <button
                      type="button"
                      onClick={copyOne}
                      className="text-[10px] text-slate-600 hover:text-slate-900 flex items-center gap-1 border border-slate-200 rounded px-1.5 py-0.5 bg-slate-50"
                      title="نسخ السطر التقني الكامل لهذه العينة"
                    >
                      <Copy className="w-3 h-3" /> Copy raw Meta error
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-1 text-[10px]">
                    <MetaField label="code"        value={s.meta_error_code} />
                    <MetaField label="subcode"     value={s.meta_error_subcode} />
                    <MetaField label="type"        value={s.meta_error_type} />
                    <MetaField label="fbtrace_id"  value={s.fbtrace_id} />
                  </div>
                  <p
                    className="text-[10.5px] text-slate-700 leading-relaxed"
                    dir="ltr"
                  >
                    {s.meta_error_message || '—'}
                  </p>
                  {summary && (summary.template_name || summary.language) && (
                    <div className="grid grid-cols-2 gap-1 text-[10px] bg-slate-50 border border-slate-200 rounded p-1.5">
                      <MetaField label="template"     value={summary.template_name ?? null} />
                      <MetaField label="language"     value={summary.language ?? null} />
                      <MetaField label="category"     value={summary.category ?? null} />
                      <MetaField label="components"   value={String(summary.component_count ?? '')} />
                      <MetaField label="header_params" value={String(summary.header_params ?? '')} />
                      <MetaField label="body_params"  value={String(summary.body_params ?? '')} />
                      <MetaField label="button_params" value={String(summary.button_params ?? '')} />
                      <MetaField label="media"        value={summary.media ? 'yes' : 'no'} />
                    </div>
                  )}
                  {diff.length > 0 && (
                    <div className="rounded-md border border-rose-200 bg-rose-50/70 p-2">
                      <p className="text-[10.5px] font-semibold text-rose-800 mb-1">
                        🧩 اختلاف بين القالب المعتمد والبايلود المُرسَل
                      </p>
                      <ul className="space-y-0.5">
                        {diff.map((d, k) => (
                          <li
                            key={k}
                            className="text-[10.5px] text-rose-700 flex items-center gap-1.5"
                          >
                            <span className="bg-rose-100 border border-rose-200 rounded px-1 py-0.5 font-mono text-[9.5px]" dir="ltr">
                              {d.component}{d.index != null ? `#${d.index}` : ''}
                            </span>
                            <span>{d.message_ar}</span>
                            <span className="text-[9.5px] text-rose-500 font-mono" dir="ltr">
                              (expected={String(d.expected)}, sent={String(d.sent)})
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div>
                    <p className="text-[10px] font-semibold text-slate-600 mb-1">
                      Request payload (masked):
                    </p>
                    <pre
                      className="text-[9.5px] bg-slate-900 text-emerald-200 rounded p-2 overflow-x-auto max-h-56 leading-relaxed whitespace-pre-wrap break-words"
                      dir="ltr"
                    >
                      {JSON.stringify(s.request_payload, null, 2)}
                    </pre>
                  </div>
                  <div>
                    <p className="text-[10px] font-semibold text-slate-600 mb-1">
                      Response payload (raw):
                    </p>
                    <pre
                      className="text-[9.5px] bg-slate-900 text-rose-200 rounded p-2 overflow-x-auto max-h-56 leading-relaxed whitespace-pre-wrap break-words"
                      dir="ltr"
                    >
                      {JSON.stringify(s.response_payload, null, 2)}
                    </pre>
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Retry-health (circuit breaker + watchdog signals) ───────────────────────

/** Panel that exposes the dispatcher's runaway-protection state.
 *
 *  Surfaces:
 *    * ``retry_storm_detected`` — at least one row crossed
 *      ATTEMPT_CIRCUIT_BREAKER and was force-terminated.
 *    * ``max_attempt_count``    — the highest attempt count on any row.
 *    * ``rows_at_attempt_ceiling`` — rows that hit MAX_SEND_ATTEMPTS.
 *    * ``zombie_sending_count`` — rows stuck in ``sending`` past the
 *      watchdog timeout (will be revived next dispatch).
 *
 *  When everything is healthy this panel still renders (with a green
 *  reassurance banner) so the merchant knows the protections are
 *  active rather than missing. */
/** Delivery-stage breakdown panel — renders directly under the
 *  text diagnostic dump on the campaigns debug pane.
 *
 *  Surfaces five buckets sourced from CampaignSendLog.{delivered,read,
 *  failed}_at timestamps which are populated by the WhatsApp status
 *  webhook. Together they answer the merchant's #1 question:
 *  "the campaign says 4 sent but did anyone actually receive it?"
 *
 *  Three failure modes are colour-coded distinct from "delivered/read":
 *  - failed_after_accept       — amber, with a hint about Meta status
 *  - unknown_delivery          — slate, no judgement (could still arrive)
 *  - missing_provider_message_id — rose, a hard CORRUPTION warning.
 *
 *  The optional sample list under the pills shows the FIRST FEW
 *  recipient phones with their delivery stage so support can verify
 *  individual rows without running a separate query. */
function DeliverySummaryPanel({
  summary,
  sample,
}: {
  summary: NonNullable<CampaignDebugSnapshot['delivery_summary']>
  sample:  CampaignDebugSnapshot['sample_sent'] | null
}) {
  const total = summary.accepted_by_provider
  if (total === 0 && summary.missing_provider_message_id === 0) {
    // No sent rows at all — nothing to show; the regular counters
    // already explain why ("audience zero", "all skipped", etc.).
    return null
  }

  const pill = (
    label: string,
    value: number,
    tone: 'slate' | 'emerald' | 'sky' | 'amber' | 'rose',
  ) => {
    const cls = {
      slate:   'bg-slate-100 text-slate-700 border-slate-200',
      emerald: 'bg-emerald-50 text-emerald-800 border-emerald-200',
      sky:     'bg-sky-50 text-sky-800 border-sky-200',
      amber:   'bg-amber-50 text-amber-800 border-amber-200',
      rose:    'bg-rose-50 text-rose-800 border-rose-200',
    }[tone]
    return (
      <div className={`rounded-lg border px-2.5 py-1.5 text-[11.5px] font-medium ${cls}`}>
        <div className="text-[10px] opacity-70 mb-0.5">{label}</div>
        <div className="text-base font-bold leading-tight">{value}</div>
      </div>
    )
  }

  const stageLabel: Record<string, string> = {
    accepted_by_provider: 'قبلتها Meta',
    delivered:            'وصلت للعميل',
    read:                 'قرأها العميل',
    failed_after_accept:  'فشلت بعد القبول',
  }
  const stageTone: Record<string, string> = {
    accepted_by_provider: 'text-slate-600',
    delivered:            'text-sky-700',
    read:                 'text-emerald-700',
    failed_after_accept:  'text-amber-700',
  }

  return (
    <div className="mt-3 rounded-lg bg-white border border-slate-200 p-3">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-[12px] font-bold text-slate-800">
          📬 توصيل الحملة (من Meta status webhook)
        </h4>
        <span className="text-[10.5px] text-slate-500">
          {summary.delivered}/{total} وصلت
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        {pill('قبلتها Meta',         summary.accepted_by_provider, 'slate')}
        {pill('وصلت للعميل',         summary.delivered,            'sky')}
        {pill('قرأها العميل',        summary.read,                 'emerald')}
        {pill('فشلت بعد القبول',     summary.failed_after_accept,  'amber')}
        {pill('لم تصل بعد',          summary.unknown_delivery,     'slate')}
      </div>
      {summary.missing_provider_message_id > 0 && (
        <div className="mt-2 rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-[11.5px] px-3 py-2">
          ⛔ {summary.missing_provider_message_id} صف مُعلَّم
          {' '}"تم الإرسال" بدون provider_message_id — لا يجوز اعتبارها
          مُرسلة فعلاً. راجع لوج الإرسال.
        </div>
      )}
      {sample && sample.length > 0 && (
        <div className="mt-2 border-t border-slate-100 pt-2">
          <div className="text-[11px] text-slate-500 mb-1">
            عينة آخر إرسالات:
          </div>
          <ul className="space-y-0.5">
            {sample.slice(0, 5).map((row, idx) => (
              <li
                key={idx}
                className="text-[11px] flex items-center justify-between font-mono"
              >
                <span className="text-slate-600">{row.phone}</span>
                <span className={stageTone[row.delivery_stage] || 'text-slate-600'}>
                  {stageLabel[row.delivery_stage] || row.delivery_stage}
                </span>
                {!row.has_provider_message_id && (
                  <span className="text-rose-700 text-[10px] ms-1">
                    (بدون wamid)
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}


function RetryHealthPanel({
  health,
}: {
  health: NonNullable<CampaignDebugSnapshot['retry_health']>
}) {
  const storm = health.retry_storm_detected
  const atCeiling = health.rows_at_attempt_ceiling > 0
  const zombies = health.zombie_sending_count > 0
  const tone =
    storm ? 'border-rose-300 bg-rose-50/70'
    : atCeiling || zombies ? 'border-amber-300 bg-amber-50/70'
    : 'border-emerald-300 bg-emerald-50/70'
  const titleTone =
    storm ? 'text-rose-800'
    : atCeiling || zombies ? 'text-amber-800'
    : 'text-emerald-800'
  const icon = storm ? '🚨' : atCeiling || zombies ? '⚠️' : '🛡️'
  const headline =
    storm
      ? `تم رصد retry storm — حدّ المحاولات تجاوز ${health.attempt_circuit_breaker}`
      : atCeiling
      ? `${health.rows_at_attempt_ceiling} صف وصل إلى الحد الأقصى للمحاولات`
      : zombies
      ? `${health.zombie_sending_count} صف عالق في sending`
      : 'حماية المحاولات نشطة وكل الصفوف ضمن الحدود الآمنة'
  return (
    <div className={`mt-3 rounded-lg border ${tone} p-3`}>
      <p className={`text-[11px] font-semibold ${titleTone} mb-2 flex items-center gap-1`}>
        {icon} صحة المحاولات (Retry Health)
      </p>
      <p className={`text-[10.5px] mb-2 ${titleTone}`}>{headline}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5 text-[10.5px]">
        <RetryMetric
          label="أقصى محاولات"
          value={health.max_attempt_count}
          tone={
            health.max_attempt_count > health.attempt_circuit_breaker ? 'rose'
            : health.max_attempt_count >= health.max_send_attempts ? 'amber'
            : 'slate'
          }
        />
        <RetryMetric
          label="صفوف بلغت الحد"
          value={health.rows_at_attempt_ceiling}
          tone={health.rows_at_attempt_ceiling > 0 ? 'amber' : 'slate'}
        />
        <RetryMetric
          label="صفوف عالقة (sending)"
          value={health.zombie_sending_count}
          tone={health.zombie_sending_count > 0 ? 'amber' : 'slate'}
        />
        <RetryMetric
          label="MAX_SEND_ATTEMPTS"
          value={health.max_send_attempts}
          tone="slate"
        />
      </div>
      {storm && (
        <p className="mt-2 text-[10.5px] text-rose-700 leading-relaxed">
          تم إيقاف الصفوف المتأثرة تلقائياً (error_code=retry_storm). راجع
          لوغات Railway للبحث عن{' '}
          <code className="font-mono">campaign_send_retry_storm</code>.
        </p>
      )}
      {!storm && zombies && (
        <p className="mt-2 text-[10.5px] text-amber-700 leading-relaxed">
          ستعيدها watchdog إلى queued تلقائياً عند إطلاق الإرسال التالي
          (timeout = {health.sending_timeout_seconds}s).
        </p>
      )}
    </div>
  )
}

function RetryMetric({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: 'slate' | 'amber' | 'rose'
}) {
  const toneClasses: Record<typeof tone, string> = {
    slate: 'bg-white border-slate-200 text-slate-700',
    amber: 'bg-amber-100 border-amber-200 text-amber-800',
    rose:  'bg-rose-100 border-rose-200 text-rose-800',
  }
  return (
    <div className={`border rounded px-2 py-1.5 ${toneClasses[tone]}`}>
      <div className="text-[10px] opacity-80">{label}</div>
      <div className="font-bold font-mono" dir="ltr">{value}</div>
    </div>
  )
}

// ── Status breakdown + send-log row drill-down ───────────────────────────────

/** Verbatim per-status counters. Shown for every diagnose so the
 *  merchant can spot the exact statuses present in campaign_send_logs.
 *  Especially important for ``orphaned_materialized_rows`` (funnel
 *  promised rows but DB disagrees) and ``unknown_status`` (rows have
 *  values not in the canonical set). */
function StatusBreakdownPanel({
  breakdown,
  raw,
}: {
  breakdown: NonNullable<CampaignDebugSnapshot['status_breakdown']>
  raw: CampaignDebugSnapshot['status_breakdown_raw'] | null | undefined
}) {
  const items: Array<{ key: string; label: string; value: number; tone: 'slate' | 'sky' | 'emerald' | 'rose' | 'amber' }> = [
    { key: 'queued',                   label: 'في الطابور',        value: breakdown.queued,                   tone: 'sky' },
    { key: 'sending',                  label: 'جارٍ الإرسال',      value: breakdown.sending,                  tone: 'sky' },
    { key: 'sent',                     label: 'تم الإرسال',        value: breakdown.sent,                     tone: 'emerald' },
    { key: 'failed',                   label: 'فشل',               value: breakdown.failed,                   tone: 'rose' },
    { key: 'skipped_duplicate',        label: 'تخطّي تكرار',       value: breakdown.skipped_duplicate,        tone: 'slate' },
    { key: 'skipped_invalid',          label: 'بيانات غير صالحة',   value: breakdown.skipped_invalid,          tone: 'slate' },
    { key: 'skipped_unsubscribed',     label: 'ألغى الاشتراك',     value: breakdown.skipped_unsubscribed,     tone: 'slate' },
    { key: 'skipped_unreachable',      label: 'غير قابل للوصول',   value: breakdown.skipped_unreachable,      tone: 'slate' },
    { key: 'skipped_manual_exclusion', label: 'مستبعد يدوياً',     value: breakdown.skipped_manual_exclusion, tone: 'slate' },
    { key: 'unknown_status',           label: 'حالة غير معروفة',   value: breakdown.unknown_status,           tone: 'amber' },
  ]
  const total = items.reduce((s, it) => s + (it.value || 0), 0)
  const toneClasses: Record<typeof items[number]['tone'], string> = {
    slate:   'bg-slate-50 text-slate-700 border-slate-200',
    sky:     'bg-sky-50 text-sky-700 border-sky-200',
    emerald: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    rose:    'bg-rose-50 text-rose-700 border-rose-200',
    amber:   'bg-amber-50 text-amber-700 border-amber-200',
  }
  // Show non-canonical raw keys alongside the canonical bucket so
  // support sees exactly which legacy/unknown values are present.
  const exoticRaw = raw
    ? Object.entries(raw).filter(
        ([k]) => ![
          'queued', 'sending', 'sent', 'failed',
          'skipped_duplicate', 'skipped_invalid',
          'skipped_unsubscribed', 'skipped_unreachable',
          'skipped_manual_exclusion',
        ].includes(k),
      )
    : []
  return (
    <div className="mt-3 rounded-lg border border-slate-300 bg-white p-3">
      <p className="text-[11px] font-semibold text-slate-700 mb-2 flex items-center gap-1">
        📊 توزيع حالات صفوف الإرسال ({total})
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-1.5">
        {items.map(it => (
          <div
            key={it.key}
            className={`border rounded px-2 py-1.5 text-[10.5px] ${toneClasses[it.tone]} ${it.value === 0 ? 'opacity-60' : ''}`}
          >
            <div className="font-mono" dir="ltr">{it.key}</div>
            <div className="flex items-baseline justify-between gap-1">
              <span>{it.label}</span>
              <span className="font-bold">{it.value}</span>
            </div>
          </div>
        ))}
      </div>
      {exoticRaw.length > 0 && (
        <div className="mt-2 rounded border border-amber-200 bg-amber-50/60 p-2">
          <p className="text-[10.5px] font-semibold text-amber-800 mb-1">
            ⚠️ قيم حالة غير قانونية مرصودة:
          </p>
          <ul className="space-y-0.5">
            {exoticRaw.map(([k, v]) => (
              <li key={k} className="text-[10.5px] text-amber-700 font-mono" dir="ltr">
                {k} = {v}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** First 10 send-log rows. Renders id / phone / status / skip_reason /
 *  error_code / timestamps as a compact table so support can verify
 *  what rows exist when the lifecycle is ``orphaned_materialized_rows``
 *  or ``unknown_status``. */
function SampleRowsPanel({
  rows,
}: {
  rows: NonNullable<CampaignDebugSnapshot['sample_rows']>
}) {
  const statusTone = (status: string): string => {
    if (status === 'sent') return 'bg-emerald-100 text-emerald-700 border-emerald-200'
    if (status === 'failed') return 'bg-rose-100 text-rose-700 border-rose-200'
    if (status === 'queued' || status === 'sending') return 'bg-sky-100 text-sky-700 border-sky-200'
    if (status?.startsWith('skipped_')) return 'bg-slate-100 text-slate-700 border-slate-200'
    return 'bg-amber-100 text-amber-700 border-amber-200'
  }
  return (
    <div className="mt-3 rounded-lg border border-slate-300 bg-white p-3">
      <p className="text-[11px] font-semibold text-slate-700 mb-2 flex items-center gap-1">
        🔎 أول {rows.length} صفوف من سجل الإرسال
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-[10.5px] border-separate border-spacing-y-1">
          <thead>
            <tr className="text-slate-500 text-right">
              <th className="px-2 font-medium">#</th>
              <th className="px-2 font-medium">رقم العميل</th>
              <th className="px-2 font-medium">الحالة</th>
              <th className="px-2 font-medium">سبب التخطّي</th>
              <th className="px-2 font-medium">رمز الخطأ</th>
              <th className="px-2 font-medium">محاولات</th>
              <th className="px-2 font-medium">آخر تعديل</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} className="bg-slate-50">
                <td className="px-2 font-mono text-slate-600" dir="ltr">{r.id}</td>
                <td className="px-2 font-mono text-slate-700" dir="ltr">{r.phone_masked || '—'}</td>
                <td className="px-2">
                  <span className={`border rounded px-1.5 py-0.5 font-mono ${statusTone(r.status || '')}`} dir="ltr">
                    {r.status || '—'}
                  </span>
                </td>
                <td className="px-2 text-slate-600">{r.skip_reason || '—'}</td>
                <td className="px-2 text-slate-600 font-mono" dir="ltr">{r.error_code || '—'}</td>
                <td className="px-2 text-slate-600 font-mono text-center" dir="ltr">{r.attempt_count ?? 0}</td>
                <td className="px-2 text-slate-500 font-mono text-[10px]" dir="ltr">
                  {(r.updated_at || r.created_at || '').slice(0, 19).replace('T', ' ') || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Campaign list row ─────────────────────────────────────────────────────────

/** Append frequency-cap audit lines to the diagnostic ``pre`` block —
 * surfaces WHICH phone was capped and WHEN the last successful Meta
 * send happened so merchants stop blaming "ghost" prior sends. */
function appendFrequencyCapDiagnostic(lines: string[], snap: CampaignDebugSnapshot) {
  const fc = snap.frequency_cap
  if (!fc || fc.capped_count <= 0) return
  lines.push(
    `⏱️ حد التكرار (${fc.cap_days} يوماً): تم تخطّي ${fc.capped_count} عميل بسبب إرسال تسويقي ناجح سابق (مسجّل لدى Meta فقط).`,
  )
  if (fc.last_successful_sent_at) {
    let agg = `   أحدث إرسال ناجح في السجل: ${fc.last_successful_sent_at}`
    if (fc.last_successful_campaign_id != null) {
      agg += ` (حملة #${fc.last_successful_campaign_id})`
    }
    lines.push(agg)
  }
  const rows = (fc.frequency_cap_source_rows?.length ?? 0) > 0
    ? fc.frequency_cap_source_rows
    : fc.source_rows
  for (const row of rows || []) {
    const cid =
      row.last_successful_campaign_id != null
        ? `#${row.last_successful_campaign_id}`
        : '—'
    const ts = row.last_successful_sent_at ?? '—'
    lines.push(`   • ${row.phone_masked}: آخر نجاح ${ts} (${cid})`)
  }
}

function CampaignRow({ campaign, onStatusChange, checked, onCheck, onDelete }: {
  campaign: CampaignRecord
  onStatusChange: (id: number, status: string) => void
  checked: boolean
  onCheck: (id: number, v: boolean) => void
  onDelete: (id: number) => void
}) {
  // Prefer the granular lifecycle label (e.g. "ينتظر بدء الإرسال")
  // over the raw status pill ("نشطة"). Fall back to STATUS_META if
  // the backend didn't ship a lifecycle key (older clients hitting a
  // freshly redeployed backend).
  const lifecycleKey = campaign.lifecycle || campaign.status || 'draft'
  const sm = LIFECYCLE_META[lifecycleKey] ?? STATUS_META[campaign.status] ?? STATUS_META['draft']
  const tm = TYPE_META[campaign.campaign_type] ?? TYPE_META['broadcast']
  const openRate = campaign.sent_count > 0 ? Math.round((campaign.read_count / campaign.sent_count) * 100) : 0
  const convRate = campaign.sent_count > 0 ? Math.round((campaign.converted_count / campaign.sent_count) * 100) : 0
  const [showErrors, setShowErrors] = useState(false)
  const [diagnosing, setDiagnosing] = useState(false)
  const [dispatching, setDispatching] = useState(false)
  const [diagnostic, setDiagnostic] = useState<string | null>(null)
  // Structured per-customer drill-down. Rendered as cards below the
  // free-text diagnostic so support can scan field flags at a glance
  // ("has_phone=true, normalized=false" → import didn't normalise).
  // ``null`` instead of ``[]`` means "haven't fetched yet" — we hide
  // the section until the first /debug call returns.
  const [excludedSample, setExcludedSample] =
    useState<CampaignDebugSnapshot['sample_excluded_before_send'] | null>(null)
  /** Failed-sample drill-down (parsed Meta fields, shown prominently
   *  when ``error_code == "unknown"`` — fingerprint collection). */
  const [failedSample, setFailedSample] =
    useState<CampaignDebugSnapshot['sample_failed'] | null>(null)
  /** Raw Meta request/response payloads captured on failure — only
   *  populated when at least one failure was "unknown". Used by
   *  support to add new Meta codes to the canonical classifier. */
  const [rawMetaSamples, setRawMetaSamples] =
    useState<CampaignDebugSnapshot['raw_meta_error_samples'] | null>(null)
  /** Verbatim status counters — shown when counters disagree with the
   *  funnel so the merchant sees the EXACT status values present. */
  const [statusBreakdown, setStatusBreakdown] =
    useState<CampaignDebugSnapshot['status_breakdown'] | null>(null)
  const [statusBreakdownRaw, setStatusBreakdownRaw] =
    useState<CampaignDebugSnapshot['status_breakdown_raw'] | null>(null)
  /** First 10 send-log rows — drill-down for orphaned_materialized_rows
   *  and unknown_status lifecycle states. */
  const [sampleRows, setSampleRows] =
    useState<CampaignDebugSnapshot['sample_rows'] | null>(null)
  /** Retry-storm + watchdog signals. Surfaces the circuit breaker
   *  when a single row's attempt_count crossed the safety threshold. */
  const [retryHealth, setRetryHealth] =
    useState<CampaignDebugSnapshot['retry_health'] | null>(null)
  /** Provider-side billing/account block summary. When ``detected``
   *  is true we render the rose support banner, hide the dispatch
   *  CTA, and show the "نسخ تقرير الدعم" button. */
  const [providerBlock, setProviderBlock] =
    useState<CampaignDebugSnapshot['provider_block'] | null>(null)
  /** Delivery breakdown sourced from WhatsApp status webhooks. We
   *  also render it as a small standalone panel under the diagnostic
   *  text so the merchant sees the four pills without scrolling
   *  through the entire diagnostic dump. */
  const [deliverySummary, setDeliverySummary] =
    useState<CampaignDebugSnapshot['delivery_summary'] | null>(null)
  const [sampleSent, setSampleSent] =
    useState<CampaignDebugSnapshot['sample_sent'] | null>(null)
  /** Tracks the support-bundle copy state so we can flash a "✓ تم
   *  النسخ" affordance back to the merchant. */
  const [supportBundleStatus, setSupportBundleStatus] =
    useState<'idle' | 'loading' | 'copied' | 'error'>('idle')
  /** QA escape hatch — POST dispatch-now with ``bypass_frequency_cap``. */
  const [ignoreFreqCapForDispatch, setIgnoreFreqCapForDispatch] = useState(false)

  // Treat both ``failed`` and ``failed_all`` as red. Note we
  // explicitly do NOT include ``partial_minor`` or
  // ``no_whatsapp_recipients`` here — those mean "the campaign
  // worked, the recipient list just didn't fully match".
  const isFailed = campaign.status === 'failed' || lifecycleKey === 'failed_all' || lifecycleKey === 'failed'
  const isStuck =
    lifecycleKey === 'pending_dispatch'
    || lifecycleKey === 'orphaned_materialized_rows'
    || lifecycleKey === 'unknown_status'
  const hasErrors = (campaign.dispatch_errors?.length ?? 0) > 0
  const failedCount = campaign.failed_count ?? 0
  // Provider-side billing/account block: when detected we MUST hide
  // the dispatch CTA (auto-retry is pointless — the recipient/WABA
  // is restricted by Meta/360dialog), and instead surface the
  // support-escalation workflow.
  const providerBlocked = !!providerBlock?.detected

  /**
   * Fetch the support bundle for a 360dialog escalation and copy
   * it as pretty-printed JSON to the merchant's clipboard. The
   * endpoint is read-only and idempotent, so we just call it on
   * demand instead of pre-fetching with the debug snapshot.
   */
  const handleCopySupportBundle = useCallback(async () => {
    setSupportBundleStatus('loading')
    try {
      const bundle = await campaignsApi.supportBundle(campaign.id)
      await navigator.clipboard.writeText(JSON.stringify(bundle, null, 2))
      setSupportBundleStatus('copied')
      // Auto-clear the affordance after a short pause so the
      // button keeps working for repeated clicks.
      window.setTimeout(() => setSupportBundleStatus('idle'), 4_000)
    } catch (err) {
      console.error('[campaigns] support bundle copy failed', err)
      setSupportBundleStatus('error')
      window.setTimeout(() => setSupportBundleStatus('idle'), 4_000)
    }
  }, [campaign.id])

  const handleDiagnose = async () => {
    setDiagnosing(true)
    setDiagnostic(null)
    setExcludedSample(null)
    setFailedSample(null)
    setRawMetaSamples(null)
    setStatusBreakdown(null)
    setStatusBreakdownRaw(null)
    setSampleRows(null)
    setRetryHealth(null)
    setProviderBlock(null)
    setDeliverySummary(null)
    setSampleSent(null)
    setSupportBundleStatus('idle')
    try {
      const snap = await campaignsApi.debug(campaign.id)
      setExcludedSample(snap.sample_excluded_before_send || [])
      setFailedSample(snap.sample_failed || [])
      setRawMetaSamples(snap.raw_meta_error_samples || [])
      setStatusBreakdown(snap.status_breakdown || null)
      setStatusBreakdownRaw(snap.status_breakdown_raw || null)
      setSampleRows(snap.sample_rows || [])
      setRetryHealth(snap.retry_health || null)
      setProviderBlock(snap.provider_block || null)
      setDeliverySummary(snap.delivery_summary || null)
      setSampleSent(snap.sample_sent || null)
      const r = snap.recipients
      const total = r.total || campaign.audience_count || 0
      const skipped = r.skipped_duplicate + r.skipped_invalid +
                      r.skipped_unsubscribed + r.skipped_unreachable +
                      r.skipped_manual_exclusion
      const wa = snap.wa_connection
        ? `${snap.wa_connection.status} / ${snap.wa_connection.phone_number_id ?? '—'}`
        : 'لا اتصال'
      const tpl = snap.template
        ? `${snap.template.name} (${snap.template.status})`
        : 'القالب غير موجود'
      const lines = [
        `📤 تم الإرسال إلى ${r.sent} من ${total} عملاء${
          r.failed > 0 ? ` — فشل ${r.failed}` : ''
        }${skipped > 0 ? ` — تخطّي ${skipped}` : ''}`,
        `📨 القالب: ${tpl}`,
        `📞 الواتساب: ${wa}`,
        `🕐 المُجدول: ${snap.scheduler.campaign_dispatcher_enabled ? 'مفعّل' : 'معطّل'}`,
      ]

      // Audience funnel — render the complete pipeline so the
      // merchant can see exactly where customers were dropped. We
      // ALWAYS show this when the campaign has either an audience
      // > 0 or has materialised any rows; otherwise it's noise.
      const f = snap.audience_funnel
      if (f && (f.raw_audience > 0 || f.materialized_rows > 0)) {
        lines.push('📊 مسار الجمهور:')
        lines.push(`  • العدد الأولي: ${f.raw_audience}`)
        lines.push(`  • قابل للوصول: ${f.after_reachable_filter}`)
        lines.push(`  • صفوف فعليّة: ${f.materialized_rows}`)
        if (f.queued_for_send > 0) {
          lines.push(`  • في الطابور: ${f.queued_for_send}`)
        }
        if (f.frequency_cap_skipped > 0) {
          lines.push(`  • تخطّى التكرار: ${f.frequency_cap_skipped}`)
        }
      }

      // Pre-send exclusion breakdown — surfaces "🚫 تم استبعاد 4
      // عملاء: 2 لا يملكون واتساب، 2 أرقام غير صالحة" instead of
      // the generic "حملة بلا مستلمين".
      if ((snap.excluded_reasons_summary || []).length > 0) {
        lines.push(`🚫 تم استبعاد ${snap.excluded_before_send_count} عميل قبل الإرسال:`)
        for (const ex of snap.excluded_reasons_summary) {
          lines.push(`  • ${ex.label_ar} (${ex.count})`)
        }
      }

      // Delivery summary — split "sent" into the four downstream
      // stages so the merchant sees the difference between
      // "Meta accepted" and "the customer actually received it".
      // Only render when we have any rows to talk about; otherwise
      // it's noise on a freshly-launched campaign that hasn't
      // received any webhooks yet.
      const ds = snap.delivery_summary
      if (ds && ds.accepted_by_provider > 0) {
        lines.push('📬 حالة التسليم:')
        lines.push(`  • قبلتها Meta: ${ds.accepted_by_provider}`)
        lines.push(`  • وصلت للعميل: ${ds.delivered}`)
        lines.push(`  • قرأها العميل: ${ds.read}`)
        if (ds.failed_after_accept > 0) {
          lines.push(`  ⚠️ فشلت بعد قبول Meta: ${ds.failed_after_accept}`)
        }
        if (ds.unknown_delivery > 0) {
          lines.push(`  • لم تصل بعد (لم نستلم إشعار من Meta): ${ds.unknown_delivery}`)
        }
        if (ds.missing_provider_message_id > 0) {
          lines.push(
            `  ⛔ ${ds.missing_provider_message_id} صف مُعلَّم "تم الإرسال" بدون provider_message_id ` +
            `— لا يجوز اعتبارها مُرسلة فعلاً.`,
          )
        }
      }

      // Failure summary — group by canonical Meta key so the merchant
      // doesn't see 4 raw rows; just "3 عملاء لا يملكون واتساب".
      if ((snap.failure_summary || []).length > 0) {
        lines.push('🚨 تفصيل الفشل:')
        for (const fs of snap.failure_summary) {
          const sev = fs.severity === 'minor' ? 'ℹ️'
                    : fs.severity === 'major' ? '⚠️' : '⛔'
          lines.push(`  ${sev} ${fs.error_label_ar} (${fs.count})`)
          if (fs.advice_ar) {
            lines.push(`     ↳ ${fs.advice_ar}`)
          }
        }
      }
      appendFrequencyCapDiagnostic(lines, snap)
      const hints = (snap.hints || []).join(' • ')
      if (hints) lines.push(`💡 ${hints}`)
      setDiagnostic(lines.join('\n'))
    } catch (err: any) {
      setDiagnostic(`تعذر تشغيل التشخيص: ${err?.message || err}`)
    } finally {
      setDiagnosing(false)
    }
  }

  /**
   * Kick a background dispatch and watch the recipient counters
   * tick up via the debug endpoint.
   *
   * The backend can no longer block the HTTP response on the full
   * send (would exceed our 25s timeout for any sizeable audience —
   * the dispatcher has 1.5s pauses between sends + Meta API
   * latency), so we get an immediate ``{kicked: true}`` and then
   * poll /debug 5 times over ~30s to show progress. The list itself
   * refreshes on focus + manually whenever the parent reloads it.
   */
  const handleDispatchNow = async () => {
    let msg =
      `سيتم تشغيل الإرسال للحملة "${campaign.name}" الآن في الخلفية. ` +
      `لن يُعاد إرسال أي مستلم تم إرساله مسبقاً.`
    if (ignoreFreqCapForDispatch) {
      msg +=
        '\n\n⚠️ تم تفعيل «تجاهل حد التكرار لهذه الحملة» — ستُرسل هذه الجولة حتى للعملاء الذين تلقّوا رسالة تسويقية ناجحة مؤخراً (استخدام للاختبار).'
    }
    if (!confirm(msg)) return
    setDispatching(true)
    setDiagnostic('⏳ بدأ الإرسال في الخلفية — جاري متابعة التقدّم…')
    try {
      const res = await campaignsApi.dispatchNow(campaign.id, {
        bypassFrequencyCap: ignoreFreqCapForDispatch,
      })
      if (res.skipped) {
        setDiagnostic(res.message || 'تم تجاوز الإرسال.')
        return
      }
      if (res.ok === false) {
        setDiagnostic(`❌ تعذر تشغيل الإرسال: ${res.error || res.message || 'unknown'}`)
        return
      }
      // Surface the pre-dispatch bookkeeping so the merchant sees
      // what we did before kicking the background task: how many
      // failures we rescheduled and how many zombies we revived.
      const preLines: string[] = []
      if ((res.rescheduled_failed ?? 0) > 0) {
        preLines.push(
          `🔁 تمت إعادة جدولة ${res.rescheduled_failed} صف فاشل ضمن حدّ المحاولات.`,
        )
      }
      if ((res.revived_zombies ?? 0) > 0) {
        preLines.push(
          `🧟 تم تحرير ${res.revived_zombies} صف عالق في sending وإعادته إلى queued.`,
        )
      }
      if (preLines.length > 0) {
        setDiagnostic(preLines.join('\n') + '\n\n⏳ جاري متابعة التقدّم…')
      }
      // Background task is now running. Poll the debug endpoint a
      // few times so the merchant sees counters tick up without
      // having to manually refresh.
      let lastSnapshot: string | null = null
      for (let i = 0; i < 6; i++) {
        await new Promise(r => setTimeout(r, 4_000))
        try {
          const snap = await campaignsApi.debug(campaign.id)
          setExcludedSample(snap.sample_excluded_before_send || [])
          setFailedSample(snap.sample_failed || [])
          setRawMetaSamples(snap.raw_meta_error_samples || [])
          setStatusBreakdown(snap.status_breakdown || null)
          setStatusBreakdownRaw(snap.status_breakdown_raw || null)
          setSampleRows(snap.sample_rows || [])
          setRetryHealth(snap.retry_health || null)
          setProviderBlock(snap.provider_block || null)
          const r = snap.recipients
          const total = r.total || campaign.audience_count || 0
          const lifecycleLabel =
            LIFECYCLE_META[snap.campaign.lifecycle]?.label
            || snap.campaign.lifecycle
          const lines: string[] = [
            `📤 تم الإرسال إلى ${r.sent} من ${total} عملاء`,
          ]
          if (r.queued > 0) lines.push(`⏳ في الطابور: ${r.queued}`)
          if (r.failed > 0) lines.push(`❌ فشل: ${r.failed}`)
          if ((snap.failure_summary || []).length > 0) {
            lines.push('🚨 تفصيل الفشل:')
            for (const fs of snap.failure_summary) {
              const sev = fs.severity === 'minor' ? 'ℹ️'
                        : fs.severity === 'major' ? '⚠️' : '⛔'
              lines.push(`  ${sev} ${fs.error_label_ar} (${fs.count})`)
            }
          }
          // Surface the funnel during polling too — the merchant
          // can see "raw=4, after_reachable=0" the moment we know.
          if ((snap.excluded_reasons_summary || []).length > 0) {
            lines.push(`🚫 مستبعدون: ${snap.excluded_before_send_count}`)
            for (const ex of snap.excluded_reasons_summary) {
              lines.push(`  • ${ex.label_ar} (${ex.count})`)
            }
          }
          appendFrequencyCapDiagnostic(lines, snap)
          lines.push(`🚦 الحالة: ${lifecycleLabel}`)
          lastSnapshot = lines.join('\n')
          setDiagnostic(lastSnapshot)
          // Refresh the parent list so the lifecycle pill updates.
          onStatusChange(campaign.id, campaign.status)
          // Stop polling early once the campaign reaches a
          // terminal lifecycle (success OR failure variants).
          const terminal = new Set([
            'sent', 'partial', 'partial_minor',
            'no_whatsapp_recipients', 'failed_all', 'failed',
            'completed_empty', 'excluded_before_send',
            'orphaned_materialized_rows', 'unknown_status',
          ])
          if (terminal.has(snap.campaign.lifecycle)) break
        } catch {
          // Ignore individual poll failures — we'll retry.
        }
      }
      if (!lastSnapshot) {
        setDiagnostic(
          res.message ||
          '✅ تم تشغيل الإرسال في الخلفية. حدّث الصفحة لرؤية النتيجة.'
        )
      }
    } catch (err: any) {
      setDiagnostic(`❌ تعذر تشغيل الإرسال: ${err?.message || err}`)
    } finally {
      setDispatching(false)
      setIgnoreFreqCapForDispatch(false)
    }
  }

  return (
    <>
      <tr className={`hover:bg-slate-50 transition-colors ${isFailed ? 'bg-red-50/40' : ''} ${checked ? 'bg-brand-50/30' : ''}`}>
        <td className="px-3 py-3.5 w-8">
          <button onClick={() => onCheck(campaign.id, !checked)} className="text-slate-400 hover:text-brand-500">
            {checked ? <CheckSquare className="w-4 h-4 text-brand-500" /> : <Square className="w-4 h-4" />}
          </button>
        </td>
        <td className="px-5 py-3.5">
          <p className="text-xs font-semibold text-slate-900">{campaign.name}</p>
          <p className="text-[10px] text-slate-400 font-mono mt-0.5">{campaign.template_name?.replace(/_/g, ' ')}</p>
        </td>
        <td className="px-5 py-3.5">
          <span className="flex items-center gap-1.5 text-xs text-slate-600">{tm.icon} {tm.label}</span>
        </td>
        <td className="px-5 py-3.5">
          <Badge label={sm.label} variant={sm.variant} dot />
          {/* Wave/Batch indicator: surface the chosen strategy
              right under the status pill so the merchant can
              tell "إرسال على دفعات" apart from a stalled
              immediate campaign at a glance. Only renders when
              the campaign is actually batched/adaptive — small
              immediate campaigns are not labelled. */}
          {campaign.send_strategy && campaign.send_strategy !== 'immediate' && (
            <div className="mt-1">
              <Badge
                label={
                  campaign.send_strategy === 'adaptive'
                    ? 'إرسال تلقائي على دفعات'
                    : 'إرسال على دفعات'
                }
                variant="purple"
              />
            </div>
          )}
          {/* last_error gets the same surface as failed_count: a small
              one-line hint under the status badge so the merchant
              doesn't have to open the drawer to know "what broke?".
              We surface the Arabic translation (last_error_ar) and
              a tiny "نسخ الخطأ التقني" copy icon so support can
              paste the raw Meta payload into a ticket without having
              to ask the merchant to find it. */}
          {(campaign.last_error_ar || campaign.last_error) && (
            <div className="flex items-center gap-1 mt-1 max-w-[200px]">
              <p
                className="text-[10px] text-red-500 truncate flex-1"
                title={campaign.last_error || ''}
              >
                {campaign.last_error_ar || campaign.last_error}
              </p>
              {campaign.last_error && (
                <button
                  type="button"
                  onClick={() => {
                    navigator.clipboard.writeText(campaign.last_error || '')
                      .then(() => setDiagnostic('📋 تم نسخ الخطأ التقني إلى الحافظة'))
                      .catch(() => setDiagnostic('تعذر النسخ — انسخ يدوياً.'))
                  }}
                  className="text-slate-400 hover:text-slate-700 p-0.5 rounded"
                  title="نسخ الخطأ التقني للدعم"
                >
                  <Copy className="w-3 h-3" />
                </button>
              )}
            </div>
          )}
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
            {campaign.sent_count > 0 ? `${convRate}%` : '—'}
          </span>
        </td>
        <td className="px-5 py-3.5">
          <div className="flex items-center gap-2">
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
            {/* Diagnose + manual dispatch — visible whenever a campaign
                is in a state where the merchant might want to know
                what's happening (stuck pending, partial, failed) and
                also for completed campaigns so support can re-check. */}
            <button
              onClick={handleDiagnose}
              disabled={diagnosing}
              className="text-xs text-slate-500 hover:text-slate-800 transition-colors flex items-center gap-1 disabled:opacity-50"
              title="تشخيص حالة الإرسال"
            >
              <AlertCircle className="w-3.5 h-3.5" />
              {diagnosing ? 'جاري…' : 'تشخيص'}
            </button>
            {/* Hide the dispatch CTA entirely on provider-blocked
                campaigns — retrying produces the same restriction
                from Meta/360dialog and creates noise in the logs.
                The merchant gets a "Contact 360dialog" workflow
                instead, surfaced by ProviderBlockBanner below. */}
            {!providerBlocked && (isStuck || isFailed || lifecycleKey === 'partial' || lifecycleKey === 'completed_empty' || lifecycleKey === 'excluded_before_send') && (
              <div className="flex flex-col items-end gap-1">
                <label className="flex items-center gap-1.5 cursor-pointer text-[10px] text-slate-600 max-w-[155px] leading-snug text-right">
                  <input
                    type="checkbox"
                    className="rounded border-slate-300 text-amber-600 shrink-0"
                    checked={ignoreFreqCapForDispatch}
                    onChange={e => setIgnoreFreqCapForDispatch(e.target.checked)}
                  />
                  <span>تجاهل حد التكرار لهذه الحملة</span>
                </label>
                <button
                  onClick={handleDispatchNow}
                  disabled={dispatching}
                  className="text-xs text-amber-600 hover:text-amber-800 transition-colors flex items-center gap-1 disabled:opacity-50"
                  title="تشغيل الإرسال يدوياً الآن"
                >
                  <Send className="w-3.5 h-3.5" />
                  {dispatching ? 'جاري…' : 'إرسال الآن'}
                </button>
              </div>
            )}
            <button
              onClick={() => onDelete(campaign.id)}
              className="text-xs text-slate-300 hover:text-red-500 transition-colors p-1 rounded"
              title="حذف"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </td>
      </tr>
      {diagnostic && (
        <tr className="bg-slate-50/70">
          <td colSpan={TABLE_HEADERS.length} className="px-6 py-3">
            {/* Provider-side billing/account block — must render
                BEFORE the diagnostic dump so the merchant sees the
                escalation workflow first instead of getting lost
                in counters. */}
            {providerBlocked && providerBlock && (
              <ProviderBlockBanner
                block={providerBlock}
                onCopyBundle={handleCopySupportBundle}
                bundleStatus={supportBundleStatus}
              />
            )}
            {/* Wave timeline — only renders for batched/adaptive
                campaigns; immediate ones get nothing (the panel
                self-suppresses based on the API response). */}
            <WavesPanel campaignId={campaign.id} />
            <pre className="text-[11px] text-slate-700 whitespace-pre-wrap break-words font-mono bg-white border border-slate-200 rounded-lg p-3 leading-relaxed">
              {diagnostic}
            </pre>
            {deliverySummary && (
              <DeliverySummaryPanel
                summary={deliverySummary}
                sample={sampleSent}
              />
            )}
            {retryHealth && (
              <RetryHealthPanel health={retryHealth} />
            )}
            {excludedSample && excludedSample.length > 0 && (
              <ExcludedCustomersDrillDown rows={excludedSample} />
            )}
            {statusBreakdown && (
              <StatusBreakdownPanel
                breakdown={statusBreakdown}
                raw={statusBreakdownRaw}
              />
            )}
            {sampleRows && sampleRows.length > 0 && (
              <SampleRowsPanel rows={sampleRows} />
            )}
            {failedSample && failedSample.some(r => r.error_code === 'unknown') && (
              <UnknownMetaErrorsPanel rows={failedSample.filter(r => r.error_code === 'unknown')} />
            )}
            {rawMetaSamples && rawMetaSamples.length > 0 && (
              <RawMetaSamplesPanel samples={rawMetaSamples} />
            )}
          </td>
        </tr>
      )}
      {showErrors && hasErrors && (
        <tr className="bg-red-50/60">
          <td colSpan={TABLE_HEADERS.length} className="px-6 py-3">
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

const TABLE_HEADERS = ['', 'الحملة', 'النوع', 'الحالة', 'الجمهور', 'الإرسال', 'معدل القراءة', 'التحويل', '']

export default function Campaigns() {
  const [showWizard, setShowWizard] = useState(false)
  // Admin-only direct WhatsApp test send modal. Hidden for merchants.
  const [showAdminSend, setShowAdminSend] = useState(false)
  // Admin-only inbound-media environment diagnostic.
  const [showMediaEnv, setShowMediaEnv] = useState(false)
  // Includes platform admins AND admins actively impersonating a
  // merchant — both should see internal debug buttons.
  const adminMode = canUseInternalDebug()
  const [campaigns, setCampaigns] = useState<CampaignRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
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

  const handleDelete = async (id: number) => {
    try {
      await campaignsApi.deleteCampaign(id)
      setCampaigns(cs => cs.filter(c => c.id !== id))
      setSelectedIds(s => { const n = new Set(s); n.delete(id); return n })
    } catch { /* ignore */ }
  }

  const handleBulkDelete = async () => {
    if (selectedIds.size === 0) return
    try {
      await campaignsApi.bulkDelete([...selectedIds])
      setCampaigns(cs => cs.filter(c => !selectedIds.has(c.id)))
      setSelectedIds(new Set())
    } catch { /* ignore */ }
  }

  const toggleSelectAll = () => {
    if (selectedIds.size === campaigns.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(campaigns.map(c => c.id)))
    }
  }

  const handleCheck = (id: number, v: boolean) => {
    setSelectedIds(s => {
      const n = new Set(s)
      v ? n.add(id) : n.delete(id)
      return n
    })
  }

  const stats = useMemo(() => {
    const completed = campaigns.filter(c => c.status === 'completed').length
    const failedCampaigns = campaigns.filter(c => c.status === 'failed').length
    const totalSent = campaigns.reduce((s, c) => s + c.sent_count, 0)
    const totalFailed = campaigns.reduce((s, c) => s + (c.failed_count ?? 0), 0)
    const totalRead = campaigns.reduce((s, c) => s + c.read_count, 0)
    const totalConv = campaigns.reduce((s, c) => s + c.converted_count, 0)
    const openRate = totalSent > 0 ? Math.round((totalRead / totalSent) * 100) : 0
    const convRate = totalSent > 0 ? Math.round((totalConv / totalSent) * 100) : 0
    return { completed, failedCampaigns, totalSent, totalFailed, openRate, convRate }
  }, [campaigns])

  return (
    <div className="space-y-6">
      <PageHeader
        title={t(tr => tr.pages.campaigns.title)}
        subtitle="حملات واتساب ذكية مبنية على شرائح نحلة وقوالب Meta المعتمدة"
        action={
          <div className="flex items-center gap-2">
            {adminMode && (
              <>
                <button
                  onClick={() => setShowMediaEnv(true)}
                  className="text-xs font-medium px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50 inline-flex items-center gap-1.5"
                  title="فحص إعدادات الوسائط على الخادم (OpenAI / تخزين / ffmpeg) — Admin only"
                >
                  <AlertCircle className="w-3.5 h-3.5" /> فحص الوسائط
                </button>
                <button
                  onClick={() => setShowAdminSend(true)}
                  className="text-xs font-medium px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50 inline-flex items-center gap-1.5"
                  title="إرسال قالب واتساب مباشرة عبر المزود — يتجاوز نظام الحملات (Admin only)"
                >
                  <Send className="w-3.5 h-3.5" /> إرسال اختبار مباشر
                </button>
              </>
            )}
            <button onClick={() => setShowWizard(true)} className="btn-primary text-sm">
              <Plus className="w-4 h-4" /> حملة جديدة
            </button>
          </div>
        }
      />

      {adminMode && (
        <>
          <AdminDirectSendModal
            open={showAdminSend}
            onClose={() => setShowAdminSend(false)}
          />
          <MediaEnvModal
            open={showMediaEnv}
            onClose={() => setShowMediaEnv(false)}
          />
        </>
      )}

      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="حملات مكتملة" value={stats.completed.toString()} icon={CheckCircle} />
        <StatCard label="إجمالي المُرسَل" value={`${stats.totalSent.toLocaleString('ar-SA')}${stats.totalFailed > 0 ? ` / ${stats.totalFailed} فشلت` : ''}`} icon={Send} />
        <StatCard label="معدل القراءة" value={`${stats.openRate}%`} icon={BarChart2} />
        <StatCard label="معدل التحويل" value={`${stats.convRate}%`} icon={TrendingUp} />
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
        {selectedIds.size > 0 && (
          <div className="flex items-center justify-between px-5 py-2.5 bg-brand-50 border-b border-brand-100">
            <span className="text-xs font-medium text-brand-700">
              تم تحديد {selectedIds.size} حملة
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={handleBulkDelete}
                className="flex items-center gap-1.5 text-xs font-medium text-red-600 hover:text-red-700 bg-red-50 hover:bg-red-100 px-3 py-1.5 rounded-lg transition-colors"
              >
                <Trash2 className="w-3.5 h-3.5" /> حذف المحدد
              </button>
              <button
                onClick={() => setSelectedIds(new Set())}
                className="text-xs text-slate-500 hover:text-slate-700 px-2 py-1.5"
              >
                إلغاء
              </button>
            </div>
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-500 text-xs uppercase tracking-wider">
              <tr>
                <th className="px-3 py-3 w-8">
                  <button onClick={toggleSelectAll} className="text-slate-400 hover:text-brand-500">
                    {selectedIds.size === campaigns.length && campaigns.length > 0
                      ? <CheckSquare className="w-4 h-4 text-brand-500" />
                      : <Square className="w-4 h-4" />
                    }
                  </button>
                </th>
                {TABLE_HEADERS.slice(1).map(h => (
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
                <CampaignRow
                  key={c.id}
                  campaign={c}
                  onStatusChange={handleStatusChange}
                  checked={selectedIds.has(c.id)}
                  onCheck={handleCheck}
                  onDelete={handleDelete}
                />
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
