/**
 * Regression: CTA handler must not pass legacy API_BASE wrapper into
 * resolveOauthReconcileStartUrl (apiBase.replace requires a primitive string).
 *
 * Run: npm run check:embedded-reconcile-cta-apibase
 */
import { readFileSync } from 'node:fs'
import { API_BASE } from '../src/api/client.ts'
import {
  resolveOauthReconcileStartUrl,
  SALLA_EMBEDDED_OAUTH_START_PATH,
} from '../src/lib/embeddedLogin.ts'

const TEST_BASE = 'https://api.nahlah.ai'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

const sallaEmbeddedSource = readFileSync(
  new URL('../src/pages/SallaEmbedded.tsx', import.meta.url),
  'utf8',
)

assert(
  'C CTA call site uses getApiBase(), not API_BASE wrapper',
  sallaEmbeddedSource.includes('resolveOauthReconcileStartUrl(getApiBase(), storeLinkPayload)'),
)
assert(
  'C CTA call site does not pass API_BASE into resolveOauthReconcileStartUrl',
  !sallaEmbeddedSource.includes('resolveOauthReconcileStartUrl(API_BASE, storeLinkPayload)'),
)

let legacyWrapperThrew = false
try {
  resolveOauthReconcileStartUrl(API_BASE as unknown as string, null)
} catch (err) {
  legacyWrapperThrew = err instanceof TypeError
}
assert('C legacy API_BASE wrapper throws TypeError on .replace', legacyWrapperThrew)

const storage = new Map<string, string>()
const localStorageMock = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => { storage.set(key, value) },
  removeItem: (key: string) => { storage.delete(key) },
  clear: () => { storage.clear() },
  key: () => null,
  length: 0,
} as Storage
localStorageMock.setItem('nahla_api_base_override', TEST_BASE)
globalThis.localStorage = localStorageMock
;(globalThis as typeof globalThis & { window?: Window }).window = globalThis as typeof globalThis & Window
globalThis.window.localStorage = localStorageMock

const { getApiBase } = await import('../src/auth.ts')

assert('A getApiBase returns primitive string', typeof getApiBase() === 'string')

const startUrl = resolveOauthReconcileStartUrl(getApiBase(), null)
assert(
  'B reconcile URL uses current getApiBase() host',
  startUrl === `${TEST_BASE}${SALLA_EMBEDDED_OAUTH_START_PATH}`,
)
assert(
  'B reconcile URL includes embedded_reconcile flag',
  startUrl.includes('/api/salla/oauth/start?embedded_reconcile=1'),
)

const openBlock = sallaEmbeddedSource.slice(
  sallaEmbeddedSource.indexOf('const openOauthReconcile = useCallback'),
  sallaEmbeddedSource.indexOf('}, [storeLinkPayload])', sallaEmbeddedSource.indexOf('const openOauthReconcile')),
)
assert(
  'D CTA handler reaches telemetry emit after URL construction',
  openBlock.indexOf('resolveOauthReconcileStartUrl(getApiBase()') < openBlock.indexOf('emitSallaReconcileTelemetry'),
)
assert(
  'F CTA handler reaches navigation after URL construction',
  openBlock.indexOf('resolveOauthReconcileStartUrl(getApiBase()') < openBlock.indexOf('navigateEmbeddedExternalUrl'),
)
assert(
  'E telemetry emit remains in handler (reachable once URL builds)',
  openBlock.includes("event: 'SALLA_RECONCILE_CTA_CLICK'"),
)

if (failed > 0) {
  console.error(`\n${failed} embedded reconcile CTA API base check(s) failed`)
  process.exit(1)
}

console.log('\nAll embedded reconcile CTA API base checks passed.')
