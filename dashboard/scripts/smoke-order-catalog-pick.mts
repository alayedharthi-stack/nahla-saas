/**
 * Smoke tests for order catalog picker helpers.
 *
 * Run: npx --yes tsx@4 scripts/smoke-order-catalog-pick.mts
 */
import {
  autoSelectVariantId,
  parseCatalogPrice,
  requiresVariantSelection,
  sellableVariants,
  variantRef,
} from '../src/lib/orderCatalogPick.ts'
import type { CatalogProductDiagRow, CatalogProductVariantRow } from '../src/api/catalog.ts'

let failed = 0

function assert(name: string, ok: boolean) {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

assert('parseCatalogPrice accepts SAR string', parseCatalogPrice('120.00 ر.س') === 120)
assert('parseCatalogPrice rejects zero', parseCatalogPrice('0') === null)

const v1: CatalogProductVariantRow = {
  id: 1, salla_variant_id: 'sv-1', sku: '1kg', retailer_id: null,
  price: '100', currency: 'SAR', stock_quantity: 5, in_stock: true,
  is_default: false, options: {}, option_summary: '1kg', image_url: '',
}
const v2: CatalogProductVariantRow = {
  id: 2, salla_variant_id: 'sv-2', sku: '2kg', retailer_id: null,
  price: '180', currency: 'SAR', stock_quantity: 3, in_stock: true,
  is_default: false, options: {}, option_summary: '2kg', image_url: '',
}

assert('sellableVariants returns both when >=2 non-default', sellableVariants([v1, v2]).length === 2)
assert('requiresVariantSelection when has_variants', requiresVariantSelection(
  { has_variants: true } as CatalogProductDiagRow,
  [v1, v2],
))
assert('autoSelectVariantId picks sole variant', autoSelectVariantId(
  {} as CatalogProductDiagRow,
  [v1],
) === variantRef(v1))

if (failed > 0) {
  console.error(`\n${failed} order catalog pick check(s) failed`)
  process.exit(1)
}
console.log('\nAll order catalog pick checks passed.')
