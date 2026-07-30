/**
 * Regression checks for the shared platform telemetry module.
 *
 * Run: npm run check:platform-telemetry (from dashboard/)
 */
import { existsSync, readFileSync } from 'node:fs'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const telemetryPath = new URL('../src/lib/platformTelemetry.ts', import.meta.url)

assert(
  'platformTelemetry.ts exists',
  existsSync(telemetryPath),
)

const source = existsSync(telemetryPath)
  ? readFileSync(telemetryPath, 'utf8')
  : ''

const REQUIRED_EVENTS = [
  'salla_redirect_clicked',
  'billing_payment_success_landed',
  'salla_returned_without_subscription',
  'platform_page_view',
  'platform_nav_click',
  'overview_loaded',
  'overview_period_changed',
  'overview_cta_clicked',
] as const

for (const eventName of REQUIRED_EVENTS) {
  assert(
    `event "${eventName}" is registered in PLATFORM_TELEMETRY_EVENTS`,
    source.includes(`${eventName}: '${eventName}'`)
      || source.includes(`${eventName}: "${eventName}"`),
  )
}

assert(
  'trackPlatformEvent is exported',
  source.includes('export function trackPlatformEvent'),
)

assert(
  'PLATFORM_TELEMETRY_EVENTS registry is exported',
  source.includes('export const PLATFORM_TELEMETRY_EVENTS'),
)

assert(
  'PlatformTelemetryPayload type is exported',
  source.includes('export type PlatformTelemetryPayload'),
)

const layoutSource = readFileSync(
  new URL('../src/components/layout/Layout.tsx', import.meta.url),
  'utf8',
)
const sidebarSource = readFileSync(
  new URL('../src/components/layout/Sidebar.tsx', import.meta.url),
  'utf8',
)
const overviewSource = readFileSync(
  new URL('../src/pages/Overview.tsx', import.meta.url),
  'utf8',
)

assert(
  'Layout wires platform_page_view',
  layoutSource.includes("trackPlatformEvent('platform_page_view'")
    || layoutSource.includes('trackPlatformEvent("platform_page_view"'),
)

assert(
  'Sidebar wires platform_nav_click',
  sidebarSource.includes("trackPlatformEvent('platform_nav_click'")
    || sidebarSource.includes('trackPlatformEvent("platform_nav_click"'),
)

assert(
  'Sidebar uses stable nav_group keys',
  sidebarSource.includes("groupKey: 'main'")
    && sidebarSource.includes("groupKey: 'admin_platform'"),
)

assert(
  'Overview wires overview_loaded',
  overviewSource.includes("trackPlatformEvent('overview_loaded'")
    || overviewSource.includes('trackPlatformEvent("overview_loaded"'),
)

assert(
  'Overview wires overview_period_changed',
  overviewSource.includes("trackPlatformEvent('overview_period_changed'")
    || overviewSource.includes('trackPlatformEvent("overview_period_changed"'),
)

assert(
  'Overview wires overview_cta_clicked',
  overviewSource.includes("trackPlatformEvent('overview_cta_clicked'")
    || overviewSource.includes('trackPlatformEvent("overview_cta_clicked"'),
)

if (failed > 0) {
  console.error(`\n${failed} platform telemetry check(s) failed`)
  process.exit(1)
}

console.log('\nAll platform telemetry checks passed.')
