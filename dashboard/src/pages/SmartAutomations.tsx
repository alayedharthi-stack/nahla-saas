import { useState, useEffect, useCallback } from 'react'
import {
  Zap, Send, CheckCircle, TrendingUp, Sparkles,
  ChevronDown, ChevronUp, AlertCircle, RefreshCw,
  Settings2, ArrowRight,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import StatCard from '../components/ui/StatCard'
import { useLanguage } from '../i18n/context'
import {
  automationsApi,
  AutomationRecord,
  AutomationType,
  AUTOMATION_META,
} from '../api/automations'
import { templatesApi, TemplateVarMapRecord } from '../api/templates'

// ── Template variable map panel ───────────────────────────────────────────────

// Inline static var maps for the two default templates (no extra API call needed)
const STATIC_VAR_MAPS: Record<string, Record<string, string>> = {
  predictive_reorder_reminder_ar: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'اسم المنتج',
    '{{3}}': 'رابط إعادة الطلب',
  },
  cod_order_confirmation_ar: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'اسم المنتج',
    '{{3}}': 'مبلغ الطلب (ر.س)',
  },
  abandoned_cart_reminder: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'رابط السلة',
  },
  special_offer: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'نسبة الخصم',
    '{{3}}': 'كود الكوبون',
  },
  win_back: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'نسبة الخصم',
    '{{3}}': 'كود الكوبون',
  },
  vip_exclusive: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'نسبة الخصم',
    '{{3}}': 'كود الكوبون',
  },
  new_arrivals: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'اسم المتجر',
  },
  order_confirmed: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'رقم الطلب',
  },
}

function TemplateVarMapPanel({ templateName }: { templateName: string }) {
  const varMap = STATIC_VAR_MAPS[templateName]
  if (!varMap || Object.keys(varMap).length === 0) return null

  return (
    <div className="mt-3 bg-white rounded-xl border border-brand-100 p-3">
      <p className="text-[11px] font-semibold text-brand-700 mb-2 flex items-center gap-1.5">
        <ArrowRight className="w-3 h-3" />
        ربط متغيرات القالب
      </p>
      <div className="space-y-1.5">
        {Object.entries(varMap).map(([varKey, label]) => (
          <div key={varKey} className="flex items-center gap-2 text-xs">
            <span className="font-mono bg-amber-50 border border-amber-200 text-amber-700 px-1.5 py-0.5 rounded text-[11px] w-12 text-center shrink-0 tabular-nums">{varKey}</span>
            <span className="text-slate-300">→</span>
            <span className="text-slate-700">{label}</span>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-slate-400 mt-2">
        تُملأ هذه القيم تلقائياً من بيانات العميل قبل إرسال الرسالة.
      </p>
    </div>
  )
}

// ── Toggle component ──────────────────────────────────────────────────────────

interface ToggleProps {
  enabled: boolean
  onChange: (next: boolean) => void
  size?: 'sm' | 'lg'
  disabled?: boolean
}

function Toggle({ enabled, onChange, size = 'sm', disabled = false }: ToggleProps) {
  const trackBase =
    size === 'lg'
      ? 'w-14 h-7 rounded-full transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-emerald-500'
      : 'w-10 h-5 rounded-full transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-emerald-500'
  const thumbBase =
    size === 'lg'
      ? 'w-6 h-6 rounded-full bg-white shadow-md transition-transform duration-200'
      : 'w-4 h-4 rounded-full bg-white shadow-md transition-transform duration-200'
  const thumbOn = size === 'lg' ? 'translate-x-7' : 'translate-x-5'
  const thumbOff = 'translate-x-0.5'

  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      disabled={disabled}
      onClick={() => !disabled && onChange(!enabled)}
      className={`${trackBase} ${enabled ? 'bg-emerald-500' : 'bg-slate-200'} ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <span
        className={`${thumbBase} ${enabled ? thumbOn : thumbOff} block`}
        style={{ marginTop: size === 'lg' ? '0.125rem' : '0.125rem' }}
      />
    </button>
  )
}

// ── Config panel ──────────────────────────────────────────────────────────────

function renderConfigValue(value: unknown, depth = 0): React.ReactNode {
  if (value === null || value === undefined) {
    return <span className="text-slate-400">—</span>
  }
  if (typeof value === 'boolean') {
    return (
      <span className={value ? 'text-emerald-600' : 'text-slate-400'}>
        {value ? 'نعم' : 'لا'}
      </span>
    )
  }
  if (typeof value === 'number') {
    return <span className="text-slate-700">{value}</span>
  }
  if (typeof value === 'string') {
    return <span className="text-slate-700">"{value}"</span>
  }
  if (Array.isArray(value)) {
    return (
      <div className={`mt-1 space-y-2 ${depth > 0 ? 'ps-4 border-s-2 border-slate-100' : ''}`}>
        {value.map((item, idx) => (
          <div key={idx} className="bg-slate-50 rounded-lg p-3 text-xs">
            {typeof item === 'object' && item !== null ? (
              <ConfigObject obj={item as Record<string, unknown>} depth={depth + 1} />
            ) : (
              renderConfigValue(item, depth + 1)
            )}
          </div>
        ))}
      </div>
    )
  }
  if (typeof value === 'object') {
    return <ConfigObject obj={value as Record<string, unknown>} depth={depth + 1} />
  }
  return <span className="text-slate-700">{String(value)}</span>
}

function ConfigObject({ obj, depth = 0 }: { obj: Record<string, unknown>; depth?: number }) {
  const CONFIG_KEY_LABELS: Record<string, string> = {
    delay_hours: 'التأخير (ساعة)',
    delay_days: 'التأخير (أيام)',
    discount_percent: 'نسبة الخصم %',
    discount_code: 'كود الخصم',
    message_count: 'عدد الرسائل',
    steps: 'الخطوات',
    step: 'خطوة',
    template: 'القالب',
    trigger_after_hours: 'الإرسال بعد (ساعة)',
    trigger_after_days: 'الإرسال بعد (أيام)',
    inactivity_days: 'أيام الخمول',
    min_spend: 'الحد الأدنى للإنفاق',
    send_hour: 'وقت الإرسال',
    enabled: 'مُفعّل',
    max_messages: 'أقصى رسائل',
    confidence_threshold: 'حد الثقة',
  }

  return (
    <div className={`space-y-1.5 ${depth > 0 ? 'ps-4' : ''}`}>
      {Object.entries(obj).map(([key, val]) => (
        <div key={key} className="flex items-start gap-2 text-xs">
          <span className="text-slate-500 shrink-0 min-w-0 font-medium">
            {CONFIG_KEY_LABELS[key] ?? key}:
          </span>
          <span className="text-start">{renderConfigValue(val, depth)}</span>
        </div>
      ))}
    </div>
  )
}

// ── AutomationCard ────────────────────────────────────────────────────────────

interface AutomationCardProps {
  automation: AutomationRecord
  onToggle: (id: number, enabled: boolean) => void
}

function AutomationCard({ automation, onToggle }: AutomationCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [toggling, setToggling] = useState(false)

  const meta = AUTOMATION_META[automation.automation_type]

  const triggerVariantMap: Record<string, 'amber' | 'blue' | 'purple' | 'green' | 'slate'> = {
    amber: 'amber',
    blue: 'blue',
    purple: 'purple',
    emerald: 'green',
    green: 'green',
    brand: 'blue',
  }
  const triggerVariant = triggerVariantMap[meta.color] ?? 'slate'

  const handleToggle = async (next: boolean) => {
    if (toggling) return
    setToggling(true)
    onToggle(automation.id, next)
    try {
      await automationsApi.toggle(automation.id, next)
    } catch {
      // revert on failure
      onToggle(automation.id, !next)
    } finally {
      setToggling(false)
    }
  }

  const isAbandonedCart = automation.automation_type === 'abandoned_cart'
  const steps =
    isAbandonedCart && Array.isArray((automation.config as Record<string, unknown>).steps)
      ? ((automation.config as Record<string, unknown>).steps as Record<string, unknown>[])
      : null

  return (
    <div className={`card overflow-hidden transition-all duration-200 ${automation.enabled ? 'ring-1 ring-emerald-200' : ''}`}>
      {/* Header row */}
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0">
            <span className="text-2xl leading-none mt-0.5 shrink-0">{meta.icon}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="text-sm font-semibold text-slate-900">{automation.name || meta.label}</h3>
                <Badge
                  label={automation.enabled ? 'مُفعّل' : 'معطّل'}
                  variant={automation.enabled ? 'green' : 'slate'}
                  dot
                />
              </div>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">{meta.desc}</p>
            </div>
          </div>
          <Toggle
            enabled={automation.enabled}
            onChange={handleToggle}
            disabled={toggling}
          />
        </div>

        {/* Trigger + Template row */}
        <div className="flex items-center gap-4 mt-4 flex-wrap">
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-slate-400">المُشغِّل:</span>
            <Badge label={meta.trigger} variant={triggerVariant} />
          </div>
          {automation.template_name && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-slate-400">القالب:</span>
              <span className="text-xs font-medium text-slate-700 bg-slate-50 px-2 py-0.5 rounded-md border border-slate-200">
                {automation.template_name}
              </span>
            </div>
          )}
        </div>

        {/* Stats row */}
        <div className="flex items-center gap-5 mt-4 pt-4 border-t border-slate-100">
          <div className="text-center">
            <p className="text-base font-bold text-slate-900">{automation.stats_triggered.toLocaleString('ar-SA')}</p>
            <p className="text-xs text-slate-400 mt-0.5">مُشغَّل</p>
          </div>
          <div className="w-px h-6 bg-slate-100" />
          <div className="text-center">
            <p className="text-base font-bold text-slate-900">{automation.stats_sent.toLocaleString('ar-SA')}</p>
            <p className="text-xs text-slate-400 mt-0.5">مُرسَل</p>
          </div>
          <div className="w-px h-6 bg-slate-100" />
          <div className="text-center">
            <p className="text-base font-bold text-emerald-600">{automation.stats_converted.toLocaleString('ar-SA')}</p>
            <p className="text-xs text-slate-400 mt-0.5">تحويل</p>
          </div>
          {automation.stats_sent > 0 && (
            <>
              <div className="w-px h-6 bg-slate-100" />
              <div className="text-center">
                <p className="text-base font-bold text-brand-600">
                  {((automation.stats_converted / automation.stats_sent) * 100).toFixed(1)}%
                </p>
                <p className="text-xs text-slate-400 mt-0.5">معدل التحويل</p>
              </div>
            </>
          )}

          <button
            type="button"
            onClick={() => setExpanded(v => !v)}
            className="ms-auto flex items-center gap-1.5 text-xs text-slate-500 hover:text-brand-600 transition-colors px-2.5 py-1.5 rounded-lg hover:bg-brand-50"
          >
            <Settings2 className="w-3.5 h-3.5" />
            <span>تعديل الإعداد</span>
            {expanded ? (
              <ChevronUp className="w-3.5 h-3.5" />
            ) : (
              <ChevronDown className="w-3.5 h-3.5" />
            )}
          </button>
        </div>
      </div>

      {/* Expandable config panel */}
      {expanded && (
        <div className="border-t border-slate-100 bg-slate-50 px-5 py-4">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">
            إعدادات الأتمتة
          </p>

          {steps ? (
            <div className="space-y-3">
              {steps.map((step, idx) => (
                <div key={idx} className="bg-white rounded-xl border border-slate-200 p-3">
                  <p className="text-xs font-semibold text-slate-700 mb-2">
                    الخطوة {idx + 1}
                  </p>
                  <ConfigObject obj={step} />
                </div>
              ))}
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-slate-200 p-3">
              <ConfigObject obj={automation.config as Record<string, unknown>} />
            </div>
          )}

          {/* Template variable mapping panel */}
          {automation.template_name && (
            <TemplateVarMapPanel templateName={automation.template_name} />
          )}
          {!automation.template_name && (automation.config as Record<string, unknown>).template_name && (
            <TemplateVarMapPanel templateName={String((automation.config as Record<string, unknown>).template_name)} />
          )}

          <p className="text-xs text-slate-400 mt-3 flex items-center gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            التعديل على الإعدادات متاح من لوحة الإعدادات المتقدمة.
          </p>
        </div>
      )}
    </div>
  )
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function AutomationSkeleton() {
  return (
    <div className="animate-pulse bg-slate-100 rounded-xl h-40" />
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SmartAutomations() {
  const { t } = useLanguage()

  const [automations, setAutomations] = useState<AutomationRecord[]>([])
  const [autopilot, setAutopilot] = useState(false)
  const [autopilotLoading, setAutopilotLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await automationsApi.list()
      setAutomations(data.automations)
      setAutopilot(data.autopilot_enabled)
    } catch {
      setError('تعذّر تحميل بيانات الأتمتة. يرجى المحاولة مرة أخرى.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
  }, [loadData])

  const handleAutopilot = async (next: boolean) => {
    setAutopilot(next) // optimistic
    setAutopilotLoading(true)
    try {
      const res = await automationsApi.setAutopilot(next)
      setAutopilot(res.autopilot_enabled)
    } catch {
      setAutopilot(!next) // revert
    } finally {
      setAutopilotLoading(false)
    }
  }

  const handleToggleAutomation = (id: number, enabled: boolean) => {
    setAutomations(prev =>
      prev.map(a => (a.id === id ? { ...a, enabled } : a))
    )
  }

  // ── Computed stats ──
  const enabledCount = automations.filter(a => a.enabled).length
  const totalSent = automations.reduce((sum, a) => sum + a.stats_sent, 0)
  const totalConverted = automations.reduce((sum, a) => sum + a.stats_converted, 0)
  const conversionRate =
    totalSent > 0 ? ((totalConverted / totalSent) * 100).toFixed(1) : '0.0'

  return (
    <div className="space-y-6">
      <PageHeader
        title="التشغيل التلقائي الذكي"
        subtitle="أتمتة تسويقية مبنية على سلوك العملاء"
      />

      {/* ── Master autopilot card ── */}
      <div className="card p-5">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className="w-11 h-11 bg-brand-50 rounded-xl flex items-center justify-center shrink-0">
              <Sparkles className="w-6 h-6 text-brand-600" />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-base font-bold text-slate-900">طيّار التسويق التلقائي</h2>
                <Badge
                  label={autopilot ? 'مُفعّل' : 'غير مُفعّل'}
                  variant={autopilot ? 'green' : 'slate'}
                  dot
                />
              </div>
              <p className="text-sm text-slate-500 mt-1 leading-relaxed max-w-lg">
                عند التفعيل، تتولى نهلة إدارة العربات المتروكة وتذكيرات إعادة الطلب وحملات الاسترجاع تلقائياً دون تدخل منك.
              </p>
            </div>
          </div>
          <div className="shrink-0">
            <Toggle
              enabled={autopilot}
              onChange={handleAutopilot}
              size="lg"
              disabled={autopilotLoading}
            />
          </div>
        </div>
      </div>

      {/* ── Stats row ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="أتمتة مُفعّلة"
          value={String(enabledCount)}
          icon={Zap}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label="رسائل أُرسلت"
          value={totalSent.toLocaleString('ar-SA')}
          icon={Send}
          iconColor="text-blue-600"
          iconBg="bg-blue-50"
        />
        <StatCard
          label="تحويلات"
          value={totalConverted.toLocaleString('ar-SA')}
          icon={CheckCircle}
          iconColor="text-purple-600"
          iconBg="bg-purple-50"
        />
        <StatCard
          label="معدل التحويل"
          value={`${conversionRate}%`}
          icon={TrendingUp}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
      </div>

      {/* ── Compliance notice ── */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-3">
        <AlertCircle className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
        <p className="text-sm text-blue-700">
          جميع الرسائل التلقائية تستخدم قوالب واتساب معتمدة من Meta فقط.
        </p>
      </div>

      {/* ── Error state ── */}
      {error && (
        <div className="flex items-center justify-between gap-3 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-red-500 shrink-0" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
          <button
            type="button"
            onClick={loadData}
            className="flex items-center gap-1.5 text-xs text-red-600 hover:text-red-700 font-medium"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            إعادة المحاولة
          </button>
        </div>
      )}

      {/* ── Automations grid ── */}
      {loading ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <AutomationSkeleton key={i} />
          ))}
        </div>
      ) : !error && automations.length === 0 ? (
        <div className="card p-10 text-center text-slate-400">
          <Zap className="w-10 h-10 mx-auto mb-3 opacity-30" />
          <p className="text-sm">لا توجد أتمتة مُهيَّأة بعد.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {automations.map(automation => (
            <AutomationCard
              key={automation.id}
              automation={automation}
              onToggle={handleToggleAutomation}
            />
          ))}
        </div>
      )}
    </div>
  )
}
