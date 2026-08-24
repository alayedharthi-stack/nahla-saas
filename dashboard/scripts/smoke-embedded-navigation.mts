/**
 * Regression tests for embedded external navigation fallback chain (PR #874 Layer C).
 *
 * Run: npx --yes tsx@4 scripts/smoke-embedded-navigation.mts
 */
import {
  EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER,
  invokeEmbeddedSdkPageCall,
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
    ...overrides,
  }
}

// F — URL redaction
const redacted = redactExternalNavUrlForLog(START_URL)
assert('F URL redaction hides values', !redacted.includes('secret') && redacted.includes('embedded_reconcile'))
assert('fallback order prefers redirect then navigate', EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[0] === 'sdk_page_redirect'
  && EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[1] === 'sdk_page_navigate')

// A — redirect success
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
  }))
  assert('A redirect success selects redirect', result.method === 'sdk_page_redirect' && result.handedOff)
  assert('A redirect invoked once', redirectCalls === 1)
}

// B — redirect throws → navigate attempted
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
  }))
  assert('B redirect throw falls through to navigate', result.method === 'sdk_page_navigate' && result.handedOff)
  assert('B navigate invoked after redirect throw', navigateCalls === 1)
}

// B2 — redirect rejects → navigate attempted
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
  }))
  assert('B redirect reject falls through to navigate', result.method === 'sdk_page_navigate' && result.handedOff)
  assert('B navigate invoked after redirect reject', navigateCalls === 1)
}

// C — redirect unavailable + navigate succeeds
{
  const page: EmbeddedPageApi = {
    navigate(url) {
      if (url !== START_URL) throw new Error('unexpected url')
    },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
  }))
  assert('C navigate succeeds when redirect missing', result.method === 'sdk_page_navigate' && result.handedOff)
}

// D — redirect + navigate fail → window.open
{
  let openCalls = 0
  const page: EmbeddedPageApi = {
    redirect() { throw new Error('no redirect') },
    navigate() { throw new Error('no navigate') },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    windowOpen(url) {
      openCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  assert('D window.open fallback after SDK failures', result.method === 'window_open' && result.handedOff)
  assert('D window.open invoked', openCalls === 1)
}

// E — all methods fail → blocked
{
  const page: EmbeddedPageApi = {
    redirect() { throw new Error('no redirect') },
    navigate() { throw new Error('no navigate') },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    windowOpen: () => null,
    setWindowTopLocation: () => false,
    setWindowLocation: () => false,
  }))
  assert('E all methods fail → blocked', result.method === 'blocked' && result.handedOff === false)
}

// invokeEmbeddedSdkPageCall unit checks
assert('invoke returns false for missing fn', await invokeEmbeddedSdkPageCall(undefined) === false)
assert('invoke returns true for sync success', await invokeEmbeddedSdkPageCall(() => undefined) === true)
assert('invoke returns false for sync throw', await invokeEmbeddedSdkPageCall(() => { throw new Error('x') }) === false)
assert('invoke returns false for async reject', await invokeEmbeddedSdkPageCall(() => Promise.reject(new Error('x'))) === false)

if (failed > 0) {
  console.error(`\n${failed} navigation regression case(s) failed`)
  process.exit(1)
}
console.log('\nAll embedded navigation regression cases passed.')
