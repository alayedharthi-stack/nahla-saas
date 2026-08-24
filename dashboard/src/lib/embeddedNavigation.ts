/**
 * Salla embedded external navigation — host-mediated redirect first, then safe fallbacks.
 *
 * Direct window.top.location.href from the iframe is unreliable in Salla's sandbox;
 * the embedded SDK page.redirect() asks the host frame to navigate externally.
 */
import {
  EMBEDDED_SDK_INIT_TIMEOUT_MS,
  startEmbeddedSdkHandshake,
  waitForEmbeddedSdkContext,
} from './embeddedSdkHandshake'

export type EmbeddedExternalNavMethod =
  | 'sdk_page_redirect'
  | 'sdk_page_navTo'
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
  'sdk_page_navTo',
  'window_open',
  'window_top_location',
  'window_location',
]

interface EmbeddedPageApi {
  redirect?: (url: string) => void
  navTo?: (url: string, options?: { replace?: boolean; state?: unknown }) => void
}

function getEmbeddedPageApi(): EmbeddedPageApi | null {
  if (typeof window === 'undefined') return null
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const page = (window as any).Salla?.embedded?.page as EmbeddedPageApi | undefined
  return page ?? null
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

function trySdkRedirect(page: EmbeddedPageApi, startUrl: string): boolean {
  if (typeof page.redirect !== 'function') return false
  page.redirect(startUrl)
  return true
}

function trySdkNavTo(page: EmbeddedPageApi, startUrl: string): boolean {
  if (typeof page.navTo !== 'function') return false
  page.navTo(startUrl)
  return true
}

function tryWindowOpen(startUrl: string): boolean {
  try {
    const opened = window.open(startUrl, '_blank', 'noopener,noreferrer')
    return opened != null
  } catch {
    return false
  }
}

function tryWindowTopLocation(startUrl: string): boolean {
  try {
    if (window.top && window.top !== window) {
      window.top.location.href = startUrl
      return true
    }
  } catch {
    return false
  }
  return false
}

function tryWindowLocation(startUrl: string): boolean {
  try {
    window.location.href = startUrl
    return true
  } catch {
    return false
  }
}

export interface NavigateEmbeddedExternalUrlOptions {
  logPrefix?: string
  waitForSdkMs?: number
  skipSdkWait?: boolean
}

/**
 * Navigate the merchant out of the embedded iframe to an external URL (OAuth start).
 * Prefers Salla SDK host redirect; falls back to window.open then direct location changes.
 */
export async function navigateEmbeddedExternalUrl(
  startUrl: string,
  options: NavigateEmbeddedExternalUrlOptions = {},
): Promise<EmbeddedExternalNavResult> {
  const logPrefix = options.logPrefix ?? '[SallaEmbedded]'
  const waitForSdkMs = options.waitForSdkMs ?? EMBEDDED_SDK_INIT_TIMEOUT_MS

  if (!options.skipSdkWait) {
    await waitForEmbeddedSdkContext(waitForSdkMs, logPrefix)
  } else {
    void startEmbeddedSdkHandshake(logPrefix)
  }

  const page = getEmbeddedPageApi()
  const sdkAvailable = page != null
  const safeUrl = redactExternalNavUrlForLog(startUrl)

  // eslint-disable-next-line no-console
  console.info(
    '%s embedded external nav start | url=%s | sdk_available=%s',
    logPrefix,
    safeUrl,
    sdkAvailable,
  )

  if (page && trySdkRedirect(page, startUrl)) {
    // eslint-disable-next-line no-console
    console.info('%s embedded external nav handed off | method=sdk_page_redirect | url=%s', logPrefix, safeUrl)
    return {
      method: 'sdk_page_redirect',
      startUrl,
      sdkAvailable,
      handedOff: true,
    }
  }

  if (page && trySdkNavTo(page, startUrl)) {
    // eslint-disable-next-line no-console
    console.info('%s embedded external nav handed off | method=sdk_page_navTo | url=%s', logPrefix, safeUrl)
    return {
      method: 'sdk_page_navTo',
      startUrl,
      sdkAvailable,
      handedOff: true,
    }
  }

  if (tryWindowOpen(startUrl)) {
    // eslint-disable-next-line no-console
    console.info('%s embedded external nav handed off | method=window_open | url=%s', logPrefix, safeUrl)
    return {
      method: 'window_open',
      startUrl,
      sdkAvailable,
      handedOff: true,
    }
  }

  if (tryWindowTopLocation(startUrl)) {
    // eslint-disable-next-line no-console
    console.info('%s embedded external nav handed off | method=window_top_location | url=%s', logPrefix, safeUrl)
    return {
      method: 'window_top_location',
      startUrl,
      sdkAvailable,
      handedOff: true,
    }
  }

  if (tryWindowLocation(startUrl)) {
    // eslint-disable-next-line no-console
    console.info('%s embedded external nav handed off | method=window_location | url=%s', logPrefix, safeUrl)
    return {
      method: 'window_location',
      startUrl,
      sdkAvailable,
      handedOff: true,
    }
  }

  console.error('%s embedded external nav blocked | url=%s', logPrefix, safeUrl)
  return {
    method: 'blocked',
    startUrl,
    sdkAvailable,
    handedOff: false,
  }
}
