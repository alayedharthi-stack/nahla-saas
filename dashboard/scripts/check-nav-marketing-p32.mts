/**
 * P3.2 Marketing Hub + Nahla Template Library regression checks.
 *
 * Run: npm run check:nav-marketing-p32 (from dashboard/)
 *
 * CI-safe: reads source files as text — no imports from app modules (avoids lucide-react).
 */
import { readFileSync } from 'node:fs'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8')
}

function extractConstStringArray(tsSource: string, constName: string): string[] {
  const marker = `export const ${constName}`
  const start = tsSource.indexOf(marker)
  if (start < 0) return []
  const bracketStart = tsSource.indexOf('[', start)
  const bracketEnd = tsSource.indexOf('] as const', bracketStart)
  if (bracketStart < 0 || bracketEnd < 0) return []
  const block = tsSource.slice(bracketStart, bracketEnd + 1)
  return [...block.matchAll(/'([^']+)'/g)].map(m => m[1])
}

function extractSimplifiedNavPaths(tsSource: string): string[] {
  const marker = 'export const SIMPLIFIED_NAV_DESTINATIONS'
  const start = tsSource.indexOf(marker)
  if (start < 0) return []
  const end = tsSource.indexOf('export function collectSimplifiedNavPaths', start)
  const block = tsSource.slice(start, end > start ? end : undefined)
  return [...block.matchAll(/to:\s*'([^']+)'/g)].map(m => m[1])
}

const navDataSource = source('../src/lib/merchantNavSimplified.ts')
const appSource = source('../src/App.tsx')
const templateLibrarySource = source('../src/pages/NahlaTemplateLibrary.tsx')
const flagsSource = source('../src/lib/platformFeatureFlags.ts')

const legacyPaths = extractConstStringArray(navDataSource, 'LEGACY_MERCHANT_NAV_PATHS')
const simplifiedPaths = new Set(extractSimplifiedNavPaths(navDataSource))

assert(
  'LEGACY_MERCHANT_NAV_PATHS still defines exactly 27 routes',
  legacyPaths.length === 27,
  `got ${legacyPaths.length}`,
)

for (const legacyPath of legacyPaths) {
  assert(
    `legacy path "${legacyPath}" remains in simplified tree`,
    simplifiedPaths.has(legacyPath),
  )
}

assert(
  'simplified tree includes /marketing hub route',
  simplifiedPaths.has('/marketing'),
)
assert(
  'simplified tree includes /marketing/templates library route',
  simplifiedPaths.has('/marketing/templates'),
)
assert(
  'simplified tree exposes 29 routes (27 legacy + 2 P3.2)',
  simplifiedPaths.size === 29,
  `got ${simplifiedPaths.size}`,
)

assert(
  'marketing destination has directLink to /marketing',
  navDataSource.includes("to: '/marketing'")
    && navDataSource.includes('dest_marketing'),
)

assert(
  'App.tsx registers /marketing route',
  appSource.includes('path="marketing"') && appSource.includes('MarketingHub'),
)
assert(
  'App.tsx registers /marketing/templates route',
  appSource.includes('path="marketing/templates"') && appSource.includes('NahlaTemplateLibrary'),
)
assert(
  'App.tsx keeps /templates route',
  appSource.includes('path="templates"') && appSource.includes('Templates'),
)
assert(
  'App.tsx keeps /widgets route',
  appSource.includes('path="widgets"') && appSource.includes('MerchantWidgets'),
)

const allowedOrderUpdateKeys = ['order_confirmation', 'shipping_tracking']
for (const key of allowedOrderUpdateKeys) {
  assert(
    `NahlaTemplateLibrary mentions allowed template "${key}"`,
    templateLibrarySource.includes(`'${key}'`),
  )
}

const forbiddenOrderStates = [
  'post_delivery',
  'delivered',
  'cancelled',
  'cod_confirmation',
  'predictive_reorder',
  'payment_reminder',
  'out_for_delivery',
  'failed_delivery',
  'reorder',
]
for (const forbidden of forbiddenOrderStates) {
  assert(
    `NahlaTemplateLibrary does not expose forbidden order state "${forbidden}" as a card key`,
    !templateLibrarySource.includes(`'${forbidden}'`),
  )
}

assert(
  'NahlaTemplateLibrary documents Meta open-window scope comment',
  templateLibrarySource.includes('Open-window')
    && templateLibrarySource.includes('Lifecycle'),
)

assert(
  'NahlaTemplateLibrary links WhatsApp section to /templates',
  templateLibrarySource.includes('to="/templates"'),
)

assert(
  'isNavSimplified8Enabled still defaults OFF',
  flagsSource.includes('Default OFF'),
)

if (failed > 0) {
  console.error(`\n${failed} marketing nav P3.2 check(s) failed`)
  process.exit(1)
}

console.log('\nAll marketing nav P3.2 checks passed.')
