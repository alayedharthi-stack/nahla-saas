/**
 * Smoke tests for order catalog picker helpers.
 *
 * Run: npm run smoke:order-catalog-pick   (from dashboard/)
 */
import {
  autoSelectVariantId,
  canSubmitCatalogPick,
  catalogPickApiPayload,
  normalizeCatalogProductRow,
  parseCatalogPrice,
  requiresVariantSelection,
  sellableVariants,
  showsVariantPicker,
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
assert('parseCatalogPrice accepts numeric price', parseCatalogPrice(99.5) === 99.5)
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

const honeyRow: CatalogProductDiagRow = {
  id: 7,
  title: 'عسل طلح',
  external_id: 'ext-7',
  sku: null,
  meta_retailer_id: null,
  effective_retailer_id: 'ext-7',
  publish_status: 'published',
  in_stock: true,
  stock_quantity: 10,
  price: null,
  currency: 'SAR',
  image_url: 'https://img/honey.jpg',
  product_url: 'https://shop/honey',
  source: 'salla',
  readiness_badge: null,
  has_variants: true,
  default_variant_id: 1,
  variants: [v1, v2],
  sellable_variants_count: 2,
}

assert('sellableVariants returns both when >=2 non-default', sellableVariants([v1, v2]).length === 2)
assert('requiresVariantSelection when has_variants flag', requiresVariantSelection(
  { has_variants: true } as CatalogProductDiagRow,
  [v1],
))
assert('showsVariantPicker when multiple sellable', showsVariantPicker(honeyRow, [v1, v2]))
assert('autoSelectVariantId picks sole sellable variant', autoSelectVariantId(
  {} as CatalogProductDiagRow,
  [v1],
) === variantRef(v1))

const norm = normalizeCatalogProductRow(honeyRow)
assert('search row with variants shows starting price', norm.displayPrice.includes('100'))
assert('add disabled until variant selected', !canSubmitCatalogPick(honeyRow, [v1, v2], '').ok)
assert('add enabled after variant selected', canSubmitCatalogPick(honeyRow, [v1, v2], 'sv-2').ok)

const payload = catalogPickApiPayload({
  productRef: 'ext-7',
  variantId: 'sv-2',
  quantity: 2,
})
assert('payload includes product_id + variant_id', payload.product_id === 'ext-7' && payload.variant_id === 'sv-2')

const simpleRow: CatalogProductDiagRow = {
  ...honeyRow,
  has_variants: false,
  variants: [],
  price: '150',
}
assert('simple product displayPrice shows unit price', normalizeCatalogProductRow(simpleRow).displayPrice === '150.00 ر.س')
assert('simple product can submit without variant', canSubmitCatalogPick(simpleRow, [], '').ok)

if (failed > 0) {
  console.error(`\n${failed} order catalog pick check(s) failed`)
  process.exit(1)
}
console.log('\nAll order catalog pick checks passed.')
