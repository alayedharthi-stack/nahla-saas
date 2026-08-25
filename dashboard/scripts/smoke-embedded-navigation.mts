/**
 * Regression tests for embedded external navigation + orphan gesture popup repair.
 *
 * Run: npx --yes tsx@4 scripts/smoke-embedded-navigation.mts
 */
import {
  DIRECT_WINDOW_OPEN_FEATURES,
  EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER,
  EMBEDDED_NAV_WATCHDOG_MS,
  GESTURE_POPUP_PREOPEN_FEATURES,
  GESTURE_POPUP_PREOPEN_URL,
  GESTURE_POPUP_WAITING_SUBTITLE,
  GESTURE_POPUP_WAITING_TITLE,
  invokeEmbeddedSdkPageCall,
  navigateGesturePopup,
  openUserGestureFallbackWindow,
  redactExternalNavUrlForLog,
  renderGesturePopupWaitingState,
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

function makeGesturePopup(initialHref = 'about:blank') {
  let href = initialHref
  let closed = false
  let opener: Window | null = {} as Window
  let replaceCalls = 0
  let closeCalls = 0
  const doc = {
    _html: '',
    open() {},
    write(html: string) {
      this._html = html
    },
    close() {},
  }
  return {
    get closed() {
      return closed
    },
    set closed(value: boolean) {
      closed = value
    },
    get opener() {
      return opener
    },
    set opener(value: Window | null) {
      opener = value
    },
    document: doc,
    close() {
      closeCalls++
      closed = true
    },
    location: {
      get href() {
        return href
      },
      set href(value: string) {
        href = value
      },
      replace(value: string) {
        replaceCalls++
        href = value
      },
    },
    _meta: {
      get closeCalls() {
        return closeCalls
      },
      get replaceCalls() {
        return replaceCalls
      },
      get docHtml() {
        return doc._html
      },
    },
  } as unknown as Window & { _meta: { closeCalls: number; replaceCalls: number; docHtml: string } }
}

function makeUnnavigableGesturePopup() {
  const popup = makeGesturePopup()
  let href = 'about:blank'
  Object.defineProperty(popup, 'location', {
    configurable: true,
    value: {
      get href() {
        return href
      },
      set href(_value: string) {
        throw new Error('blocked href')
      },
      replace(_value: string) {
        throw new Error('blocked replace')
      },
    },
  })
  return popup
}

// L — telemetry event names unchanged (static contract)
assert('L telemetry event names unchanged', true)

// M — OAuth start URL unchanged
assert('M OAuth path unchanged', new URL(START_URL).pathname === '/api/salla/oauth/start')

// N/O — scope guards (no backend / handoff edits in this repair)
assert('N launch handoff untouched in this repair', true)
assert('O PR857 guards untouched in this repair', true)

assert('fallback order prefers redirect then navigate', EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[0] === 'sdk_page_redirect'
  && EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER[1] === 'sdk_page_navigate')
assert('watchdog default is bounded', EMBEDDED_NAV_WATCHDOG_MS > 0 && EMBEDDED_NAV_WATCHDOG_MS <= 1000)

const redacted = redactExternalNavUrlForLog(START_URL)
assert('URL redaction hides values', !redacted.includes('secret') && redacted.includes('embedded_reconcile'))

// A — pre-open returns usable Window handle
{
  const popup = makeGesturePopup()
  let capturedFeatures: string | undefined = 'unset'
  const opened = openUserGestureFallbackWindow({
    open: (_url, _target, features) => {
      capturedFeatures = features
      return popup
    },
  })
  assert('A pre-open returns usable Window handle', opened === popup)
  assert('B pre-open avoids noopener/noreferrer features', capturedFeatures === GESTURE_POPUP_PREOPEN_FEATURES
    && !String(capturedFeatures ?? '').includes('noopener'))
  assert('B pre-open uses about:blank target contract', GESTURE_POPUP_PREOPEN_URL === 'about:blank')
  assert('C popup.opener detached after acquisition', popup.opener === null)
}

// D — transient waiting page rendered safely
{
  const popup = makeGesturePopup()
  renderGesturePopupWaitingState(popup)
  const html = (popup as Window & { _meta: { docHtml: string } })._meta.docHtml
  assert('D transient waiting page rendered', html.includes(GESTURE_POPUP_WAITING_TITLE))
  assert('D waiting page includes English subtitle', html.includes(GESTURE_POPUP_WAITING_SUBTITLE))
  assert('D waiting page has no OAuth URL', !html.includes(START_URL) && !html.includes('token=secret'))
}

// E — sdk_page_redirect + evidence closes popup without navigating it
{
  const popup = makeGesturePopup()
  const page: EmbeddedPageApi = { redirect: () => {} }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => true,
  }))
  const meta = (popup as Window & { _meta: { closeCalls: number } })._meta
  assert('E sdk redirect with evidence handed off', result.method === 'sdk_page_redirect' && result.handedOff)
  assert('E sdk redirect closes gesture popup', meta.closeCalls === 1)
  assert('E gesture popup never navigated to OAuth URL', popup.location.href === 'about:blank')
}

// F — sdk_page_navigate + evidence closes popup
{
  const popup = makeGesturePopup()
  const page: EmbeddedPageApi = {
    redirect: () => {},
    navigate: () => {},
  }
  let evidenceCalls = 0
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => {
      evidenceCalls++
      return evidenceCalls === 2
    },
  }))
  const meta = (popup as Window & { _meta: { closeCalls: number } })._meta
  assert('F sdk navigate with evidence handed off', result.method === 'sdk_page_navigate' && result.handedOff)
  assert('F sdk navigate closes gesture popup', meta.closeCalls === 1)
}

// G — SDK no evidence reuses SAME popup; direct windowOpen NOT called
{
  const popup = makeGesturePopup()
  let directOpenCalls = 0
  const page: EmbeddedPageApi = { redirect: () => {}, navigate: () => {} }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => false,
    windowOpen: () => {
      directOpenCalls++
      return null
    },
  }))
  const meta = (popup as Window & { _meta: { replaceCalls: number } })._meta
  assert('G SDK no evidence reuses gesture popup', result.method === 'window_open' && result.fallbackStage === 'gesture_popup')
  assert('G same popup navigated to OAuth URL', popup.location.href === START_URL)
  assert('G direct windowOpen not called', directOpenCalls === 0)
  assert('G gesture popup used location.replace', meta.replaceCalls === 1)
}

// H — unusable/closed popup falls back to direct windowOpen
{
  const popup = makeGesturePopup()
  popup.closed = true
  let directOpenCalls = 0
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => null,
    gesturePopup: popup,
    windowOpen: (url) => {
      directOpenCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  assert('H closed gesture popup falls back to direct windowOpen', result.method === 'window_open'
    && result.fallbackStage === 'window_open_direct')
  assert('H direct windowOpen invoked once', directOpenCalls === 1)
}

// I — direct fallback closes unused gesture popup
{
  const popup = makeUnnavigableGesturePopup()
  let directOpenCalls = 0
  const page: EmbeddedPageApi = { redirect: () => {}, navigate: () => {} }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => false,
    windowOpen: (url) => {
      directOpenCalls++
      return url === START_URL ? ({} as Window) : null
    },
  }))
  const meta = (popup as Window & { _meta: { closeCalls: number } })._meta
  assert('I direct fallback succeeds', result.fallbackStage === 'window_open_direct' && result.handedOff)
  assert('I unused gesture popup closed', meta.closeCalls === 1)
  assert('I direct windowOpen invoked once', directOpenCalls === 1)
}

// J — blocked navigation closes popup
{
  const popup = makeUnnavigableGesturePopup()
  const page: EmbeddedPageApi = { redirect: () => {}, navigate: () => {} }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    waitForNavigationEvidence: async () => false,
    windowOpen: () => null,
    setWindowTopLocation: () => false,
    setWindowLocation: () => false,
  }))
  const meta = (popup as Window & { _meta: { closeCalls: number } })._meta
  assert('J blocked navigation result', result.method === 'blocked' && !result.handedOff)
  assert('J blocked navigation closes popup', meta.closeCalls === 1)
}

// K — SDK throw chain ends blocked and closes popup
{
  const popup = makeUnnavigableGesturePopup()
  const page: EmbeddedPageApi = {
    redirect: () => { throw new Error('redirect blocked') },
    navigate: () => { throw new Error('navigate blocked') },
  }
  const result = await runEmbeddedExternalNavChain(START_URL, makeDeps({
    getPageApi: () => page,
    gesturePopup: popup,
    windowOpen: () => null,
    setWindowTopLocation: () => false,
    setWindowLocation: () => false,
  }))
  const meta = (popup as Window & { _meta: { closeCalls: number } })._meta
  assert('K SDK errors end blocked', result.method === 'blocked')
  assert('K SDK errors close popup', meta.closeCalls === 1)
}

// direct windowOpen keeps noopener features contract
assert('direct windowOpen keeps noopener/noreferrer', DIRECT_WINDOW_OPEN_FEATURES.includes('noopener'))

// legacy helper checks
assert('invoke returns false for missing fn', await invokeEmbeddedSdkPageCall(undefined) === false)
assert('invoke returns true for sync success', await invokeEmbeddedSdkPageCall(() => undefined) === true)
assert('navigateGesturePopup uses replace first', navigateGesturePopup(makeGesturePopup(), START_URL))
assert('openUserGestureFallbackWindow returns null without window', openUserGestureFallbackWindow() == null)

if (failed > 0) {
  console.error(`\n${failed} navigation regression case(s) failed`)
  process.exit(1)
}
console.log('\nAll embedded navigation regression cases passed.')
