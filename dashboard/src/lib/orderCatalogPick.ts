import type { CatalogProductDiagRow, CatalogProductVariantRow } from '../api/catalog'

export function parseCatalogPrice(value?: string | null): number | null {
  if (!value) return null
  const text = value.replace(/ر\.س|SAR|,/gi, '').trim()
  if (!text) return null
  const n = parseFloat(text.split(/\s+/)[0])
  return Number.isFinite(n) && n > 0 ? n : null
}

export function formatCatalogPrice(value?: string | null): string {
  const n = parseCatalogPrice(value)
  return n != null ? `${n.toFixed(2)} ر.س` : '—'
}

export function productRef(row: CatalogProductDiagRow): string {
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

export function requiresVariantSelection(
  row: CatalogProductDiagRow,
  variants: CatalogProductVariantRow[],
): boolean {
  if (row.has_variants) return sellableVariants(variants).length > 1
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

export function displayPrice(
  row: CatalogProductDiagRow,
  variant?: CatalogProductVariantRow,
): string {
  return formatCatalogPrice(variant?.price || row.price)
}
