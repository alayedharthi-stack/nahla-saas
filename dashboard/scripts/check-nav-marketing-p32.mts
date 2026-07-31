/**
 * P3.2 Marketing Hub + Templates IA regression checks.
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
const templatesHubSource = source('../src/pages/TemplatesHub.tsx')
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
  'simplified tree includes /marketing/templates store templates route',
  simplifiedPaths.has('/marketing/templates'),
)
assert(
  'simplified tree opens conversations at /conversations (inbox hub retired)',
  simplifiedPaths.has('/conversations') && !simplifiedPaths.has('/inbox'),
)
assert(
  'simplified tree includes products hub route',
  simplifiedPaths.has('/products'),
)
assert(
  'simplified tree opens orders at /orders (orders-hub retired from rail)',
  simplifiedPaths.has('/orders') && !simplifiedPaths.has('/orders-hub'),
)
assert(
  'simplified tree includes automation hub route',
  simplifiedPaths.has('/automation'),
)
assert(
  'simplified tree includes templates hub route',
  simplifiedPaths.has('/templates-hub'),
)
assert(
  'simplified tree includes channels hub route',
  simplifiedPaths.has('/channels'),
)
assert(
  'simplified tree includes settings hub route',
  simplifiedPaths.has('/settings-hub'),
)
assert(
  'simplified tree exposes expected route count after daily-use correction',
  simplifiedPaths.size >= 34 && simplifiedPaths.size <= 38,
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
  'App.tsx redirects inbox hub to conversations',
  appSource.includes('path="inbox"')
    && appSource.includes('to="/conversations"')
    && appSource.includes('RedirectPreserveSearch'),
)
assert(
  'App.tsx registers products hub route',
  appSource.includes('path="products"') && appSource.includes('ProductsHub'),
)
assert(
  'App.tsx redirects orders-hub to orders',
  appSource.includes('path="orders-hub"')
    && appSource.includes('to="/orders"'),
)
assert(
  'App.tsx registers automation hub route',
  appSource.includes('path="automation"') && appSource.includes('AutomationHub'),
)
assert(
  'App.tsx registers templates hub route',
  appSource.includes('path="templates-hub"') && appSource.includes('TemplatesHub'),
)
assert(
  'App.tsx registers channels hub route',
  appSource.includes('path="channels"') && appSource.includes('ChannelsHub'),
)
assert(
  'App.tsx registers settings hub route',
  appSource.includes('path="settings-hub"') && appSource.includes('SettingsHub'),
)
assert(
  'App.tsx keeps /templates route',
  appSource.includes('path="templates"') && appSource.includes('Templates'),
)
assert(
  'App.tsx keeps /widgets route',
  appSource.includes('path="widgets"') && appSource.includes('MerchantWidgets'),
)

assert(
  'TemplatesHub exposes exactly two hub cards',
  (templatesHubSource.match(/to:\s*'\/[^']+'/g) ?? []).length === 2
    && templatesHubSource.includes("to: '/templates'")
    && templatesHubSource.includes("to: '/marketing/templates'")
    && !templatesHubSource.includes('nahlaLibrary'),
)

assert(
  'Store templates page keeps #ecommerce anchor for legacy links',
  templateLibrarySource.includes('id="ecommerce"'),
)
assert(
  'Store templates page anchors order-updates section',
  templateLibrarySource.includes('id="order-updates"'),
)
assert(
  'Store templates page does not call nahlaLibrary for store cards',
  !templateLibrarySource.includes('templatesApi.nahlaLibrary')
    && !templateLibrarySource.includes("from '../api/templates'"),
)
assert(
  'Store templates page loads order updates via ORDER_UPDATE_SERVICE_KEYS',
  templateLibrarySource.includes('ORDER_UPDATE_SERVICE_KEYS')
    && templateLibrarySource.includes('orderUpdatesApi.getService'),
)
assert(
  'Store templates page does not expose order-updates as a primary hub family key',
  !templateLibrarySource.includes('ORDER_UPDATE_TEMPLATE_KEYS'),
)
assert(
  'Store templates page links ops to settings order_updates',
  templateLibrarySource.includes('/settings?tab=order_updates'),
)
assert(
  'Store templates page documents Meta open-window scope comment',
  templateLibrarySource.includes('Open-window')
    && templateLibrarySource.includes('Lifecycle'),
)
assert(
  'Active-path helper prefers longer destination matches',
  navDataSource.includes('destinationMatchLength')
    && navDataSource.includes('longer / more specific'),
)
assert(
  'Sidebar simplified destinations use Link with owned active matching',
  source('../src/components/layout/Sidebar.tsx').includes('isPathInSimplifiedDestination')
    && source('../src/components/layout/Sidebar.tsx').includes("aria-current={isActive ? 'page' : undefined}"),
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
