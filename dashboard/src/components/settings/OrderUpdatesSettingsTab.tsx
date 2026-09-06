import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle, CheckCircle, Clock, Loader2, Package, Send, ToggleLeft, ToggleRight,
} from 'lucide-react'
import { useLanguage } from '../../i18n/context'
import {
  approvedRevision,
  isMasterEnabled,
  isNotFoundError,
  isServiceEnabled,
  orderUpdatesApi,
  revisionBodyText,
  serviceBodyText,
  serviceVariables,
  type MetaRevisionStatus,
  type OrderUpdateServiceDetail,
  type OrderUpdateServiceKey,
  type OrderUpdatesSettings,
  ORDER_UPDATE_SERVICE_KEYS,
} from '../../api/orderUpdates'

// ── Static service metadata (UI only) ─────────────────────────────────────────

interface ServiceMeta {
  key: OrderUpdateServiceKey
  labelAr: string
  labelEn: string
  descAr: string
  descEn: string
  icon: string
  defaultVariables: Array<{ key: string; labelAr: string; labelEn: string; sample: string }>
}

const SERVICE_META: Record<OrderUpdateServiceKey, ServiceMeta> = {
  order_confirmation: {
    key: 'order_confirmation',
    labelAr: 'تأكيد الطلب',
    labelEn: 'Order confirmation',
    descAr: 'رسالة تُرسل للعميل عند قبول المتجر للطلب.',
    descEn: 'Sent when the store accepts/confirms the order.',
    icon: '📦',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  cod_confirmation: {
    key: 'cod_confirmation',
    labelAr: 'تأكيد الدفع عند الاستلام',
    labelEn: 'Cash on delivery confirmation',
    descAr: 'يطلب من العميل تأكيد أو إلغاء طلب الدفع عند الاستلام.',
    descEn: 'Asks the customer to confirm or cancel a cash-on-delivery order.',
    icon: '💵',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  payment_pending: {
    key: 'payment_pending',
    labelAr: 'بانتظار الدفع',
    labelEn: 'Payment needed',
    descAr: 'تُرسل فقط عندما يكون الدفع ما زال مطلوباً بثقة.',
    descEn: 'Sent only when payment is still genuinely required.',
    icon: '💳',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
      { key: 'payment_url', labelAr: 'رابط الدفع', labelEn: 'Payment URL', sample: 'https://pay.example/12345' },
    ],
  },
  payment_confirmed: {
    key: 'payment_confirmed',
    labelAr: 'تم استلام الدفع',
    labelEn: 'Payment received',
    descAr: 'بعد ثبوت استلام الدفع من مصدر موثوق.',
    descEn: 'After trusted payment confirmation.',
    icon: '✅',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  order_preparing: {
    key: 'order_preparing',
    labelAr: 'جاري تجهيز الطلب',
    labelEn: 'Order preparing',
    descAr: 'عند بدء تجهيز الطلب.',
    descEn: 'When the order starts being prepared.',
    icon: '🛠️',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  order_ready: {
    key: 'order_ready',
    labelAr: 'تم تجهيز الطلب',
    labelEn: 'Order ready',
    descAr: 'عندما يصبح الطلب جاهزاً / معبّأ.',
    descEn: 'When the order is packed or ready.',
    icon: '📦',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  shipping_tracking: {
    key: 'shipping_tracking',
    labelAr: 'تم شحن الطلب',
    labelEn: 'Order shipped',
    descAr: 'عند توفر دليل شحن موثوق مع التتبع إن وُجد.',
    descEn: 'When trusted shipment evidence is available.',
    icon: '🚚',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
      { key: 'carrier', labelAr: 'الناقل', labelEn: 'Carrier', sample: 'سمسا' },
      { key: 'tracking_number', labelAr: 'رقم التتبع', labelEn: 'Tracking number', sample: 'RRRD1234' },
      { key: 'tracking_url', labelAr: 'رابط التتبع', labelEn: 'Tracking URL', sample: 'https://track.example/12345' },
    ],
  },
  out_for_delivery: {
    key: 'out_for_delivery',
    labelAr: 'خرج الطلب للتوصيل',
    labelEn: 'Out for delivery',
    descAr: 'عندما يخرج الطلب للتوصيل.',
    descEn: 'When the order is out for delivery.',
    icon: '🛵',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
      { key: 'carrier', labelAr: 'الناقل', labelEn: 'Carrier', sample: 'سمسا' },
    ],
  },
  order_delivered: {
    key: 'order_delivered',
    labelAr: 'تم تسليم الطلب',
    labelEn: 'Order delivered',
    descAr: 'عند تسليم الطلب.',
    descEn: 'When the order is delivered.',
    icon: '🏡',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  order_cancelled: {
    key: 'order_cancelled',
    labelAr: 'تم إلغاء الطلب',
    labelEn: 'Order cancelled',
    descAr: 'عند إلغاء الطلب.',
    descEn: 'When the order is cancelled.',
    icon: '🚫',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
  order_refunded: {
    key: 'order_refunded',
    labelAr: 'تم استرجاع المبلغ',
    labelEn: 'Refund issued',
    descAr: 'عند رد المبلغ.',
    descEn: 'When a refund is issued.',
    icon: '↩️',
    defaultVariables: [
      { key: 'customer_name', labelAr: 'اسم العميل', labelEn: 'Customer name', sample: 'أحمد' },
      { key: 'order_number', labelAr: 'رقم الطلب', labelEn: 'Order number', sample: '12345' },
    ],
  },
}

const PREVIEW_SAMPLES: Record<string, string> = {
  customer_name: 'أحمد',
  order_number: '12345',
  order_id: '12345',
  tracking_url: 'https://track.example/12345',
  tracking_number: 'RRRD1234',
  carrier: 'سمسا',
  payment_url: 'https://pay.example/12345',
  store_name: 'متجر تجريبي عام',
  order_total: '350',
}

function metaStatusLabel(status: MetaRevisionStatus | null | undefined, isAr: boolean): string {
  const s = (status ?? '').toUpperCase()
  if (s === 'APPROVED') return isAr ? 'معتمد من Meta' : 'Meta approved'
  if (s === 'PENDING') return isAr ? 'قيد المراجعة' : 'Pending review'
  if (s === 'REJECTED') return isAr ? 'مرفوض' : 'Rejected'
  if (s === 'DRAFT') return isAr ? 'مسودة' : 'Draft'
  return status ? String(status) : (isAr ? 'غير متصل' : 'Not connected')
}

function metaStatusClasses(status: MetaRevisionStatus | null | undefined): string {
  const s = (status ?? '').toUpperCase()
  if (s === 'APPROVED') return 'bg-emerald-50 text-emerald-700 border-emerald-200'
  if (s === 'PENDING') return 'bg-amber-50 text-amber-700 border-amber-200'
  if (s === 'REJECTED') return 'bg-red-50 text-red-700 border-red-200'
  return 'bg-slate-50 text-slate-600 border-slate-200'
}

function buildPreview(text: string, variableKeys: string[]): string {
  let out = text
  variableKeys.forEach((key, idx) => {
    const sample = PREVIEW_SAMPLES[key] ?? `[${key}]`
    out = out.split(`{{${key}}}`).join(sample)
    out = out.split(`{{${idx + 1}}}`).join(sample)
  })
  return out
}

function revisionId(rev: { id?: string | number | null; template_id?: string | number | null } | null | undefined) {
  if (!rev) return null
  return rev.template_id ?? rev.id ?? null
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Toggle({
  label, hint, value, onChange, disabled,
}: {
  label: string
  hint?: string
  value: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between py-3 border-b border-slate-50 last:border-0">
      <div>
        <p className="text-sm text-slate-800">{label}</p>
        {hint && <p className="text-xs text-slate-400 mt-0.5">{hint}</p>}
      </div>
      <button
        type="button"
        onClick={() => onChange(!value)}
        disabled={disabled}
        className="ms-4 shrink-0 disabled:opacity-50"
      >
        {value
          ? <ToggleRight className="w-6 h-6 text-brand-500" />
          : <ToggleLeft className="w-6 h-6 text-slate-300" />}
      </button>
    </div>
  )
}

function WaBubblePreview({ body, footer }: { body: string; footer?: string }) {
  return (
    <div className="bg-[#e5ddd5] rounded-xl p-4 flex items-end min-h-28" dir="rtl">
      <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs w-full p-3 space-y-1">
        {body ? (
          <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">{body}</p>
        ) : (
          <p className="text-slate-400 text-xs italic">—</p>
        )}
        {footer && <p className="text-[10px] text-slate-400 mt-1">{footer}</p>}
        <p className="text-[10px] text-slate-300 text-end">✓✓</p>
      </div>
    </div>
  )
}

function ServiceCard({
  meta,
  settings,
  detail,
  apiMissing,
  onSettingsChange,
  onDetailChange,
}: {
  meta: ServiceMeta
  settings: OrderUpdatesSettings | null
  detail: OrderUpdateServiceDetail | null
  apiMissing: boolean
  onSettingsChange: (enabled: boolean) => Promise<void>
  onDetailChange: (detail: OrderUpdateServiceDetail | null) => void
}) {
  const { lang } = useLanguage()
  const isAr = lang === 'ar'

  const enabled = isServiceEnabled(settings, meta.key)
  const [bodyText, setBodyText] = useState('')
  const [toggleSaving, setToggleSaving] = useState(false)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [toggleError, setToggleError] = useState<string | null>(null)

  useEffect(() => {
    setBodyText(serviceBodyText(detail))
  }, [detail])

  const variableKeys = useMemo(() => {
    const fromApi = serviceVariables(detail)
    if (fromApi.length > 0) return fromApi
    return meta.defaultVariables.map(v => v.key)
  }, [detail, meta.defaultVariables])

  const variableRows = useMemo(() => {
    const apiVars = serviceVariables(detail)
    if (apiVars.length > 0) {
      return apiVars.map(key => {
        const known = meta.defaultVariables.find(v => v.key === key)
        return {
          key,
          label: isAr ? (known?.labelAr ?? key) : (known?.labelEn ?? key),
        }
      })
    }
    return meta.defaultVariables.map(v => ({
      key: v.key,
      label: isAr ? v.labelAr : v.labelEn,
    }))
  }, [detail, meta.defaultVariables, isAr])

  const metaStatus = detail?.meta_status
    ?? detail?.pending_revision?.meta_status
    ?? detail?.pending_revision?.status
    ?? approvedRevision(detail)?.status
    ?? null

  const approved = approvedRevision(detail)
  const pending = detail?.pending_revision ?? null
  const previewBody = buildPreview(bodyText, variableKeys)
  const previewFooter = detail?.preview_footer ?? (isAr ? 'نحلة — مساعد متجرك' : 'Nahla — your store assistant')

  const handleToggle = async (next: boolean) => {
    setToggleSaving(true)
    setToggleError(null)
    try {
      await onSettingsChange(next)
    } catch {
      setToggleError(isAr ? 'تعذّر تحديث التفعيل' : 'Could not update enable state')
    } finally {
      setToggleSaving(false)
    }
  }

  const handleSave = async () => {
    const trimmed = bodyText.trim()
    if (!trimmed) {
      setSaveError(isAr ? 'نص الرسالة مطلوب' : 'Message text is required')
      setSaveState('error')
      return
    }
    setSaveState('saving')
    setSaveError(null)
    try {
      const res = await orderUpdatesApi.createRevision(meta.key, {
        body_text: trimmed,
        submit_to_meta: true,
      })
      const nextDetail = res.service ?? res.detail ?? detail
      if (nextDetail) onDetailChange(nextDetail)
      else {
        const refreshed = await orderUpdatesApi.getService(meta.key)
        onDetailChange(refreshed)
      }
      setSaveState('saved')
      setTimeout(() => setSaveState('idle'), 3000)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : (isAr ? 'فشل الحفظ' : 'Save failed')
      setSaveError(msg)
      setSaveState('error')
    }
  }

  const approvedLabel = (() => {
    const label = approved?.label ?? approved?.meta_template_name
    if (label) return label
    const body = revisionBodyText(approved)
    if (body) return body.slice(0, 48) + (body.length > 48 ? '…' : '')
    return null
  })()

  return (
    <div className="card overflow-hidden">
      <div className="px-5 py-4 border-b border-slate-100 flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <span className="text-xl leading-none shrink-0">{meta.icon}</span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-900">
              {isAr ? meta.labelAr : meta.labelEn}
            </h3>
            <p className="text-xs text-slate-500 mt-0.5 leading-relaxed">
              {isAr ? meta.descAr : meta.descEn}
            </p>
          </div>
        </div>
        <span className={`shrink-0 text-[11px] font-medium px-2 py-1 rounded-full border ${metaStatusClasses(metaStatus)}`}>
          {metaStatusLabel(metaStatus, isAr)}
        </span>
      </div>

      <div className="p-5 space-y-4">
        {apiMissing && (
          <div className="flex items-start gap-2 rounded-lg bg-amber-50 border border-amber-200 px-3 py-2.5 text-xs text-amber-800">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <p>
              {isAr
                ? 'واجهة تحديثات الطلبات جاهزة — بانتظار تفعيل API الخلفي. يمكنك مراجعة التصميم محلياً.'
                : 'Order Updates UI is ready — waiting for backend API. You can preview the layout locally.'}
            </p>
          </div>
        )}

        <Toggle
          label={isAr ? 'تفعيل التحديث' : 'Enable update'}
          hint={isAr ? 'إرسال هذه الرسالة تلقائياً عند الحدث المناسب' : 'Send automatically on the matching lifecycle event'}
          value={enabled}
          onChange={handleToggle}
          disabled={toggleSaving || apiMissing}
        />
        {toggleError && (
          <p className="text-xs text-red-600 flex items-center gap-1">
            <AlertCircle className="w-3.5 h-3.5" /> {toggleError}
          </p>
        )}

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            {isAr ? 'نص الرسالة' : 'Message text'}
          </label>
          <textarea
            className="input w-full min-h-[140px] resize-y"
            dir="rtl"
            value={bodyText}
            onChange={e => setBodyText(e.target.value)}
            placeholder={
              isAr
                ? 'اكتب رسالة التحديث هنا — متغير واحد موحّد للجلسة والقالب'
                : 'Write the update message here — one unified field for session and template'
            }
            disabled={apiMissing}
          />
        </div>

        <div>
          <p className="text-xs font-semibold text-slate-600 mb-2">
            {isAr ? 'المتغيرات المتاحة' : 'Available variables'}
          </p>
          <div className="flex flex-wrap gap-2">
            {variableRows.map(v => (
              <span
                key={v.key}
                className="inline-flex items-center gap-1 text-[11px] bg-slate-100 text-slate-700 px-2 py-1 rounded-md font-mono"
                dir="ltr"
              >
                <span className="text-slate-400">{`{{${v.key}}}`}</span>
                <span className="text-slate-500 font-sans">· {v.label}</span>
              </span>
            ))}
          </div>
        </div>

        <div>
          <p className="text-xs font-semibold text-slate-600 mb-2">
            {isAr ? 'معاينة' : 'Preview'}
          </p>
          <WaBubblePreview body={previewBody} footer={previewFooter} />
        </div>

        {approvedLabel && (
          <div className="rounded-lg bg-emerald-50 border border-emerald-100 px-3 py-2.5 text-xs text-emerald-800">
            <p className="font-semibold flex items-center gap-1.5">
              <CheckCircle className="w-3.5 h-3.5" />
              {isAr ? 'آخر نسخة معتمدة (النشطة)' : 'Last approved revision (live)'}
            </p>
            <p className="mt-1 leading-relaxed">{approvedLabel}</p>
            {approved?.approved_at && (
              <p className="mt-1 text-emerald-600/80">{approved.approved_at}</p>
            )}
          </div>
        )}

        {(metaStatus ?? '').toUpperCase() === 'PENDING' && approved && (
          <div className="rounded-lg bg-blue-50 border border-blue-100 px-3 py-2.5 text-xs text-blue-800 flex items-start gap-2">
            <Clock className="w-4 h-4 shrink-0 mt-0.5" />
            <p>
              {isAr
                ? 'النسخة السابقة المعتمدة ما زالت هي المستخدمة حتى تتم موافقة Meta على التعديل الجديد.'
                : 'The previously approved revision stays live until Meta approves your pending change.'}
            </p>
          </div>
        )}

        {pending && revisionBodyText(pending) && (
          <div className="rounded-lg bg-amber-50/80 border border-amber-100 px-3 py-2.5 text-xs text-amber-900">
            <p className="font-semibold">{isAr ? 'نسخة قيد المراجعة' : 'Pending revision'}</p>
            <p className="mt-1 leading-relaxed line-clamp-3">{revisionBodyText(pending)}</p>
            {revisionId(pending) != null && (
              <p className="mt-1 text-amber-700/80 font-mono text-[10px]" dir="ltr">
                #{String(revisionId(pending))}
              </p>
            )}
          </div>
        )}

        <div className="flex items-center gap-3 flex-wrap pt-1">
          <button
            type="button"
            onClick={handleSave}
            disabled={saveState === 'saving' || apiMissing}
            className="btn-primary inline-flex items-center gap-2 text-sm"
          >
            {saveState === 'saving'
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <Send className="w-4 h-4" />}
            {saveState === 'saving'
              ? (isAr ? 'جاري الحفظ...' : 'Saving…')
              : (isAr ? 'حفظ وإرسال إلى Meta' : 'Save & submit to Meta')}
          </button>
          {saveState === 'saved' && (
            <span className="flex items-center gap-1.5 text-sm text-emerald-600">
              <CheckCircle className="w-4 h-4" />
              {isAr ? 'تم الحفظ' : 'Saved'}
            </span>
          )}
          {saveState === 'error' && saveError && (
            <span className="flex items-center gap-1.5 text-sm text-red-600">
              <AlertCircle className="w-4 h-4" /> {saveError}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main tab ──────────────────────────────────────────────────────────────────

export default function OrderUpdatesSettingsTab() {
  const { lang } = useLanguage()
  const isAr = lang === 'ar'

  const [settings, setSettings] = useState<OrderUpdatesSettings | null>(null)
  const [details, setDetails] = useState<Partial<Record<OrderUpdateServiceKey, OrderUpdateServiceDetail | null>>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [apiMissing, setApiMissing] = useState(false)

  const [masterSaving, setMasterSaving] = useState(false)

  const loadAll = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    let missing = false
    try {
      const settingsRes = await orderUpdatesApi.getSettings()
      setSettings(settingsRes)
    } catch (err) {
      if (isNotFoundError(err)) {
        missing = true
        setSettings({
          enabled: false,
          services: Object.fromEntries(
            ORDER_UPDATE_SERVICE_KEYS.map(key => [key, { enabled: false }]),
          ) as OrderUpdatesSettings['services'],
        })
      } else {
        setLoadError(err instanceof Error ? err.message : (isAr ? 'تعذّر التحميل' : 'Failed to load'))
      }
    }

    const nextDetails: Partial<Record<OrderUpdateServiceKey, OrderUpdateServiceDetail | null>> = {}
    await Promise.all(
      ORDER_UPDATE_SERVICE_KEYS.map(async key => {
        try {
          nextDetails[key] = await orderUpdatesApi.getService(key)
        } catch (err) {
          if (isNotFoundError(err)) {
            missing = true
            nextDetails[key] = {
              service_key: key,
              enabled: false,
              body_text: '',
              meta_status: null,
            }
          } else {
            nextDetails[key] = null
          }
        }
      }),
    )
    setDetails(nextDetails)
    setApiMissing(missing)
    setLoading(false)
  }, [isAr])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  const handleMasterChange = async (enabled: boolean) => {
    setMasterSaving(true)
    try {
      const saved = await orderUpdatesApi.patchSettings({ enabled })
      setSettings(saved)
    } finally {
      setMasterSaving(false)
    }
  }

  const handleSettingsChange = async (serviceKey: OrderUpdateServiceKey, enabled: boolean) => {
    const next: OrderUpdatesSettings = {
      ...(settings ?? {}),
      services: {
        ...(settings?.services ?? {}),
        [serviceKey]: { enabled },
      },
      [serviceKey]: { enabled },
    }
    const saved = await orderUpdatesApi.putSettings(next)
    setSettings(saved)
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
        <span className="ms-3 text-sm text-slate-500">
          {isAr ? 'تحميل تحديثات الطلبات...' : 'Loading order updates…'}
        </span>
      </div>
    )
  }

  if (loadError) {
    return (
      <div className="card p-8 max-w-md mx-auto flex flex-col items-center gap-3 text-center">
        <AlertCircle className="w-8 h-8 text-red-400" />
        <p className="text-sm text-slate-700">{loadError}</p>
        <button type="button" className="btn-secondary text-sm" onClick={loadAll}>
          {isAr ? 'إعادة المحاولة' : 'Retry'}
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="card px-5 py-4 flex items-start gap-3">
        <div className="w-9 h-9 rounded-xl bg-brand-50 border border-brand-100 flex items-center justify-center shrink-0">
          <Package className="w-4 h-4 text-brand-600" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">
                {isAr ? 'تحديثات الطلبات' : 'Order updates'}
              </h2>
              <p className="text-xs text-slate-500 mt-0.5 leading-relaxed max-w-2xl">
                {isAr
                  ? 'قالب واحد لكل إشعار — نفس النص والأزرار داخل نافذة المحادثة وخارجها. التعطيل يوقف الإرسال فقط ولا يحذف القوالب.'
                  : 'One template per notification — the same copy and buttons inside and outside the 24h window. Disabling stops delivery only; templates are kept.'}
              </p>
            </div>
            <button
              type="button"
              onClick={() => handleMasterChange(!isMasterEnabled(settings))}
              disabled={masterSaving || apiMissing}
              className="shrink-0 disabled:opacity-50"
              aria-label={isAr ? 'تشغيل الكل' : 'Enable all'}
            >
              {isMasterEnabled(settings)
                ? <ToggleRight className="w-7 h-7 text-brand-500" />
                : <ToggleLeft className="w-7 h-7 text-slate-300" />}
            </button>
          </div>
          <p className="text-[11px] text-slate-400 mt-1">
            {isAr ? 'تشغيل الكل' : 'Master switch'}
          </p>
        </div>
      </div>

      {ORDER_UPDATE_SERVICE_KEYS.map(key => (
        <ServiceCard
          key={key}
          meta={SERVICE_META[key]}
          settings={settings}
          detail={details[key] ?? null}
          apiMissing={apiMissing}
          onSettingsChange={enabled => handleSettingsChange(key, enabled)}
          onDetailChange={detail => setDetails(prev => ({ ...prev, [key]: detail }))}
        />
      ))}
    </div>
  )
}
