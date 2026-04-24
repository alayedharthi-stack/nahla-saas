/**
 * AbandonedCartEditor — Autopilot Mode
 * ─────────────────────────────────────
 * Simplified merchant editor for the 3-stage abandoned-cart recovery
 * workflow. The merchant can ONLY adjust timing per stage. Everything
 * else (template binding, delivery mode, coupon logic) is managed
 * automatically by the system.
 *
 * - "مكتبة القوالب" → edit template text + submit to Meta
 * - "إعدادات التذكير" → adjust timing per stage (this editor)
 */
import { useEffect, useMemo, useState } from 'react'
import { Save, RotateCcw, AlertCircle, Clock, Sparkles, ShoppingCart } from 'lucide-react'

// ── Types ────────────────────────────────────────────────────────────────────

interface RecoveryStep {
  enabled?: boolean
  delay_minutes?: number
  delivery_mode?: string
  message_type?: string
  service_key?: string
  step_number?: number
  auto_coupon?: boolean
  [key: string]: unknown
}

interface AbandonedCartConfig {
  steps?: RecoveryStep[]
  language?: string
  respect_saudi_quiet_hours?: boolean
  [key: string]: unknown
}

interface AbandonedCartEditorProps {
  config: AbandonedCartConfig
  onSave: (next: AbandonedCartConfig) => Promise<void>
}

// ── Stage metadata ───────────────────────────────────────────────────────────

const STAGES: {
  title: string
  desc: string
  icon: React.ReactNode
  tone: string
  defaultMinutes: number
}[] = [
  {
    title: '١. التذكير الأول',
    desc: 'قالب معتمد من ميتا — يُرسل تلقائياً بعد ترك السلة',
    icon: <Clock className="w-4 h-4" />,
    tone: 'border-amber-200 bg-amber-50/60',
    defaultMinutes: 30,
  },
  {
    title: '٢. المتابعة',
    desc: 'قالب معتمد ثانٍ — تذكير لطيف بأن السلة لا تزال محفوظة',
    icon: <Sparkles className="w-4 h-4" />,
    tone: 'border-blue-200 bg-blue-50/60',
    defaultMinutes: 360,
  },
  {
    title: '٣. التذكير الأخير مع كوبون',
    desc: 'قالب معتمد أخير — مع كود خصم تلقائي إن كان مفعّلاً',
    icon: <ShoppingCart className="w-4 h-4" />,
    tone: 'border-emerald-200 bg-emerald-50/60',
    defaultMinutes: 1425,
  },
]

// ── Component ────────────────────────────────────────────────────────────────

export default function AbandonedCartEditor({
  config, onSave,
}: AbandonedCartEditorProps) {
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
      // Force template-only delivery_mode on all steps before saving
      const cleaned = clone(draft)
      if (Array.isArray(cleaned.steps)) {
        cleaned.steps = cleaned.steps.slice(0, 3).map((s, i) => ({
          ...s,
          delivery_mode: 'template',
          service_key: 'cart_recovery',
          step_number: i + 1,
          auto_coupon: i === 2 ? true : undefined,
        }))
      }
      await onSave(cleaned)
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
      {/* Autopilot info */}
      <section className="bg-gradient-to-br from-amber-50 to-emerald-50 rounded-xl border border-amber-200 p-4">
        <h4 className="text-sm font-semibold text-slate-800 mb-1 flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-amber-500" />
          طيار آلي — إعدادات التذكير
        </h4>
        <p className="text-xs text-slate-600 leading-relaxed">
          نحلة ترسل التذكيرات تلقائياً عبر قوالب Meta المعتمدة.
          يمكنك تعديل <strong>التوقيت</strong> لكل مرحلة فقط.
          لتعديل نص الرسائل، استخدم <strong>مكتبة قوالب نحلة</strong>.
        </p>
        <p className="text-[11px] text-slate-500 mt-2 leading-relaxed">
          إذا ردّ العميل على أي رسالة، يتوقف الفلو تلقائياً وتنتقل المحادثة للذكاء أو الموظف.
        </p>
      </section>

      {/* Quiet hours */}
      <section className="bg-white rounded-xl border border-slate-200 p-4">
        <ToggleRow
          label="احترام أوقات الهدوء (السعودية)"
          hint="لا نُرسل بين ١٢ منتصف الليل و٨ صباحاً — نؤجّل تلقائياً للساعة ٨:٣٠ صباحاً."
          enabled={draft.respect_saudi_quiet_hours !== false}
          onChange={v => updateRoot({ respect_saudi_quiet_hours: v })}
        />
      </section>

      {/* Stages — timing only */}
      <div className="space-y-3">
        {STAGES.map((stage, idx) => {
          const step = steps[idx] ?? {}
          const totalMinutes = Number(step.delay_minutes ?? stage.defaultMinutes) || 0
          const hh = Math.floor(totalMinutes / 60)
          const mm = totalMinutes % 60

          const setDelay = (hours: number, minutes: number) => {
            const safe = Math.max(0, hours * 60 + minutes)
            updateStep(idx, { delay_minutes: safe })
          }

          return (
            <div key={idx} className={`bg-white rounded-xl border ${stage.tone} overflow-hidden`}>
              <header className="px-4 py-3 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="shrink-0">{stage.icon}</span>
                  <div>
                    <p className="text-sm font-semibold text-slate-800">{stage.title}</p>
                    <p className="text-[11px] text-slate-500 mt-0.5">{stage.desc}</p>
                  </div>
                </div>
                <ToggleSwitch
                  enabled={step.enabled !== false}
                  onChange={v => updateStep(idx, { enabled: v })}
                />
              </header>

              <div className="px-4 pb-4 pt-2 border-t border-slate-100">
                <p className="text-[11px] font-medium text-slate-600 mb-2 flex items-center gap-1.5">
                  <Clock className="w-3.5 h-3.5" />
                  إرسال التذكير بعد
                </p>
                <div className="grid grid-cols-2 gap-3 max-w-xs">
                  <label className="block">
                    <span className="text-[10px] text-slate-500">ساعات</span>
                    <input
                      type="number" min={0} value={hh}
                      onChange={e => setDelay(Number(e.target.value || 0), mm)}
                      className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400"
                    />
                  </label>
                  <label className="block">
                    <span className="text-[10px] text-slate-500">دقائق</span>
                    <input
                      type="number" min={0} max={59} value={mm}
                      onChange={e => setDelay(hh, Number(e.target.value || 0))}
                      className="w-full px-3 py-1.5 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-200 focus:border-brand-400"
                    />
                  </label>
                </div>
                {idx === 2 && (
                  <p className="text-[11px] text-emerald-600 mt-2 flex items-center gap-1">
                    <ShoppingCart className="w-3 h-3" />
                    كود الخصم يُرفق تلقائياً إذا كانت الكوبونات مفعّلة
                  </p>
                )}
              </div>
            </div>
          )
        })}
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

// ── Tiny UI primitives ───────────────────────────────────────────────────────

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
