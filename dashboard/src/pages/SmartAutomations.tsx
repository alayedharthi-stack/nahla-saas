import React, { useState, useEffect, useCallback } from 'react'
import {
  Zap, Send, CheckCircle, TrendingUp, Sparkles,
  ChevronDown, ChevronUp, AlertCircle, RefreshCw,
  Settings2, ArrowRight, ShoppingCart,
  Package, RotateCcw,
  Clock, Phone, ExternalLink,
  RefreshCcw, Rocket, HeartHandshake, Brain,
  ShieldCheck, HelpCircle, CreditCard, BadgeAlert,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import StatCard from '../components/ui/StatCard'
import {
  automationsApi,
  AutomationRecord,
  AUTOMATION_META,
  type AutomationType,
  type EngineKey,
  type EngineSummary,
} from '../api/automations'
import {
  autopilotApi,
  type AutopilotQueues,
  type AbandonedCartItem,
  type AbandonedCartRecoveryTimeline,
  type RecoveryStatus,
  type PredictiveReorderItem,
  type OrderStatusUpdateItem,
  type PendingPaymentOrderItem,
  type CodPendingOrderItem,
  type GovernorLogItem,
  type OrderReminderTimeline,
  type OrderReminderStep,
  ORDER_STATUS_LABELS,
  ORDER_STATUS_COLORS,
  GOVERNOR_REASON_META,
} from '../api/autopilot'
import AbandonedCartEditor from './AbandonedCartEditor'
import { formatRiyadh, formatRelativeRiyadh } from '../lib/datetime'

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
  title?: string
}

function Toggle({ enabled, onChange, size = 'sm', disabled = false, title }: ToggleProps) {
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
      aria-label={title}
      title={title}
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

type QueueTab = 'order_status' | 'abandoned_carts' | 'predictive_reorder' | 'pending_payment' | 'cod_pending'

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

// ── Recovery status badge (per-cart, derived) ────────────────────────────────
//
// Maps the backend ``RecoveryStatus`` taxonomy onto the merchant-facing
// vocabulary the dashboard already uses elsewhere. Always returns
// something printable — never an empty pill.
const RECOVERY_BADGE: Record<string, { label: string; cls: string; tip: string }> = {
  // ── Overall cart statuses ──
  no_recovery: {
    label: 'لم يبدأ',
    cls:   'bg-slate-50 text-slate-500 border-slate-200',
    tip:   'لم يتم إنشاء حدث استعادة لهذه السلة بعد — تحقق من تفعيل الأتمتة وأن العميل لديه رقم جوال.',
  },
  pending: {
    label: 'بانتظار الإرسال',
    cls:   'bg-amber-50 text-amber-700 border-amber-200',
    tip:   'تم جدولة التذكير، سيُرسل في الجولة التالية للمحرك (كل 60 ثانية).',
  },
  in_progress: {
    label: 'قيد المتابعة',
    cls:   'bg-blue-50 text-blue-700 border-blue-200',
    tip:   'تم إرسال تذكير واحد على الأقل، ولا يزال هناك تذكيرات قادمة.',
  },
  completed: {
    label: 'اكتملت التذكيرات',
    cls:   'bg-purple-50 text-purple-700 border-purple-200',
    tip:   'أُرسلت كل التذكيرات المعرّفة دون أن يكمل العميل الشراء.',
  },
  converted: {
    label: 'تم الشراء — أوقفت',
    cls:   'bg-emerald-50 text-emerald-700 border-emerald-200',
    tip:   'العميل أكمل طلباً بعد التذكير — تم إيقاف بقية المراحل تلقائيًا.',
  },
  failed: {
    label: 'فشل الإرسال',
    cls:   'bg-red-50 text-red-700 border-red-200',
    tip:   'فشل آخر تذكير في الإرسال — افتح التفاصيل لمعرفة السبب.',
  },
  // ── Per-step statuses (returned by step-level API) ──
  sent: {
    label: 'تم الإرسال',
    cls:   'bg-green-50 text-green-700 border-green-200',
    tip:   'تم إرسال هذا التذكير بنجاح.',
  },
  skipped: {
    label: 'تم التخطي',
    cls:   'bg-slate-50 text-slate-400 border-slate-200',
    tip:   'تم تخطي هذه المرحلة (تم الشراء أو المرحلة معطلة).',
  },
  upcoming: {
    label: 'قادمة',
    cls:   'bg-indigo-50 text-indigo-500 border-indigo-200',
    tip:   'هذه المرحلة ستُرسل تلقائياً في الموعد المحدد.',
  },
}

function RecoveryStatusBadge({ status }: { status: RecoveryStatus | string }) {
  const meta = RECOVERY_BADGE[status] ?? RECOVERY_BADGE.no_recovery
  return (
    <span
      title={meta.tip}
      className={`inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 border rounded-full ${meta.cls}`}
    >
      {meta.label}
    </span>
  )
}

function RecoveryDrawer({
  cart,
  onClose,
  manualRetryEnabled = false,
  onRetried,
}: {
  cart: AbandonedCartItem
  onClose: () => void
  /** Show the temporary "إعادة الإرسال" button — gated by the backend
   *  feature flag (`AUTOPILOT_ENABLE_MANUAL_RETRY`). */
  manualRetryEnabled?: boolean
  /** Called after a successful retry so the parent can refresh the
   *  queue list (status badge → "قيد المتابعة"). */
  onRetried?: () => void
}) {
  const [timeline, setTimeline] = useState<AbandonedCartRecoveryTimeline | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [retryNotice, setRetryNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const refresh = useCallback(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    autopilotApi
      .abandonedCartRecovery(cart.order_id)
      .then((res) => {
        if (!cancelled) setTimeline(res)
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message || 'تعذر تحميل تفاصيل الاستعادة')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [cart.order_id])

  useEffect(() => {
    return refresh()
  }, [refresh])

  const handleRetry = useCallback(async () => {
    setRetrying(true)
    setRetryNotice(null)
    try {
      const res = await autopilotApi.rescheduleFailedCartSteps(cart.order_id)
      setRetryNotice({
        kind: 'ok',
        text: res.message || `تمت إعادة جدولة ${res.steps_rescheduled} مرحلة فاشلة.`,
      })
      refresh()
      onRetried?.()
    } catch (e) {
      const raw = e instanceof Error ? e.message : ''
      setRetryNotice({
        kind: 'err',
        text: raw || 'تعذّر إعادة الإرسال — حاول مرة أخرى.',
      })
    } finally {
      setRetrying(false)
    }
  }, [cart.order_id, refresh, onRetried])

  // Show button whenever ANY individual step has failed (not just overall status).
  // Overall status stays 'in_progress' when earlier stages sent but last stage failed.
  const canRetry = Boolean(
    manualRetryEnabled
    && timeline
    && timeline.steps.some((s) => s.status === 'failed')
    && timeline.recovery_event_id,
  )

  return (
    <div
      className="fixed inset-0 bg-slate-900/40 z-40 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md h-full bg-white shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-5 border-b border-slate-100 flex items-start justify-between">
          <div>
            <h3 className="text-base font-semibold text-slate-800">تفاصيل الاستعادة</h3>
            <p className="text-xs text-slate-500 mt-1">{cart.customer_name} · {cart.customer_phone || '—'}</p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-sm"
            aria-label="إغلاق"
          >
            ✕
          </button>
        </div>

        <div className="p-5 space-y-4">
          {loading && (
            <div className="text-center py-6 text-slate-400 text-sm">جارٍ التحميل…</div>
          )}
          {error && (
            <div className="text-center py-6 text-red-500 text-sm">{error}</div>
          )}
          {!loading && !error && timeline && (
            <>
              {/* Summary card */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">الحالة</p>
                  <div className="mt-1"><RecoveryStatusBadge status={timeline.status} /></div>
                </div>
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">تذكيرات أُرسلت</p>
                  <p className="mt-1 text-sm font-medium text-slate-700">{timeline.steps_sent}</p>
                </div>
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">آخر إرسال</p>
                  <p className="mt-1 text-xs text-slate-700">{formatRelativeRiyadh(timeline.last_sent_at) || '—'}</p>
                </div>
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">التذكير القادم</p>
                  <p className="mt-1 text-xs text-slate-700">
                    {timeline.next_pending_at ? formatRiyadh(timeline.next_pending_at) : '—'}
                  </p>
                </div>
              </div>

              {timeline.converted_at && (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3">
                  <p className="text-xs text-emerald-700">
                    تم الشراء في <strong>{formatRiyadh(timeline.converted_at)}</strong>
                    {timeline.cancel_reason ? ` (${timeline.cancel_reason})` : ''} — تم
                    إيقاف بقية التذكيرات تلقائيًا.
                  </p>
                </div>
              )}

              {(timeline.last_failure_label || timeline.last_error) && (
                <div className="rounded-lg border border-red-200 bg-red-50 p-3 space-y-2">
                  <div>
                    <p className="text-xs text-red-700">
                      <span className="font-medium">سبب آخر فشل:</span>{' '}
                      {timeline.last_failure_label || timeline.last_error}
                    </p>
                    {timeline.last_failure_code && (
                      <p className="text-[10px] text-red-500/80 mt-0.5 font-mono">
                        ({timeline.last_failure_code})
                      </p>
                    )}
                  </div>
                  {canRetry && (
                    <button
                      type="button"
                      onClick={handleRetry}
                      disabled={retrying}
                      className="text-xs px-3 py-1.5 rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                      title="إعادة جدولة المراحل الفاشلة فقط — المراحل المُرسَلة تبقى كما هي"
                    >
                      <RefreshCw className={`w-3.5 h-3.5 ${retrying ? 'animate-spin' : ''}`} />
                      {retrying ? 'جارٍ الجدولة…' : 'إعادة جدولة المرحلة الفاشلة'}
                    </button>
                  )}
                </div>
              )}

              {retryNotice && (
                <div
                  className={`rounded-lg border p-3 text-xs ${
                    retryNotice.kind === 'ok'
                      ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                      : 'border-amber-200 bg-amber-50 text-amber-700'
                  }`}
                >
                  {retryNotice.text}
                </div>
              )}

              {/* Steps timeline */}
              <div>
                <h4 className="text-xs font-medium text-slate-500 mb-2">
                  خطة سير التذكيرات
                  {timeline.total_stages ? <span className="text-slate-400 font-normal ms-1">({timeline.steps_sent}/{timeline.total_stages})</span> : ''}
                </h4>
                {timeline.steps.length === 0 ? (
                  <p className="text-xs text-slate-400">لا توجد مراحل بعد.</p>
                ) : (
                  <ol className="relative">
                    {timeline.steps.map((step, idx) => {
                      const isLast = idx === timeline.steps.length - 1
                      const isSent = step.status === 'sent'
                      const isFailed = step.status === 'failed'
                      const isUpcoming = step.status === 'upcoming'
                      const isPending = step.status === 'pending'

                      const dotCls = isSent ? 'bg-green-500 ring-green-100'
                        : isFailed ? 'bg-red-500 ring-red-100'
                        : isPending ? 'bg-amber-400 ring-amber-100'
                        : 'bg-indigo-300 ring-indigo-100'

                      const borderCls = isSent ? 'border-green-200 bg-green-50/30'
                        : isFailed ? 'border-red-200 bg-red-50/30'
                        : isPending ? 'border-amber-200 bg-amber-50/30'
                        : isUpcoming ? 'border-slate-100 bg-slate-50/50'
                        : 'border-slate-100'

                      return (
                        <li key={`${step.event_id}-${step.step_idx}`} className="flex gap-3 pb-0">
                          {/* Vertical line + dot */}
                          <div className="flex flex-col items-center">
                            <div className={`shrink-0 w-5 h-5 rounded-full ring-2 flex items-center justify-center text-white text-[10px] font-bold ${dotCls}`}>
                              {step.step_idx}
                            </div>
                            {!isLast && <div className="w-0.5 flex-1 min-h-[16px] bg-slate-200 my-0.5" />}
                          </div>

                          {/* Content */}
                          <div className={`flex-1 min-w-0 rounded-lg border p-2.5 mb-2 ${borderCls}`}>
                            <div className="flex items-center justify-between gap-2">
                              <span className={`text-xs font-medium ${isUpcoming ? 'text-slate-500' : 'text-slate-700'}`}>
                                {(step as any).label || `المرحلة ${step.step_idx}`}
                                {step.is_root ? ' (أساسية)' : ''}
                              </span>
                              <RecoveryStatusBadge status={step.status} />
                            </div>

                            <div className="text-[11px] text-slate-400 mt-0.5">
                              {step.sent_at
                                ? <>أُرسلت {formatRiyadh(step.sent_at)}</>
                                : isPending && step.scheduled_at
                                  ? <>مجدولة {formatRiyadh(step.scheduled_at)}</>
                                  : isUpcoming && step.scheduled_at
                                    ? <>ستُرسل {formatRiyadh(step.scheduled_at)}</>
                                    : ''}
                              {(step as any).delay_label && (
                                <span className="ms-1 text-slate-300">({(step as any).delay_label})</span>
                              )}
                            </div>

                            {step.template_name && (
                              <div className="text-[11px] text-slate-500 mt-0.5">
                                قالب: <span className="font-mono">{step.template_name}</span>
                              </div>
                            )}
                            {step.skip_reason && (
                              <div className="text-[11px] text-amber-700 mt-0.5">
                                تم التخطّي: {step.skip_reason}
                              </div>
                            )}
                            {(step.failure_label || step.error) && isFailed && (
                              <div className="text-[11px] text-red-600 mt-0.5">
                                {step.failure_label || step.error}
                                {step.failure_code && (
                                  <span className="text-red-400/70 font-mono ms-1">({step.failure_code})</span>
                                )}
                              </div>
                            )}
                          </div>
                        </li>
                      )
                    })}
                  </ol>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function AbandonedCartsQueue({
  items,
  manualRetryEnabled = false,
  onRetried,
}: {
  items: AbandonedCartItem[]
  manualRetryEnabled?: boolean
  onRetried?: () => void
}) {
  const [openCart, setOpenCart] = useState<AbandonedCartItem | null>(null)
  const [cleaningStale, setCleaningStale] = useState(false)
  const [staleNotice, setStaleNotice] = useState<string | null>(null)
  const [retryDone, setRetryDone] = useState(false)

  const hasStale = !retryDone && items.some(i => {
    const r = i.recovery
    if (!r) return true
    return (r.status === 'pending' || r.status === 'failed' || r.status === 'no_recovery')
      && r.steps_sent === 0
  })

  const [noticeKind, setNoticeKind] = useState<'ok' | 'warn' | 'err'>('ok')

  const handleCleanStale = async () => {
    setCleaningStale(true)
    setStaleNotice(null)
    try {
      const res = await autopilotApi.retryAllStaleCarts()
      console.log('[NahlaRetry] response:', JSON.stringify(res, null, 2))

      if (!res.ok && res.engine_error) {
        setStaleNotice(res.engine_error)
        setNoticeKind('warn')
      } else {
        const msg = res.message || `تم إعادة جدولة ${res.retried} سلة`
        setStaleNotice(msg)
        setNoticeKind('ok')
        setRetryDone(true)
      }
      setTimeout(() => onRetried?.(), 2000)
    } catch (e) {
      const raw = e instanceof Error ? e.message : String(e)
      let msg: string
      if (raw.includes('انتهت صلاحية الجلسة') || raw.includes('missing_token') || raw.includes('Authentication')) {
        msg = 'انتهت صلاحية الجلسة — يرجى تسجيل الدخول مجدداً'
      } else if (raw.includes('تعذر الوصول') || raw.includes('Failed to fetch') || raw.includes('NetworkError')) {
        msg = 'تعذر الاتصال بالخادم — تحقق من الشبكة وحاول مجدداً'
      } else {
        msg = raw || 'حدث خطأ غير متوقع'
      }
      console.error('[NahlaRetry] error:', raw)
      setStaleNotice(msg)
      setNoticeKind('err')
    } finally {
      setCleaningStale(false)
    }
  }

  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <ShoppingCart className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد سلات متروكة</p>
      </div>
    )
  }
  return (
    <>
      {hasStale && manualRetryEnabled && (
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 px-3 py-2.5 bg-amber-50 border-b border-amber-200 rounded-t-lg">
          <div className="flex items-center gap-2 text-xs text-amber-700">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            <span>يوجد سلات عالقة لم تُرسل تذكيراتها — يمكن إعادة جدولتها دفعة واحدة</span>
          </div>
          <button
            onClick={handleCleanStale}
            disabled={cleaningStale}
            className="text-xs px-3 py-1 rounded-md bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50 shrink-0 flex items-center gap-1.5"
          >
            <RefreshCw className={`w-3 h-3 ${cleaningStale ? 'animate-spin' : ''}`} />
            {cleaningStale ? 'جارٍ...' : 'إعادة جدولة الكل'}
          </button>
        </div>
      )}
      {staleNotice && (
        <div className={`sticky top-10 z-10 px-3 py-2 text-xs border-b ${
          noticeKind === 'ok'   ? 'bg-green-50 text-green-700 border-green-200' :
          noticeKind === 'warn' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                                  'bg-red-50 text-red-700 border-red-200'
        }`}>{staleNotice}</div>
      )}
      <div className="divide-y divide-slate-100">
        {items.map((item) => {
          // Recovery payload is always present (backend never returns null),
          // but we tolerate the legacy shape so an old cached UI build does
          // not crash if it predates the schema bump.
          const recovery = item.recovery ?? {
            status:             'no_recovery' as RecoveryStatus,
            steps_sent:         0,
            steps_failed:       0,
            last_sent_at:       null,
            last_status:        null,
            last_error:         null,
            last_failure_code:  null,
            last_failure_label: null,
            next_pending_at:    null,
            converted_at:       null,
            cancel_reason:      null,
            recovery_event_id:  null,
          }
          return (
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
                  {/* Recovery progress on the queue row itself, so the
                      merchant can scan "who got reminders / who's stuck"
                      without opening every drawer. */}
                  <span className="text-[11px] text-slate-400">
                    {recovery.steps_sent > 0
                      ? <>أُرسل {recovery.steps_sent} تذكير{recovery.steps_sent > 1 ? '' : ''}</>
                      : 'لم يُرسل تذكير بعد'}
                    {recovery.last_sent_at && <> · آخرها {formatRelativeRiyadh(recovery.last_sent_at)}</>}
                  </span>
                  {recovery.status === 'failed' && recovery.last_failure_label && (
                    <span
                      className="text-[11px] text-red-600 truncate max-w-[180px]"
                      title={recovery.last_failure_label}
                    >
                      · {recovery.last_failure_label}
                    </span>
                  )}
                </div>
              </div>
              <div className="shrink-0 flex items-center gap-2">
                <RecoveryStatusBadge status={recovery.status} />
                <button
                  onClick={() => setOpenCart(item)}
                  className="text-[11px] text-slate-500 hover:text-brand-600 underline-offset-2 hover:underline"
                  title="عرض تفاصيل التذكيرات"
                >
                  تفاصيل
                </button>
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
          )
        })}
      </div>

      {openCart && (
        <RecoveryDrawer
          cart={openCart}
          onClose={() => setOpenCart(null)}
          manualRetryEnabled={manualRetryEnabled}
          onRetried={onRetried}
        />
      )}
    </>
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

// ── Order Reminder Timeline Drawer ───────────────────────────────────────────
// Sliding panel that shows the real send status of each emitted reminder stage
// (similar to CartRecoveryDetail for abandoned carts).

function OrderReminderDrawer({
  orderId,
  onClose,
}: {
  orderId: number
  onClose: () => void
}) {
  const [timeline, setTimeline]         = useState<OrderReminderTimeline | null>(null)
  const [loading, setLoading]           = useState(true)
  const [error, setError]               = useState<string | null>(null)
  const [rescheduling, setRescheduling] = useState(false)
  const [rescheduleNotice, setRescheduleNotice] = useState<{
    kind: 'ok' | 'warn' | 'err'
    text: string
  } | null>(null)

  const load = useCallback(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    autopilotApi
      .orderReminderTimeline(orderId)
      .then((res) => { if (!cancelled) setTimeline(res) })
      .catch((e) => { if (!cancelled) setError(e?.message || 'تعذّر تحميل التفاصيل') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [orderId])

  useEffect(() => load(), [load])

  const handleReschedule = useCallback(async () => {
    setRescheduling(true)
    setRescheduleNotice(null)
    try {
      const res = await autopilotApi.rescheduleOrderReminders(orderId)
      if (res.has_template_error && res.steps_cleared === 0) {
        setRescheduleNotice({
          kind: 'warn',
          text: 'القالب غير معتمد من Meta — اعتمد القالب أولاً ثم أعد الجدولة.',
        })
      } else if (res.has_template_error) {
        setRescheduleNotice({
          kind: 'warn',
          text: `أُعيدت جدولة ${res.steps_cleared} مرحلة، لكن بعض المراحل تحتاج اعتماد القالب من Meta.`,
        })
      } else if (res.has_permanent_block) {
        setRescheduleNotice({
          kind: 'warn',
          text: 'لا يمكن إعادة جدولة بعض المراحل (إلغاء اشتراك العميل أو الطلب مغلق).',
        })
      } else {
        setRescheduleNotice({ kind: 'ok', text: res.message })
        load()
      }
    } catch (e) {
      setRescheduleNotice({
        kind: 'err',
        text: e instanceof Error ? e.message : 'تعذّرت إعادة الجدولة — حاول مجدداً.',
      })
    } finally {
      setRescheduling(false)
    }
  }, [orderId, load])

  // Show reschedule button only when there are failed/skipped (non-permanent) steps
  const canReschedule = Boolean(
    timeline &&
    timeline.steps.some(
      (s) => s.status === 'failed' ||
             (s.status === 'skipped' && s.skip_reason !== 'blocked_by_unsubscribe'),
    )
  )
  // Two distinct template failure shapes — surface a different banner
  // copy for each because the merchant action is different:
  //   • not_approved   → go to the Templates page and submit/approve
  //   • param_mismatch → the template IS approved but its variable
  //                       contract doesn't match what Nahla can fill
  //                       (usually because the merchant edited the
  //                        body without keeping the same {{N}} count).
  const hasUnapprovedTemplate = Boolean(
    timeline?.steps.some(
      (s) => (s.status === 'failed' || s.status === 'skipped') &&
             (s.error_code === 'template_not_approved' ||
              s.error_code === 'no_approved_template' ||
              s.skip_reason === 'no_approved_template' ||
              s.error_message?.includes('no_approved_template')),
    )
  )
  const hasTemplateParamMismatch = Boolean(
    timeline?.steps.some(
      (s) => s.status === 'failed' &&
             (s.error_code === 'template_param_mismatch' ||
              s.error_message?.includes('template_param_mismatch')),
    )
  )
  const hasTemplateError = hasUnapprovedTemplate || hasTemplateParamMismatch

  // Step dot colour by status
  const dotCls = (s: OrderReminderStep['status']) => {
    if (s === 'sent')    return 'bg-green-500  ring-green-100'
    if (s === 'failed')  return 'bg-red-500    ring-red-100'
    if (s === 'skipped') return 'bg-amber-400  ring-amber-100'
    if (s === 'pending') return 'bg-indigo-300 ring-indigo-100'
    return 'bg-slate-300 ring-slate-100'   // emitted
  }
  const cardCls = (s: OrderReminderStep['status']) => {
    if (s === 'sent')    return 'border-green-200  bg-green-50/30'
    if (s === 'failed')  return 'border-red-200    bg-red-50/30'
    if (s === 'skipped') return 'border-amber-200  bg-amber-50/30'
    if (s === 'pending') return 'border-indigo-200 bg-indigo-50/20'
    return 'border-slate-100 bg-slate-50/40'   // emitted
  }

  const skipReasonAr: Record<string, string> = {
    blocked_by_unsubscribe:   'إلغاء اشتراك العميل',
    blocked_by_daily_limit:   'تجاوز الحد اليومي',
    blocked_by_weekly_limit:  'تجاوز الحد الأسبوعي',
    blocked_by_cooldown:      'فترة راحة بين الرسائل (6 ساعات)',
    blocked_by_higher_priority: 'أولوية أعلى للخدمة',
    autopilot_disabled:       'الطيار الآلي معطّل',
    order_no_longer_pending:  'تغيّرت حالة الطلب',
  }

  return (
    <div
      className="fixed inset-0 bg-slate-900/40 z-40 flex justify-end"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md h-full bg-white shadow-2xl overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="p-5 border-b border-slate-100 flex items-start justify-between">
          <div>
            <h3 className="text-base font-semibold text-slate-800">سير التذكيرات</h3>
            {timeline && (
              <p className="text-xs text-slate-500 mt-1">
                {timeline.customer_name} · {timeline.order_number}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600"
            aria-label="إغلاق"
          >✕</button>
        </div>

        <div className="p-5 space-y-4">
          {loading && (
            <div className="text-center py-8 text-slate-400 text-sm">جارٍ التحميل…</div>
          )}
          {error && (
            <div className="text-center py-6 text-red-500 text-sm">{error}</div>
          )}
          {!loading && !error && timeline && (
            <>
              {/* Summary */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">حالة الطلب</p>
                  <p className="mt-1 text-xs font-medium text-slate-700">
                    {ORDER_STATUS_LABELS[timeline.order_status] ?? timeline.order_status}
                  </p>
                </div>
                <div className="rounded-lg border border-slate-100 p-3">
                  <p className="text-[11px] text-slate-400">تذكيرات أُرسلت فعلياً</p>
                  <p className="mt-1 text-sm font-semibold text-slate-700">
                    {timeline.steps_sent} / {timeline.total_emitted}
                  </p>
                </div>
              </div>

              {/* Legend */}
              <div className="flex flex-wrap gap-2 text-[11px] text-slate-500 bg-slate-50 rounded-lg p-3">
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-green-500 inline-block"/>أُرسلت للعميل</span>
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-indigo-300 inline-block"/>قيد الإرسال</span>
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-slate-300 inline-block"/>جُدولت (لم تُعالج بعد)</span>
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-amber-400 inline-block"/>تم التخطّي</span>
                <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-red-500 inline-block"/>فشل الإرسال</span>
              </div>

              {/* Actions row: refresh + reschedule */}
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={load}
                  disabled={loading}
                  className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-600"
                >
                  <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
                  تحديث
                </button>

                {canReschedule && (
                  <button
                    type="button"
                    onClick={handleReschedule}
                    disabled={rescheduling}
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    title="إعادة جدولة المراحل الفاشلة فقط — المراحل المُرسَلة تبقى كما هي"
                  >
                    <RefreshCcw className={`w-3.5 h-3.5 ${rescheduling ? 'animate-spin' : ''}`} />
                    {rescheduling ? 'جارٍ إعادة الجدولة…' : 'إعادة جدولة الفاشلة'}
                  </button>
                )}
              </div>

              {/* Template approval warning — shown above reschedule notice */}
              {hasUnapprovedTemplate && (
                <div className="rounded-lg border border-orange-200 bg-orange-50 p-3 flex items-start gap-2">
                  <AlertCircle className="w-4 h-4 text-orange-500 shrink-0 mt-0.5" />
                  <div className="text-xs text-orange-700">
                    <p className="font-medium mb-0.5">القالب غير معتمد من Meta</p>
                    <p>اعتمد قوالب الرسائل من لوحة <strong>القوالب</strong> ثم أعد الجدولة — ستفشل الرسالة مجدداً قبل الاعتماد.</p>
                  </div>
                </div>
              )}
              {hasTemplateParamMismatch && !hasUnapprovedTemplate && (() => {
                // Collect detailed mismatch info from the step's action_taken
                const mismatchSteps = timeline?.steps.filter(
                  (s) => s.status === 'failed' &&
                    (s.error_code === 'template_param_mismatch' ||
                     s.error_message?.includes('template_param_mismatch'))
                ) ?? []
                const firstMismatch = mismatchSteps[0]
                const paramCounts = (firstMismatch as any)?.param_counts as
                  | { expected: { body: number; header: number; buttons: number }; sent: { body: number; header: number; buttons: number } }
                  | undefined
                const tplName = (firstMismatch as any)?.template_name as string | undefined

                return (
                  <div className="rounded-lg border border-orange-200 bg-orange-50 p-3 flex items-start gap-2">
                    <AlertCircle className="w-4 h-4 text-orange-500 shrink-0 mt-0.5" />
                    <div className="text-xs text-orange-700 w-full">
                      <p className="font-medium mb-1">متغيرات القالب غير مطابقة</p>

                      {tplName && (
                        <p className="mb-1 text-[11px]">
                          القالب: <code className="font-mono px-1 rounded bg-white/60">{tplName}</code>
                        </p>
                      )}

                      {paramCounts ? (
                        <div className="mb-1.5 rounded bg-white/50 border border-orange-100 p-2 text-[11px] space-y-0.5">
                          {paramCounts.expected.header !== paramCounts.sent.header && (
                            <p><span className="font-medium text-red-600">HEADER</span>: متوقع {paramCounts.expected.header} — أُرسل {paramCounts.sent.header}</p>
                          )}
                          {paramCounts.expected.body !== paramCounts.sent.body && (
                            <p><span className="font-medium text-red-600">BODY</span>: متوقع {paramCounts.expected.body} — أُرسل {paramCounts.sent.body}</p>
                          )}
                          {paramCounts.expected.buttons !== paramCounts.sent.buttons && (
                            <p><span className="font-medium text-red-600">BUTTONS</span>: متوقع {paramCounts.expected.buttons} — أُرسل {paramCounts.sent.buttons}</p>
                          )}
                          {paramCounts.expected.header === paramCounts.sent.header &&
                           paramCounts.expected.body === paramCounts.sent.body &&
                           paramCounts.expected.buttons === paramCounts.sent.buttons && (
                            <p className="text-orange-600">الأعداد متطابقة — قد يكون قيمة المتغير فارغة أو غير صالحة.</p>
                          )}
                        </div>
                      ) : (
                        <p className="mb-1 text-[11px]">
                          القالب معتمد لكن عدد المتغيرات في Meta لا يطابق ما يرسله نحلة.
                        </p>
                      )}

                      <p className="font-medium mb-0.5">الحل الموصى به:</p>
                      <ol className="list-decimal list-inside space-y-0.5">
                        <li>اضغط <strong>إعادة جدولة الفاشلة</strong> أدناه — نحلة ستحاول مجدداً مع الإصلاح التلقائي.</li>
                        <li>إذا استمر الفشل: افتح لوحة <strong>القوالب</strong> ومزامن القوالب من Meta لتحديث عدد المتغيرات المخزّنة.</li>
                        <li>إذا استمر بعد المزامنة: أنشئ قالباً جديداً من <strong>مكتبة نحلة</strong> (تذكير الدفع) واطلب اعتماده.</li>
                      </ol>
                    </div>
                  </div>
                )
              })()}

              {/* Reschedule notice */}
              {rescheduleNotice && (
                <div className={`rounded-lg border p-3 text-xs ${
                  rescheduleNotice.kind === 'ok'
                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                    : rescheduleNotice.kind === 'warn'
                      ? 'border-amber-200 bg-amber-50 text-amber-700'
                      : 'border-red-200 bg-red-50 text-red-700'
                }`}>
                  {rescheduleNotice.text}
                </div>
              )}

              {/* Steps timeline */}
              {timeline.steps.length === 0 ? (
                <div className="text-center py-8">
                  <Clock className="w-8 h-8 text-slate-300 mx-auto mb-2" />
                  <p className="text-sm text-slate-500">لم يُرسل أي تذكير حتى الآن</p>
                  <p className="text-xs text-slate-400 mt-1">
                    سيبدأ الطيار الآلي بالإرسال بعد انتهاء فترة الانتظار
                  </p>
                </div>
              ) : (
                <ol className="relative">
                  {timeline.steps.map((step, idx) => {
                    const isLast = idx === timeline.steps.length - 1
                    const timeLabel = step.executed_at
                      ? formatRelativeRiyadh(step.executed_at)
                      : step.emitted_at
                        ? `جُدولت ${formatRelativeRiyadh(step.emitted_at)}`
                        : null
                    const skipLabel = step.skip_reason
                      ? (skipReasonAr[step.skip_reason] ?? step.skip_reason)
                      : null
                    return (
                      <li key={step.step_idx} className="flex gap-3 pb-0">
                        <div className="flex flex-col items-center">
                          <div className={`shrink-0 w-5 h-5 rounded-full ring-2 flex items-center justify-center text-white text-[10px] font-bold ${dotCls(step.status)}`}>
                            {step.step_idx}
                          </div>
                          {!isLast && <div className="w-0.5 flex-1 min-h-[16px] bg-slate-200 my-0.5" />}
                        </div>
                        <div className={`flex-1 min-w-0 rounded-lg border p-2.5 mb-2 ${cardCls(step.status)}`}>
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-medium text-slate-700">
                              المرحلة {step.step_idx}
                            </span>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${
                              step.status === 'sent'    ? 'bg-green-50 text-green-700 border-green-200' :
                              step.status === 'failed'  ? 'bg-red-50 text-red-700 border-red-200' :
                              step.status === 'skipped' ? 'bg-amber-50 text-amber-700 border-amber-200' :
                              step.status === 'pending' ? 'bg-indigo-50 text-indigo-700 border-indigo-200' :
                              'bg-slate-50 text-slate-500 border-slate-200'
                            }`}>
                              {step.status_label}
                            </span>
                          </div>
                          {timeLabel && (
                            <p className="text-[11px] text-slate-400 mt-0.5">{timeLabel}</p>
                          )}
                          {step.template_name && (
                            <p className="text-[11px] text-slate-500 mt-0.5">
                              قالب: <span className="font-mono">{step.template_name}</span>
                            </p>
                          )}
                          {skipLabel && (
                            <p className="text-[11px] text-amber-700 mt-0.5">
                              السبب: {skipLabel}
                            </p>
                          )}
                          {step.status === 'failed' && (step.error_label || step.error_message) && (
                            <div className="text-[11px] text-red-600 mt-0.5">
                              <span>{step.error_label || step.error_message}</span>
                              {step.error_code && step.error_code !== (step.error_label || step.error_message) && (
                                <span className="text-red-400/70 font-mono ms-1">
                                  ({step.error_code})
                                </span>
                              )}
                            </div>
                          )}
                        </div>
                      </li>
                    )
                  })}
                </ol>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// ── Pending Payment Orders Queue ─────────────────────────────────────────────

function PendingPaymentQueue({ items }: { items: PendingPaymentOrderItem[] }) {
  const [openOrderId, setOpenOrderId] = useState<number | null>(null)

  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <CreditCard className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد طلبات بانتظار الدفع</p>
        <p className="text-xs mt-1 text-slate-300">كل الطلبات الحالية مكتملة الدفع</p>
      </div>
    )
  }
  return (
    <>
      <div className="divide-y divide-slate-100">
        {items.map((item) => {
          const ageLabel = item.created_at ? formatRelativeRiyadh(item.created_at) : null
          const lastReminderLabel = item.last_reminder_at ? formatRelativeRiyadh(item.last_reminder_at) : null
          // Stage chip: 0 = no event emitted yet; ≥1 = event queued (not necessarily delivered)
          const stageLabel =
            item.current_stage === 0 ? 'لم يُجدَّل تذكير' :
            `المرحلة ${item.current_stage} جُدولت`
          const stageColor =
            item.current_stage === 0 ? 'bg-slate-50 text-slate-500 border-slate-200' :
            item.current_stage === 1 ? 'bg-amber-50 text-amber-700 border-amber-200' :
            'bg-red-50 text-red-700 border-red-200'
          return (
            <div key={item.order_id} className="py-3 px-1">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-sm font-medium text-slate-800 truncate">{item.customer_name}</p>
                    <span className="text-xs text-slate-400 font-mono shrink-0">{item.order_number}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    {item.customer_phone && (
                      <span className="flex items-center gap-1 text-xs text-slate-400">
                        <Phone className="w-3 h-3" />{item.customer_phone}
                      </span>
                    )}
                    {ageLabel && (
                      <span className="flex items-center gap-1 text-xs text-slate-400">
                        <Clock className="w-3 h-3" />{ageLabel}
                      </span>
                    )}
                  </div>
                  {lastReminderLabel && (
                    <p className="text-[10px] text-slate-400 mt-0.5">
                      آخر تذكير: {lastReminderLabel}
                    </p>
                  )}
                </div>
                <div className="shrink-0 flex flex-col items-end gap-1.5">
                  <span className="text-sm font-semibold text-slate-700 tabular-nums">
                    {item.total > 0 ? `${item.total.toFixed(2)} ر.س` : '—'}
                  </span>
                  <div className="flex items-center gap-1.5 flex-wrap justify-end">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${stageColor}`}>
                      {stageLabel}
                    </span>
                    {item.checkout_url && (
                      <a
                        href={item.checkout_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] px-1.5 py-0.5 rounded-full bg-brand-50 text-brand-700 border border-brand-200 hover:bg-brand-100 transition-colors flex items-center gap-0.5"
                      >
                        <ExternalLink className="w-2.5 h-2.5" />
                        رابط الدفع
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => setOpenOrderId(item.order_id)}
                      className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-200 hover:bg-slate-200 transition-colors flex items-center gap-0.5"
                    >
                      <ChevronDown className="w-2.5 h-2.5" />
                      تفاصيل
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {openOrderId !== null && (
        <OrderReminderDrawer
          orderId={openOrderId}
          onClose={() => setOpenOrderId(null)}
        />
      )}
    </>
  )
}

// ── COD Pending Confirmation Queue ────────────────────────────────────────────

function CodPendingQueue({ items }: { items: CodPendingOrderItem[] }) {
  const [openOrderId, setOpenOrderId] = useState<number | null>(null)

  if (items.length === 0) {
    return (
      <div className="text-center py-8 text-slate-400">
        <BadgeAlert className="w-8 h-8 mx-auto mb-2 opacity-40" />
        <p className="text-sm">لا توجد طلبات بانتظار التأكيد</p>
        <p className="text-xs mt-1 text-slate-300">كل طلبات الدفع عند الاستلام مؤكدة</p>
      </div>
    )
  }
  return (
    <>
      <div className="divide-y divide-slate-100">
        {items.map((item) => {
          const ageLabel = item.created_at ? formatRelativeRiyadh(item.created_at) : null
          const lastReminderLabel = item.last_reminder_at ? formatRelativeRiyadh(item.last_reminder_at) : null
          const statusLabel = ORDER_STATUS_LABELS[item.status] ?? item.status
          return (
            <div key={item.order_id} className="py-3 px-1">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-sm font-medium text-slate-800 truncate">{item.customer_name}</p>
                    <span className="text-xs text-slate-400 font-mono shrink-0">{item.order_number}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    {item.customer_phone && (
                      <span className="flex items-center gap-1 text-xs text-slate-400">
                        <Phone className="w-3 h-3" />{item.customer_phone}
                      </span>
                    )}
                    {ageLabel && (
                      <span className="flex items-center gap-1 text-xs text-slate-400">
                        <Clock className="w-3 h-3" />{ageLabel}
                      </span>
                    )}
                  </div>
                  {lastReminderLabel && (
                    <p className="text-[10px] text-slate-400 mt-0.5">
                      آخر تذكير للتأكيد: {lastReminderLabel}
                    </p>
                  )}
                </div>
                <div className="shrink-0 flex flex-col items-end gap-1.5">
                  <span className="text-sm font-semibold text-slate-700 tabular-nums">
                    {item.total > 0 ? `${item.total.toFixed(2)} ر.س` : '—'}
                  </span>
                  <div className="flex items-center gap-1.5 flex-wrap justify-end">
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-orange-50 text-orange-700 border border-orange-200 font-medium">
                      {statusLabel}
                    </span>
                    {item.reminders_sent > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 font-medium">
                        {item.reminders_sent} جُدولت
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => setOpenOrderId(item.order_id)}
                      className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 text-slate-600 border border-slate-200 hover:bg-slate-200 transition-colors flex items-center gap-0.5"
                    >
                      <ChevronDown className="w-2.5 h-2.5" />
                      تفاصيل
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {openOrderId !== null && (
        <OrderReminderDrawer
          orderId={openOrderId}
          onClose={() => setOpenOrderId(null)}
        />
      )}
    </>
  )
}


interface OperationalQueuesProps {
  queues: AutopilotQueues | null
  loading: boolean
  onRefresh: () => void
  /** Backend feature flag for the temporary "إعادة الإرسال" button on
   *  failed cart-recovery rows. When false the button is hidden
   *  everywhere — see backend ``AUTOPILOT_ENABLE_MANUAL_RETRY``. */
  manualRetryEnabled?: boolean
}

function OperationalQueues({ queues, loading, onRefresh, manualRetryEnabled = false }: OperationalQueuesProps) {
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
    {
      id: 'pending_payment',
      label: 'بانتظار الدفع',
      icon: <CreditCard className="w-3.5 h-3.5" />,
      count: queues?.pending_payment_orders.length ?? 0,
    },
    {
      id: 'cod_pending',
      label: 'بانتظار التأكيد',
      icon: <BadgeAlert className="w-3.5 h-3.5" />,
      count: queues?.cod_pending_orders.length ?? 0,
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
              <AbandonedCartsQueue
                items={queues?.abandoned_carts ?? []}
                manualRetryEnabled={manualRetryEnabled}
                onRetried={onRefresh}
              />
            )}
            {activeTab === 'predictive_reorder' && (
              <PredictiveReorderQueue items={queues?.predictive_reorder ?? []} />
            )}
            {activeTab === 'pending_payment' && (
              <PendingPaymentQueue items={queues?.pending_payment_orders ?? []} />
            )}
            {activeTab === 'cod_pending' && (
              <CodPendingQueue items={queues?.cod_pending_orders ?? []} />
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
  readiness: import('../api/autopilot').AutomationReadiness | null
  readinessLoading: boolean
}

function AutomationCard({ automation, onToggle, readiness, readinessLoading }: AutomationCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [toggling, setToggling] = useState(false)

  // readiness comes from the page-level allReadiness call (one API call for all automations)
  const templatesReady = readiness?.all_ready === true
  const templatesNotReady = !readinessLoading && readiness !== null && !readiness.all_ready

  const meta =
    AUTOMATION_META[automation.automation_type as AutomationType] ?? {
      label: automation.name || String(automation.automation_type),
      desc: 'أتمتة من الطيار الآلي',
      trigger: String(automation.automation_type),
      icon: '📣',
      color: 'blue',
    }

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
    if (next && !templatesReady) return
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
  const isOrderNotifications = automation.automation_type === 'order_notifications'
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

  let statusBadgeLabel: string
  let statusBadgeVariant: 'amber' | 'green' | 'slate'
  if (isOrderNotifications) {
    if (readinessLoading) {
      statusBadgeLabel = 'جاري التحقق من مراحل القوالب…'
      statusBadgeVariant = 'slate'
    } else if (automation.enabled && templatesNotReady) {
      statusBadgeLabel = 'مُفعَّل — يتطلّب اعتماد كامل قبل أي إرسال'
      statusBadgeVariant = 'amber'
    } else if (automation.enabled) {
      statusBadgeLabel = 'مُفعَّل'
      statusBadgeVariant = 'green'
    } else if (!templatesReady) {
      statusBadgeLabel = 'التفعيل مقفل — اعتماد القوالب مطلوب'
      statusBadgeVariant = 'amber'
    } else {
      statusBadgeLabel = 'متوقف — جاهز للتفعيل'
      statusBadgeVariant = 'slate'
    }
  } else {
    statusBadgeLabel =
      automation.enabled && templatesNotReady
        ? 'مُفعَّل — القوالب غير معتمدة'
        : automation.enabled
          ? 'مُفعَّل'
          : 'معطّل'
    statusBadgeVariant =
      automation.enabled && templatesNotReady ? 'amber'
        : automation.enabled ? 'green' : 'slate'
  }

  const cardAccent =
    automation.enabled ? 'ring-1 ring-emerald-200 shadow-sm'
      : isOrderNotifications
        ? `ring-1 ${!templatesReady && !readinessLoading ? 'ring-amber-200 shadow-sm' : 'ring-slate-200'}`
        : ''

  const toggleLockHint =
    isOrderNotifications && !templatesReady && !automation.enabled
      ? 'يجب اعتماد القوالب الأربعة من مكتبة نحلة أولاً (اعتماد Meta).'
      : undefined

  return (
    <div className={`card overflow-hidden transition-all duration-200 bg-white ${cardAccent}`}>
      <div className="p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0">
            <span className="text-2xl leading-none mt-0.5 shrink-0">{meta.icon}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h3 className="text-sm font-semibold text-slate-900">{automation.name || meta.label}</h3>
                <Badge label={statusBadgeLabel} variant={statusBadgeVariant} dot />
              </div>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">{meta.desc}</p>
              {isOrderNotifications && (
                <p className="text-[11px] text-slate-600 mt-2 leading-relaxed border-s-2 border-slate-200 ps-2">
                  الإرسال الفعلي لتحديثات الطلب عبر واتساب يُربَط تلقائياً بأحداث الطلب من المتجر (مثل سلة) بعد اعتماد القوالب.
                  لا يوجد إرسال مكتمل بهذه الخدمة قبل اعتماد القوالب واكتمال الربط الفني بالأحداث.
                </p>
              )}
            </div>
          </div>
          <Toggle
            enabled={automation.enabled}
            onChange={handleToggle}
            disabled={toggling || (!templatesReady && !automation.enabled)}
            title={toggleLockHint}
          />
        </div>

        {/* إشعارات الطلبات: المراحل الظاهرة دائماً للديمو */}
        {isOrderNotifications && (
          <div className="mt-4 space-y-2">
            <p className="text-[11px] font-semibold text-slate-700">مراحل القوالب (مكتبة نحلة)</p>
            {readinessLoading ? (
              <div className="space-y-2 animate-pulse">
                {[1, 2, 3, 4].map(i => (
                  <div key={i} className="h-9 rounded-lg bg-slate-100 border border-slate-100" />
                ))}
              </div>
            ) : (
              <div className="space-y-1.5">
                {(readiness?.steps ?? []).map((s, i) => (
                  <div
                    key={i}
                    className={`flex items-center justify-between gap-2 px-3 py-2 rounded-lg border text-xs ${
                      s.ready
                        ? 'bg-emerald-50/80 border-emerald-200 text-emerald-800'
                        : 'bg-white border-slate-200 text-slate-700'
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      {s.ready ? (
                        <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
                      ) : (
                        <AlertCircle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                      )}
                      <span className="font-medium truncate">{s.label}</span>
                    </div>
                    <span
                      className={`shrink-0 px-2 py-0.5 rounded text-[10px] font-medium ${
                        s.ready
                          ? 'bg-emerald-100 text-emerald-700'
                          : s.status === 'MISSING'
                            ? 'bg-red-50 text-red-700'
                            : 'bg-amber-100 text-amber-800'
                      }`}
                    >
                      {s.ready ? 'معتمد ✓' : s.status === 'MISSING' ? 'غير معتمد بعد' : s.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {!readinessLoading && !templatesReady && (
              <div className="rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-2">
                <p className="text-[11px] font-semibold text-amber-900">التفعيل مقفل</p>
                <p className="text-[11px] text-amber-800/95 mt-0.5">
                  اعتمِد كل مرحلة في الأسفل عبر{' '}
                  <a href="/templates" className="underline font-medium hover:text-amber-950">
                    مكتبة القوالب
                  </a>{' '}
                  ثم عُد لتفعيل الخدمة.
                </p>
              </div>
            )}
            <a
              href="/templates"
              className="inline-flex items-center gap-2 text-xs font-semibold text-brand-700 hover:text-brand-900 px-1 py-0.5"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              افتح مكتبة القوالب — اعتماد وربط القوالب بالخدمة
            </a>
            <p className="text-[10px] text-slate-500 leading-relaxed pt-1">
              تحتاج هذه الخدمة إلى قالب واتساب معتمد قبل الإرسال خارج نافذة خدمة الـ 24 ساعة.
            </p>
          </div>
        )}

        {/* السلات المتروكة: مراحل القوالب — ظاهرة دائماً */}
        {isAbandonedCart && (
          <div className="mt-4 space-y-2">
            <p className="text-[11px] font-semibold text-slate-700">مراحل قوالب الإرسال</p>
            {readinessLoading ? (
              <div className="space-y-2 animate-pulse">
                {[1, 2, 3].map(i => (
                  <div key={i} className="h-9 rounded-lg bg-slate-100 border border-slate-100" />
                ))}
              </div>
            ) : (
              <div className="space-y-1.5">
                {(readiness?.steps ?? []).map((s, i) => (
                  <div
                    key={i}
                    className={`flex items-center justify-between gap-2 px-3 py-2 rounded-lg border text-xs ${
                      s.ready
                        ? 'bg-emerald-50/80 border-emerald-200 text-emerald-800'
                        : automation.enabled
                          ? 'bg-red-50/70 border-red-200 text-red-800'
                          : 'bg-white border-slate-200 text-slate-700'
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      {s.ready ? (
                        <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
                      ) : (
                        <AlertCircle className={`w-3.5 h-3.5 shrink-0 ${automation.enabled ? 'text-red-500' : 'text-amber-500'}`} />
                      )}
                      <span className="font-medium truncate">{s.label}</span>
                    </div>
                    <span
                      className={`shrink-0 px-2 py-0.5 rounded text-[10px] font-medium ${
                        s.ready
                          ? 'bg-emerald-100 text-emerald-700'
                          : s.status === 'MISSING'
                            ? automation.enabled ? 'bg-red-100 text-red-700' : 'bg-red-50 text-red-700'
                            : 'bg-amber-100 text-amber-800'
                      }`}
                    >
                      {s.ready ? 'معتمد ✓' : s.status === 'MISSING' ? 'غير موجود' : s.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {!readinessLoading && !templatesReady && (
              <div className={`rounded-lg border px-3 py-2 ${automation.enabled ? 'bg-red-50 border-red-200' : 'bg-amber-50/70 border-amber-200'}`}>
                <p className={`text-[11px] font-semibold ${automation.enabled ? 'text-red-900' : 'text-amber-900'}`}>
                  {automation.enabled ? '⚠️ لن يُرسَل حتى تعتمد هذه المراحل' : 'التفعيل مقفل'}
                </p>
                <p className={`text-[11px] mt-0.5 ${automation.enabled ? 'text-red-700' : 'text-amber-800/95'}`}>
                  اعتمِد القوالب الناقصة عبر{' '}
                  <a href="/templates" className="underline font-medium">مكتبة القوالب</a>{' '}
                  ثم عُد لتفعيل الخدمة.
                </p>
              </div>
            )}
            {!readinessLoading && templatesReady && (
              <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
                <CheckCircle className="w-4 h-4 text-emerald-500" />
                <span className="text-xs font-medium text-emerald-700">جميع المراحل معتمدة — يمكنك تفعيل الخدمة</span>
              </div>
            )}
            <a
              href="/templates"
              className="inline-flex items-center gap-2 text-xs font-semibold text-brand-700 hover:text-brand-900 px-1 py-0.5"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              افتح مكتبة القوالب — اعتماد وربط القوالب بالخدمة
            </a>
            <p className="text-[10px] text-slate-500 leading-relaxed pt-1">
              تحتاج هذه الخدمة إلى قالب واتساب معتمد قبل الإرسال خارج نافذة خدمة الـ 24 ساعة.
            </p>
          </div>
        )}

        {/* Readiness — loading (لا نكرره لبطاقة إشعارات الطلبات أو السلات المتروكة) */}
        {!isOrderNotifications && !isAbandonedCart && readinessLoading && (
          <div className="mt-3 flex items-center gap-2 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2">
            <div className="w-3.5 h-3.5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin shrink-0" />
            <span className="text-xs text-slate-500">جاري التحقق من اعتماد قوالب WhatsApp...</span>
          </div>
        )}

        {/* Readiness gate — templates NOT ready (البطاقات الأخرى فقط؛ إشعارات الطلبات والسلات المتروكة تُعرَض أعلاه) */}
        {!readinessLoading && templatesNotReady && !isOrderNotifications && !isAbandonedCart && (
          <div className={`mt-3 rounded-xl p-4 border ${automation.enabled ? 'bg-red-50 border-red-200' : 'bg-amber-50 border-amber-200'}`}>
            <div className="flex items-start gap-2 mb-2">
              <AlertCircle className={`w-4 h-4 shrink-0 mt-0.5 ${automation.enabled ? 'text-red-500' : 'text-amber-600'}`} />
              <div>
                {automation.enabled ? (
                  <>
                    <p className="text-sm font-semibold text-red-800">
                      ⚠️ الطيار الآلي مُفعَّل لكنه لن يرسل أي رسائل
                    </p>
                    <p className="text-xs text-red-600 mt-1">
                      القوالب المطلوبة لم تعتمدها WhatsApp بعد — لن تُرسَل أي رسائل حتى اكتمال الاعتماد.
                    </p>
                  </>
                ) : (
                  <>
                    <p className="text-sm font-semibold text-amber-800">
                      يتطلب اعتماد القوالب من WhatsApp قبل التفعيل.
                    </p>
                    <p className="text-xs text-amber-600 mt-1">
                      كل قالب يحتاج اعتماد Meta — لا يتم الإرسال بدون قوالب معتمدة.
                    </p>
                  </>
                )}
              </div>
            </div>
            <div className="space-y-1.5 mt-3">
              {(readiness?.steps ?? []).map((s, i) => (
                <div key={i} className={`flex items-center justify-between px-3 py-2 rounded-lg border text-xs ${
                  s.ready
                    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : automation.enabled ? 'bg-white border-red-200 text-red-700' : 'bg-white border-amber-200 text-amber-700'
                }`}>
                  <div className="flex items-center gap-2">
                    {s.ready ? (
                      <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
                    ) : (
                      <AlertCircle className={`w-3.5 h-3.5 ${automation.enabled ? 'text-red-500' : 'text-amber-500'}`} />
                    )}
                    <span className="font-medium">{s.label}</span>
                  </div>
                  <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                    s.ready
                      ? 'bg-emerald-100 text-emerald-700'
                      : s.status === 'MISSING'
                        ? 'bg-red-100 text-red-700'
                        : 'bg-amber-100 text-amber-700'
                  }`}>
                    {s.ready ? 'معتمد ✓' : s.status === 'MISSING' ? 'غير موجود' : s.status}
                  </span>
                </div>
              ))}
            </div>
            <a
              href="/templates"
              className={`mt-3 inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors ${
                automation.enabled
                  ? 'text-red-700 hover:text-red-900 bg-red-100 hover:bg-red-200'
                  : 'text-amber-700 hover:text-amber-900 bg-amber-100 hover:bg-amber-200'
              }`}
            >
              <ExternalLink className="w-3.5 h-3.5" />
              الانتقال إلى مكتبة القوالب
            </a>
          </div>
        )}

        {/* Readiness OK badge */}
        {!readinessLoading && readiness?.all_ready && !isOrderNotifications && !isAbandonedCart && (
          <div className="mt-3 flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
            <CheckCircle className="w-4 h-4 text-emerald-500" />
            <span className="text-xs font-medium text-emerald-700">جاهز للتشغيل — جميع القوالب معتمدة</span>
          </div>
        )}
        {!readinessLoading && readiness?.all_ready && isOrderNotifications && (
          <div className="mt-3 flex items-center gap-2 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
            <CheckCircle className="w-4 h-4 text-emerald-500" />
            <span className="text-xs font-medium text-emerald-700">جميع المراحل معتمدة — يمكنك تفعيل الخدمة</span>
          </div>
        )}

        {!isOrderNotifications && (
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
        )}

        <div className={`flex items-center gap-5 mt-4 pt-4 border-t border-slate-100 flex-wrap ${isOrderNotifications ? 'gap-y-2' : ''}`}>
          {isOrderNotifications && (
            <p className="text-[10px] text-slate-500 w-full basis-full order-first">
              أرقام المُشغَّل والمُرسَل تتبع نشاط الطيار بعد اكتمال اعتماد القوالب والربط الفني بأحداث الطلب من المتجر.
            </p>
          )}
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

          {isAbandonedCart ? (
            <AbandonedCartEditor
              config={(automation.config || {}) as Record<string, unknown>}
              onSave={async (next) => {
                await automationsApi.updateConfig(automation.id, next)
              }}
            />
          ) : isOrderNotifications ? (
            <div className="space-y-2">
              <p className="text-xs text-slate-600 leading-relaxed">
                تفاصيل القوالب والمراحل مُعرَضة في البطاقة الرئيسية. لا حاجة لإعداد إضافي هنا قبل الديمو.
              </p>
            </div>
          ) : steps ? (
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

          {!isAbandonedCart && !isOrderNotifications && automation.template_name && (
            <TemplateVarMapPanel templateName={automation.template_name} />
          )}
          {!isAbandonedCart && !isOrderNotifications && !automation.template_name && !!(automation.config as Record<string, unknown>).template_name && (
            <TemplateVarMapPanel templateName={String((automation.config as Record<string, unknown>).template_name)} />
          )}

          {!isAbandonedCart && !isOrderNotifications && (
            <p className="text-xs text-slate-400 mt-3 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              التعديل على الإعدادات متاح من لوحة الإعدادات المتقدمة.
            </p>
          )}
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


/** ترتيب العرض داخل «محرك الاسترداد» — إشعارات الطلبات أولاً للديمو والوضوح. */
const RECOVERY_AUTOMATION_DISPLAY_ORDER: AutomationType[] = [
  'order_notifications',
  'abandoned_cart',
  'customer_winback',
  'unpaid_order_reminder',
  'cod_confirmation',
]

function sortAutomationsForEngine(engine: EngineKey, list: AutomationRecord[]): AutomationRecord[] {
  if (engine !== 'recovery') return list
  return [...list].sort((a, b) => {
    const ia = RECOVERY_AUTOMATION_DISPLAY_ORDER.indexOf(a.automation_type)
    const ib = RECOVERY_AUTOMATION_DISPLAY_ORDER.indexOf(b.automation_type)
    const ra = ia === -1 ? 1000 + a.id : ia
    const rb = ib === -1 ? 1000 + b.id : ib
    if (ra !== rb) return ra - rb
    return a.id - b.id
  })
}

// ── EngineSection: one collapsible section per engine ─────────────────────────

interface EngineSectionProps {
  engine: EngineSummary
  automations: AutomationRecord[]
  onToggleAutomation: (id: number, enabled: boolean) => void
  onToggleEngine: (engine: EngineKey, enabled: boolean) => Promise<void>
  defaultOpen: boolean
  allReadiness: import('../api/autopilot').AllAutomationsReadiness | null
  readinessLoading: boolean
}

function EngineSection({
  engine,
  automations,
  onToggleAutomation,
  onToggleEngine,
  defaultOpen,
  allReadiness,
  readinessLoading,
}: EngineSectionProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [toggling, setToggling] = useState(false)
  const display = ENGINE_DISPLAY[engine.engine]
  const IconCmp = display.icon

  const items = sortAutomationsForEngine(
    engine.engine,
    automations.filter(a => a.engine === engine.engine),
  )
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
                  readiness={allReadiness?.[automation.automation_type] ?? null}
                  readinessLoading={readinessLoading}
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

// ── Governor Log Panel ────────────────────────────────────────────────────────

function GovernorLogPanel() {
  const [items, setItems]   = useState<GovernorLogItem[]>([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen]     = useState(false)
  const [expandedId, setExpandedId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await autopilotApi.governorLog({ limit: 50 })
      setItems(res.items)
    } catch {
      // non-critical
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open && items.length === 0) load()
  }, [open, items.length, load])

  const AUTOMATION_ICONS: Record<string, string> = {
    abandoned_cart:        '🛒',
    unpaid_order_reminder: '💳',
    cod_confirmation:      '💰',
    back_in_stock:         '📦',
    predictive_reorder:    '🔄',
    vip_upgrade:           '👑',
    new_product_alert:     '✨',
    seasonal_offer:        '🎊',
    salary_payday_offer:   '💵',
    customer_winback:      '💛',
    order_notifications:   '📣',
  }

  return (
    <div className="card overflow-hidden">
      {/* Header */}
      <button
        type="button"
        className="w-full flex items-center justify-between gap-3 px-5 py-4 text-right hover:bg-slate-50 transition-colors"
        onClick={() => setOpen(p => !p)}
      >
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 bg-violet-50 rounded-xl flex items-center justify-center shrink-0">
            <ShieldCheck className="w-5 h-5 text-violet-600" />
          </div>
          <div>
            <h3 className="text-sm font-semibold text-slate-900">سجل حماية العملاء — Governor</h3>
            <p className="text-xs text-slate-500 mt-0.5">
              يُظهر كل رسالة مُنعت أو أُجّلت مع السبب الكامل
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {items.length > 0 && (
            <span className="text-xs font-medium bg-violet-100 text-violet-700 px-2 py-0.5 rounded-full">
              {items.length}
            </span>
          )}
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </div>
      </button>

      {open && (
        <div className="border-t border-slate-100">
          {/* Priority legend */}
          <div className="px-5 py-3 bg-violet-50/60 border-b border-violet-100">
            <p className="text-xs text-violet-700 font-medium mb-2">نظام الأولويات المُطبَّق:</p>
            <div className="flex flex-wrap gap-2 text-[11px]">
              <span className="bg-red-100 text-red-700 px-2 py-0.5 rounded-full">🔴 HIGH: سلة متروكة · دفع غير مكتمل · COD</span>
              <span className="bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full">🟡 MEDIUM: عودة مخزون · إعادة طلب تنبؤية</span>
              <span className="bg-slate-100 text-slate-700 px-2 py-0.5 rounded-full">⚪ LOW: VIP · عروض · استرجاع عملاء</span>
            </div>
            <div className="flex flex-wrap gap-2 mt-1.5 text-[11px] text-slate-500">
              <span>⏱ حد 6 ساعات بين الرسائل</span>
              <span>·</span>
              <span>📊 حد 2 رسالة / يوم</span>
              <span>·</span>
              <span>📅 حد 4 رسائل / أسبوع</span>
            </div>
          </div>

          {/* Refresh */}
          <div className="flex items-center justify-between px-5 py-2 border-b border-slate-100">
            <span className="text-xs text-slate-400">آخر {items.length} حدث</span>
            <button
              type="button"
              onClick={load}
              disabled={loading}
              className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 disabled:opacity-50"
            >
              <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
              تحديث
            </button>
          </div>

          {loading && items.length === 0 ? (
            <div className="py-8 text-center text-sm text-slate-400">جاري التحميل…</div>
          ) : items.length === 0 ? (
            <div className="py-10 text-center">
              <CheckCircle className="w-8 h-8 text-emerald-400 mx-auto mb-2" />
              <p className="text-sm text-slate-500">لا توجد حالات منع أو تأجيل حتى الآن</p>
              <p className="text-xs text-slate-400 mt-1">النظام يرسل دون قيود في الوقت الحالي</p>
            </div>
          ) : (
            <div className="divide-y divide-slate-50 max-h-[480px] overflow-y-auto">
              {items.map(item => {
                const meta = GOVERNOR_REASON_META[item.reason_code] ?? { icon: '⚠️', color: 'slate' as const }
                const colorCls = {
                  red:     'border-red-200   bg-red-50/40   text-red-700',
                  amber:   'border-amber-200 bg-amber-50/40 text-amber-700',
                  slate:   'border-slate-200 bg-slate-50/40 text-slate-600',
                  emerald: 'border-emerald-200 bg-emerald-50/40 text-emerald-700',
                }[meta.color]
                const isExpanded = expandedId === item.id
                const autoIcon = AUTOMATION_ICONS[item.automation_type ?? ''] ?? '⚙️'

                return (
                  <div key={item.id} className="px-5 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-3 min-w-0">
                        <span className="text-lg leading-none mt-0.5 shrink-0">{autoIcon}</span>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-xs font-semibold text-slate-800 truncate">
                              {item.automation_name ?? item.automation_type}
                            </span>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium ${colorCls}`}>
                              {meta.icon} {item.label_ar}
                            </span>
                          </div>
                          {item.customer_name || item.customer_phone ? (
                            <p className="text-[11px] text-slate-500 mt-0.5 truncate">
                              {item.customer_name ?? item.customer_phone}
                            </p>
                          ) : null}
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0">
                        {item.executed_at && (
                          <span className="text-[10px] text-slate-400 hidden sm:block">
                            {new Date(item.executed_at).toLocaleString('ar-SA', {
                              month: 'short', day: 'numeric',
                              hour: '2-digit', minute: '2-digit',
                            })}
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={() => setExpandedId(isExpanded ? null : item.id)}
                          className="p-1 rounded hover:bg-slate-100 text-slate-400 hover:text-slate-600"
                          title="عرض التفاصيل"
                        >
                          <HelpCircle className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* Expanded explanation */}
                    {isExpanded && item.suggestion_ar && (
                      <div className={`mt-2 ms-8 text-xs rounded-lg px-3 py-2 border ${colorCls} leading-relaxed`}>
                        {item.suggestion_ar}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}


export default function SmartAutomations() {
  const [automations, setAutomations] = useState<AutomationRecord[]>([])
  const [engines, setEngines] = useState<EngineSummary[]>([])
  const [autopilot, setAutopilot] = useState(false)
  const [autopilotLoading, setAutopilotLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [queues, setQueues] = useState<AutopilotQueues | null>(null)
  const [queuesLoading, setQueuesLoading] = useState(false)
  const [manualRetryEnabled, setManualRetryEnabled] = useState(false)
  // Readiness for all automation types — loaded once for the whole page
  const [allReadiness, setAllReadiness] = useState<import('../api/autopilot').AllAutomationsReadiness | null>(null)
  const [readinessLoading, setReadinessLoading] = useState(false)

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

  const loadReadiness = useCallback(async () => {
    setReadinessLoading(true)
    try {
      const r = await autopilotApi.allReadiness()
      setAllReadiness(r)
    } catch {
      // non-critical — each card will show a fallback warning
    } finally {
      setReadinessLoading(false)
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
      setManualRetryEnabled(Boolean(autopilotStatus.manual_retry_enabled))
      setEngines(summary.engines)
      // تشخيص مؤقت: هل وصل order_notifications من الـ API؟ (يُزال عند استقرار الإنتاج)
      console.info('[Nahla Autopilot] automations from API', {
        count: data.automations.length,
        types: data.automations.map(a => a.automation_type),
        hasOrderNotifications: data.automations.some(a => a.automation_type === 'order_notifications'),
      })
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
    loadReadiness()
  }, [loadData, loadQueues, loadReadiness])

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
        manualRetryEnabled={manualRetryEnabled}
      />

      {/* ── Compliance notice ── */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-3">
        <AlertCircle className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
        <p className="text-sm text-blue-700">
          جميع الرسائل التلقائية تستخدم قوالب واتساب معتمدة من Meta فقط.
        </p>
      </div>

      {/* ── Governor Log ── */}
      <GovernorLogPanel />

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
              allReadiness={allReadiness}
              readinessLoading={readinessLoading}
            />
          ))}
        </div>
      )}
    </div>
  )
}
