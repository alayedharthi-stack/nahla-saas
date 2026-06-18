import { useEffect, useMemo, useState } from 'react'
import { Loader2, Package, Search } from 'lucide-react'
import {
  catalogApi,
  type CatalogProductDiagRow,
  type CatalogProductVariantRow,
} from '../../api/catalog'
import {
  autoSelectVariantId,
  displayPrice,
  productRef,
  requiresVariantSelection,
  selectedVariantRow,
  sellableVariants,
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
  onSubmit: (payload: CatalogPickPayload) => void
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
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<CatalogProductDiagRow | null>(null)
  const [variants, setVariants] = useState<CatalogProductVariantRow[]>([])
  const [variantId, setVariantId] = useState('')
  const [qty, setQty] = useState('1')
  const [variantHint, setVariantHint] = useState(false)

  useEffect(() => {
    const needle = query.trim()
    if (disabled || needle.length < MIN_QUERY_LEN) {
      setHits([])
      setLoading(false)
      setError(null)
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
  }, [query, disabled])

  const needsVariant = useMemo(
    () => (selected ? requiresVariantSelection(selected, variants) : false),
    [selected, variants],
  )

  const pickedVariant = useMemo(
    () => selectedVariantRow(variants, variantId),
    [variants, variantId],
  )

  const pickRow = (row: CatalogProductDiagRow) => {
    const rows = row.variants || []
    setSelected(row)
    setQuery(row.title)
    setHits([])
    setError(null)
    setVariantHint(false)
    setVariants(rows)
    setVariantId(autoSelectVariantId(row, rows))
  }

  const clearSelection = () => {
    setSelected(null)
    setVariants([])
    setVariantId('')
    setVariantHint(false)
  }

  const handleSubmit = () => {
    if (!selected || disabled || busy) return
    if (needsVariant && !variantId) {
      setVariantHint(true)
      return
    }
    onSubmit({
      productId: selected.id,
      productRef: productRef(selected),
      variantId: variantId || undefined,
      quantity: Math.max(parseInt(qty, 10) || 1, 1),
    })
    setQuery('')
    clearSelection()
    setHits([])
    setQty('1')
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
          disabled={disabled || busy}
          onChange={(e) => {
            setQuery(e.target.value)
            if (selected && e.target.value !== selected.title) {
              clearSelection()
            }
          }}
        />
      </div>

      {loading && (
        <p className="text-[11px] text-slate-500 inline-flex items-center gap-1">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> جاري البحث…
        </p>
      )}

      {!loading && error && query.trim().length >= MIN_QUERY_LEN && (
        <p className="text-[11px] text-amber-800 bg-amber-50 border border-amber-100 rounded px-2 py-1">
          {error}
        </p>
      )}

      {!loading && hits.length > 0 && !selected && (
        <ul className="border border-slate-100 rounded-md divide-y max-h-48 overflow-auto bg-white">
          {hits.map((row) => (
            <li key={row.id}>
              <button
                type="button"
                className="w-full text-start px-2 py-2 hover:bg-slate-50 flex items-center gap-2"
                disabled={disabled || busy}
                onClick={() => pickRow(row)}
              >
                {row.image_url
                  ? <img src={row.image_url} alt="" className="w-10 h-10 rounded object-cover shrink-0" />
                  : <div className="w-10 h-10 rounded bg-slate-100 flex items-center justify-center shrink-0"><Package className="w-4 h-4 text-slate-400" /></div>}
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-medium text-slate-800 truncate">{row.title}</p>
                  <p className="text-[10px] text-slate-500">{displayPrice(row)}</p>
                  {(row.sellable_variants_count ?? 0) > 1 && (
                    <p className="text-[10px] text-slate-400">أحجام متعددة</p>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {selected && (
        <div className="rounded-md border border-brand-100 bg-brand-50/40 p-2 space-y-2">
          <div className="flex items-start gap-2">
            {selected.image_url
              ? <img src={selected.image_url} alt="" className="w-12 h-12 rounded object-cover shrink-0" />
              : <div className="w-12 h-12 rounded bg-slate-100 flex items-center justify-center shrink-0"><Package className="w-5 h-5 text-slate-400" /></div>}
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-slate-900">{selected.title}</p>
              <p className="text-[10px] text-slate-600">{displayPrice(selected, pickedVariant)}</p>
              {pickedVariant && (
                <p className="text-[10px] text-slate-500">
                  الحجم: {pickedVariant.option_summary || pickedVariant.sku || `#${pickedVariant.id}`}
                </p>
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

          {needsVariant && (
            <select
              className="input text-xs"
              value={variantId}
              disabled={disabled || busy}
              onChange={(e) => { setVariantId(e.target.value); setVariantHint(false) }}
            >
              <option value="">اختر الحجم / variant</option>
              {sellable.map((v) => (
                <option key={v.id} value={variantRef(v)}>
                  {v.option_summary || v.sku || `#${v.id}`} — {displayPrice(selected, v)}
                </option>
              ))}
            </select>
          )}

          {variantHint && needsVariant && !variantId && (
            <p className="text-[10px] text-red-700">اختر الحجم أولًا</p>
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
              className="btn-secondary text-xs"
              disabled={disabled || busy || !selected || (needsVariant && !variantId)}
              onClick={handleSubmit}
            >
              {submitLabel}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
