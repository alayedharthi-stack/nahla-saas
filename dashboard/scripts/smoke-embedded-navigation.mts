/**
 * Regression tests for embedded external navigation fallback chain (PR #874 + live fallback repair).
 *
 * Run: npx --yes tsx@4 scripts/smoke-embedded-navigation.mts
 */
import {
  EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER,
  EMBEDDED_NAV_WATCHDOG_MS,
  invokeEmbeddedSdkPageCall,
  navigateGesturePopup,
  openUserGestureFallbackWindow,
  redactExternalNavUrlForLog,
  runEmbeddedExternalNavChain,
  type EmbeddedExternalNavDeps,
  type EmbeddedPageApi,
} from '../src/lib/embeddedNavigation.ts'

const START_URL = 'https://api.nahlah.ai/api/salla/oauth/start?embedded_reconcile=1&token=secret'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

function makeDeps(overrides: Partial<EmbeddedExternalNavDeps>): EmbeddedExternalNavDeps {
  return {
    getPageApi: () => null,
    windowOpen: () => null,
    setWindowTopLocation: () => false,
    setWindowLocation: () => false,
    waitForNavigationEvidence: async () => false,
    watchdogMs: 1,
    ...overrides,
  }
}

function makeGesturePopup() {
  let href = 'about:blank'
  return {
    closed: false,
    location: {
      get href() {
        return href
      },
      set href(value: string) {
        href = value
      },
    },
  } as unknown as Window
}

// F — URL redaction
const redacted = redactExternalNavUrlForLog(START_URL)
assert('F URL redaction hides values', !redacted.includes('secret') && redacted.includes('embedded_reconcile'))
assert('I destination path helper omits query', new URL(START_URL).pathname === '/api/salla/oauth/start')
assert('fallback order prefers redirect then navigate', EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[0] === 'sdk_page_redirect'
  && EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[1] === 'sdk_page_navigate')
assert('H OAuth path unchanged', new URL(START_URL).pathname === '/api/salla/oauth/start')
assert('watchdog default is bounded', EMBEDDED_NAV_WATCHDOG_MS > 0 && EMBEDDED_NAV_WATCHDOG_MS <= 1000)

// A — SDK redirect with navigation evidence → stop fallback
{
  let redirectCalls = 0
  const page: EmbeddedPageApi = {
    redirect(url) {
      redirectCalls++
      if (url !== START_URL) throw new Error('unexpected url')
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    waitForNavigationEvidence: async () => true,
  }))
  assert('A redirect with evidence selects redirect', result.method === 'sdk_page_redirect' && result.handedOff)
  assert('A redirect invoked once', redirectCalls === 1)
  assert('A records navigation evidence', result.navigationEvidence === true)
}

// B — redirect fire-and-forget with no navigation evidence → must NOT suppress fallback
{
  let redirectCalls = 0
  let openCalls = 0
  const page: EmbeddedPageApi = {
    redirect() {
      redirectCalls++
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    waitForNavigationEvidence: async () => false,
    windowOpen(url) {
      openCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  assert('B silent redirect does not stop fallback', result.method === 'window_open' && result.handedOff)
  assert('B redirect was attempted', redirectCalls === 1)
  assert('B window.open invoked after no evidence', openCalls === 1)
}

// C — SDK unavailable → safe fallback
{
  let openCalls = 0
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => null,
    windowOpen(url) {
      openCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  assert('C SDK unavailable falls back to window.open', result.method === 'window_open' && result.handedOff)
  assert('C window.open invoked without SDK', openCalls === 1)
}

// D — redirect throws → navigate attempted
{
  let navigateCalls = 0
  const page: EmbeddedPageApi = {
    redirect() {
      throw new Error('redirect blocked')
    },
    navigate(url) {
      navigateCalls++
      if (url !== START_URL) throw new Error('unexpected url')
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    waitForNavigationEvidence: async () => true,
  }))
  assert('D redirect throw falls through to navigate', result.method === 'sdk_page_navigate' && result.handedOff)
  assert('D navigate invoked after redirect throw', navigateCalls === 1)
}

// D2 — redirect rejects → navigate attempted
{
  let navigateCalls = 0
  const page: EmbeddedPageApi = {
    redirect() {
      return Promise.reject(new Error('redirect rejected'))
    },
    navigate() {
      navigateCalls++
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    waitForNavigationEvidence: async () => true,
  }))
  assert('D redirect reject falls through to navigate', result.method === 'sdk_page_navigate' && result.handedOff)
  assert('D navigate invoked after redirect reject', navigateCalls === 1)
}

// E — navigate rejection → fallback
{
  let openCalls = 0
  const page: EmbeddedPageApi = {
    redirect() {
      return Promise.reject(new Error('no redirect'))
    },
    navigate() {
      throw new Error('no navigate')
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    windowOpen(url) {
      openCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  assert('E navigate rejection reaches window.open', result.method === 'window_open' && result.handedOff)
  assert('E window.open invoked', openCalls === 1)
}

// F — user-gesture popup fallback remains viable
{
  const popup = makeGesturePopup()
  const page: EmbeddedPageApi = {
    redirect() {},
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => false,
    windowOpen: () => {
      throw new Error('windowOpen should not run when gesture popup succeeds')
    },
  }))
  assert('F gesture popup fallback succeeds', result.method === 'window_open' && result.handedOff)
  assert('F gesture popup navigated to OAuth URL', (popup.location as Location).href === START_URL)
}

// G — all methods fail → blocked (no silent no-op)
{
  const page: EmbeddedPageApi = {
    redirect() {},
    navigate() {},
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    waitForNavigationEvidence: async () => false,
    windowOpen: () => null,
    setWindowTopLocation: () => false,
    setWindowLocation: () => false,
  }))
  assert('G all methods fail → blocked', result.method === 'blocked' && result.handedOff === false)
}

// invokeEmbeddedSdkPageCall unit checks
assert('invoke returns false for missing fn', await invokeEmbeddedSdkPageCall(undefined) === false)
assert('invoke returns true for sync success', await invokeEmbeddedSdkPageCall(() => undefined) === true)
assert('invoke returns false for sync throw', await invokeEmbeddedSdkPageCall(() => { throw new Error('x') }) === false)
assert('invoke returns false for async reject', await invokeEmbeddedSdkPageCall(() => Promise.reject(new Error('x'))) === false)

// gesture helpers
{
  const popup = makeGesturePopup()
  assert('gesture popup navigation helper works', navigateGesturePopup(popup, START_URL))
  assert('openUserGestureFallbackWindow returns null without window', openUserGestureFallbackWindow() == null)
}

if (failed > 0) {
  console.error(`\n${failed} navigation regression case(s) failed`)
  process.exit(1)
}
console.log('\nAll embedded navigation regression cases passed.')
