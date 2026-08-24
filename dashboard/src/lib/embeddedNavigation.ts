/**
 * Salla embedded external navigation — host-mediated redirect first, then safe fallbacks.
 *
 * Direct window.top.location.href from the iframe is unreliable in Salla's sandbox;
 * the embedded SDK page.redirect() asks the host frame to navigate externally.
 *
 * Pinned SDK: @salla.sa/embedded-sdk@0.2.4 — official page API: redirect(), navigate().
 */
import {
  EMBEDDED_SDK_INIT_TIMEOUT_MS,
  startEmbeddedSdkHandshake,
  waitForEmbeddedSdkContext,
} from './embeddedSdkHandshake'

export type EmbeddedExternalNavMethod =
  | 'sdk_page_redirect'
  | 'sdk_page_navigate'
  | 'window_open'
  | 'window_top_location'
  | 'window_location'
  | 'blocked'

export interface EmbeddedExternalNavResult {
  method: EmbeddedExternalNavMethod
  startUrl: string
  sdkAvailable: boolean
  handedOff: boolean
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

export interface EmbeddedExternalNavDeps {
  getPageApi: () => EmbeddedPageApi | null
  windowOpen: (url: string) => Window | null
  setWindowTopLocation: (url: string) => boolean
  setWindowLocation: (url: string) => boolean
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

async function trySdkRedirect(page: EmbeddedPageApi, startUrl: string): Promise<boolean> {
  if (typeof page.redirect !== 'function') return false
  return invokeEmbeddedSdkPageCall(() => page.redirect!(startUrl))
}

async function trySdkNavigate(page: EmbeddedPageApi, startUrl: string): Promise<boolean> {
  if (typeof page.navigate !== 'function') return false
  return invokeEmbeddedSdkPageCall(() => page.navigate!(startUrl))
}

/** Run the external-navigation fallback chain with injectable deps (tests + runtime). */
export async function runEmbeddedExternalNavChain(
  startUrl: string,
  deps: EmbeddedExternalNavDeps = defaultEmbeddedExternalNavDeps,
): Promise<EmbeddedExternalNavResult> {
  const page = deps.getPageApi()
  const sdkAvailable = page != null

  if (page && await trySdkRedirect(page, startUrl)) {
    return { method: 'sdk_page_redirect', startUrl, sdkAvailable, handedOff: true }
  }

  if (page && await trySdkNavigate(page, startUrl)) {
    return { method: 'sdk_page_navigate', startUrl, sdkAvailable, handedOff: true }
  }

  if (deps.windowOpen(startUrl) != null) {
    return { method: 'window_open', startUrl, sdkAvailable, handedOff: true }
  }

  if (deps.setWindowTopLocation(startUrl)) {
    return { method: 'window_top_location', startUrl, sdkAvailable, handedOff: true }
  }

  if (deps.setWindowLocation(startUrl)) {
    return { method: 'window_location', startUrl, sdkAvailable, handedOff: true }
  }

  return { method: 'blocked', startUrl, sdkAvailable, handedOff: false }
}

export interface NavigateEmbeddedExternalUrlOptions {
  logPrefix?: string
  waitForSdkMs?: number
  skipSdkWait?: boolean
  deps?: EmbeddedExternalNavDeps
}

/** Navigate out of the embedded iframe to an external OAuth start URL. */
export async function navigateEmbeddedExternalUrl(
  startUrl: string,
  options: NavigateEmbeddedExternalUrlOptions = {},
): Promise<EmbeddedExternalNavResult> {
  const logPrefix = options.logPrefix ?? '[SallaEmbedded]'
  const waitForSdkMs = options.waitForSdkMs ?? EMBEDDED_SDK_INIT_TIMEOUT_MS
  const deps = options.deps ?? defaultEmbeddedExternalNavDeps

  if (!options.skipSdkWait) {
    await waitForEmbeddedSdkContext(waitForSdkMs, logPrefix)
  } else {
    void startEmbeddedSdkHandshake(logPrefix)
  }

  const safeUrl = redactExternalNavUrlForLog(startUrl)
  const sdkAvailable = deps.getPageApi() != null

  // eslint-disable-next-line no-console
  console.info(
    '%s embedded external nav start | url=%s | sdk_available=%s',
    logPrefix,
    safeUrl,
    sdkAvailable,
  )

  const result = await runEmbeddedExternalNavChain(startUrl, deps)

  if (result.handedOff) {
    // eslint-disable-next-line no-console
    console.info(
      '%s embedded external nav handed off | method=%s | url=%s',
      logPrefix,
      result.method,
      safeUrl,
    )
  } else {
    console.error('%s embedded external nav blocked | url=%s', logPrefix, safeUrl)
  }

  return result
}
