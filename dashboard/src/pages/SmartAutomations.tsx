import React, { useState, useEffect, useCallback } from 'react'
import {
  Zap, Send, CheckCircle, TrendingUp, Sparkles,
  ChevronDown, ChevronUp, AlertCircle, RefreshCw,
  Settings2, ArrowRight, ShoppingCart,
  Package, RotateCcw,
  Clock, Phone, ExternalLink,
  RefreshCcw, Rocket, HeartHandshake, Brain,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import StatCard from '../components/ui/StatCard'
import {
  automationsApi,
  AutomationRecord,
  AUTOMATION_META,
  type EngineKey,
  type EngineSummary,
} from '../api/automations'
import {
  autopilotApi,
  type AutopilotQueues,
  type AbandonedCartItem,
  type PredictiveReorderItem,
  type OrderStatusUpdateItem,
  ORDER_STATUS_LABELS,
  ORDER_STATUS_COLORS,
} from '../api/autopilot'

// ── Template variable map panel ───────────────────────────────────────────────

const STATIC_VAR_MAPS: Record<string, Record<string, string>> = {
  order_status_update_ar: {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'رقم الطلب',
    '{{3}}': 'حالة الطلب',
  },
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

function TemplateVarMapPanel({ templateName }: { templateName: string }): React.ReactNode {
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

// ── Operational Queues ────────────────────────────────────────────────────────

type QueueTab = 'order_status' | 'abandoned_carts' | 'predictive_reorder'

const STATUS_COLOR_MAP: Record<string, string> = {
  amber:  'bg-amber-100 text-amber-700 border-amber-200',
  blue:   'bg-blue-100 text-blue-700 border-blue-200',
  purple: 'bg-purple-100 text-purple-700 border-purple-200',
  green:  'bg-emerald-100 text-emerald-700 border-emerald-200',
  red:    'bg-red-100 text-red-700 border-red-200',
  orange: 'bg-orange-100 text-orange-700 border-orange-200',
  teal:   'bg-teal-100 text-teal-700 border-teal-200',
  slate:  'bg-slate-100 text-slate-600 border-slate-200',
}

function StatusBadge({ status }: { status: string }) {
  const label = ORDER_STATUS_LABELS[status] ?? status
  const color = ORDER_STATUS_COLORS[status] ?? 'slate'
  const cls = STATUS_COLOR_MAP[color] ?? STATUS_COLOR_MAP.slate
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium border ${cls}`}>
      {label}
    </span>
  )
}

function OrderStatusQueue({ items }: { items: OrderStatusUpdateItem[] }) {
  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <Package className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد طلبات بانتظار الإشعار</p>
      </div>
    )
  }
  return (
    <div className="divide-y divide-slate-100">
      {items.map((item) => (
        <div key={item.order_id} className="flex items-center justify-between gap-3 py-3 px-1">
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-800 truncate">{item.customer_name}</p>
            <div className="flex items-center gap-2 mt-0.5 flex-wrap">
              {item.external_id && (
                <span className="text-xs text-slate-400">#{item.external_id}</span>
              )}
              {item.customer_phone && (
                <span className="flex items-center gap-1 text-xs text-slate-400">
                  <Phone className="w-3 h-3" />{item.customer_phone}
                </span>
              )}
            </div>
          </div>
          <div className="shrink-0 flex items-center gap-2">
            {item.previous_status && (
              <>
                <StatusBadge status={item.previous_status} />
                <ArrowRight className="w-3 h-3 text-slate-300" />
              </>
            )}
            <StatusBadge status={item.status} />
          </div>
        </div>
      ))}
    </div>
  )
}

function AbandonedCartsQueue({ items }: { items: AbandonedCartItem[] }) {
  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <ShoppingCart className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد سلات متروكة</p>
      </div>
    )
  }
  return (
    <div className="divide-y divide-slate-100">
      {items.map((item) => (
        <div key={item.order_id} className="flex items-center justify-between gap-3 py-3 px-1">
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-800 truncate">{item.customer_name}</p>
            <div className="flex items-center gap-2 mt-0.5 flex-wrap">
              {item.customer_phone && (
                <span className="flex items-center gap-1 text-xs text-slate-400">
                  <Phone className="w-3 h-3" />{item.customer_phone}
                </span>
              )}
              {item.total > 0 && (
                <span className="text-xs font-medium text-slate-600">
                  {item.total.toLocaleString('ar-SA')} ر.س
                </span>
              )}
            </div>
          </div>
          <div className="shrink-0 flex items-center gap-2">
            <StatusBadge status="abandoned" />
            {item.checkout_url && (
              <a
                href={item.checkout_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-brand-500 hover:text-brand-700"
              >
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function PredictiveReorderQueue({ items }: { items: PredictiveReorderItem[] }) {
  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <RotateCcw className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد تقديرات إعادة طلب مستحقة هذا الأسبوع</p>
      </div>
    )
  }
  return (
    <div className="divide-y divide-slate-100">
      {items.map((item) => (
        <div key={item.estimate_id} className="flex items-center justify-between gap-3 py-3 px-1">
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-800 truncate">{item.customer_name}</p>
            <p className="text-xs text-slate-500 mt-0.5 truncate">{item.product_name}</p>
          </div>
          <div className="shrink-0 flex items-center gap-2">
            <span className={`flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full border ${
              item.days_remaining <= 1
                ? 'bg-red-50 text-red-600 border-red-200'
                : item.days_remaining <= 3
                ? 'bg-amber-50 text-amber-700 border-amber-200'
                : 'bg-blue-50 text-blue-700 border-blue-200'
            }`}>
              <Clock className="w-3 h-3" />
              {item.days_remaining === 0 ? 'اليوم' : `${item.days_remaining} أيام`}
            </span>
          </div>
        </div>
      ))}
    </div>
  )
}

interface OperationalQueuesProps {
  queues: AutopilotQueues | null
  loading: boolean
  onRefresh: () => void
}

function OperationalQueues({ queues, loading, onRefresh }: OperationalQueuesProps) {
  const [activeTab, setActiveTab] = useState<QueueTab>('order_status')

  const tabs: { id: QueueTab; label: string; icon: React.ReactNode; count: number }[] = [
    {
      id: 'order_status',
      label: 'تحديثات الطلبات',
      icon: <Package className="w-3.5 h-3.5" />,
      count: queues?.order_status_updates.length ?? 0,
    },
    {
      id: 'abandoned_carts',
      label: 'السلات المتروكة',
      icon: <ShoppingCart className="w-3.5 h-3.5" />,
      count: queues?.abandoned_carts.length ?? 0,
    },
    {
      id: 'predictive_reorder',
      label: 'إعادة الطلب التنبؤي',
      icon: <RotateCcw className="w-3.5 h-3.5" />,
      count: queues?.predictive_reorder.length ?? 0,
    },
  ]

  return (
    <div className="card overflow-hidden">
      {/* Header */}
      <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">قوائم الانتظار التشغيلية</h3>
          <p className="text-xs text-slate-400 mt-0.5">البنود المنتظرة لإرسال إشعار واتساب</p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-brand-600 px-2.5 py-1.5 rounded-lg hover:bg-brand-50 transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-slate-100 bg-slate-50/60">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors ${
              activeTab === tab.id
                ? 'text-brand-700 border-b-2 border-brand-500 bg-white'
                : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.count > 0 && (
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${
                activeTab === tab.id
                  ? 'bg-brand-100 text-brand-700'
                  : 'bg-slate-200 text-slate-600'
              }`}>
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="px-5 py-2 max-h-80 overflow-y-auto">
        {loading ? (
          <div className="space-y-3 py-4">
            {[1, 2, 3].map(i => (
              <div key={i} className="animate-pulse flex items-center justify-between gap-3">
                <div className="space-y-1.5 flex-1">
                  <div className="h-3 bg-slate-100 rounded w-32" />
                  <div className="h-2.5 bg-slate-100 rounded w-24" />
                </div>
                <div className="h-5 bg-slate-100 rounded-full w-20" />
              </div>
            ))}
          </div>
        ) : (
          <>
            {activeTab === 'order_status' && (
              <OrderStatusQueue items={queues?.order_status_updates ?? []} />
            )}
            {activeTab === 'abandoned_carts' && (
              <AbandonedCartsQueue items={queues?.abandoned_carts ?? []} />
            )}
            {activeTab === 'predictive_reorder' && (
              <PredictiveReorderQueue items={queues?.predictive_reorder ?? []} />
            )}
          </>
        )}
      </div>

      {/* Footer note */}
      <div className="px-5 py-3 border-t border-slate-100 bg-slate-50/60">
        <p className="text-[11px] text-slate-400 flex items-center gap-1.5">
          <AlertCircle className="w-3 h-3 shrink-0" />
          يُرسل الطيار الآلي إشعارات واتساب لهذه البنود تلقائياً عند تفعيله.
        </p>
      </div>
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

  // Discount source (promotion vs coupon vs none) — derived from config so
  // a merchant who edits the config from /promotions sees the badge update.
  const cfg = (automation.config || {}) as Record<string, unknown>
  const stepsForSource = Array.isArray(cfg.steps) ? (cfg.steps as Record<string, unknown>[]) : []
  const stepHasCoupon = stepsForSource.some(
    s => s.auto_coupon === true || s.message_type === 'coupon' || s.discount_source === 'coupon',
  )
  const stepUsesPromotion = stepsForSource.some(s => s.discount_source === 'promotion')
  const discountSource: 'promotion' | 'coupon' | 'none' =
    cfg.discount_source === 'promotion' || stepUsesPromotion
      ? 'promotion'
      : cfg.discount_source === 'coupon' || cfg.auto_coupon === true || stepHasCoupon
      ? 'coupon'
      : 'none'

  const discountSourceMeta: Record<typeof discountSource, { label: string; variant: 'amber' | 'purple' | 'slate' }> = {
    promotion: { label: '🎁 عرض ترويجي',     variant: 'purple' },
    coupon:    { label: '🎟️ كوبون شخصي',     variant: 'amber'  },
    none:      { label: 'بدون خصم',           variant: 'slate'  },
  }
  const dsMeta = discountSourceMeta[discountSource]

  return (
    <div className={`card overflow-hidden transition-all duration-200 ${automation.enabled ? 'ring-1 ring-emerald-200' : ''}`}>
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
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-slate-400">الخصم:</span>
            <Badge label={dsMeta.label} variant={dsMeta.variant} />
          </div>
        </div>

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

          {automation.template_name && (
            <TemplateVarMapPanel templateName={automation.template_name} />
          )}
          {!automation.template_name && !!(automation.config as Record<string, unknown>).template_name && (
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
  return <div className="animate-pulse bg-slate-100 rounded-xl h-40" />
}

// ── Engine metadata (icon + accent color per engine) ─────────────────────────

const ENGINE_DISPLAY: Record<EngineKey, {
  icon: React.ComponentType<{ className?: string }>
  iconColor: string
  iconBg: string
  accent: string
}> = {
  recovery: {
    icon: RefreshCcw,
    iconColor: 'text-amber-600',
    iconBg: 'bg-amber-50',
    accent: 'border-amber-200',
  },
  growth: {
    icon: Rocket,
    iconColor: 'text-emerald-600',
    iconBg: 'bg-emerald-50',
    accent: 'border-emerald-200',
  },
  experience: {
    icon: HeartHandshake,
    iconColor: 'text-blue-600',
    iconBg: 'bg-blue-50',
    accent: 'border-blue-200',
  },
  intelligence: {
    icon: Brain,
    iconColor: 'text-purple-600',
    iconBg: 'bg-purple-50',
    accent: 'border-purple-200',
  },
}


// ── EngineSection: one collapsible section per engine ─────────────────────────

interface EngineSectionProps {
  engine: EngineSummary
  automations: AutomationRecord[]
  onToggleAutomation: (id: number, enabled: boolean) => void
  onToggleEngine: (engine: EngineKey, enabled: boolean) => Promise<void>
  defaultOpen: boolean
}

function EngineSection({
  engine,
  automations,
  onToggleAutomation,
  onToggleEngine,
  defaultOpen,
}: EngineSectionProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [toggling, setToggling] = useState(false)
  const display = ENGINE_DISPLAY[engine.engine]
  const IconCmp = display.icon

  const items = automations.filter(a => a.engine === engine.engine)
  const showEmpty = engine.available && items.length === 0

  const handleEngineToggle = async (next: boolean) => {
    if (!engine.available || toggling) return
    setToggling(true)
    try {
      await onToggleEngine(engine.engine, next)
    } finally {
      setToggling(false)
    }
  }

  return (
    <section className={`card overflow-hidden border ${display.accent}`}>
      {/* Header */}
      <header className="px-5 py-4 flex items-start justify-between gap-4 bg-white">
        <button
          type="button"
          className="flex items-start gap-3 text-start min-w-0 flex-1"
          onClick={() => setOpen(v => !v)}
        >
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${display.iconBg}`}>
            <IconCmp className={`w-5 h-5 ${display.iconColor}`} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-sm font-bold text-slate-900">{engine.name}</h2>
              {!engine.available ? (
                <Badge label="قريباً" variant="slate" />
              ) : (
                <Badge
                  label={engine.enabled ? 'مُفعّل' : 'متوقف'}
                  variant={engine.enabled ? 'green' : 'slate'}
                  dot
                />
              )}
              <span className="text-[11px] text-slate-400">
                {engine.active_automations}/{engine.automations_count} أتمتة نشطة
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-1">{engine.description}</p>
          </div>
        </button>
        <div className="flex items-center gap-2 shrink-0">
          {engine.available && (
            <Toggle
              enabled={engine.enabled}
              onChange={handleEngineToggle}
              disabled={toggling || engine.automations_count === 0}
              size="sm"
            />
          )}
          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            className="p-1.5 text-slate-400 hover:text-slate-700 rounded-lg hover:bg-slate-50"
            aria-label={open ? 'إغلاق' : 'فتح'}
          >
            {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </button>
        </div>
      </header>

      {/* KPIs */}
      <div className="grid grid-cols-3 divide-x divide-slate-100 [direction:rtl] border-t border-slate-100 bg-slate-50/40">
        <div className="px-4 py-3 text-center">
          <p className="text-xs text-slate-400">رسائل آخر 30 يوم</p>
          <p className="text-base font-bold text-slate-900 mt-0.5">
            {engine.kpis.messages_sent_30d.toLocaleString('ar-SA')}
          </p>
        </div>
        <div className="px-4 py-3 text-center">
          <p className="text-xs text-slate-400">طلبات منسوبة</p>
          <p className="text-base font-bold text-slate-900 mt-0.5">
            {engine.kpis.orders_attributed_30d.toLocaleString('ar-SA')}
          </p>
        </div>
        <div className="px-4 py-3 text-center">
          <p className="text-xs text-slate-400">إيرادات (ر.س)</p>
          <p className="text-base font-bold text-emerald-700 mt-0.5">
            {engine.kpis.revenue_sar_30d.toLocaleString('ar-SA', { maximumFractionDigits: 2 })}
          </p>
        </div>
      </div>

      {/* Body */}
      {open && (
        <div className="px-5 py-5 border-t border-slate-100 space-y-4">
          {!engine.available && (
            <div className="rounded-xl border border-dashed border-slate-200 bg-white px-4 py-6 text-center">
              <p className="text-sm font-medium text-slate-700">قيد التطوير</p>
              <p className="text-xs text-slate-400 mt-1 max-w-md mx-auto">
                {engine.engine === 'experience'
                  ? 'سيوفّر هذا المحرك رسائل الشكر، طلب التقييم، واقتراح المنتجات المكملة بعد الشراء.'
                  : 'سيقوم هذا المحرك بتحليل العملاء واقتراح الحملات وتحسين الرسائل وتوقيت الإرسال تلقائياً.'}
              </p>
            </div>
          )}
          {showEmpty && (
            <div className="rounded-xl border border-dashed border-slate-200 bg-white px-4 py-6 text-center">
              <p className="text-sm text-slate-500">لا توجد أتمتات في هذا المحرك بعد.</p>
            </div>
          )}
          {items.length > 0 && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {items.map(automation => (
                <AutomationCard
                  key={automation.id}
                  automation={automation}
                  onToggle={onToggleAutomation}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}


// ── Engine KPI strip ─────────────────────────────────────────────────────────

function EngineKpiStrip({ engines }: { engines: EngineSummary[] }) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      {engines.map(eng => {
        const display = ENGINE_DISPLAY[eng.engine]
        return (
          <div key={eng.engine} className="card p-4">
            <div className="flex items-start gap-3">
              <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${display.iconBg}`}>
                <display.icon className={`w-5 h-5 ${display.iconColor}`} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="text-xs font-semibold text-slate-700 truncate">{eng.name}</p>
                  {!eng.available && <Badge label="قريباً" variant="slate" />}
                </div>
                <p className="text-lg font-bold text-slate-900 mt-1">
                  {eng.kpis.revenue_sar_30d.toLocaleString('ar-SA', { maximumFractionDigits: 0 })}
                  <span className="text-xs font-normal text-slate-400 ms-1">ر.س / 30 يوم</span>
                </p>
                <div className="flex items-center gap-3 mt-1 text-[11px] text-slate-500">
                  <span>{eng.kpis.messages_sent_30d.toLocaleString('ar-SA')} رسالة</span>
                  <span className="text-slate-300">•</span>
                  <span>{eng.kpis.orders_attributed_30d.toLocaleString('ar-SA')} طلب</span>
                </div>
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}


// ── Main page ─────────────────────────────────────────────────────────────────

export default function SmartAutomations() {
  const [automations, setAutomations] = useState<AutomationRecord[]>([])
  const [engines, setEngines] = useState<EngineSummary[]>([])
  const [autopilot, setAutopilot] = useState(false)
  const [autopilotLoading, setAutopilotLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [queues, setQueues] = useState<AutopilotQueues | null>(null)
  const [queuesLoading, setQueuesLoading] = useState(false)

  const loadQueues = useCallback(async () => {
    setQueuesLoading(true)
    try {
      const q = await autopilotApi.queues()
      setQueues(q)
    } catch {
      // non-critical — queues panel just shows empty
    } finally {
      setQueuesLoading(false)
    }
  }, [])

  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [data, autopilotStatus, summary] = await Promise.all([
        automationsApi.list(),
        autopilotApi.status(),
        automationsApi.enginesSummary(30),
      ])
      setAutomations(data.automations)
      setAutopilot(Boolean(autopilotStatus.settings.enabled))
      setEngines(summary.engines)
    } catch (e) {
      const message = e instanceof Error ? e.message : ''
      if (message.includes('402') || message.includes('خطة نحلة') || message.includes('التجربة')) {
        setError(message)
      } else {
        setError('تعذّر تحميل بيانات الأتمتة. يرجى المحاولة مرة أخرى.')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
    loadQueues()
  }, [loadData, loadQueues])

  const handleAutopilot = async (next: boolean) => {
    setAutopilot(next)
    setAutopilotLoading(true)
    try {
      const res = await autopilotApi.save({ enabled: next })
      setAutopilot(Boolean(res.settings.enabled))
      setError(null)
    } catch (e) {
      const message = e instanceof Error ? e.message : 'تعذّر تحديث حالة الطيار الآلي.'
      setAutopilot(!next)
      setError(message)
    } finally {
      setAutopilotLoading(false)
    }
  }

  const handleToggleAutomation = (id: number, enabled: boolean) => {
    setAutomations(prev => prev.map(a => (a.id === id ? { ...a, enabled } : a)))
    // Optimistically update the engine's active_automations count.
    setEngines(prev => prev.map(eng => {
      const auto = automations.find(a => a.id === id)
      if (!auto || auto.engine !== eng.engine) return eng
      const delta = enabled ? 1 : -1
      const next = Math.max(0, eng.active_automations + delta)
      return { ...eng, active_automations: next, enabled: next > 0 }
    }))
  }

  const handleToggleEngine = async (engineKey: EngineKey, enabled: boolean) => {
    // Optimistic update.
    const prevEngines = engines
    const prevAutomations = automations
    setEngines(prev => prev.map(e =>
      e.engine === engineKey
        ? { ...e, enabled, active_automations: enabled ? e.automations_count : 0 }
        : e,
    ))
    setAutomations(prev => prev.map(a => (a.engine === engineKey ? { ...a, enabled } : a)))
    try {
      await automationsApi.toggleEngine(engineKey, enabled)
    } catch (e) {
      // Roll back.
      setEngines(prevEngines)
      setAutomations(prevAutomations)
      const message = e instanceof Error ? e.message : 'تعذّر تحديث حالة المحرك.'
      setError(message)
    }
  }

  const enabledCount   = automations.filter(a => a.enabled).length
  const totalSent      = engines.reduce((s, e) => s + e.kpis.messages_sent_30d, 0)
  const totalAttributed = engines.reduce((s, e) => s + e.kpis.orders_attributed_30d, 0)
  const totalRevenue   = engines.reduce((s, e) => s + e.kpis.revenue_sar_30d, 0)

  return (
    <div className="space-y-6">
      <PageHeader
        title="الطيار الآلي"
        subtitle="مركز تشغيل المبيعات الذكي — 4 محركات تعمل تلقائياً"
      />

      {/* ── Master autopilot toggle ── */}
      <div className="card p-5">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-start gap-4">
            <div className="w-11 h-11 bg-brand-50 rounded-xl flex items-center justify-center shrink-0">
              <Sparkles className="w-6 h-6 text-brand-600" />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h2 className="text-base font-bold text-slate-900">المفتاح الرئيسي للطيار الآلي</h2>
                <Badge
                  label={autopilot ? 'مُفعّل' : 'متوقف'}
                  variant={autopilot ? 'green' : 'slate'}
                  dot
                />
              </div>
              <p className="text-sm text-slate-500 mt-1 leading-relaxed max-w-lg">
                عند الإيقاف، تتوقف جميع المحركات حتى لو كانت أتمتاتها مُفعّلة. عند التشغيل، يبدأ كل محرك بالعمل وفق إعداداته.
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

      {/* ── Top KPI strip — one card per engine ── */}
      {!loading && engines.length > 0 && (
        <EngineKpiStrip engines={engines} />
      )}

      {/* ── Aggregate stats row ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="أتمتة مُفعّلة"
          value={String(enabledCount)}
          icon={Zap}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label="رسائل آخر 30 يوم"
          value={totalSent.toLocaleString('ar-SA')}
          icon={Send}
          iconColor="text-blue-600"
          iconBg="bg-blue-50"
        />
        <StatCard
          label="طلبات منسوبة"
          value={totalAttributed.toLocaleString('ar-SA')}
          icon={CheckCircle}
          iconColor="text-purple-600"
          iconBg="bg-purple-50"
        />
        <StatCard
          label="إيرادات (ر.س)"
          value={totalRevenue.toLocaleString('ar-SA', { maximumFractionDigits: 0 })}
          icon={TrendingUp}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
      </div>

      {/* ── Operational queues ── */}
      <OperationalQueues
        queues={queues}
        loading={queuesLoading}
        onRefresh={loadQueues}
      />

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

      {/* ── Engines ── */}
      {loading ? (
        <div className="grid grid-cols-1 gap-4">
          {Array.from({ length: 4 }).map((_, i) => <AutomationSkeleton key={i} />)}
        </div>
      ) : !error && (
        <div className="space-y-5">
          {engines.map(engine => (
            <EngineSection
              key={engine.engine}
              engine={engine}
              automations={automations}
              onToggleAutomation={handleToggleAutomation}
              onToggleEngine={handleToggleEngine}
              defaultOpen={engine.available && engine.automations_count > 0}
            />
          ))}
        </div>
      )}
    </div>
  )
}
