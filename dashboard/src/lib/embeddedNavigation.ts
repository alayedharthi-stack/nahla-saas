/**
 * Salla embedded external navigation — host-mediated redirect first, then safe fallbacks.
 *
 * SDK @0.2.4 page.redirect() is fire-and-forget postMessage with no host acknowledgement.
 * Success requires navigation lifecycle evidence, not merely a void return from redirect().
 *
 * Pinned SDK: @salla.sa/embedded-sdk@0.2.4 — official page API: redirect(), navigate().
 */
import {
  EMBEDDED_SDK_INIT_TIMEOUT_MS,
  getEmbeddedSdkRuntimeState,
  startEmbeddedSdkHandshake,
  waitForEmbeddedSdkContext,
} from './embeddedSdkHandshake'
import {
  emitSallaReconcileTelemetry,
  extractDestinationPath,
  type SallaReconcileTelemetryEvent,
} from './embeddedReconcileTelemetry'

export type EmbeddedExternalNavMethod =
  | 'sdk_page_redirect'
  | 'sdk_page_navigate'
  | 'window_open'
  | 'window_top_location'
  | 'window_location'
  | 'blocked'

/** Host navigation evidence window — SDK redirect has no ack; 400ms covers postMessage handoff. */
export const EMBEDDED_NAV_WATCHDOG_MS = 400

export interface EmbeddedExternalNavResult {
  method: EmbeddedExternalNavMethod
  startUrl: string
  sdkAvailable: boolean
  handedOff: boolean
  navigationEvidence?: boolean
  fallbackStage?: string
}

export const EMBEDDED_EXTERNAL_NAV_FALLBACK_ORDER: EmbeddedExternalNavMethod[] = [
  'sdk_page_redirect',
  'sdk_page_navigate',
  'window_open',
  'window_top_location',
  'window_location',
]

export interface EmbeddedPageApi {
  redirect?: (url: string) => void | Promise<void>
  navigate?: (url: string, options?: { replace?: boolean; state?: unknown }) => void | Promise<void>
}

export interface NavAttemptTelemetry {
  attemptedMethod: EmbeddedExternalNavMethod
  fallbackStage: string
  navigationEvidence?: boolean
  handedOff: boolean
}

export interface EmbeddedExternalNavDeps {
  getPageApi: () => EmbeddedPageApi | null
  /** Pre-opened synchronously on user click to preserve activation for popup fallback. */
  gesturePopup?: Window | null
  windowOpen: (url: string) => Window | null
  setWindowTopLocation: (url: string) => boolean
  setWindowLocation: (url: string) => boolean
  waitForNavigationEvidence?: (timeoutMs: number) => Promise<boolean>
  watchdogMs?: number
  onNavAttempt?: (payload: NavAttemptTelemetry) => void
}

function getEmbeddedPageApiFromWindow(): EmbeddedPageApi | null {
  if (typeof window === 'undefined') return null
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const page = (window as any).Salla?.embedded?.page as EmbeddedPageApi | undefined
  return page ?? null
}

export const defaultEmbeddedExternalNavDeps: EmbeddedExternalNavDeps = {
  getPageApi: getEmbeddedPageApiFromWindow,
  windowOpen(url: string) {
    try {
      return window.open(url, '_blank', 'noopener,noreferrer')
    } catch {
      return null
    }
  },
  setWindowTopLocation(url: string) {
    try {
      if (window.top && window.top !== window) {
        window.top.location.href = url
        return true
      }
    } catch {
      return false
    }
    return false
  },
  setWindowLocation(url: string) {
    try {
      window.location.href = url
      return true
    } catch {
      return false
    }
  },
}

/** Open a blank tab synchronously on user click — preserves activation for later navigation. */
export function openUserGestureFallbackWindow(): Window | null {
  try {
    return window.open('about:blank', '_blank', 'noopener,noreferrer')
  } catch {
    return null
  }
}

export function navigateGesturePopup(popup: Window | null | undefined, url: string): boolean {
  if (!popup || popup.closed) return false
  try {
    popup.location.href = url
    return true
  } catch {
    return false
  }
}

export function closeGesturePopupIfOpen(popup: Window | null | undefined): void {
  try {
    if (popup && !popup.closed) popup.close()
  } catch {
    /* ignore */
  }
}

/** Safe URL summary for logs — origin, path, and query keys only (no values). */
export function redactExternalNavUrlForLog(url: string): string {
  try {
    const parsed = new URL(url)
    const keys = [...parsed.searchParams.keys()].sort().join(',') || '(none)'
    return `${parsed.origin}${parsed.pathname}?keys=${keys}`
  } catch {
    return '(invalid-url)'
  }
}

/** Invoke an SDK page method; local failures never abort the fallback chain. */
export async function invokeEmbeddedSdkPageCall(
  call: (() => unknown) | undefined,
): Promise<boolean> {
  if (typeof call !== 'function') return false
  try {
    const result = call()
    if (result != null && typeof (result as Promise<unknown>).then === 'function') {
      await (result as Promise<unknown>)
    }
    return true
  } catch {
    return false
  }
}

/** Wait for host navigation lifecycle signals after SDK redirect (no SDK ack exists). */
export function waitForNavigationEvidence(timeoutMs: number): Promise<boolean> {
  if (typeof document === 'undefined' || typeof window === 'undefined') {
    return Promise.resolve(false)
  }

  return new Promise((resolve) => {
    let settled = false
    const finish = (ok: boolean) => {
      if (settled) return
      settled = true
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('pagehide', onPageHide)
      resolve(ok)
    }

    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') finish(true)
    }
    const onPageHide = () => finish(true)

    if (document.visibilityState === 'hidden') {
      finish(true)
      return
    }

    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('pagehide', onPageHide)
    window.setTimeout(() => finish(false), timeoutMs)
  })
}

async function trySdkMethodWithWatchdog(
  page: EmbeddedPageApi,
  startUrl: string,
  method: 'redirect' | 'navigate',
  deps: EmbeddedExternalNavDeps,
): Promise<{ invoked: boolean; evidence: boolean }> {
  const fn = method === 'redirect' ? page.redirect : page.navigate
  if (typeof fn !== 'function') return { invoked: false, evidence: false }

  const invoked = await invokeEmbeddedSdkPageCall(() => fn!.call(page, startUrl))
  if (!invoked) return { invoked: false, evidence: false }

  const wait = deps.waitForNavigationEvidence ?? waitForNavigationEvidence
  const watchdogMs = deps.watchdogMs ?? EMBEDDED_NAV_WATCHDOG_MS
  const evidence = await wait(watchdogMs)
  return { invoked: true, evidence }
}

function reportNavAttempt(
  deps: EmbeddedExternalNavDeps,
  attemptedMethod: EmbeddedExternalNavMethod,
  fallbackStage: string,
  handedOff: boolean,
  navigationEvidence?: boolean,
): void {
  deps.onNavAttempt?.({
    attemptedMethod,
    fallbackStage,
    navigationEvidence,
    handedOff,
  })
}

/** Run the external-navigation fallback chain with injectable deps (tests + runtime). */
export async function runEmbeddedExternalNavChain(
  startUrl: string,
  deps: EmbeddedExternalNavDeps = defaultEmbeddedExternalNavDeps,
): Promise<EmbeddedExternalNavResult> {
  const page = deps.getPageApi()
  const sdkAvailable = page != null
  const gesturePopup = deps.gesturePopup

  if (page) {
    const redirectAttempt = await trySdkMethodWithWatchdog(page, startUrl, 'redirect', deps)
    reportNavAttempt(
      deps,
      'sdk_page_redirect',
      redirectAttempt.evidence ? 'sdk_redirect_evidence' : 'sdk_redirect_no_evidence',
      redirectAttempt.evidence,
      redirectAttempt.evidence,
    )
    if (redirectAttempt.evidence) {
      closeGesturePopupIfOpen(gesturePopup)
      return {
        method: 'sdk_page_redirect',
        startUrl,
        sdkAvailable,
        handedOff: true,
        navigationEvidence: true,
        fallbackStage: 'sdk_redirect_evidence',
      }
    }

    const navigateAttempt = await trySdkMethodWithWatchdog(page, startUrl, 'navigate', deps)
    reportNavAttempt(
      deps,
      'sdk_page_navigate',
      navigateAttempt.evidence ? 'sdk_navigate_evidence' : 'sdk_navigate_no_evidence',
      navigateAttempt.evidence,
      navigateAttempt.evidence,
    )
    if (navigateAttempt.evidence) {
      closeGesturePopupIfOpen(gesturePopup)
      return {
        method: 'sdk_page_navigate',
        startUrl,
        sdkAvailable,
        handedOff: true,
        navigationEvidence: true,
        fallbackStage: 'sdk_navigate_evidence',
      }
    }
  }

  if (navigateGesturePopup(gesturePopup, startUrl)) {
    reportNavAttempt(deps, 'window_open', 'gesture_popup', true)
    return {
      method: 'window_open',
      startUrl,
      sdkAvailable,
      handedOff: true,
      fallbackStage: 'gesture_popup',
    }
  }

  if (deps.windowOpen(startUrl) != null) {
    reportNavAttempt(deps, 'window_open', 'window_open_direct', true)
    return {
      method: 'window_open',
      startUrl,
      sdkAvailable,
      handedOff: true,
      fallbackStage: 'window_open_direct',
    }
  }

  if (deps.setWindowTopLocation(startUrl)) {
    reportNavAttempt(deps, 'window_top_location', 'window_top_location', true)
    return {
      method: 'window_top_location',
      startUrl,
      sdkAvailable,
      handedOff: true,
      fallbackStage: 'window_top_location',
    }
  }

  if (deps.setWindowLocation(startUrl)) {
    reportNavAttempt(deps, 'window_location', 'window_location', true)
    return {
      method: 'window_location',
      startUrl,
      sdkAvailable,
      handedOff: true,
      fallbackStage: 'window_location',
    }
  }

  reportNavAttempt(deps, 'blocked', 'blocked', false)
  closeGesturePopupIfOpen(gesturePopup)
  return {
    method: 'blocked',
    startUrl,
    sdkAvailable,
    handedOff: false,
    fallbackStage: 'blocked',
  }
}

export interface NavigateEmbeddedExternalUrlOptions {
  logPrefix?: string
  waitForSdkMs?: number
  skipSdkWait?: boolean
  deps?: EmbeddedExternalNavDeps
  correlationId?: string
  gesturePopup?: Window | null
  emitTelemetry?: boolean
}

function emitNavTelemetry(
  event: SallaReconcileTelemetryEvent,
  correlationId: string | undefined,
  destinationPath: string,
  extra: {
    sdk_loaded?: boolean
    sdk_initialized?: boolean
    attempted_method?: string
    fallback_stage?: string
  } = {},
): void {
  if (!correlationId) return
  emitSallaReconcileTelemetry({
    event,
    correlation_id: correlationId,
    destination_path: destinationPath,
    ...extra,
  })
}

/** Navigate out of the embedded iframe to an external OAuth start URL. */
export async function navigateEmbeddedExternalUrl(
  startUrl: string,
  options: NavigateEmbeddedExternalUrlOptions = {},
): Promise<EmbeddedExternalNavResult> {
  const logPrefix = options.logPrefix ?? '[SallaEmbedded]'
  const waitForSdkMs = options.waitForSdkMs ?? EMBEDDED_SDK_INIT_TIMEOUT_MS
  const baseDeps = options.deps ?? defaultEmbeddedExternalNavDeps
  const correlationId = options.correlationId
  const destinationPath = extractDestinationPath(startUrl)
  const emitTelemetry = options.emitTelemetry !== false

  const deps: EmbeddedExternalNavDeps = {
    ...baseDeps,
    gesturePopup: options.gesturePopup ?? baseDeps.gesturePopup,
    onNavAttempt: (payload) => {
      baseDeps.onNavAttempt?.(payload)
      if (emitTelemetry) {
        emitNavTelemetry('SALLA_RECONCILE_NAV_ATTEMPT', correlationId, destinationPath, {
          attempted_method: payload.attemptedMethod,
          fallback_stage: payload.fallbackStage,
        })
      }
    },
  }

  if (!options.skipSdkWait) {
    await waitForEmbeddedSdkContext(waitForSdkMs, logPrefix)
  } else {
    void startEmbeddedSdkHandshake(logPrefix)
  }

  const sdkState = getEmbeddedSdkRuntimeState()
  if (emitTelemetry) {
    emitNavTelemetry('SALLA_EMBEDDED_SDK_STATE', correlationId, destinationPath, {
      sdk_loaded: sdkState.sdkLoaded,
      sdk_initialized: sdkState.sdkInitialized,
    })
  }

  const safeUrl = redactExternalNavUrlForLog(startUrl)
  const sdkAvailable = deps.getPageApi() != null

  // eslint-disable-next-line no-console
  console.info(
    '%s embedded external nav start | url=%s | sdk_available=%s | sdk_initialized=%s',
    logPrefix,
    safeUrl,
    sdkAvailable,
    sdkState.sdkInitialized,
  )

  const result = await runEmbeddedExternalNavChain(startUrl, deps)

  if (result.handedOff) {
    // eslint-disable-next-line no-console
    console.info(
      '%s embedded external nav handed off | method=%s | stage=%s | evidence=%s | url=%s',
      logPrefix,
      result.method,
      result.fallbackStage ?? '(none)',
      result.navigationEvidence ?? 'n/a',
      safeUrl,
    )
  } else {
    console.error('%s embedded external nav blocked | url=%s', logPrefix, safeUrl)
  }

  return result
}
