import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  CheckCircle2,
  ExternalLink,
  Package,
  Pencil,
  Plus,
  Save,
  Trash2,
  X,
} from 'lucide-react'
import { catalogApi, type CatalogProductVariantRow } from '../../api/catalog'
import {
  featureRealityApi,
  type OrderDetail as OrderDetailType,
  type OrderDetailLineItem,
} from '../../api/featureReality'
import { knowledgeApi, type ProductLite } from '../../api/knowledge'
import { orderApiId } from '../../lib/orderRoutes'

const MISSING_FIELD_LABELS: Record<string, string> = {
  customer_first_name: 'الاسم الأول',
  customer_last_name:  'اسم العائلة',
  city:                'المدينة',
  delivery_address:    'عنوان التوصيل / الموقع',
  product:             'المنتجات',
  catalog_review_required: 'مراجعة مطابقة الكتالوج',
  catalog_needs_variant: 'اختيار الحجم/variant',
  catalog_price_missing: 'السعر غير معروف',
}

const MATCH_STATUS_LABEL: Record<string, string> = {
  confirmed:             'مطابق للكتالوج',
  needs_review:          'يحتاج مراجعة',
  needs_variant:         'يحتاج اختيار الحجم',
  custom_unmatched_item: 'يحتاج مراجعة',
}

const MATCH_STATUS_CLASS: Record<string, string> = {
  confirmed:             'bg-emerald-50 text-emerald-700 border-emerald-200',
  needs_review:          'bg-amber-50 text-amber-800 border-amber-200',
  needs_variant:         'bg-orange-50 text-orange-800 border-orange-200',
  custom_unmatched_item: 'bg-red-50 text-red-700 border-red-200',
}

function resolveItemStatus(item: OrderDetailLineItem): string {
  if (item.is_catalog_matched) return 'confirmed'
  return item.match_status || (item.product_id ? 'needs_review' : 'custom_unmatched_item')
}

function formatSar(value?: number | null): string {
  if (typeof value !== 'number') return '—'
  return `${value.toFixed(2)} ر.س`
}

type Props = {
  order: OrderDetailType
  onOrderUpdated: (order: OrderDetailType) => void
}

export default function OrderEditPanel({ order, onOrderUpdated }: Props) {
  const navigate = useNavigate()
  const apiId = orderApiId(order)
  const [editMode, setEditMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null)

  const [firstName, setFirstName] = useState(order.customer_first_name || '')
  const [lastName, setLastName] = useState(order.customer_last_name || '')
  const [phone, setPhone] = useState(order.phone !== '—' ? order.phone : '')
  const [internalNote, setInternalNote] = useState(order.internal_note || '')

  const [city, setCity] = useState(order.customer_address.city || '')
  const [district, setDistrict] = useState(order.customer_address.district || '')
  const [street, setStreet] = useState(order.customer_address.street || '')
  const [address, setAddress] = useState(order.customer_address.address || '')
  const [shortCode, setShortCode] = useState(order.short_address_code || '')
  const [mapsUrl, setMapsUrl] = useState(order.google_maps_url || '')
  const [deliveryNotes, setDeliveryNotes] = useState(
    order.shipping_meta?.delivery_notes || '',
  )

  const [shippingProvider, setShippingProvider] = useState(
    order.shipping_meta?.shipping_provider || 'manual',
  )
  const [shippingCost, setShippingCost] = useState(
    order.shipping_meta?.shipping_cost != null ? String(order.shipping_meta.shipping_cost) : '',
  )
  const [trackingNumber, setTrackingNumber] = useState(
    order.shipping_meta?.tracking_number || '',
  )
  const [shippingStatus, setShippingStatus] = useState(
    order.shipping_meta?.shipping_status || '',
  )

  const [productQuery, setProductQuery] = useState('')
  const [productHits, setProductHits] = useState<ProductLite[]>([])
  const [selectedProduct, setSelectedProduct] = useState<ProductLite | null>(null)
  const [variants, setVariants] = useState<CatalogProductVariantRow[]>([])
  const [selectedVariantId, setSelectedVariantId] = useState('')
  const [addQty, setAddQty] = useState('1')

  useEffect(() => {
    if (!editMode) return
    setFirstName(order.customer_first_name || '')
    setLastName(order.customer_last_name || '')
    setPhone(order.phone !== '—' ? order.phone : '')
    setInternalNote(order.internal_note || '')
    setCity(order.customer_address.city || '')
    setDistrict(order.customer_address.district || '')
    setStreet(order.customer_address.street || '')
    setAddress(order.customer_address.address || '')
    setShortCode(order.short_address_code || '')
    setMapsUrl(order.google_maps_url || '')
    setDeliveryNotes(order.shipping_meta?.delivery_notes || '')
    setShippingProvider(order.shipping_meta?.shipping_provider || 'manual')
    setShippingCost(
      order.shipping_meta?.shipping_cost != null
        ? String(order.shipping_meta.shipping_cost)
        : '',
    )
    setTrackingNumber(order.shipping_meta?.tracking_number || '')
    setShippingStatus(order.shipping_meta?.shipping_status || '')
  }, [order, editMode])

  useEffect(() => {
    if (!editMode || productQuery.trim().length < 2) {
      setProductHits([])
      return
    }
    const t = setTimeout(() => {
      knowledgeApi.searchProducts(productQuery, 8)
        .then((r: { items: ProductLite[] }) => setProductHits(r.items))
        .catch(() => setProductHits([]))
    }, 250)
    return () => clearTimeout(t)
  }, [productQuery, editMode])

  const missingLabels = useMemo(() => {
    const keys = [...(order.missing_fields || []), ...(order.confirm_blockers || [])]
    return [...new Set(keys)].map((k: string) => MISSING_FIELD_LABELS[k] || k)
  }, [order.missing_fields, order.confirm_blockers])

  const canConfirmReady = Boolean(order.can_confirm_ready)
    && order.line_items.length > 0
    && order.line_items.every((it) => it.is_catalog_matched)

  const run = async (fn: () => Promise<{ order: OrderDetailType }>, okText: string) => {
    setBusy(true)
    setToast(null)
    try {
      const res = await fn()
      onOrderUpdated(res.order)
      setToast({ ok: true, text: okText })
    } catch (e: unknown) {
      setToast({
        ok: false,
        text: e instanceof Error ? e.message : 'تعذّر حفظ التعديل',
      })
    } finally {
      setBusy(false)
    }
  }

  const saveCustomer = () => run(
    () => featureRealityApi.updateOrderCustomer(apiId, {
      first_name: firstName.trim(),
      last_name: lastName.trim(),
      phone: phone.trim(),
      internal_note: internalNote.trim(),
    }),
    'تم حفظ بيانات العميل',
  )

  const saveAddress = () => run(
    () => featureRealityApi.updateOrderAddress(apiId, {
      city: city.trim(),
      district: district.trim(),
      street: street.trim(),
      address: address.trim(),
      short_address_code: shortCode.trim(),
      google_maps_url: mapsUrl.trim(),
      delivery_notes: deliveryNotes.trim(),
    }),
    'تم حفظ عنوان التوصيل',
  )

  const saveShippingMeta = () => run(
    () => featureRealityApi.updateOrderShippingMeta(apiId, {
      shipping_provider: shippingProvider,
      shipping_cost: shippingCost.trim() ? Number(shippingCost) : undefined,
      tracking_number: trackingNumber.trim(),
      shipping_status: shippingStatus.trim(),
      delivery_notes: deliveryNotes.trim(),
    }),
    'تم حفظ بيانات الشحن',
  )

  const pickProduct = async (p: ProductLite) => {
    setSelectedProduct(p)
    setProductQuery(p.title)
    setProductHits([])
    setSelectedVariantId('')
    try {
      const detail = await catalogApi.productDetail(p.id)
      const rows = (detail.product.variants || []) as unknown as CatalogProductVariantRow[]
      setVariants(rows)
      if (rows.length === 1) {
        setSelectedVariantId(String(rows[0].salla_variant_id || rows[0].id))
      }
    } catch {
      setVariants([])
    }
  }

  const addCatalogItem = async () => {
    if (!selectedProduct) return
    const variant = variants.find(
      (v) => String(v.salla_variant_id || v.id) === selectedVariantId,
    )
    await run(
      () => featureRealityApi.addOrderLineItem(apiId, {
        product_id: String(selectedProduct.external_id || selectedProduct.id),
        variant_id: variant
          ? String(variant.salla_variant_id || variant.retailer_id || variant.id)
          : undefined,
        quantity: Math.max(parseInt(addQty, 10) || 1, 1),
      }),
      'تمت إضافة المنتج',
    )
    setSelectedProduct(null)
    setProductQuery('')
    setVariants([])
    setAddQty('1')
  }

  const updateQty = (idx: number, qty: number) => {
    if (qty < 1) return
    run(
      () => featureRealityApi.patchOrderLineItem(apiId, idx, { quantity: qty }),
      'تم تحديث الكمية',
    )
  }

  const removeItem = (idx: number) => {
    if (!window.confirm('حذف هذا المنتج من الطلب؟')) return
    run(
      () => featureRealityApi.deleteOrderLineItem(apiId, idx),
      'تم حذف المنتج',
    )
  }

  const replaceItem = (idx: number, p: ProductLite, variantId?: string) => {
    run(
      () => featureRealityApi.patchOrderLineItem(apiId, idx, {
        product_id: String(p.external_id || p.id),
        variant_id: variantId,
        quantity: order.line_items[idx]?.quantity || 1,
      }),
      'تم تغيير المنتج',
    )
  }

  const handleConfirmReady = () => run(
    () => featureRealityApi.confirmOrderReady(apiId),
    'تم تأكيد جاهزية الطلب — بانتظار الدفع',
  )

  const handleCancel = () => {
    if (!window.confirm('إلغاء هذا الطلب؟')) return
    run(
      () => featureRealityApi.cancelOrder(apiId, { reason: 'merchant_cancelled' }),
      'تم إلغاء الطلب',
    )
  }

  const handleDeleteDraft = () => {
    if (!window.confirm('حذف المسودة نهائياً؟ لا يمكن التراجع.')) return
    setBusy(true)
    featureRealityApi.deleteDraftOrder(apiId)
      .then(() => navigate('/orders'))
      .catch((e: unknown) => {
        setToast({
          ok: false,
          text: e instanceof Error ? e.message : 'تعذّر حذف المسودة',
        })
      })
      .finally(() => setBusy(false))
  }

  if (!order.is_editable && !order.can_cancel && !order.can_delete_draft) {
    return null
  }

  return (
    <div className="card p-4 space-y-4 border border-brand-100 bg-brand-50/30">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-900 inline-flex items-center gap-2">
            <Pencil className="w-4 h-4 text-brand-600" />
            تعديل الطلب
          </h2>
          {order.merchant_edited_at && (
            <p className="text-[11px] text-slate-500 mt-0.5">
              آخر تعديل يدوي: {order.merchant_edited_at}
            </p>
          )}
        </div>
        {order.is_editable && (
          <button
            type="button"
            className="btn-secondary text-xs"
            onClick={() => setEditMode((v) => !v)}
          >
            {editMode ? <><X className="w-3.5 h-3.5" /> إغلاق</> : <><Pencil className="w-3.5 h-3.5" /> تعديل</>}
          </button>
        )}
      </div>

      {missingLabels.length > 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <p className="font-medium inline-flex items-center gap-1">
            <AlertTriangle className="w-3.5 h-3.5" /> حقول ناقصة قبل التأكيد:
          </p>
          <p className="mt-1">{missingLabels.join(' · ')}</p>
        </div>
      )}

      {toast && (
        <div className={`px-3 py-2 rounded-md border text-xs ${
          toast.ok ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-red-50 border-red-200 text-red-800'
        }`}>
          {toast.text}
        </div>
      )}

      <section className="space-y-2">
        <h3 className="text-xs font-semibold text-slate-700">المنتجات</h3>
        <div className="space-y-2">
          {order.line_items.map((it: OrderDetailLineItem, idx: number) => (
            <LineItemEditRow
              key={`${it.product_id}-${idx}`}
              item={it}
              idx={idx}
              busy={busy}
              editMode={editMode && Boolean(order.is_editable)}
              onUpdateQty={updateQty}
              onRemove={removeItem}
              onReplace={replaceItem}
            />
          ))}
        </div>
        {editMode && order.is_editable && (
          <div className="rounded-md border border-slate-200 bg-white p-3 space-y-2">
            <p className="text-[11px] font-medium text-slate-600">إضافة من الكتالوج</p>
            <input className="input text-xs" placeholder="ابحث عن منتج…" value={productQuery} onChange={(e) => { setProductQuery(e.target.value); setSelectedProduct(null) }} />
            {productHits.length > 0 && (
              <ul className="border border-slate-100 rounded-md divide-y max-h-40 overflow-auto">
                {productHits.map((p) => (
                  <li key={p.id}>
                    <button type="button" className="w-full text-start px-2 py-1.5 text-xs hover:bg-slate-50" onClick={() => pickProduct(p)}>{p.title}</button>
                  </li>
                ))}
              </ul>
            )}
            {variants.length > 1 && (
              <select className="input text-xs" value={selectedVariantId} onChange={(e) => setSelectedVariantId(e.target.value)}>
                <option value="">اختر الحجم / variant</option>
                {variants.map((v) => (
                  <option key={v.id} value={String(v.salla_variant_id || v.id)}>{v.option_summary || v.sku || `#${v.id}`}</option>
                ))}
              </select>
            )}
            <div className="flex gap-2 items-center">
              <input className="input text-xs w-20" type="number" min={1} value={addQty} onChange={(e) => setAddQty(e.target.value)} />
              <button type="button" className="btn-secondary text-xs" disabled={busy || !selectedProduct || (variants.length > 1 && !selectedVariantId)} onClick={addCatalogItem}>
                <Plus className="w-3.5 h-3.5" /> إضافة
              </button>
            </div>
          </div>
        )}
      </section>

      {editMode && order.is_editable && (
        <div className="space-y-4">
          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-slate-700">بيانات العميل</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <input className="input text-xs" placeholder="الاسم الأول" value={firstName} onChange={(e) => setFirstName(e.target.value)} />
              <input className="input text-xs" placeholder="اسم العائلة" value={lastName} onChange={(e) => setLastName(e.target.value)} />
              <input className="input text-xs sm:col-span-2" placeholder="الجوال" dir="ltr" value={phone} onChange={(e) => setPhone(e.target.value)} />
              <textarea className="input text-xs sm:col-span-2 min-h-[60px]" placeholder="ملاحظة داخلية (اختياري)" value={internalNote} onChange={(e) => setInternalNote(e.target.value)} />
            </div>
            <button type="button" className="btn-primary text-xs" disabled={busy} onClick={saveCustomer}>
              <Save className="w-3.5 h-3.5" /> حفظ العميل
            </button>
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-slate-700">عنوان التوصيل</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <input className="input text-xs" placeholder="المدينة" value={city} onChange={(e) => setCity(e.target.value)} />
              <input className="input text-xs" placeholder="الحي" value={district} onChange={(e) => setDistrict(e.target.value)} />
              <input className="input text-xs sm:col-span-2" placeholder="الشارع / وصف العنوان" value={street || address} onChange={(e) => { setStreet(e.target.value); setAddress(e.target.value) }} />
              <input className="input text-xs" placeholder="الرمز الوطني المختصر" value={shortCode} onChange={(e) => setShortCode(e.target.value)} />
              <input className="input text-xs" placeholder="رابط Google Maps" dir="ltr" value={mapsUrl} onChange={(e) => setMapsUrl(e.target.value)} />
              <textarea className="input text-xs sm:col-span-2 min-h-[50px]" placeholder="ملاحظات التوصيل" value={deliveryNotes} onChange={(e) => setDeliveryNotes(e.target.value)} />
            </div>
            <button type="button" className="btn-primary text-xs" disabled={busy} onClick={saveAddress}>
              <Save className="w-3.5 h-3.5" /> حفظ العنوان
            </button>
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold text-slate-700">بيانات الشحن (تجهيز OTO / Beez)</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <select className="input text-xs" value={shippingProvider} onChange={(e) => setShippingProvider(e.target.value)}>
                <option value="manual">يدوي</option>
                <option value="oto">OTO</option>
                <option value="beez">Beez</option>
              </select>
              <input className="input text-xs" placeholder="تكلفة الشحن" dir="ltr" value={shippingCost} onChange={(e) => setShippingCost(e.target.value)} />
              <input className="input text-xs" placeholder="رقم التتبع" dir="ltr" value={trackingNumber} onChange={(e) => setTrackingNumber(e.target.value)} />
              <input className="input text-xs" placeholder="حالة الشحن" value={shippingStatus} onChange={(e) => setShippingStatus(e.target.value)} />
            </div>
            <button type="button" className="btn-secondary text-xs" disabled={busy} onClick={saveShippingMeta}>
              <Save className="w-3.5 h-3.5" /> حفظ بيانات الشحن
            </button>
          </section>
        </div>
      )}

      <div className="flex flex-wrap gap-2 pt-1 border-t border-slate-200">
        {order.is_editable && (
          <button
            type="button"
            className="btn-primary text-xs disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={busy || !canConfirmReady}
            title={!canConfirmReady ? 'أكمل مطابقة المنتجات والبيانات الناقصة أولاً' : undefined}
            onClick={handleConfirmReady}
          >
            <CheckCircle2 className="w-3.5 h-3.5" /> تأكيد جاهزية الطلب
          </button>
        )}
        {order.can_cancel && (
          <button type="button" className="btn-secondary text-xs text-amber-800" disabled={busy} onClick={handleCancel}>
            إلغاء الطلب
          </button>
        )}
        {order.can_delete_draft && (
          <button type="button" className="btn-secondary text-xs text-red-700" disabled={busy} onClick={handleDeleteDraft}>
            <Trash2 className="w-3.5 h-3.5" /> حذف المسودة
          </button>
        )}
      </div>
    </div>
  )
}

function LineItemEditRow({
  item,
  idx,
  busy,
  editMode,
  onUpdateQty,
  onRemove,
  onReplace,
}: {
  item: OrderDetailLineItem
  idx: number
  busy: boolean
  editMode: boolean
  onUpdateQty: (idx: number, qty: number) => void
  onRemove: (idx: number) => void
  onReplace: (idx: number, p: ProductLite, variantId?: string) => void
}) {
  const [replaceQuery, setReplaceQuery] = useState('')
  const [replaceHits, setReplaceHits] = useState<ProductLite[]>([])
  const [replaceProduct, setReplaceProduct] = useState<ProductLite | null>(null)
  const [replaceVariants, setReplaceVariants] = useState<CatalogProductVariantRow[]>([])
  const [replaceVariantId, setReplaceVariantId] = useState('')

  useEffect(() => {
    if (!editMode || replaceQuery.trim().length < 2) {
      setReplaceHits([])
      return
    }
    const t = setTimeout(() => {
      knowledgeApi.searchProducts(replaceQuery, 5)
        .then((r: { items: ProductLite[] }) => setReplaceHits(r.items))
        .catch(() => setReplaceHits([]))
    }, 250)
    return () => clearTimeout(t)
  }, [replaceQuery, editMode])

  const pickReplaceProduct = async (p: ProductLite) => {
    setReplaceProduct(p)
    setReplaceQuery(p.title)
    setReplaceHits([])
    setReplaceVariantId('')
    try {
      const detail = await catalogApi.productDetail(p.id)
      const rows = (detail.product.variants || []) as unknown as CatalogProductVariantRow[]
      setReplaceVariants(rows)
      if (rows.length === 1) {
        setReplaceVariantId(String(rows[0].salla_variant_id || rows[0].id))
      }
    } catch {
      setReplaceVariants([])
    }
  }

  const applyReplace = () => {
    if (!replaceProduct) return
    if (replaceVariants.length > 1 && !replaceVariantId) return
    onReplace(
      idx,
      replaceProduct,
      replaceVariantId || undefined,
    )
    setReplaceQuery('')
    setReplaceHits([])
    setReplaceProduct(null)
    setReplaceVariants([])
    setReplaceVariantId('')
  }

  const status = resolveItemStatus(item)
  const statusLabel = MATCH_STATUS_LABEL[status] || status
  const statusCls = MATCH_STATUS_CLASS[status] || MATCH_STATUS_CLASS.needs_review
  const matched = item.is_catalog_matched === true
  const displayName = item.catalog_product_name || item.name
  const catalogUrl = item.catalog_product_id
    ? `/catalog?product=${item.catalog_product_id}`
    : item.product_url || null

  return (
    <div className="rounded-md border border-slate-200 bg-white p-3 space-y-2">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex items-start gap-2 min-w-0 flex-1">
          {item.image_url
            ? <img src={item.image_url} alt="" className="w-12 h-12 rounded object-cover shrink-0" />
            : <div className="w-12 h-12 rounded bg-slate-100 flex items-center justify-center shrink-0"><Package className="w-5 h-5 text-slate-400" /></div>}
          <div className="min-w-0">
            <p className="text-xs font-medium text-slate-800">{displayName}</p>
            {!matched && item.name !== displayName && (
              <p className="text-[10px] text-slate-500 mt-0.5">نص العميل: {item.name}</p>
            )}
            <span className={`inline-flex mt-1 px-1.5 py-0.5 rounded border text-[10px] ${statusCls}`}>
              {statusLabel}
            </span>
            {matched && (item.variant_label || item.variant_name) && (
              <p className="text-[10px] text-slate-600 mt-1">الحجم: {item.variant_label || item.variant_name}</p>
            )}
            {matched && typeof item.unit_price === 'number' && (
              <p className="text-[10px] text-slate-600 mt-0.5">
                {formatSar(item.unit_price)} × {item.quantity} = {formatSar(item.line_total ?? item.unit_price * item.quantity)}
              </p>
            )}
            {!matched && item.query_hint && (
              <p className="text-[10px] text-slate-400 mt-1">تلميح: {item.query_hint}</p>
            )}
            {catalogUrl && matched && (
              <a href={catalogUrl} className="inline-flex items-center gap-1 text-[10px] text-blue-600 mt-1" target="_blank" rel="noreferrer">
                <ExternalLink className="w-3 h-3" /> فتح في الكتالوج
              </a>
            )}
          </div>
        </div>
        {editMode && (
          <div className="flex items-center gap-2">
            <input type="number" min={1} className="input text-xs w-16" defaultValue={item.quantity} disabled={busy}
              onBlur={(e) => { const q = parseInt(e.target.value, 10); if (q > 0 && q !== item.quantity) onUpdateQty(idx, q) }} />
            <button type="button" className="text-red-600 hover:text-red-700" disabled={busy} onClick={() => onRemove(idx)}>
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
      {editMode && !matched && (
        <div className="space-y-2 border-t border-slate-100 pt-2">
          <p className="text-[10px] font-medium text-slate-600">استبدال بمنتج من الكتالوج</p>
          <input className="input text-xs" placeholder="ابحث عن منتج…" value={replaceQuery}
            onChange={(e) => { setReplaceQuery(e.target.value); setReplaceProduct(null); setReplaceVariants([]) }} />
          {replaceHits.length > 0 && (
            <ul className="border border-slate-100 rounded-md divide-y max-h-32 overflow-auto">
              {replaceHits.map((p) => (
                <li key={p.id}>
                  <button type="button" className="w-full text-start px-2 py-1 text-[11px] hover:bg-slate-50"
                    onClick={() => pickReplaceProduct(p)}>
                    {p.title}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {replaceVariants.length > 1 && (
            <select className="input text-xs" value={replaceVariantId} onChange={(e) => setReplaceVariantId(e.target.value)}>
              <option value="">اختر الحجم / variant</option>
              {replaceVariants.map((v) => (
                <option key={v.id} value={String(v.salla_variant_id || v.id)}>
                  {v.option_summary || v.sku || `#${v.id}`}
                </option>
              ))}
            </select>
          )}
          {replaceProduct && (
            <button
              type="button"
              className="btn-secondary text-xs"
              disabled={busy || (replaceVariants.length > 1 && !replaceVariantId)}
              onClick={applyReplace}
            >
              تأكيد الاستبدال
            </button>
          )}
        </div>
      )}
    </div>
  )
}
