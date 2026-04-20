/**
 * AbandonedCartEditor
 * ───────────────────
 * Inline merchant editor for the four-stage abandoned-cart recovery
 * workflow. Replaces the read-only `ConfigObject` panel that used to
 * sit inside the SmartAutomations expanded card.
 *
 * What the merchant can edit per step:
 *   • enable / disable
 *   • delay (hours, minutes — auto-converted to delay_minutes)
 *   • delivery mode (template / interactive / ai_recovery)
 *   • Arabic + English body text (with `{{customer_name}}`, `{{store_name}}`,
 *     `{{discount_code}}`, `{{cart_total}}`, `{{checkout_url}}` slots)
 *   • dynamic buttons (resume_cart / apply_coupon / ask_question /
 *     human_help / postpone) + per-button CTA label override
 *   • auto_coupon toggle (drives the coupon stage)
 *   • ai_recovery_enabled toggle (only meaningful for the AI step)
 *
 * Plus two automation-wide toggles:
 *   • respect_saudi_quiet_hours — defers any 00:00→08:00 KSA send to 08:30
 *   • ai_recovery_enabled       — global guard for the AI stage
 *
 * The component keeps a local working copy of `config` and pushes it
 * back through `onSave` only when the merchant explicitly clicks "حفظ".
 * No background autosave: this is a billing-relevant flow and we want a
 * deliberate confirmation gesture.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Save, RotateCcw, AlertCircle, Clock, MessageSquare,
  ToggleLeft, Sparkles, ShoppingCart, Plus, X,
} from 'lucide-react'

// ── Types ────────────────────────────────────────────────────────────────────

export type DeliveryMode = 'template' | 'interactive' | 'ai_recovery'
export type ButtonAction =
  | 'resume_cart' | 'apply_coupon' | 'ask_question'
  | 'human_help' | 'postpone'

interface RecoveryStep {
  enabled?: boolean
  delay_minutes?: number
  delivery_mode?: DeliveryMode
  message_type?: string
  template_name?: string
  template_name_en?: string
  body_text_ar?: string
  body_text_en?: string
  buttons?: ButtonAction[]
  cta_labels?: Partial<Record<ButtonAction, string>>
  auto_coupon?: boolean
  ai_recovery_enabled?: boolean
  ai_persona?: string
  language?: string
  [key: string]: unknown
}

interface AbandonedCartConfig {
  steps?: RecoveryStep[]
  template_name?: string
  template_name_en?: string
  language?: string
  ai_recovery_enabled?: boolean
  respect_saudi_quiet_hours?: boolean
  [key: string]: unknown
}

interface AbandonedCartEditorProps {
  config: AbandonedCartConfig
  onSave: (next: AbandonedCartConfig) => Promise<void>
}

// ── Static metadata ──────────────────────────────────────────────────────────

const STAGE_LABELS_AR: Record<number, { title: string; sub: string; tone: string }> = {
  0: { title: '١. التذكير الأول',         sub: '٣٠ دقيقة — قالب رسمي',              tone: 'bg-amber-50 text-amber-700 border-amber-200' },
  1: { title: '٢. متابعة ذكية',          sub: '٦ ساعات — رسالة تفاعلية داخل النافذة', tone: 'bg-blue-50  text-blue-700  border-blue-200'  },
  2: { title: '٣. استرداد بالذكاء (اختياري)', sub: '٨ ساعات — يستخدم الذكاء عند الحاجة',    tone: 'bg-purple-50 text-purple-700 border-purple-200' },
  3: { title: '٤. الدفعة الأخيرة بالخصم', sub: '٢٣ ساعة و٥٠ دقيقة — قبل انتهاء النافذة', tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
}

const DELIVERY_MODE_LABELS: Record<DeliveryMode, string> = {
  template:    'قالب معتمد (مارا 24س)',
  interactive: 'رسالة تفاعلية (داخل النافذة)',
  ai_recovery: 'استرداد بالذكاء',
}

const BUTTON_LABELS_AR: Record<ButtonAction, { label: string; hint: string }> = {
  resume_cart:  { label: 'إكمال الطلب',     hint: 'يفتح السلة مباشرةً' },
  apply_coupon: { label: 'استخدم الخصم الآن', hint: 'يفتح السلة بالكوبون مطبّقاً' },
  ask_question: { label: 'عندي استفسار',     hint: 'يحوّل العميل لمحادثة الدعم الذكي' },
  human_help:   { label: 'تحدث مع الدعم',    hint: 'يفتح جلسة دعم بشري' },
  postpone:     { label: 'لاحقاً',           hint: 'يوقف بقية مراحل الاسترداد' },
}

const ALL_BUTTONS: ButtonAction[] = [
  'resume_cart', 'apply_coupon', 'ask_question', 'human_help', 'postpone',
]

const SLOT_HINTS: { slot: string; hint: string }[] = [
  { slot: '{{customer_name}}', hint: 'اسم العميل'           },
  { slot: '{{store_name}}',    hint: 'اسم المتجر'           },
  { slot: '{{discount_code}}', hint: 'كود الخصم (إن وُجد)'  },
  { slot: '{{cart_total}}',    hint: 'مجموع السلة'           },
  { slot: '{{checkout_url}}',  hint: 'رابط إكمال السلة'      },
]

// ── Component ────────────────────────────────────────────────────────────────

export default function AbandonedCartEditor({
  config, onSave,
}: AbandonedCartEditorProps) {
  // Working copy of the config the merchant is editing. Reset whenever
  // the parent ships a fresh config (post-save refresh).
  const [draft, setDraft] = useState<AbandonedCartConfig>(() => clone(config))
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { setDraft(clone(config)) }, [config])

  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(config),
    [draft, config],
  )

  const steps: RecoveryStep[] = Array.isArray(draft.steps) ? draft.steps : []

  const updateStep = (idx: number, patch: Partial<RecoveryStep>) => {
    const next = clone(draft)
    const ns = Array.isArray(next.steps) ? next.steps : []
    ns[idx] = { ...ns[idx], ...patch }
    next.steps = ns
    setDraft(next)
  }

  const updateRoot = (patch: Partial<AbandonedCartConfig>) => {
    setDraft(prev => ({ ...prev, ...patch }))
  }

  const handleSave = async () => {
    setSaving(true); setError(null)
    try {
      await onSave(draft)
      setSavedAt(Date.now())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذر الحفظ')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setDraft(clone(config))
    setError(null); setSavedAt(null)
  }

  return (
    <div className="space-y-4">
      {/* Automation-wide toggles */}
      <section className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
        <header className="flex items-center justify-between">
          <h4 className="text-sm font-semibold text-slate-800 flex items-center gap-2">
            <Sparkles className="w-4 h-4 text-brand-500" />
            الإعدادات العامة
          </h4>
        </header>
        <ToggleRow
          label="احترام أوقات الهدوء (السعودية)"
          hint="لا نُرسل بين ١٢ منتصف الليل و٨ صباحاً، نؤجّل تلقائياً للساعة ٨:٣٠ صباحاً."
          enabled={draft.respect_saudi_quiet_hours !== false}
          onChange={v => updateRoot({ respect_saudi_quiet_hours: v })}
        />
        <ToggleRow
          label="تفعيل الاسترداد بالذكاء (Stage 3)"
          hint="رسالة مخصّصة تُولّد بالذكاء — لا تُستخدم إلا عند بقاء نافذة المحادثة مفتوحة."
          enabled={draft.ai_recovery_enabled === true}
          onChange={v => updateRoot({ ai_recovery_enabled: v })}
        />
      </section>

      {/* Slot reference */}
      <section className="bg-amber-50/60 border border-amber-200 rounded-xl p-3">
        <p className="text-xs font-semibold text-amber-800 mb-2 flex items-center gap-1.5">
          <MessageSquare className="w-3.5 h-3.5" />
          المتغيرات المتاحة في نص الرسالة
        </p>
        <div className="flex flex-wrap gap-2">
          {SLOT_HINTS.map(s => (
            <span
              key={s.slot}
              className="text-[11px] bg-white border border-amber-200 text-amber-800 rounded-md px-2 py-1"
              title={s.hint}
            >
              <span className="font-mono">{s.slot}</span>
              <span className="text-amber-500"> — {s.hint}</span>
            </span>
          ))}
        </div>
      </section>

      {/* Per-step editors */}
      <div className="space-y-3">
        {steps.map((step, idx) => (
          <StepEditor
            key={idx}
            stepIdx={idx}
            step={step}
            onChange={patch => updateStep(idx, patch)}
          />
        ))}
        {steps.length === 0 && (
          <div className="text-center text-sm text-slate-500 py-6 bg-white rounded-xl border border-dashed border-slate-200">
            لا توجد خطوات معرّفة بعد.
          </div>
        )}
      </div>

      {/* Save bar */}
      <div className="sticky bottom-0 bg-white border-t border-slate-200 -mx-5 px-5 py-3 flex items-center justify-between">
        <div className="text-xs text-slate-500">
          {error ? (
            <span className="text-rose-600 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5" /> {error}
            </span>
          ) : savedAt ? (
            <span className="text-emerald-600">تم الحفظ بنجاح ✓</span>
          ) : dirty ? (
            <span className="text-amber-600">لديك تغييرات لم تُحفظ بعد.</span>
          ) : (
            <span>كل الإعدادات محفوظة.</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReset}
            disabled={!dirty || saving}
            className="px-3 py-1.5 text-xs rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
          >
            <RotateCcw className="w-3.5 h-3.5 inline -mt-0.5 me-1" />
            تراجع
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!dirty || saving}
            className="px-4 py-1.5 text-xs rounded-lg bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-40"
          >
            <Save className="w-3.5 h-3.5 inline -mt-0.5 me-1" />
            {saving ? 'جارٍ الحفظ...' : 'حفظ التغييرات'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Step editor ──────────────────────────────────────────────────────────────

interface StepEditorProps {
  stepIdx: number
  step: RecoveryStep
  onChange: (patch: Partial<RecoveryStep>) => void
}

function StepEditor({ stepIdx, step, onChange }: StepEditorProps) {
  const meta = STAGE_LABELS_AR[stepIdx] ?? {
    title: `خطوة ${stepIdx + 1}`, sub: '', tone: 'bg-slate-50 text-slate-700 border-slate-200',
  }
  const isAiStep = step.message_type === 'ai_recovery' || step.delivery_mode === 'ai_recovery'
  const buttons: ButtonAction[] = Array.isArray(step.buttons) ? step.buttons : []
  const ctaLabels = step.cta_labels ?? {}

  // Delay split into hours+minutes for a friendlier UX.
  const totalMinutes = Number(step.delay_minutes ?? 0) || 0
  const hh = Math.floor(totalMinutes / 60)
  const mm = totalMinutes % 60

  const setDelay = (hours: number, minutes: number) => {
    const safe = Math.max(0, hours * 60 + minutes)
    onChange({ delay_minutes: safe })
  }

  return (
    <div className={`bg-white rounded-xl border ${meta.tone} overflow-hidden`}>
      <header className="px-4 py-3 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold">{meta.title}</p>
          <p className="text-[11px] opacity-75 mt-0.5">{meta.sub}</p>
        </div>
        <ToggleSwitch
          enabled={step.enabled !== false}
          onChange={v => onChange({ enabled: v })}
        />
      </header>

      <div className="p-4 space-y-4 bg-white border-t border-slate-100">
        {/* Delay */}
        <div className="grid grid-cols-2 gap-3">
          <Field label="بعد كم ساعة؟" icon={<Clock className="w-3.5 h-3.5" />}>
            <input
              type="number" min={0} value={hh}
              onChange={e => setDelay(Number(e.target.value || 0), mm)}
              className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400"
            />
          </Field>
          <Field label="ودقيقة؟">
            <input
              type="number" min={0} max={59} value={mm}
              onChange={e => setDelay(hh, Number(e.target.value || 0))}
              className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400"
            />
          </Field>
        </div>

        {/* Delivery mode */}
        <Field label="طريقة الإرسال">
          <select
            value={step.delivery_mode ?? 'template'}
            onChange={e => onChange({ delivery_mode: e.target.value as DeliveryMode })}
            className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg bg-white"
          >
            {Object.entries(DELIVERY_MODE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </Field>

        {/* Body text — only for interactive / ai_recovery; templates
            are managed in the templates page. */}
        {step.delivery_mode !== 'template' && (
          <>
            <Field label="نص الرسالة (عربي)">
              <textarea
                value={step.body_text_ar ?? ''}
                onChange={e => onChange({ body_text_ar: e.target.value })}
                rows={4}
                placeholder="مثال: {{customer_name}} 🌷 سلتك في {{store_name}} لا تزال محفوظة لك."
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400 leading-relaxed"
                dir="auto"
              />
            </Field>
            <Field label="نص الرسالة (إنجليزي - اختياري)">
              <textarea
                value={step.body_text_en ?? ''}
                onChange={e => onChange({ body_text_en: e.target.value })}
                rows={3}
                placeholder="Hi {{customer_name}}, your cart at {{store_name}} is still saved."
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400 leading-relaxed font-sans"
                dir="ltr"
              />
            </Field>
          </>
        )}

        {/* Template names — only for template mode */}
        {step.delivery_mode === 'template' && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="اسم القالب (عربي)">
              <input
                type="text"
                value={step.template_name ?? ''}
                onChange={e => onChange({ template_name: e.target.value })}
                placeholder="abandoned_cart_recovery_ar"
                className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg font-mono"
                dir="ltr"
              />
            </Field>
            <Field label="اسم القالب (إنجليزي)">
              <input
                type="text"
                value={step.template_name_en ?? ''}
                onChange={e => onChange({ template_name_en: e.target.value })}
                placeholder="abandoned_cart_recovery_en"
                className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg font-mono"
                dir="ltr"
              />
            </Field>
          </div>
        )}

        {/* Buttons */}
        <Field label="الأزرار الديناميكية (الحدّ الأقصى ٣)">
          <ButtonsPicker
            buttons={buttons}
            ctaLabels={ctaLabels}
            onChange={(b, l) => onChange({ buttons: b, cta_labels: l })}
          />
        </Field>

        {/* Per-step toggles */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-2 border-t border-slate-100">
          <InlineToggle
            label="إرفاق كوبون خصم تلقائياً"
            enabled={step.auto_coupon === true}
            icon={<ShoppingCart className="w-3.5 h-3.5" />}
            onChange={v => onChange({ auto_coupon: v })}
          />
          {isAiStep && (
            <InlineToggle
              label="تفعيل الردّ بالذكاء"
              enabled={step.ai_recovery_enabled === true}
              icon={<Sparkles className="w-3.5 h-3.5" />}
              onChange={v => onChange({ ai_recovery_enabled: v })}
            />
          )}
        </div>
      </div>
    </div>
  )
}

// ── Buttons picker ───────────────────────────────────────────────────────────

interface ButtonsPickerProps {
  buttons: ButtonAction[]
  ctaLabels: Partial<Record<ButtonAction, string>>
  onChange: (
    buttons: ButtonAction[],
    ctaLabels: Partial<Record<ButtonAction, string>>,
  ) => void
}

function ButtonsPicker({ buttons, ctaLabels, onChange }: ButtonsPickerProps) {
  const remaining = ALL_BUTTONS.filter(b => !buttons.includes(b))

  const add = (b: ButtonAction) => {
    if (buttons.length >= 3) return
    onChange([...buttons, b], ctaLabels)
  }
  const remove = (b: ButtonAction) => {
    const nextLabels = { ...ctaLabels }
    delete nextLabels[b]
    onChange(buttons.filter(x => x !== b), nextLabels)
  }
  const setLabel = (b: ButtonAction, label: string) => {
    onChange(buttons, { ...ctaLabels, [b]: label })
  }

  return (
    <div className="space-y-2">
      {buttons.length === 0 && (
        <p className="text-[11px] text-slate-400 italic">لا توجد أزرار محدّدة لهذه الخطوة.</p>
      )}
      {buttons.map(b => {
        const meta = BUTTON_LABELS_AR[b]
        return (
          <div key={b} className="flex items-center gap-2 bg-slate-50 border border-slate-200 rounded-lg p-2">
            <span className="text-[11px] font-mono bg-white px-2 py-0.5 rounded border border-slate-200 shrink-0 text-slate-500">
              {b}
            </span>
            <input
              type="text"
              value={ctaLabels[b] ?? meta.label}
              onChange={e => setLabel(b, e.target.value)}
              maxLength={20}
              className="flex-1 min-w-0 px-2 py-1 text-xs border border-slate-200 rounded bg-white"
              placeholder={meta.label}
              title={meta.hint}
              dir="auto"
            />
            <button
              type="button"
              onClick={() => remove(b)}
              className="p-1 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded"
              aria-label="حذف"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )
      })}

      {remaining.length > 0 && buttons.length < 3 && (
        <div className="flex flex-wrap gap-1.5">
          {remaining.map(b => (
            <button
              key={b}
              type="button"
              onClick={() => add(b)}
              className="text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-md border border-dashed border-slate-300 text-slate-600 hover:border-brand-400 hover:text-brand-600 hover:bg-brand-50"
              title={BUTTON_LABELS_AR[b].hint}
            >
              <Plus className="w-3 h-3" />
              {BUTTON_LABELS_AR[b].label}
            </button>
          ))}
        </div>
      )}
      {buttons.length >= 3 && (
        <p className="text-[11px] text-slate-400">
          واتساب يسمح بثلاثة أزرار كحدّ أقصى للرسالة الواحدة.
        </p>
      )}
    </div>
  )
}

// ── Tiny UI primitives ───────────────────────────────────────────────────────

function Field({
  label, icon, children,
}: { label: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium text-slate-600 mb-1 flex items-center gap-1.5">
        {icon}
        {label}
      </span>
      {children}
    </label>
  )
}

function ToggleRow({
  label, hint, enabled, onChange,
}: { label: string; hint?: string; enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-sm text-slate-800">{label}</p>
        {hint && <p className="text-[11px] text-slate-500 mt-0.5 leading-relaxed">{hint}</p>}
      </div>
      <ToggleSwitch enabled={enabled} onChange={onChange} />
    </div>
  )
}

function InlineToggle({
  label, enabled, onChange, icon,
}: { label: string; enabled: boolean; onChange: (v: boolean) => void; icon?: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
      className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs transition-colors ${
        enabled
          ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
          : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
      }`}
    >
      <span className="flex items-center gap-1.5">
        {icon}
        {label}
      </span>
      <ToggleLeft className={`w-4 h-4 ${enabled ? 'rotate-180 text-emerald-500' : 'text-slate-400'} transition-transform`} />
    </button>
  )
}

function ToggleSwitch({
  enabled, onChange,
}: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      onClick={() => onChange(!enabled)}
      className={`shrink-0 w-10 h-5 rounded-full transition-colors ${
        enabled ? 'bg-emerald-500' : 'bg-slate-200'
      }`}
    >
      <span
        className={`block w-4 h-4 bg-white rounded-full shadow-md transition-transform mt-0.5 ${
          enabled ? 'translate-x-5' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v ?? {}))
}
