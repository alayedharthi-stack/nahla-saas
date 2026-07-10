import type { CatalogDiagnostics, DominantSource, ProductSource } from '../../api/catalog'

/** Best merchant-facing source label from diagnostics — no guessing beyond breakdown. */
export function resolveCatalogDisplaySource(
  products: CatalogDiagnostics['products'],
): DominantSource {
  const { dominant_source, source_breakdown } = products
  if (dominant_source !== 'unknown') return dominant_source
  const active = (Object.entries(source_breakdown) as Array<[ProductSource, number]>)
    .filter(([, count]) => count > 0)
  if (active.length === 1) return active[0][0] as DominantSource
  if (active.length > 1) return 'mixed'
  return 'unknown'
}

/** Row source: only upgrade ``unknown`` when Meta retailer id is present. */
export function resolveRowDisplaySource(source: ProductSource, metaRetailerId: string | null): ProductSource {
  if (source !== 'unknown') return source
  if (metaRetailerId?.trim()) return 'meta'
  return 'unknown'
}
