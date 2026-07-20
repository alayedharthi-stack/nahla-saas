/**
 * Regression checks for platform branding and current-tenant identity.
 *
 * Run: npm run check:product-identity (from dashboard/)
 */
import { readFileSync } from 'node:fs'

import { PLATFORM_BRAND, resolveMerchantName } from '../src/lib/productIdentity.ts'

let failed = 0

function assert(name: string, ok: boolean) {
  if (ok) {
    console.log(`OK   ${name}`)
    return
  }
  failed++
  console.error(`FAIL ${name}`)
}

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8')
}

const store = {
  store_name: 'Legacy Store',
  store_name_ar: 'متجر التاجر الحالي',
  store_name_en: 'Current Merchant Store',
}

assert(
  'Arabic merchant identity prefers the current tenant Arabic name',
  resolveMerchantName(store, 'ar') === 'متجر التاجر الحالي',
)
assert(
  'English merchant identity prefers the current tenant English name',
  resolveMerchantName(store, 'en') === 'Current Merchant Store',
)
assert(
  'merchant identity never falls back to platform branding',
  resolveMerchantName(null, 'ar') !== PLATFORM_BRAND.name.ar
    && resolveMerchantName(null, 'en') !== PLATFORM_BRAND.name.en,
)

const sidebarSource = source('../src/components/layout/Sidebar.tsx')
const sidebarBrandStart = sidebarSource.indexOf('{/* Logo */}')
const sidebarNavigationStart = sidebarSource.indexOf('{/* Navigation */}')
const sidebarBrandSource = sidebarSource.slice(sidebarBrandStart, sidebarNavigationStart)
assert(
  'sidebar header renders fixed platform branding',
  sidebarBrandSource.includes('PLATFORM_BRAND.logoUrl')
    && sidebarBrandSource.includes('platformBrandName'),
)
assert(
  'sidebar header never renders merchant identity',
  sidebarBrandStart >= 0
    && sidebarNavigationStart > sidebarBrandStart
    && !sidebarBrandSource.includes('merchantIdentity'),
)
assert(
  'sidebar account area renders current merchant identity',
  sidebarSource.slice(sidebarSource.indexOf('{/* Bottom badge')).includes('merchantIdentity.name'),
)

const headerSource = source('../src/components/layout/Header.tsx')
assert(
  'account menu uses shared current-tenant identity',
  headerSource.includes('const merchantIdentity = useMerchantIdentity()')
    && headerSource.includes(': merchantIdentity.name'),
)
assert(
  'account menu does not read provider-cached store names',
  !headerSource.includes('getStoreName'),
)

const contextSource = source('../src/context/MerchantIdentityContext.tsx')
assert(
  'merchant identity loads from authenticated tenant settings',
  contextSource.includes('settingsApi.getAll()')
    && contextSource.includes('[merchantScoped, tenantId]'),
)

if (failed > 0) {
  console.error(`\n${failed} product identity check(s) failed`)
  process.exit(1)
}

console.log('\nAll product identity checks passed.')
