/** Resolve the best display image URL for a catalog product row. */
export type CatalogImageSource = {
  image_url?: string | null
  variants?: Array<{ image_url?: string | null }> | null
  additional_images?: string[] | null
}

function isHttpUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false
  const s = value.trim()
  return s.startsWith('https://') || s.startsWith('http://')
}

export function resolveCatalogProductImage(row: CatalogImageSource): string {
  const candidates: unknown[] = [
    row.image_url,
    ...(row.additional_images ?? []),
    ...(row.variants ?? []).map(v => v.image_url),
  ]
  for (const candidate of candidates) {
    if (isHttpUrl(candidate)) return candidate.trim()
  }
  return ''
}
