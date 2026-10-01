import { useEffect, useState } from 'react'
import { apiCall } from '../../api/client'
import type { OrderDetail } from '../../api/featureReality'

type Environment = 'staging' | 'production'
type Connection = { environment: Environment; enabled: boolean; pickup_location_code?: string | null; pickup_city?: string | null; webhook_configured: boolean }
type Quote = { deliveryOptionId?: number; name?: string; deliveryCompany?: string; price?: number; totalPrice?: number }

export default function OtoShippingPanel({ order, reload }: { order: OrderDetail; reload: () => Promise<void> }) {
  const id = Number(order.internal_id)
  const shipment = order.shipping?.shipment
  const [environment, setEnvironment] = useState<Environment>('staging')
  const [connections, setConnections] = useState<Connection[]>([])
  const [refreshToken, setRefreshToken] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [pickupCode, setPickupCode] = useState('')
  const [originCity, setOriginCity] = useState('')
  const [destinationCity, setDestinationCity] = useState(order.customer_address?.city || '')
  const [weight, setWeight] = useState(1)
  const [width, setWidth] = useState(20)
  const [length, setLength] = useState(20)
  const [height, setHeight] = useState(20)
  const [quotes, setQuotes] = useState<Quote[]>([])
  const [optionId, setOptionId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const active = connections.find(c => c.environment === environment && c.enabled)
  const configured = connections.find(c => c.environment === environment)

  const loadConnections = () => apiCall<Connection[]>('/oto/connection').then(setConnections).catch(() => setConnections([]))
  useEffect(() => { void loadConnections() }, [])

  async function run(action: () => Promise<unknown>, done: string) {
    setBusy(true)
    setNotice('')
    try { await action(); setNotice(done); await reload() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'تعذّر إكمال العملية') }
    finally { setBusy(false) }
  }

  async function connect() {
    await run(async () => {
      await apiCall('/oto/connection', { method: 'PUT', body: JSON.stringify({
        environment, refresh_token: refreshToken, webhook_secret: webhookSecret || undefined,
        pickup_location_code: pickupCode || undefined, pickup_city: originCity || undefined,
      }) })
      setRefreshToken(''); setWebhookSecret(''); await loadConnections()
    }, 'حُفظت بيانات OTO. تحقّق من الاتصال قبل استخدام الشحن.')
  }

  async function getQuotes() {
    setBusy(true); setNotice('')
    try {
      const result = await apiCall<{ quotes: Quote[] }>('/oto/quotes', { method: 'POST', body: JSON.stringify({
        environment, origin_city: originCity || active?.pickup_city, destination_city: destinationCity,
        weight_kg: weight, width_cm: width, length_cm: length, height_cm: height,
      }) })
      setQuotes(result.quotes || [])
      setOptionId(null)
      if (!result.quotes?.length) setNotice('لا تتوفر عروض شحن لهذا العنوان حاليًا.')
    } catch (error) { setNotice(error instanceof Error ? error.message : 'تعذّر جلب أسعار الشحن') }
    finally { setBusy(false) }
  }

  const orderPath = `/oto/orders/${id}`
  const dims = { environment, weight_kg: weight, width_cm: width, length_cm: length, height_cm: height }
  const field = 'w-full rounded border border-slate-300 px-2 py-1.5 text-sm'
  const button = 'btn-secondary text-xs disabled:opacity-50'
  if (!Number.isInteger(id) || id <= 0) return null

  return <section className="card p-5 space-y-3" aria-label="شحن OTO">
    <div className="flex items-center justify-between gap-3">
      <h2 className="text-sm font-semibold">شحن OTO</h2>
      <select className={field + ' max-w-36'} value={environment} onChange={e => setEnvironment(e.target.value as Environment)} aria-label="بيئة OTO">
        <option value="staging">تجريبي</option><option value="production">إنتاج</option>
      </select>
    </div>
    {!active && <div className="space-y-2">
      {configured && <button className={button} disabled={busy} onClick={() => void run(async () => {
        await apiCall(`/oto/connection/${environment}/verify`, { method: 'POST' }); await loadConnections()
      }, 'تأكد الاتصال بحساب OTO.')}>التحقق من الاتصال</button>}
      <p className="text-xs text-slate-600">أدخل رمز تحديث حساب التاجر في OTO وموقع الاستلام. يُحفظ الرمز مشفرًا.</p>
      <input className={field} type="password" autoComplete="off" placeholder="رمز تحديث OTO" value={refreshToken} onChange={e => setRefreshToken(e.target.value)} />
      <input className={field} type="password" autoComplete="off" placeholder="سر توقيع Webhook (إن توفر)" value={webhookSecret} onChange={e => setWebhookSecret(e.target.value)} />
      <div className="grid grid-cols-2 gap-2">
        <input className={field} placeholder="رمز موقع الاستلام" value={pickupCode} onChange={e => setPickupCode(e.target.value)} />
        <input className={field} placeholder="مدينة الاستلام" value={originCity} onChange={e => setOriginCity(e.target.value)} />
      </div>
      <button className={button} disabled={busy || !refreshToken} onClick={() => void connect()}>حفظ الاتصال</button>
    </div>}
    {active && <>
      <p className="text-xs text-slate-600">موقع الاستلام: {active.pickup_location_code || 'غير محدد'} · {active.pickup_city || 'المدينة غير محددة'}</p>
      {!shipment && <>
        <div className="grid grid-cols-2 gap-2">
          <input className={field} placeholder="مدينة الاستلام" value={originCity || active.pickup_city || ''} onChange={e => setOriginCity(e.target.value)} />
          <input className={field} placeholder="مدينة العميل" value={destinationCity} onChange={e => setDestinationCity(e.target.value)} />
          <input className={field} type="number" min="0.1" step="0.1" aria-label="الوزن بالكيلوغرام" value={weight} onChange={e => setWeight(Number(e.target.value))} />
          <input className={field} type="number" min="1" aria-label="العرض بالسنتيمتر" value={width} onChange={e => setWidth(Number(e.target.value))} />
          <input className={field} type="number" min="1" aria-label="الطول بالسنتيمتر" value={length} onChange={e => setLength(Number(e.target.value))} />
          <input className={field} type="number" min="1" aria-label="الارتفاع بالسنتيمتر" value={height} onChange={e => setHeight(Number(e.target.value))} />
        </div>
        <button className={button} disabled={busy || !destinationCity} onClick={() => void getQuotes()}>عرض أسعار الشحن</button>
        {quotes.length > 0 && <div className="space-y-2">
          {quotes.map((q, i) => <label key={i} className="block text-xs rounded border p-2">
            <input type="radio" name="oto-option" checked={optionId === q.deliveryOptionId} onChange={() => setOptionId(q.deliveryOptionId || null)} />{' '}
            {q.name || q.deliveryCompany || 'شركة شحن'} · {q.price ?? q.totalPrice ?? 'السعر غير متاح'} ر.س · الخيار {q.deliveryOptionId ?? 'غير متاح'}
          </label>)}
          <button className="btn-primary text-xs" disabled={busy || !order.shipping?.can_create_shipment || !optionId} onClick={() => void run(
            () => apiCall(`${orderPath}/shipments`, { method: 'POST', body: JSON.stringify({ ...dims, delivery_option_id: optionId }) }),
            'استلم OTO طلب الشحنة. ننتظر تأكيد شركة الشحن.',
          )}>إنشاء شحنة عبر OTO</button>
        </div>}
      </>}
      {shipment?.provider === 'oto' && <div className="space-y-2 text-xs">
        <p>الحالة: {shipment.status} · رقم التتبع: {shipment.tracking_number || 'بانتظار التأكيد'}</p>
        <div className="flex flex-wrap gap-2">
          <button className={button} disabled={busy} onClick={() => void run(() => apiCall(`${orderPath}/sync`, { method: 'POST' }), 'تحدّثت حالة الشحنة.')}>تحديث الحالة</button>
          <button className={button} disabled={busy} onClick={() => void run(() => apiCall(`${orderPath}/label`, { method: 'POST' }), 'استُرجعت البوليصة.')}>استرجاع البوليصة</button>
          {shipment.label_url && <a className={button} href={shipment.label_url} target="_blank" rel="noreferrer">طباعة البوليصة</a>}
          <button className={button} disabled={busy || !shipment.label_url || !shipment.tracking_number} onClick={() => void run(() => apiCall(`${orderPath}/send-whatsapp`, { method: 'POST' }), 'قبل واتساب طلب الإرسال. تابع حالة التسليم في المحادثة.')}>إرسال البوليصة والتتبع للعميل</button>
          <button className={button} disabled={busy} onClick={() => void run(() => apiCall(`${orderPath}/cancel`, { method: 'POST' }), 'أُرسل طلب الإلغاء إلى OTO. ننتظر تأكيده.')}>طلب إلغاء الشحنة</button>
        </div>
      </div>}
    </>}
    {notice && <p role="status" className="text-xs text-amber-800">{notice}</p>}
  </section>
}
