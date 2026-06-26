/**
 * Shared Salla embedded SDK bootstrap — theme + locale from init state,
 * postMessage-ready signals, and a single init promise for navigation timing.
 */
import { extractThemeFromSdkState, notifySallaHostTheme, persistEmbeddedThemeWithSource } from '../i18n/embeddedTheme'
import {
  extractLangFromSdkState,
  notifySallaHostLang,
  persistEmbeddedLangWithSource,
} from '../i18n/embeddedLocale'

export const EMBEDDED_SDK_URL =
  'https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js'

export const EMBEDDED_SDK_INIT_TIMEOUT_MS = 1200

let sdkInitPromise: Promise<void> | null = null

export function signalEmbeddedReady(logPrefix = '[SallaEmbedded]'): void {
  // eslint-disable-next-line no-console
  console.info('%s → signaling app.ready to Salla host frame', logPrefix)
  const readyMsg = {
    event:     'embedded::ready',
    payload:   {},
    timestamp: Date.now(),
    source:    'embedded-app',
    metadata:  { version: '0.2.4' },
  }
  try { window.parent.postMessage(readyMsg, '*') } catch { /* cross-origin */ }
  try { window.parent.postMessage({ event: 'app.ready', type: 'app.ready' }, '*') } catch { /* cross-origin */ }
}

function loadSdk(): Promise<void> {
  return new Promise((resolve) => {
    if (document.querySelector(`script[src="${EMBEDDED_SDK_URL}"]`)) {
      resolve()
      return
    }
    const s   = document.createElement('script')
    s.src     = EMBEDDED_SDK_URL
    s.onload  = () => {
      // eslint-disable-next-line no-console
      console.info('[SallaEmbedded] SDK loaded from CDN')
      resolve()
    }
    s.onerror = () => {
      console.warn('[SallaEmbedded] SDK CDN load failed — continuing without SDK')
      resolve()
    }
    document.head.appendChild(s)
  })
}

export function applyEmbeddedSdkState(state: unknown, logPrefix = '[SallaEmbedded]'): void {
  const hostTheme = extractThemeFromSdkState(state)
  const hostLang  = extractLangFromSdkState(state)
  if (hostTheme) {
    // eslint-disable-next-line no-console
    console.info('%s SDK init layout theme=%s', logPrefix, hostTheme)
    persistEmbeddedThemeWithSource(hostTheme, 'salla')
    notifySallaHostTheme(hostTheme)
  }
  if (hostLang) {
    // eslint-disable-next-line no-console
    console.info('%s SDK init layout lang=%s', logPrefix, hostLang)
    persistEmbeddedLangWithSource(hostLang, 'salla')
    notifySallaHostLang(hostLang)
  }
}

function runSdkInit(logPrefix = '[SallaEmbedded]'): Promise<void> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const sdk = (window as any).Salla?.embedded
  if (!sdk) {
    // eslint-disable-next-line no-console
    console.info('%s Salla.embedded not found — using raw postMessage only', logPrefix)
    signalEmbeddedReady(logPrefix)
    return Promise.resolve()
  }

  return sdk.init({ debug: false })
    .then((state: unknown) => {
      applyEmbeddedSdkState(state, logPrefix)
      sdk.ready()
      signalEmbeddedReady(logPrefix)
    })
    .catch((err: unknown) => {
      console.warn('%s sdk.init error:', logPrefix, err)
      signalEmbeddedReady(logPrefix)
    })
}

/** Start (or reuse) SDK load + init. Safe to call from /app/salla and /app/entry. */
export function startEmbeddedSdkHandshake(logPrefix = '[SallaEmbedded]'): Promise<void> {
  if (!sdkInitPromise) {
    sdkInitPromise = loadSdk().then(() => runSdkInit(logPrefix))
  }
  return sdkInitPromise
}

/** Wait for SDK init up to `timeoutMs` — use before internal /app/entry navigation. */
export function waitForEmbeddedSdkContext(
  timeoutMs = EMBEDDED_SDK_INIT_TIMEOUT_MS,
  logPrefix = '[SallaEmbedded]',
): Promise<void> {
  return Promise.race([
    startEmbeddedSdkHandshake(logPrefix),
    new Promise<void>((resolve) => { setTimeout(resolve, timeoutMs) }),
  ])
}

/** Reset init promise — tests only. */
export function resetEmbeddedSdkHandshakeForTests(): void {
  sdkInitPromise = null
}
