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
  type AbandonedCartRecoveryTimeline,
  type RecoveryStatus,
  type PredictiveReorderItem,
  type OrderStatusUpdateItem,
  ORDER_STATUS_LABELS,
  ORDER_STATUS_COLORS,
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

// ── Recovery status badge (per-cart, derived) ────────────────────────────────
//
// Maps the backend ``RecoveryStatus`` taxonomy onto the merchant-facing
// vocabulary the dashboard already uses elsewhere. Always returns
// something printable — never an empty pill.
const RECOVERY_BADGE: Record<RecoveryStatus, { label: string; cls: string; tip: string }> = {
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
}

function RecoveryStatusBadge({ status }: { status: RecoveryStatus }) {
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
      const res = await autopilotApi.retryAbandonedCart(cart.order_id)
      setRetryNotice({
        kind: 'ok',
        text: res.deduplicated
          ? 'تم تجاهل النقر — هناك إعادة جدولة قيد التنفيذ بالفعل.'
          : (res.message || 'تمت إعادة جدولة التذكيرات من المرحلة الأولى.'),
      })
      // Re-fetch the timeline so the new pending step shows up.
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

  const canRetry = Boolean(
    manualRetryEnabled
    && timeline
    && timeline.status === 'failed'
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
                      className="text-xs px-3 py-1.5 rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                      title="إعادة جدولة التذكيرات من المرحلة الأولى"
                    >
                      <RefreshCw className={`w-3.5 h-3.5 ${retrying ? 'animate-spin' : ''}`} />
                      {retrying ? 'جارٍ الجدولة…' : 'إعادة التذكيرات من البداية'}
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
                <h4 className="text-xs font-medium text-slate-500 mb-2">سلسلة التذكيرات</h4>
                {timeline.steps.length === 0 ? (
                  <p className="text-xs text-slate-400">لا توجد مراحل بعد.</p>
                ) : (
                  <ol className="space-y-2">
                    {timeline.steps.map((step) => (
                      <li
                        key={`${step.event_id}-${step.step_idx}`}
                        className="flex items-start gap-2 rounded-md border border-slate-100 p-2.5"
                      >
                        <span className="shrink-0 w-6 h-6 rounded-full bg-slate-100 text-slate-600 text-xs flex items-center justify-center">
                          {step.step_idx}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-medium text-slate-700">
                              المرحلة {step.step_idx}{step.is_root ? ' (أساسية)' : ''}
                            </span>
                            <RecoveryStatusBadge status={step.status as RecoveryStatus} />
                          </div>
                          <div className="text-[11px] text-slate-400 mt-0.5">
                            {step.sent_at
                              ? <>أُرسلت {formatRiyadh(step.sent_at)}</>
                              : step.scheduled_at
                                ? <>مجدولة {formatRiyadh(step.scheduled_at)}</>
                                : ''}
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
                          {(step.failure_label || step.error) && step.status === 'failed' && (
                            <div className="text-[11px] text-red-600 mt-0.5">
                              {step.failure_label || step.error}
                              {step.failure_code && (
                                <span className="text-red-400/70 font-mono ms-1">
                                  ({step.failure_code})
                                </span>
                              )}
                            </div>
                          )}
                        </div>
                      </li>
                    ))}
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

  const hasStale = items.some(i => {
    const r = i.recovery
    return r && r.status === 'pending' && r.steps_sent === 0
  })

  const handleCleanStale = async () => {
    if (!confirm('سيتم تنظيف جميع السلات العالقة وإعادة جدولتها فوراً من المرحلة الأولى. متأكد؟')) return
    setCleaningStale(true)
    setStaleNotice(null)
    try {
      const res = await autopilotApi.retryAllStaleCarts()
      setStaleNotice(res.message)
      onRetried?.()
    } catch (e) {
      setStaleNotice(e instanceof Error ? e.message : 'فشل التنظيف')
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
        <div className="flex items-center justify-between gap-3 px-3 py-2.5 bg-amber-50 border-b border-amber-200 rounded-t-lg">
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
        <div className="px-3 py-2 text-xs bg-green-50 text-green-700 border-b border-green-200">{staleNotice}</div>
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

          {isAbandonedCart ? (
            <AbandonedCartEditor
              config={(automation.config || {}) as Record<string, unknown>}
              onSave={async (next) => {
                await automationsApi.updateConfig(automation.id, next)
              }}
            />
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

          {!isAbandonedCart && automation.template_name && (
            <TemplateVarMapPanel templateName={automation.template_name} />
          )}
          {!isAbandonedCart && !automation.template_name && !!(automation.config as Record<string, unknown>).template_name && (
            <TemplateVarMapPanel templateName={String((automation.config as Record<string, unknown>).template_name)} />
          )}

          {!isAbandonedCart && (
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
  // Backend-controlled feature flag (env: AUTOPILOT_ENABLE_MANUAL_RETRY).
  // We default to false so the temporary retry button stays hidden when
  // the dashboard is ahead of a backend that doesn't expose it yet.
  const [manualRetryEnabled, setManualRetryEnabled] = useState(false)

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
      setManualRetryEnabled(Boolean(autopilotStatus.manual_retry_enabled))
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
        manualRetryEnabled={manualRetryEnabled}
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
