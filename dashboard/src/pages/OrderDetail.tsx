import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  Bot,
  Crown,
  ExternalLink,
  Link as LinkIcon,
  MessageCircle,
  MessageSquare,
  Package,
  Phone as PhoneIcon,
  RefreshCw,
  ShoppingBag,
  Store,
  User,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import {
  featureRealityApi,
  type NeedsActionLevel,
  type OrderDetail as OrderDetailType,
  type OrderSourceKey,
  type OrderTimelineEvent,
} from '../api/featureReality'

const STATUS_VARIANT: Record<string, 'green' | 'amber' | 'red' | 'slate'> = {
  paid:      'green',
  pending:   'amber',
  failed:    'red',
  cancelled: 'slate',
}

const SOURCE_BADGE: Record<OrderSourceKey, string> = {
  salla:    'bg-orange-50  text-orange-700  border-orange-200',
  zid:      'bg-purple-50  text-purple-700  border-purple-200',
  shopify:  'bg-emerald-50 text-emerald-700 border-emerald-200',
  whatsapp: 'bg-green-50   text-green-700   border-green-200',
  manual:   'bg-slate-50   text-slate-600   border-slate-200',
}

const SOURCE_FALLBACK: Record<OrderSourceKey, string> = {
  salla:    'سلة',
  zid:      'زد',
  shopify:  'Shopify',
  whatsapp: 'واتساب',
  manual:   'يدوي',
}

const sourceIcon = (s: OrderSourceKey) =>
  s === 'whatsapp' ? MessageCircle :
  s === 'manual'   ? ShoppingBag   : Store

function formatDateTime(iso?: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return new Intl.DateTimeFormat('ar-SA', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(d)
  } catch {
    return iso
  }
}

const NEEDS_ACTION_CLASS: Record<NeedsActionLevel, string> = {
  amber:  'bg-amber-50  text-amber-700  border-amber-200',
  red:    'bg-red-50    text-red-700    border-red-200',
  blue:   'bg-blue-50   text-blue-700   border-blue-200',
  purple: 'bg-purple-50 text-purple-700 border-purple-200',
}

const TIMELINE_ICON: Record<string, typeof Package> = {
  package: Package,
  link:    LinkIcon,
  bell:    Bell,
  refresh: RefreshCw,
  message: MessageSquare,
}

function TimelineRow({ event }: { event: OrderTimelineEvent }) {
  const Icon = TIMELINE_ICON[event.icon] || Package
  return (
    <li className="flex items-start gap-3">
      <div className="w-6 h-6 rounded-full bg-brand-50 text-brand-600 flex items-center justify-center shrink-0 mt-0.5">
        <Icon className="w-3 h-3" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-xs text-slate-800">{event.label}</p>
        {event.at && <p className="text-[11px] text-slate-400">{formatDateTime(event.at)}</p>}
      </div>
    </li>
  )
}

export default function OrderDetail() {
  const { orderId } = useParams<{ orderId: string }>()
  const navigate    = useNavigate()
  const [order, setOrder]   = useState<OrderDetailType | null>(null)
  const [error, setError]   = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [reminderBusy, setReminderBusy] = useState(false)
  const [reminderToast, setReminderToast] = useState<{ ok: boolean; text: string } | null>(null)

  const reload = (): Promise<void> => {
    if (!orderId) return Promise.resolve()
    return featureRealityApi
      .orderDetail(orderId)
      .then(({ order }) => { setOrder(order) })
      .catch((e) => setError(e instanceof Error ? e.message : 'تعذّر تحميل تفاصيل الطلب'))
  }

  useEffect(() => {
    if (!orderId) return
    setLoading(true)
    setError(null)
    reload().finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderId])

  const handleSendReminder = async () => {
    if (!order || reminderBusy) return
    setReminderBusy(true)
    setReminderToast(null)
    try {
      const res = await featureRealityApi.sendOrderPaymentReminder(order.internal_id || order.id)
      if (res.sent) {
        setReminderToast({ ok: true, text: 'تم إرسال تذكير الدفع بنجاح ✅' })
        await reload()
      } else {
        const reasonLabel = (
          res.reason === 'whatsapp_not_connected' ? 'واتساب غير متصل لهذا المتجر'
          : res.reason === 'service_window_closed' ? 'نافذة محادثة العميل مغلقة (24 ساعة) — افتح المحادثة لإرسال يدوي'
          : res.reason === 'send_failed'           ? 'فشل الإرسال — افتح المحادثة لإرسال يدوي'
          : 'تعذّر الإرسال التلقائي — استخدم زر فتح المحادثة'
        )
        setReminderToast({ ok: false, text: reasonLabel })
        // Open the conversation page with the prefilled draft so the
        // merchant can complete the send manually.
        if (res.conversation_url) {
          setTimeout(() => navigate(res.conversation_url), 800)
        }
      }
    } catch (e) {
      setReminderToast({
        ok: false,
        text: e instanceof Error ? e.message : 'تعذّر إرسال التذكير',
      })
    } finally {
      setReminderBusy(false)
    }
  }

  const subtotal = useMemo(() => {
    if (!order) return null
    let total = 0
    let resolved = false
    for (const it of order.line_items) {
      if (typeof it.unit_price === 'number') {
        total += it.unit_price * it.quantity
        resolved = true
      }
    }
    return resolved ? total : null
  }, [order])

  if (loading) {
    return <div className="card p-12 text-center text-sm text-slate-400">جاري التحميل…</div>
  }
  if (error || !order) {
    return (
      <div className="card p-12 text-center space-y-4">
        <p className="text-sm text-slate-500">{error || 'الطلب غير موجود'}</p>
        <button onClick={() => navigate(-1)} className="btn-secondary text-xs">
          العودة
        </button>
      </div>
    )
  }

  const SourceIcon  = sourceIcon(order.source)
  const sourceLabel = order.source_label || SOURCE_FALLBACK[order.source] || order.source
  const sourceCls   = SOURCE_BADGE[order.source] || SOURCE_BADGE.manual
  const statusCls   = STATUS_VARIANT[order.status] || 'slate'
  const canRemind   = order.status === 'pending' || order.status === 'failed'
  const needsAction = order.needs_action || []

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-start gap-3 justify-between">
        <div className="space-y-1">
          <Link to="/orders" className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700">
            <ArrowRight className="w-3 h-3" /> العودة إلى الطلبات
          </Link>
          <h1 className="text-xl font-semibold text-slate-900" dir="ltr">
            {order.order_number || order.id}
          </h1>
          <div className="flex items-center gap-2 flex-wrap">
            <Badge label={order.status_label || order.status} variant={statusCls} dot />
            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[11px] font-medium ${sourceCls}`}>
              <SourceIcon className="w-3 h-3" /> {sourceLabel}
            </span>
            {order.is_ai_created
              ? <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-brand-200 bg-brand-50 text-brand-700 text-[11px] font-medium"><Bot className="w-3 h-3" /> أنشأه الذكاء</span>
              : <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-slate-200 bg-slate-50 text-slate-600 text-[11px] font-medium"><Store className="w-3 h-3" /> من المتجر</span>}
            {order.is_vip && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-purple-200 bg-purple-50 text-purple-700 text-[11px] font-medium">
                <Crown className="w-3 h-3" /> عميل VIP
              </span>
            )}
          </div>
          <p className="text-xs text-slate-400">{formatDateTime(order.createdAt)}</p>
        </div>

        {/* Quick action buttons — ordered by operational priority:
            conversation → reminder → whatsapp → store. */}
        <div className="flex flex-wrap gap-2">
          {order.links.conversation && (
            <Link to={order.links.conversation} className="btn-primary text-xs">
              <MessageSquare className="w-3.5 h-3.5" /> فتح المحادثة في نحلة
            </Link>
          )}
          {canRemind && (
            <button
              onClick={handleSendReminder}
              disabled={reminderBusy}
              className="btn-secondary text-xs disabled:opacity-50 disabled:cursor-not-allowed"
              title={order.payment_reminder_draft || 'إرسال تذكير دفع للعميل'}
            >
              <Bell className="w-3.5 h-3.5 text-amber-600" />
              {reminderBusy ? 'جارٍ الإرسال…' : 'إرسال تذكير دفع'}
            </button>
          )}
          {order.links.whatsapp && (
            <a
              href={order.links.whatsapp}
              target="_blank"
              rel="noreferrer"
              className="btn-secondary text-xs"
            >
              <MessageCircle className="w-3.5 h-3.5 text-green-600" /> فتح واتساب
            </a>
          )}
          {order.links.store && (
            <a
              href={order.links.store}
              target="_blank"
              rel="noreferrer"
              className="btn-secondary text-xs"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              {order.links.store_label || `فتح الطلب في ${sourceLabel}`}
            </a>
          )}
        </div>
      </div>

      {/* Toast for reminder result */}
      {reminderToast && (
        <div
          className={`px-4 py-2.5 rounded-lg border text-xs ${
            reminderToast.ok
              ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
              : 'bg-amber-50 border-amber-200 text-amber-800'
          }`}
        >
          {reminderToast.text}
        </div>
      )}

      {/* Needs-action banner */}
      {needsAction.length > 0 && (
        <div className="card p-4 border-l-4 border-l-amber-400">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-4 h-4 text-amber-500 mt-0.5 shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-semibold text-slate-800 mb-2">يحتاج إجراء</p>
              <div className="flex flex-wrap gap-1.5">
                {needsAction.map((a) => (
                  <span
                    key={a.key}
                    className={`inline-flex items-center px-2 py-0.5 rounded-md border text-[11px] font-medium ${NEEDS_ACTION_CLASS[a.level]}`}
                  >
                    {a.label}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Main column */}
        <div className="lg:col-span-2 space-y-5">
          {/* Line items */}
          <div className="card">
            <div className="px-5 py-3 border-b border-slate-100 flex items-center gap-2">
              <Package className="w-4 h-4 text-slate-500" />
              <h2 className="text-sm font-semibold text-slate-900">المنتجات</h2>
            </div>
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-100">
                  <th className="text-start px-5 py-2.5 text-xs font-medium text-slate-500">المنتج</th>
                  <th className="text-start px-5 py-2.5 text-xs font-medium text-slate-500">الكمية</th>
                  <th className="text-end px-5 py-2.5 text-xs font-medium text-slate-500">السعر</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {order.line_items.length === 0 && (
                  <tr>
                    <td colSpan={3} className="px-5 py-6 text-center text-xs text-slate-400">
                      لا توجد بنود لعرضها
                    </td>
                  </tr>
                )}
                {order.line_items.map((it, idx) => (
                  <tr key={`${it.product_id}-${idx}`} className="text-xs">
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2">
                        {it.image_url
                          ? <img src={it.image_url} alt="" className="w-8 h-8 rounded object-cover" />
                          : <div className="w-8 h-8 rounded bg-slate-100 flex items-center justify-center"><Package className="w-3.5 h-3.5 text-slate-400" /></div>}
                        <div>
                          <p className="font-medium text-slate-800">{it.name}</p>
                          {it.variant_id && <p className="text-[10px] text-slate-400">المتغير: {it.variant_id}</p>}
                        </div>
                      </div>
                    </td>
                    <td className="px-5 py-3 text-slate-700">×{it.quantity}</td>
                    <td className="px-5 py-3 text-end text-slate-700">
                      {typeof it.unit_price === 'number'
                        ? `${(it.unit_price * it.quantity).toFixed(2)} ر.س`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-slate-100 bg-slate-50">
                  <td colSpan={2} className="px-5 py-3 text-xs font-semibold text-slate-700">الإجمالي</td>
                  <td className="px-5 py-3 text-end text-sm font-bold text-slate-900">
                    {order.amount}
                    {subtotal !== null && Math.abs(subtotal - order.amount_sar) > 0.01 && (
                      <span className="block text-[10px] font-normal text-slate-400">
                        مجموع البنود: {subtotal.toFixed(2)} ر.س
                      </span>
                    )}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>

          {/* Address (only if any field present) */}
          {Object.values(order.customer_address).some((v) => v && String(v).trim()) && (
            <div className="card p-5 space-y-2">
              <h2 className="text-sm font-semibold text-slate-900 mb-2">عنوان الشحن</h2>
              <dl className="grid grid-cols-2 gap-y-1 text-xs">
                {order.customer_address.city && (
                  <><dt className="text-slate-500">المدينة</dt><dd className="text-slate-800">{order.customer_address.city}</dd></>
                )}
                {order.customer_address.district && (
                  <><dt className="text-slate-500">الحي</dt><dd className="text-slate-800">{order.customer_address.district}</dd></>
                )}
                {order.customer_address.street && (
                  <><dt className="text-slate-500">الشارع</dt><dd className="text-slate-800">{order.customer_address.street}</dd></>
                )}
                {order.customer_address.building_number && (
                  <><dt className="text-slate-500">رقم المبنى</dt><dd className="text-slate-800">{order.customer_address.building_number}</dd></>
                )}
                {order.customer_address.postal_code && (
                  <><dt className="text-slate-500">الرمز البريدي</dt><dd className="text-slate-800">{order.customer_address.postal_code}</dd></>
                )}
                {order.customer_address.address && (
                  <><dt className="text-slate-500">العنوان</dt><dd className="text-slate-800 col-span-1">{order.customer_address.address}</dd></>
                )}
              </dl>
            </div>
          )}

          {order.notes && (
            <div className="card p-5">
              <h2 className="text-sm font-semibold text-slate-900 mb-2">ملاحظات</h2>
              <p className="text-xs text-slate-600 whitespace-pre-wrap">{order.notes}</p>
            </div>
          )}

          {/* Timeline */}
          <div className="card p-5">
            <h2 className="text-sm font-semibold text-slate-900 mb-3">سجل الطلب</h2>
            {order.timeline.length === 0 ? (
              <p className="text-xs text-slate-400">لا توجد أحداث مسجلة بعد.</p>
            ) : (
              <ul className="space-y-3">
                {order.timeline.map((ev, idx) => (
                  <TimelineRow key={`${ev.key}-${idx}`} event={ev} />
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Side column — customer + meta */}
        <div className="space-y-5">
          <div className="card p-5 space-y-3">
            <div className="flex items-center gap-2">
              <User className="w-4 h-4 text-slate-500" />
              <h2 className="text-sm font-semibold text-slate-900">العميل</h2>
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium text-slate-900">{order.customer_name || '—'}</p>
              {order.phone && order.phone !== '—' && (
                <p className="text-xs text-slate-500 inline-flex items-center gap-1.5" dir="ltr">
                  <PhoneIcon className="w-3 h-3" /> {order.phone}
                </p>
              )}
            </div>
          </div>

          <div className="card p-5 space-y-2 text-xs">
            <h2 className="text-sm font-semibold text-slate-900 mb-2">معلومات الطلب</h2>
            <div className="flex justify-between"><span className="text-slate-500">رقم الطلب</span><span className="text-slate-800 font-mono" dir="ltr">{order.order_number}</span></div>
            <div className="flex justify-between"><span className="text-slate-500">المبلغ</span><span className="text-slate-800 font-semibold">{order.amount}</span></div>
            <div className="flex justify-between"><span className="text-slate-500">الحالة</span><span><Badge label={order.status_label || order.status} variant={statusCls} dot /></span></div>
            <div className="flex justify-between"><span className="text-slate-500">المصدر</span><span className="text-slate-800">{sourceLabel}</span></div>
            {order.payment_method && (
              <div className="flex justify-between"><span className="text-slate-500">طريقة الدفع</span><span className="text-slate-800">{order.payment_method}</span></div>
            )}
            <div className="flex justify-between"><span className="text-slate-500">التاريخ</span><span className="text-slate-800">{formatDateTime(order.createdAt)}</span></div>
            {order.paymentLink && (
              <div className="pt-2">
                <a
                  href={order.paymentLink.startsWith('http') ? order.paymentLink : `https://${order.paymentLink}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-blue-600 hover:text-blue-700 inline-flex items-center gap-1"
                  dir="ltr"
                >
                  <ExternalLink className="w-3 h-3" /> رابط الدفع
                </a>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
