import { useEffect, useMemo, useState } from 'react'
import { Loader2, Package, Search } from 'lucide-react'
import { catalogApi, type CatalogProductDiagRow, type CatalogProductVariantRow } from '../../api/catalog'
import {
  autoSelectVariantId,
  canSubmitCatalogPick,
  detailVariantsFromProduct,
  displayPrice,
  mergeCatalogRowWithDetail,
  normalizeCatalogProductRow,
  requiresVariantSelection,
  selectedVariantRow,
  sellableVariants,
  showsVariantPicker,
  variantRef,
} from '../../lib/orderCatalogPick'

const MIN_QUERY_LEN = 2

export type CatalogPickPayload = {
  productId: number
  productRef: string
  variantId?: string
  quantity: number
}

type Props = {
  disabled?: boolean
  busy?: boolean
  submitLabel: string
  onSubmit: (payload: CatalogPickPayload) => void | Promise<void>
}

export default function CatalogProductPicker({
  disabled = false,
  busy = false,
  submitLabel,
  onSubmit,
}: Props) {
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<CatalogProductDiagRow[]>([])
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [selected, setSelected] = useState<CatalogProductDiagRow | null>(null)
  const [variants, setVariants] = useState<CatalogProductVariantRow[]>([])
  const [variantId, setVariantId] = useState('')
  const [qty, setQty] = useState('1')

  useEffect(() => {
    const needle = query.trim()
    if (disabled || needle.length < MIN_QUERY_LEN || selected) {
      if (!selected) setHits([])
      if (!needle.length) setLoading(false)
      return
    }

    let cancelled = false
    setLoading(true)
    setError(null)

    const t = setTimeout(() => {
      catalogApi.products(12, 0, { q: needle })
        .then((res) => {
          if (cancelled) return
          setHits(res.rows || [])
          if ((res.rows || []).length === 0) {
            setError('لا توجد منتجات مطابقة في الكتالوج')
          }
        })
        .catch((e: unknown) => {
          if (cancelled) return
          setHits([])
          setError(e instanceof Error ? e.message : 'تعذّر البحث في الكتالوج')
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, 280)

    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [query, disabled, selected])

  const normalized = useMemo(
    () => (selected ? normalizeCatalogProductRow(selected) : null),
    [selected],
  )

  const pickedVariant = useMemo(
    () => selectedVariantRow(variants, variantId),
    [variants, variantId],
  )

  const needsVariant = useMemo(
    () => (selected ? requiresVariantSelection(selected, variants) : false),
    [selected, variants],
  )

  const showVariantPicker = useMemo(
    () => (selected ? showsVariantPicker(selected, variants) : false),
    [selected, variants],
  )

  const submitGate = useMemo(
    () => (selected
      ? canSubmitCatalogPick(selected, variants, variantId)
      : { ok: false as const, reason: 'اختر منتجًا من نتائج الكتالوج' }),
    [selected, variants, variantId],
  )

  const clearSelection = () => {
    setSelected(null)
    setVariants([])
    setVariantId('')
    setSubmitError(null)
  }

  const applySelection = (row: CatalogProductDiagRow, rows: CatalogProductVariantRow[]) => {
    setSelected(row)
    setQuery(row.title)
    setHits([])
    setError(null)
    setSubmitError(null)
    setVariants(rows)
    setVariantId(autoSelectVariantId(row, rows))
  }

  const pickRow = async (row: CatalogProductDiagRow) => {
    setDetailLoading(true)
    setSubmitError(null)
    try {
      let full = row
      const needsDetail = (
        (row.has_variants || (row.sellable_variants_count ?? 0) > 0)
        && !(row.variants?.length)
      ) || !row.price

      if (needsDetail) {
        const detail = await catalogApi.productDetail(row.id)
        const detailVariants = detailVariantsFromProduct(
          detail.product.variants as Array<Record<string, unknown>>,
        )
        full = mergeCatalogRowWithDetail(row, {
          price: detail.product.price,
          has_variants: Boolean(detail.product.variants?.length) || row.has_variants,
          default_variant_id: null,
          variants: detailVariants,
        })
      }

      applySelection(full, full.variants || [])
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل تفاصيل المنتج')
    } finally {
      setDetailLoading(false)
    }
  }

  const handleSubmit = async () => {
    if (!selected || disabled || busy) return
    const gate = canSubmitCatalogPick(selected, variants, variantId)
    if (!gate.ok) {
      setSubmitError(gate.reason || 'تعذّر إضافة المنتج')
      return
    }

    setSubmitError(null)
    try {
      await onSubmit({
        productId: selected.id,
        productRef: String(selected.external_id || selected.id),
        variantId: variantId || undefined,
        quantity: Math.max(parseInt(qty, 10) || 1, 1),
      })
      setQuery('')
      clearSelection()
      setQty('1')
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : 'تعذّر إضافة المنتج')
    }
  }

  const sellable = sellableVariants(variants)

  return (
    <div className="space-y-2" dir="rtl">
      <div className="relative">
        <Search className="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400 pointer-events-none" />
        <input
          className="input text-xs ps-8"
          placeholder="ابحث في الكتالوج… (مثل: عسل، طلح، سمر)"
          value={query}
          disabled={disabled || busy || detailLoading}
          onChange={(e) => {
            setQuery(e.target.value)
            if (selected && e.target.value !== selected.title) {
              clearSelection()
            }
          }}
        />
      </div>

      {(loading || detailLoading) && (
        <p className="text-[11px] text-slate-500 inline-flex items-center gap-1">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          {detailLoading ? 'جاري تحميل المنتج…' : 'جاري البحث…'}
        </p>
      )}

      {!loading && error && !selected && query.trim().length >= MIN_QUERY_LEN && (
        <p className="text-[11px] text-amber-800 bg-amber-50 border border-amber-100 rounded px-2 py-1">
          {error}
        </p>
      )}

      {!loading && !detailLoading && hits.length > 0 && !selected && (
        <ul className="border border-slate-100 rounded-md divide-y max-h-48 overflow-auto bg-white">
          {hits.map((row) => {
            const norm = normalizeCatalogProductRow(row)
            return (
              <li key={row.id}>
                <button
                  type="button"
                  className="w-full text-start px-2 py-2 hover:bg-slate-50 flex items-center gap-2"
                  disabled={disabled || busy}
                  onClick={() => { void pickRow(row) }}
                >
                  {row.image_url
                    ? <img src={row.image_url} alt="" className="w-10 h-10 rounded object-cover shrink-0" />
                    : <div className="w-10 h-10 rounded bg-slate-100 flex items-center justify-center shrink-0"><Package className="w-4 h-4 text-slate-400" /></div>}
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium text-slate-800 truncate">{row.title}</p>
                    <p className="text-[10px] text-slate-500">{norm.displayPrice}</p>
                    {norm.hasVariants && (
                      <p className="text-[10px] text-slate-400">
                        {showsVariantPicker(row, row.variants || [])
                          ? 'أحجام متعددة — اختر الحجم'
                          : 'يحتاج اختيار الحجم'}
                      </p>
                    )}
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      )}

      {selected && normalized && (
        <div className="rounded-md border border-brand-100 bg-brand-50/40 p-2 space-y-2">
          <div className="flex items-start gap-2">
            {selected.image_url
              ? <img src={selected.image_url} alt="" className="w-12 h-12 rounded object-cover shrink-0" />
              : <div className="w-12 h-12 rounded bg-slate-100 flex items-center justify-center shrink-0"><Package className="w-5 h-5 text-slate-400" /></div>}
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-slate-900">{normalized.name}</p>
              <p className="text-[10px] text-slate-600">
                {displayPrice(selected, variants, variantId)}
              </p>
              {pickedVariant && (
                <p className="text-[10px] text-slate-500">
                  الحجم: {pickedVariant.option_summary || pickedVariant.sku || `#${pickedVariant.id}`}
                </p>
              )}
              {needsVariant && !variantId && (
                <p className="text-[10px] text-amber-700 mt-0.5">اختر الحجم أولًا</p>
              )}
            </div>
            <button
              type="button"
              className="text-[10px] text-slate-500 hover:text-slate-700 shrink-0"
              disabled={busy}
              onClick={() => { setQuery(''); clearSelection() }}
            >
              تغيير
            </button>
          </div>

          {showVariantPicker && (
            <select
              className="input text-xs"
              value={variantId}
              disabled={disabled || busy}
              onChange={(e) => { setVariantId(e.target.value); setSubmitError(null) }}
            >
              <option value="">اختر الحجم / variant</option>
              {sellable.map((v) => (
                <option key={v.id} value={variantRef(v)}>
                  {v.option_summary || v.sku || `#${v.id}`} — {displayPrice(selected, variants, variantRef(v))}
                </option>
              ))}
            </select>
          )}

          {submitError && (
            <p className="text-[10px] text-red-700 bg-red-50 border border-red-100 rounded px-2 py-1">
              {submitError}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <input
              className="input text-xs w-20"
              type="number"
              min={1}
              value={qty}
              disabled={disabled || busy}
              onChange={(e) => setQty(e.target.value)}
            />
            <button
              type="button"
              className="btn-secondary text-xs disabled:opacity-50"
              disabled={disabled || busy || !submitGate.ok}
              title={submitGate.reason}
              onClick={() => { void handleSubmit() }}
            >
              {submitLabel}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
