/**
 * P3.1 simplified navigation shell regression checks.
 *
 * Run: npm run check:nav-simplified-p31 (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import {
  LEGACY_MERCHANT_NAV_PATHS,
  SIMPLIFIED_NAV_DESTINATIONS,
  collectSimplifiedNavPaths,
} from '../src/lib/merchantNavSimplified.ts'

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

const simplifiedPaths = new Set(collectSimplifiedNavPaths())

assert(
  'simplified tree exposes exactly 27 merchant routes',
  simplifiedPaths.size === 27,
  `got ${simplifiedPaths.size}`,
)

for (const legacyPath of LEGACY_MERCHANT_NAV_PATHS) {
  assert(
    `legacy path "${legacyPath}" appears in simplified tree`,
    simplifiedPaths.has(legacyPath),
  )
}

assert(
  'simplified shell defines eight top-level destinations',
  SIMPLIFIED_NAV_DESTINATIONS.length === 8,
  `got ${SIMPLIFIED_NAV_DESTINATIONS.length}`,
)

const forbiddenPaths = ['/post_delivery', '/order-updates', '/order-update-templates']
for (const forbidden of forbiddenPaths) {
  assert(
    `forbidden P3.1 path "${forbidden}" is absent`,
    !simplifiedPaths.has(forbidden),
  )
}

const navDataSource = source('../src/lib/merchantNavSimplified.ts')
assert(
  'simplified nav data module has no lucide-react import (CI-safe)',
  !navDataSource.includes("from 'lucide-react'")
    && !navDataSource.includes('from "lucide-react"'),
)

const flagsSource = source('../src/lib/platformFeatureFlags.ts')
assert(
  'isNavSimplified8Enabled defaults via env (OFF when unset)',
  flagsSource.includes('VITE_PLATFORM_NAV_SIMPLIFIED_8')
    && flagsSource.includes('isTruthyEnv(import.meta.env.VITE_PLATFORM_NAV_SIMPLIFIED_8)'),
)
assert(
  'DEV-only localStorage override key is present',
  flagsSource.includes("nahla_platform_nav_simplified_8"),
)

const arSource = source('../src/i18n/ar.ts')
assert(
  'Arabic security nav label contains المصادقة',
  arSource.includes("security:         'الأمان والمصادقة'"),
)

const sidebarSource = source('../src/components/layout/Sidebar.tsx')
assert(
  'Sidebar wires simplified nav behind isNavSimplified8Enabled',
  sidebarSource.includes('isNavSimplified8Enabled')
    && sidebarSource.includes('SIMPLIFIED_NAV_DESTINATIONS'),
)
assert(
  'Sidebar keeps platform_nav_click telemetry on nav links',
  sidebarSource.includes("trackPlatformEvent('platform_nav_click'"),
)
assert(
  'admin sidebar remains on ADMIN_NAV_GROUPS',
  sidebarSource.includes('ADMIN_NAV_GROUPS'),
)

if (failed > 0) {
  console.error(`\n${failed} simplified nav P3.1 check(s) failed`)
  process.exit(1)
}

console.log('\nAll simplified nav P3.1 checks passed.')
