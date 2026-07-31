/**
 * P3.1 simplified navigation shell regression checks.
 *
 * Run: npm run check:nav-simplified-p31 (from dashboard/)
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

/** Extract quoted strings from a `export const NAME = [...] as const` block. */
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

/** Collect `to: '/path'` route strings from SIMPLIFIED_NAV_DESTINATIONS. */
function extractSimplifiedNavPaths(tsSource: string): string[] {
  const marker = 'export const SIMPLIFIED_NAV_DESTINATIONS'
  const start = tsSource.indexOf(marker)
  if (start < 0) return []
  const end = tsSource.indexOf('export function collectSimplifiedNavPaths', start)
  const block = tsSource.slice(start, end > start ? end : undefined)
  return [...block.matchAll(/to:\s*'([^']+)'/g)].map(m => m[1])
}

const navDataSource = source('../src/lib/merchantNavSimplified.ts')
const flagsSource = source('../src/lib/platformFeatureFlags.ts')
const sidebarSource = source('../src/components/layout/Sidebar.tsx')
const arSource = source('../src/i18n/ar.ts')

assert(
  'simplified nav data module has no lucide-react import (CI-safe)',
  !navDataSource.includes("from 'lucide-react'")
    && !navDataSource.includes('from "lucide-react"'),
)

const legacyPaths = extractConstStringArray(navDataSource, 'LEGACY_MERCHANT_NAV_PATHS')
assert(
  'LEGACY_MERCHANT_NAV_PATHS defines exactly 27 routes',
  legacyPaths.length === 27,
  `got ${legacyPaths.length}`,
)

const simplifiedPaths = new Set(extractSimplifiedNavPaths(navDataSource))
assert(
  'simplified tree exposes exactly 27 merchant routes',
  simplifiedPaths.size === 27,
  `got ${simplifiedPaths.size}`,
)

for (const legacyPath of legacyPaths) {
  assert(
    `legacy path "${legacyPath}" is declared in merchantNavSimplified.ts`,
    navDataSource.includes(`'${legacyPath}'`),
  )
  assert(
    `legacy path "${legacyPath}" appears in simplified tree`,
    simplifiedPaths.has(legacyPath),
  )
}

const destBlockStart = navDataSource.indexOf('export const SIMPLIFIED_NAV_DESTINATIONS')
const destBlockEnd = navDataSource.indexOf('export function collectSimplifiedNavPaths', destBlockStart)
const destBlock = navDataSource.slice(destBlockStart, destBlockEnd > destBlockStart ? destBlockEnd : undefined)
const topLevelDestCount = (destBlock.match(/^\s+destKey:/gm) ?? []).length
assert(
  'simplified shell defines eight top-level destinations',
  topLevelDestCount === 8,
  `got ${topLevelDestCount}`,
)

const forbiddenPaths = [
  '/post_delivery',
  '/order-updates',
  '/order-update-templates',
  '/delivered',
  '/out_for_delivery',
  '/cancelled',
  '/failed_delivery',
  '/reorder',
]
const p31Sources = [
  { label: 'merchantNavSimplified.ts', text: navDataSource },
  { label: 'Sidebar.tsx', text: sidebarSource },
]
for (const forbidden of forbiddenPaths) {
  assert(
    `forbidden P3.1 path "${forbidden}" is absent from simplified nav`,
    !simplifiedPaths.has(forbidden),
  )
  for (const { label, text } of p31Sources) {
    assert(
      `forbidden P3.1 route "${forbidden}" absent in ${label}`,
      !text.includes(`'${forbidden}'`) && !text.includes(`"${forbidden}"`),
    )
  }
}

assert(
  'isNavSimplified8Enabled is exported and defaults OFF',
  flagsSource.includes('export function isNavSimplified8Enabled')
    && flagsSource.includes('Default OFF')
    && flagsSource.includes('VITE_PLATFORM_NAV_SIMPLIFIED_8'),
)
assert(
  'isNavSimplified8Enabled defaults via env (OFF when unset)',
  flagsSource.includes('VITE_PLATFORM_NAV_SIMPLIFIED_8')
    && flagsSource.includes('isTruthyEnv(import.meta.env.VITE_PLATFORM_NAV_SIMPLIFIED_8)'),
)
assert(
  'DEV-only localStorage override key is present',
  flagsSource.includes('nahla_platform_nav_simplified_8'),
)

assert(
  'Arabic security nav label contains المصادقة',
  arSource.includes("security:         'الأمان والمصادقة'"),
)

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
