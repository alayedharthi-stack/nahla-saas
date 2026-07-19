/**
 * Focused regression checks for role-aware settings navigation.
 *
 * Run: npm run check:navigation-policy (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import {
  NAVIGATION_PATHS,
  resolveProfileSettingsPath,
} from '../src/lib/navigationPolicy.ts'

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

assert(
  'structured contacts route is the canonical sales-channels branch route',
  NAVIGATION_PATHS.structuredContacts === '/sales-channels/branches',
)
assert(
  'platform owner profile opens personal security settings',
  resolveProfileSettingsPath({ platformOwner: true, impersonating: false })
    === NAVIGATION_PATHS.securitySettings,
)
assert(
  'impersonating platform owner stays in merchant settings',
  resolveProfileSettingsPath({ platformOwner: true, impersonating: true })
    === NAVIGATION_PATHS.merchantSettings,
)
assert(
  'merchant profile opens merchant settings',
  resolveProfileSettingsPath({ platformOwner: false, impersonating: false })
    === NAVIGATION_PATHS.merchantSettings,
)

const bannerSource = source('../src/components/operations/StructuredContactsCutoverBanner.tsx')
assert(
  'structured contacts banner uses the canonical route constant',
  bannerSource.includes('NAVIGATION_PATHS.structuredContacts'),
)
assert(
  'structured contacts banner no longer references the broken route',
  !bannerSource.includes('/intelligence/operations-center'),
)

const sidebarSource = source('../src/components/layout/Sidebar.tsx')
const adminStart = sidebarSource.indexOf('const ADMIN_NAV_GROUPS')
const merchantStart = sidebarSource.indexOf('const MERCHANT_NAV_GROUPS')
const adminNavigation = sidebarSource.slice(adminStart, merchantStart)
assert(
  'platform owner sidebar omits merchant settings',
  !adminNavigation.includes("to: '/settings'")
    && !adminNavigation.includes('NAVIGATION_PATHS.merchantSettings'),
)
assert(
  'platform owner sidebar keeps personal security settings',
  adminNavigation.includes('NAVIGATION_PATHS.securitySettings'),
)

const headerSource = source('../src/components/layout/Header.tsx')
assert(
  'header delegates settings routing to the role-aware resolver',
  headerSource.includes('resolveProfileSettingsPath')
    && headerSource.includes('navigate(profileSettingsPath)'),
)

if (failed > 0) {
  console.error(`\n${failed} navigation policy check(s) failed`)
  process.exit(1)
}

console.log('\nAll navigation policy checks passed.')
