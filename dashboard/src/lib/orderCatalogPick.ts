import type { CatalogProductDiagRow, CatalogProductVariantRow } from '../api/catalog'

export type CatalogPriceInput = string | number | null | undefined

export type NormalizedCatalogVariant = {
  variantId: string
  name: string
  unitPrice: number | null
}

export type NormalizedCatalogProduct = {
  productId: number
  productRef: string
  name: string
  imageUrl: string
  productUrl: string
  hasVariants: boolean
  variants: NormalizedCatalogVariant[]
  displayPrice: string
  unitPrice: number | null
}

export function parseCatalogPrice(value: CatalogPriceInput): number | null {
  if (value == null) return null
  if (typeof value === 'number') {
    return Number.isFinite(value) && value > 0 ? value : null
  }
  const text = String(value).replace(/ر\.س|SAR|,/gi, '').trim()
  if (!text) return null
  const n = parseFloat(text.split(/\s+/)[0])
  return Number.isFinite(n) && n > 0 ? n : null
}

export function formatCatalogPrice(value: CatalogPriceInput): string {
  const n = parseCatalogPrice(value)
  return n != null ? `${n.toFixed(2)} ر.س` : '—'
}

export function productRef(row: Pick<CatalogProductDiagRow, 'id' | 'external_id'>): string {
  return String(row.external_id || row.id)
}

export function variantRef(variant: CatalogProductVariantRow): string {
  return String(variant.salla_variant_id || variant.retailer_id || variant.id)
}

/** Mirrors backend sellable-variant counting for picker UX. */
export function sellableVariants(rows: CatalogProductVariantRow[]): CatalogProductVariantRow[] {
  if (!rows.length) return []
  const nonDefault = rows.filter((v) => !v.is_default)
  if (nonDefault.length >= 2) return nonDefault
  if (rows.length >= 2) return rows
  return rows
}

/** Align with backend ``product_requires_variant_selection``. */
export function requiresVariantSelection(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
): boolean {
  if (row.has_variants) return true
  return sellableVariants(variants).length > 1
}

export function showsVariantPicker(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
): boolean {
  return sellableVariants(variants).length > 1
}

export function autoSelectVariantId(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
): string {
  const sellable = sellableVariants(variants)
  if (sellable.length === 1) return variantRef(sellable[0])
  if (row.default_variant_id != null) {
    const hit = variants.find((v) => v.id === row.default_variant_id)
    if (hit) return variantRef(hit)
  }
  return ''
}

export function selectedVariantRow(
  variants: CatalogProductVariantRow[],
  variantId: string,
): CatalogProductVariantRow | undefined {
  if (!variantId) return undefined
  return variants.find((v) => variantRef(v) === variantId)
}

export function normalizeCatalogProductRow(row: CatalogProductDiagRow): NormalizedCatalogProduct {
  const rawVariants = row.variants || []
  const variants = sellableVariants(rawVariants).map((v) => ({
    variantId: variantRef(v),
    name: v.option_summary || v.sku || `#${v.id}`,
    unitPrice: parseCatalogPrice(v.price),
  }))
  const hasVariants = requiresVariantSelection(row, rawVariants)
  const parentPrice = parseCatalogPrice(row.price)

  let displayPrice = '—'
  if (hasVariants) {
    const prices = variants.map((v) => v.unitPrice).filter((p): p is number => p != null)
    if (prices.length === 0) {
      displayPrice = 'اختر الحجم'
    } else if (prices.length === 1) {
      displayPrice = formatCatalogPrice(prices[0])
    } else {
      const min = Math.min(...prices)
      const max = Math.max(...prices)
      displayPrice = min === max
        ? formatCatalogPrice(min)
        : `يبدأ من ${formatCatalogPrice(min)}`
    }
  } else {
    displayPrice = parentPrice != null ? formatCatalogPrice(parentPrice) : '—'
  }

  return {
    productId: row.id,
    productRef: productRef(row),
    name: row.title,
    imageUrl: row.image_url || '',
    productUrl: row.product_url || '',
    hasVariants,
    variants,
    displayPrice,
    unitPrice: parentPrice,
  }
}

export function selectedUnitPrice(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
  variantId: string,
): number | null {
  const picked = selectedVariantRow(variants, variantId)
  if (picked) return parseCatalogPrice(picked.price)
  if (!requiresVariantSelection(row, variants)) {
    return parseCatalogPrice(row.price)
  }
  return null
}

export function canSubmitCatalogPick(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
  variantId: string,
): { ok: boolean; reason?: string } {
  if (!row.id || !productRef(row)) {
    return { ok: false, reason: 'اختر منتجًا من نتائج الكتالوج' }
  }
  if (requiresVariantSelection(row, variants) && !variantId) {
    return { ok: false, reason: 'اختر الحجم أولًا' }
  }
  const price = selectedUnitPrice(row, variants, variantId)
  if (price == null) {
    return { ok: false, reason: 'السعر غير متوفر في الكتالوج' }
  }
  const picked = selectedVariantRow(variants, variantId)
  if (picked && parseCatalogPrice(picked.price) == null) {
    return { ok: false, reason: 'السعر غير متوفر للحجم المختار' }
  }
  return { ok: true }
}

export function displayPrice(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
  variantId?: string,
): string {
  if (variantId) {
    const picked = selectedVariantRow(variants, variantId)
    const vp = parseCatalogPrice(picked?.price)
    if (vp != null) return formatCatalogPrice(vp)
  }
  return normalizeCatalogProductRow(row).displayPrice
}

export function mapOrderEditError(error: unknown, fallback = 'تعذّر حفظ التعديل'): string {
  const raw = error instanceof Error ? error.message : String(error ?? '')
  const code = (raw.includes(':') ? raw.split(':').pop()?.trim() : raw.trim()) || raw.trim()

  const labels: Record<string, string> = {
    catalog_variant_required: 'اختر الحجم أولًا',
    catalog_variant_not_found: 'الحجم المختار غير موجود في الكتالوج',
    catalog_product_not_found: 'المنتج غير موجود في الكتالوج',
    catalog_evidence_incomplete: 'تعذّر إضافة المنتج — بيانات الكتالوج غير مكتملة',
    needs_variant: 'اختر الحجم أولًا',
    needs_review: 'تعذّر إضافة المنتج — بيانات الكتالوج غير مكتملة',
    product_id_required: 'اختر منتجًا من الكتالوج',
    confirmed_item_requires_price: 'السعر غير متوفر في الكتالوج',
    confirmed_item_requires_variant: 'اختر الحجم أولًا',
  }

  if (raw.startsWith('catalog_evidence_incomplete:')) {
    const status = raw.split(':')[1]?.trim()
    return labels[status || ''] || labels.catalog_evidence_incomplete
  }

  return labels[code] || labels[raw] || raw || fallback
}

/** Build API payload — never send free-text name or frontend match_status. */
export function catalogPickApiPayload(pick: {
  productRef: string
  variantId?: string
  quantity: number
}) {
  return {
    product_id: pick.productRef,
    ...(pick.variantId ? { variant_id: pick.variantId } : {}),
    quantity: pick.quantity,
  }
}

/** Merge catalog list row with product detail variants/price when list payload is thin. */
export function mergeCatalogRowWithDetail(
  row: CatalogProductDiagRow,
  detail: {
    price?: string | null
    has_variants?: boolean
    default_variant_id?: number | null
    variants?: CatalogProductVariantRow[]
  },
): CatalogProductDiagRow {
  return {
    ...row,
    price: detail.price || row.price,
    has_variants: row.has_variants ?? detail.has_variants,
    default_variant_id: row.default_variant_id ?? detail.default_variant_id ?? null,
    variants: (row.variants?.length ? row.variants : detail.variants) || [],
  }
}

export function detailVariantsFromProduct(
  variants: Array<Record<string, unknown>> | CatalogProductVariantRow[] | undefined,
): CatalogProductVariantRow[] {
  return (variants || []).map((v, idx) => ({
    id: Number(v.id ?? idx),
    salla_variant_id: (v.salla_variant_id as string | null) ?? null,
    sku: (v.sku as string | null) ?? null,
    retailer_id: (v.retailer_id as string | null) ?? null,
    price: (v.price as string | null) ?? null,
    currency: (v.currency as string | null) ?? null,
    stock_quantity: (v.stock_quantity as number | null) ?? null,
    in_stock: Boolean(v.in_stock ?? true),
    is_default: Boolean(v.is_default ?? false),
    options: (v.options as Record<string, unknown>) || {},
    option_summary: String(v.option_summary || v.sku || ''),
    image_url: String(v.image_url || ''),
  }))
}
