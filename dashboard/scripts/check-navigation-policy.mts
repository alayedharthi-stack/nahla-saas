/**
 * Focused regression checks for trusted dashboard navigation actions.
 *
 * Run: npm run check:navigation-policy (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import {
  INTEGRATION_MANAGEMENT_PATHS,
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
  'merchant settings entry is the modern settings hub',
  NAVIGATION_PATHS.merchantSettings === '/settings-hub',
)
assert(
  'platform owner profile opens personal security settings',
  resolveProfileSettingsPath({ platformOwner: true, impersonating: false })
    === NAVIGATION_PATHS.securitySettings,
)
assert(
  'impersonating platform owner stays in merchant settings hub',
  resolveProfileSettingsPath({ platformOwner: true, impersonating: true })
    === NAVIGATION_PATHS.merchantSettings,
)
assert(
  'merchant profile opens merchant settings hub',
  resolveProfileSettingsPath({ platformOwner: false, impersonating: false })
    === NAVIGATION_PATHS.merchantSettings,
)
assert(
  'store integrations use the canonical management workspace',
  INTEGRATION_MANAGEMENT_PATHS.store === '/store-integration',
)
assert(
  'WhatsApp uses its canonical connection workspace',
  INTEGRATION_MANAGEMENT_PATHS.whatsapp === '/whatsapp-connect',
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
const merchantNavigation = sidebarSource.slice(merchantStart)
assert(
  'platform owner sidebar omits merchant settings',
  !adminNavigation.includes("to: '/settings'")
    && !adminNavigation.includes("to: '/settings-hub'")
    && !adminNavigation.includes('NAVIGATION_PATHS.merchantSettings'),
)
assert(
  'platform owner sidebar keeps personal security settings',
  adminNavigation.includes('NAVIGATION_PATHS.securitySettings'),
)
assert(
  'legacy merchant sidebar settings item uses the hub constant',
  merchantNavigation.includes('NAVIGATION_PATHS.merchantSettings'),
)
assert(
  'legacy merchant sidebar does not hardcode bare /settings entry',
  !merchantNavigation.includes("to: '/settings'"),
)

const headerSource = source('../src/components/layout/Header.tsx')
assert(
  'header delegates settings routing to the role-aware resolver',
  headerSource.includes('resolveProfileSettingsPath')
    && headerSource.includes('navigate(profileSettingsPath)'),
)

const appSource = source('../src/App.tsx')
assert(
  'bare /settings redirects to the modern settings hub',
  appSource.includes('LegacySettingsEntryRedirect')
    && appSource.includes('Navigate to="/settings-hub"'),
)
assert(
  'settings hub remains the registered modern entry route',
  appSource.includes('path="settings-hub"') && appSource.includes('SettingsHub'),
)
assert(
  'security settings route remains available for authorized users',
  appSource.includes('path="settings/security"')
    && appSource.includes('SecuritySettings'),
)

const integrationsSource = source('../src/pages/Integrations.tsx')
const zidCardStart = integrationsSource.indexOf('name="Zid"')
const whatsappCardStart = integrationsSource.indexOf('name="WhatsApp Business API"')
const zidCardSource = integrationsSource.slice(zidCardStart, whatsappCardStart)
assert(
  'connected integration actions open real management workspaces',
  integrationsSource.includes('onManage={() => navigate(INTEGRATION_MANAGEMENT_PATHS.store)}')
    && integrationsSource.includes('onManage={() => navigate(INTEGRATION_MANAGEMENT_PATHS.whatsapp)}'),
)
assert(
  'connected Zid management remains an explicit external dashboard link',
  zidCardStart >= 0
    && whatsappCardStart > zidCardStart
    && zidCardSource.includes('externalHref="https://web.zid.sa/dashboard"')
    && !zidCardSource.includes('onManage='),
)
assert(
  'integration cards do not simulate synchronization with a local timer',
  !integrationsSource.includes('setSyncing')
    && !integrationsSource.includes('handleSync'),
)
assert(
  'WhatsApp management is not presented as an immediate disconnect action',
  !integrationsSource.includes('onDisconnect={() => navigate'),
)

if (failed > 0) {
  console.error(`\n${failed} navigation policy check(s) failed`)
  process.exit(1)
}

console.log('\nAll navigation policy checks passed.')
