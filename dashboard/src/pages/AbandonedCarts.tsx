import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import AbandonedCartStepper from '../components/AbandonedCartStepper'
import { ShoppingCart, Clock, CheckCircle2, AlertCircle } from 'lucide-react'
import { autopilotApi, type AbandonedCartItem, type AbandonedCartRecoveryTimeline } from '../api/autopilot'

const labels: Record<string, string> = {
  pending: 'بانتظار التنفيذ', upcoming: 'مرحلة قادمة', sent: 'قبله مزود الإرسال — التسليم غير مؤكد',
  accepted: 'قبله مزود الإرسال', delivered: 'تم التسليم', read: 'تمت القراءة',
  failed: 'تعذر الإرسال', skipped: 'تم التجاوز', unknown: 'النتيجة غير مؤكدة',
  no_recovery: 'لم تبدأ التذكيرات', in_progress: 'التذكيرات جارية', completed: 'اكتمل التسلسل', converted: 'تم الشراء',
}
const date = (value?: string | null) => value ? new Date(value).toLocaleString('ar-SA', { timeZone: 'Asia/Riyadh' }) : 'غير متوفر'
const label = (value: string) => labels[value] || 'حالة غير معروفة'

export default function AbandonedCarts() {
  const [items, setItems] = useState<AbandonedCartItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selected, setSelected] = useState<AbandonedCartItem | null>(null)
  const [timeline, setTimeline] = useState<AbandonedCartRecoveryTimeline | null>(null)
  const [detailError, setDetailError] = useState('')
  useEffect(() => {
    let active = true
    autopilotApi.queues().then(data => { if (active) setItems(data.abandoned_carts) })
      .catch(() => { if (active) setError('تعذر تحميل السلات. أعد فتح الصفحة للمحاولة.') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    setTimeline(null); setDetailError('')
    if (!selected) return
    let active = true
    autopilotApi.abandonedCartRecovery(selected.order_id).then(data => { if (active) setTimeline(data) })
      .catch(() => { if (active) setDetailError('تعذر تحميل تفاصيل التذكيرات') })
    return () => { active = false }
  }, [selected])
  const visible = items.filter(item => (filter === 'all' || (filter === 'needs_review' ? item.recovery.status === 'failed' || (item.recovery.delivery_failures || 0) > 0 : item.recovery.status === filter))
    && `${item.customer_name} ${item.customer_phone} ${item.external_id || ''}`.toLowerCase().includes(query.toLowerCase()))
  return <main dir="rtl" className="space-y-6 text-slate-800">
    <header className="rounded-2xl bg-white border border-slate-100 p-6 flex flex-wrap justify-between gap-4">
      <div><h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2"><ShoppingCart className="text-emerald-600" />السلات المتروكة</h1>
        <p className="text-sm text-slate-500 mt-2">تابع السلات ومراحل التذكير وتفاصيل كل محاولة إرسال.</p></div>
      <Link to="/smart-automations" className="text-emerald-700 border border-emerald-200 rounded-xl px-4 py-2 self-start">إعدادات التذكيرات</Link>
    </header>
    <div className="grid gap-4 sm:grid-cols-3">{[
      { title: 'السلات في القائمة', count: items.length, icon: ShoppingCart, tone: 'bg-emerald-50' },
      { title: 'بانتظار التذكير', count: items.filter(i => i.recovery.status === 'pending').length, icon: Clock, tone: 'bg-blue-50' },
      { title: 'تحتاج مراجعة', count: items.filter(i => i.recovery.status === 'failed' || (i.recovery.delivery_failures || 0) > 0).length, icon: AlertCircle, tone: 'bg-purple-50' },
    ].map(card => <div key={card.title} className={`rounded-2xl p-5 ${card.tone}`}><card.icon className="w-5 h-5 mb-2" /><p className="text-sm">{card.title}</p><strong className="text-2xl">{loading ? '—' : card.count}</strong></div>)}</div>
    <section className="bg-white border border-slate-100 rounded-2xl p-5 space-y-4">
      <input aria-label="بحث في السلات" placeholder="ابحث بالعميل أو الهاتف أو رقم السلة" value={query} onChange={e => setQuery(e.target.value)} className="border rounded-xl px-4 py-2 w-full" />
      <div className="flex flex-wrap gap-2">{['all', 'pending', 'in_progress', 'needs_review', 'completed', 'no_recovery'].map(status => <button key={status} onClick={() => setFilter(status)} aria-pressed={filter === status} className={`rounded-full px-3 py-2 text-xs ${filter === status ? 'bg-emerald-600 text-white' : 'bg-slate-50'}`}>{status === 'all' ? 'الكل' : status === 'needs_review' ? 'تحتاج مراجعة' : label(status)} ({items.filter(i => status === 'all' || (status === 'needs_review' ? i.recovery.status === 'failed' || (i.recovery.delivery_failures || 0) > 0 : i.recovery.status === status)).length})</button>)}</div>
      {error && <p role="alert">{error}</p>}{loading && <p role="status">جارٍ تحميل السلات…</p>}
      {!loading && !error && visible.length === 0 && <p className="py-8 text-center text-slate-500">لا توجد سلات مطابقة في القائمة الحالية.</p>}
      <div className="overflow-x-auto"><table className="w-full text-sm text-right"><thead><tr className="text-slate-500 border-b">{['العميل / السلة', 'القيمة', 'وقت الترك', 'حالة السلة', 'حالة التذكيرات', 'آخر إرسال / التالي', 'التفاصيل'].map(text => <th key={text} className="p-3 font-medium">{text}</th>)}</tr></thead><tbody>{visible.map(item => <tr key={item.order_id} className="border-b border-slate-100">
        <td className="p-3"><strong>{item.customer_name}</strong><p dir="ltr" className="text-right text-xs text-slate-500">{item.customer_phone}</p><small>{item.external_id}</small></td>
        <td className="p-3">{item.total.toLocaleString('ar-SA')}</td><td className="p-3">{date(item.abandoned_at || item.created_at)}</td>
        <td className="p-3">{item.status === 'abandoned' ? 'متروكة' : item.status || 'غير مسجلة'}</td>
        <td className="p-3"><span className="bg-blue-50 rounded-full px-3 py-1">{label(item.recovery.status)}</span></td>
        <td className="p-3 text-xs">آخر إرسال: {date(item.recovery.last_sent_at)}<p>حدث التذكير التالي: {date(item.recovery.next_pending_at)}</p></td>
        <td className="p-3"><button className="text-emerald-700 font-semibold" onClick={() => setSelected(item)}>عرض المراحل</button></td>
      </tr>)}</tbody></table></div>
    </section>
    {selected && <section aria-label="تفاصيل تذكيرات السلة" className="bg-white border rounded-2xl p-6 space-y-4">
      <header className="flex justify-between"><h2 className="font-bold">تذكيرات سلة {selected.customer_name}</h2><button onClick={() => setSelected(null)}>إغلاق</button></header>
      <p className="text-sm text-slate-500">قبول المزود لا يثبت التسليم أو القراءة. نعرض الأحداث المتوفرة فقط.</p>
      {detailError && <p role="alert">{detailError}</p>}{!timeline && !detailError && <p role="status">جارٍ تحميل المراحل…</p>}
      {timeline?.converted_at && <p className="bg-emerald-50 p-3 rounded-xl">تم الشراء: {date(timeline.converted_at)} — توقفت التذكيرات المتبقية.</p>}
      {timeline?.steps.length === 0 && <p>لا توجد مراحل تذكير مسجلة لهذه السلة.</p>}
      {timeline && timeline.steps.length > 0 && <AbandonedCartStepper steps={timeline.steps} />}
      <details className="rounded-xl border border-slate-100 p-4"><summary className="cursor-pointer font-semibold text-blue-700">تفاصيل محاولات الإرسال</summary>
      <p className="text-xs text-slate-500 my-3">الأوقات بتوقيت الرياض. وقت تسجيل الحدث لا يحدد موعد الإرسال.</p>
      <ol className="space-y-3">{timeline?.steps.map((step, index) => <li key={`${step.event_id}-${index}`} className="border-r-4 border-slate-200 rounded-xl bg-slate-50 p-4">
        <h3 className="font-semibold flex gap-2">{step.status === 'failed' ? <AlertCircle className="w-4 h-4 text-red-500" /> : step.delivery_status === 'delivered' || step.delivery_status === 'read' ? <CheckCircle2 className="w-4 h-4 text-blue-600" /> : <Clock className="w-4 h-4 text-blue-500" />}{step.label || `التذكير ${step.step_idx}`}</h3>
        <p className="text-sm mt-2">{label(step.delivery_status || step.status)}</p>
        <div className="text-xs text-slate-500 mt-2 space-y-1"><p>{step.event_id ? 'وقت تسجيل الحدث' : 'موعد الإرسال غير معروف'}: {step.event_id ? date(step.scheduled_at) : 'لا يوجد موعد مؤكد'}</p><p>وقت محاولة التنفيذ: {date(step.sent_at)}</p><p>وقت تحديث التسليم: {date(step.delivery_event_at)}</p><p>القالب: {step.template_name || 'غير مسجل'}</p><p>معرف الرسالة: {step.wa_message_id || 'غير مسجل'}</p></div>
        <p className="text-xs text-slate-500 mt-2">النص المرسل غير متاح في السجل</p>
        {(step.delivery_status === 'failed' || step.status === 'failed') && !step.failure_label && <p className="text-red-700 text-sm">{step.delivery_failure_code ? `تعذر التسليم — رمز الخطأ: ${step.delivery_failure_code}` : 'سبب الفشل غير مسجل'}</p>}
        {step.failure_label && <p className="text-red-700 mt-2 text-sm">{step.failure_label}</p>}
        {step.skip_reason && <p className="text-sm mt-2">سبب التجاوز: {step.skip_reason}</p>}
      </li>)}</ol></details>
    </section>}
  </main>
}
