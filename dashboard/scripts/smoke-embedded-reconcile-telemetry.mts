/**
 * Regression checks for embedded reconcile CTA telemetry transport.
 *
 * Run: npm run check:embedded-reconcile-telemetry (from dashboard/)
 */
import { readFileSync } from 'node:fs'

const OAUTH_URL = 'https://api.nahlah.ai/api/salla/oauth/start?embedded_reconcile=1&token=secret'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

const telemetrySource = readFileSync(
  new URL('../src/lib/embeddedReconcileTelemetry.ts', import.meta.url),
  'utf8',
)
const sallaEmbeddedSource = readFileSync(
  new URL('../src/pages/SallaEmbedded.tsx', import.meta.url),
  'utf8',
)
const emitBlock = telemetrySource.slice(telemetrySource.indexOf('export function emitSallaReconcileTelemetry'))

assert('B endpoint uses getApiBase()', telemetrySource.includes("import { getApiBase } from '../auth'"))
assert(
  'B endpoint path built from getApiBase()',
  telemetrySource.includes('`${getApiBase()}/api/salla/embedded/reconcile-telemetry`'),
)
assert('A fetch is primary transport in emit()', emitBlock.indexOf('fetch(') < emitBlock.indexOf('trySendBeaconFallback'))
assert('A keepalive=true on fetch', telemetrySource.includes('keepalive: true'))
assert('A credentials omit on fetch', telemetrySource.includes("credentials: 'omit'"))
assert(
  'F sendBeacon return is not treated as receipt',
  !telemetrySource.includes('if (navigator.sendBeacon(endpoint, blob))'),
)
assert(
  'E sendBeacon remains fallback only',
  telemetrySource.includes('trySendBeaconFallback'),
)
assert(
  'H no await before navigation in CTA handler',
  !/openOauthReconcile[\s\S]{0,800}await emitSallaReconcileTelemetry/.test(sallaEmbeddedSource),
)
assert(
  'H gesture popup opens before telemetry emit',
  sallaEmbeddedSource.indexOf('openUserGestureFallbackWindow()')
    < sallaEmbeddedSource.indexOf("event: 'SALLA_RECONCILE_CTA_CLICK'"),
)


const storage = new Map<string, string>()
const localStorageMock = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => { storage.set(key, value) },
  removeItem: (key: string) => { storage.delete(key) },
  clear: () => { storage.clear() },
  key: () => null,
  length: 0,
} as Storage
localStorageMock.setItem('nahla_api_base_override', 'https://api.nahlah.ai')
globalThis.localStorage = localStorageMock
;(globalThis as typeof globalThis & { window?: Window }).window = globalThis as typeof globalThis & Window
globalThis.window.localStorage = localStorageMock

const { emitSallaReconcileTelemetry, extractDestinationPath } = await import('../src/lib/embeddedReconcileTelemetry.ts')
const { getApiBase } = await import('../src/auth.ts')

const destinationPath = extractDestinationPath(OAUTH_URL)
assert('D destination path omits query values', destinationPath === '/api/salla/oauth/start')
assert('D destination path omits token', !destinationPath.includes('secret'))

const originalFetch = globalThis.fetch
const nav = globalThis.navigator as Navigator
const originalSendBeacon = nav.sendBeacon?.bind(nav)

let fetchCalls = 0
let beaconCalls = 0
let lastFetchInit: RequestInit | undefined
let lastBeaconEndpoint = ''

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  fetchCalls++
  lastFetchInit = init
  const url = typeof input === 'string' ? input : input.toString()
  assert('B fetch targets getApiBase host', url.startsWith(`${getApiBase()}/api/salla/embedded/reconcile-telemetry`))
  return new Response('{}', { status: 200 })
}) as typeof fetch

Object.defineProperty(nav, 'sendBeacon', {
  configurable: true,
  writable: true,
  value: (endpoint: string, _data?: BodyInit | null) => {
    beaconCalls++
    lastBeaconEndpoint = endpoint
    return true
  },
})

emitSallaReconcileTelemetry({
  event: 'SALLA_RECONCILE_CTA_CLICK',
  correlation_id: 'src_test_fetch_first',
  destination_path: destinationPath,
  ts: 1,
})

await sleep(0)

assert('A fetch attempted first on happy path', fetchCalls === 1)
assert('A fetch uses POST + JSON body', lastFetchInit?.method === 'POST' && typeof lastFetchInit?.body === 'string')
assert(
  'D serialized payload has no token/query values',
  typeof lastFetchInit?.body === 'string'
    && !String(lastFetchInit.body).includes('secret')
    && !String(lastFetchInit.body).includes('embedded_reconcile=1'),
)
assert('E beacon not used when fetch succeeds', beaconCalls === 0)

fetchCalls = 0
beaconCalls = 0
globalThis.fetch = (async () => {
  fetchCalls++
  throw new Error('fetch failed')
}) as typeof fetch

emitSallaReconcileTelemetry({
  event: 'SALLA_RECONCILE_NAV_ATTEMPT',
  correlation_id: 'src_test_beacon_fallback',
  destination_path: destinationPath,
  attempted_method: 'window_open',
  fallback_stage: 'gesture_popup',
  ts: 2,
})

await sleep(0)

assert('E fetch rejection triggers beacon fallback', fetchCalls === 1 && beaconCalls === 1)
assert('F beacon true does not skip fetch path', fetchCalls === 1)
assert(
  'E fallback beacon uses same endpoint',
  lastBeaconEndpoint === `${getApiBase()}/api/salla/embedded/reconcile-telemetry`,
)

let threw = false
globalThis.fetch = (() => {
  throw new Error('sync fetch throw')
}) as typeof fetch
beaconCalls = 0

try {
  emitSallaReconcileTelemetry({
    event: 'SALLA_EMBEDDED_SDK_STATE',
    correlation_id: 'src_test_no_throw',
    sdk_loaded: true,
    sdk_initialized: false,
    ts: 3,
  })
} catch {
  threw = true
}

assert('G telemetry failure never throws into caller', !threw)
assert('G sync fetch throw still invokes beacon fallback', beaconCalls === 1)

globalThis.fetch = originalFetch
if (originalSendBeacon) {
  Object.defineProperty(nav, 'sendBeacon', {
    configurable: true,
    writable: true,
    value: originalSendBeacon,
  })
}

if (failed > 0) {
  console.error(`\n${failed} embedded reconcile telemetry check(s) failed`)
  process.exit(1)
}

console.log('\nAll embedded reconcile telemetry checks passed.')
