/**
 * Side-effect module: MUST stay the first import of ``main.tsx`` so it runs
 * before any other module of the app is evaluated and before Sentry, SEO,
 * the runtime boot log, auth and the router read the URL. See
 * ``returnCapture.ts``.
 *
 * Fail-closed: when a Shopify return URL could not be verified clean, this
 * module shows fixed text and throws, which stops the evaluation of the
 * whole module graph (no App, auth, Sentry, SEO, telemetry or boot log),
 * while ``location.replace`` reloads the bare path. Nothing from the URL is
 * rendered, logged or kept.
 */
import {
  EARLY_BLOCKED_FLAG,
  EARLY_RETURN_GETTER,
  type CaptureResult,
  captureShopifyReturn,
} from './returnCapture'

const BLOCKED_TEXT_AR = 'تعذّر إكمال ربط Shopify بأمان. أغلق هذه الصفحة وابدأ الربط من جديد من صفحة التكاملات.'
const BLOCKED_TEXT_EN = 'The Shopify connection could not be finished safely. Close this page and start again from Integrations.'

let result: CaptureResult = { blocked: false }

if (typeof window !== 'undefined') {
  try {
    const w = window as unknown as Record<string, unknown>
    const getter = w[EARLY_RETURN_GETTER]
    result = captureShopifyReturn(
      {
        location: window.location,
        history: window.history,
        takeEarly: typeof getter === 'function' ? (getter as () => string) : undefined,
        earlyBlocked: w[EARLY_BLOCKED_FLAG] === true,
        replaceLocation: (url) => window.location.replace(url),
        setTimer: (fn, ms) => window.setTimeout(fn, ms),
        clearTimer: (id) => window.clearTimeout(id as number),
      },
      Date.now(),
    )
  } catch {
    result = { blocked: true }
  }
}

if (result.blocked) {
  try {
    const root = document.getElementById('root') ?? document.body
    const box = document.createElement('main')
    box.setAttribute('data-testid', 'shopify-return-blocked')
    box.style.cssText = 'font-family:system-ui,sans-serif;max-width:560px;margin:10vh auto;padding:24px;line-height:1.7'
    for (const [text, dir] of [[BLOCKED_TEXT_AR, 'rtl'], [BLOCKED_TEXT_EN, 'ltr']] as const) {
      const p = document.createElement('p')
      p.dir = dir
      p.textContent = text
      box.appendChild(p)
    }
    root.replaceChildren(box)
  } catch {
    /* fixed text is best effort; the boot still stops below */
  }
  // Fixed message only — never the URL or any captured value.
  throw new Error('shopify_return_scrub_failed')
}
