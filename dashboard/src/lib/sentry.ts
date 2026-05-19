/**
 * dashboard/src/lib/sentry.ts
 * ───────────────────────────
 * Phase 1A: Sentry initialisation for the dashboard SPA.
 *
 * Why a wrapper
 * ─────────────
 * - One place to set the DSN, environment, release tag, traces sample rate.
 * - One place to scrub PII (email / token) BEFORE the event leaves the
 *   browser. The backend has matching scrubbing in
 *   ``backend/core/observability_sentry.py``.
 * - Lets ``main.tsx`` stay short — it just imports and calls ``initSentry()``.
 *
 * Behaviour
 * ─────────
 * - No-op when ``VITE_SENTRY_DSN`` is unset (every dev/preview build by
 *   default — we never want to ship dev errors to the production project).
 * - User context attached via ``setSentryUser`` after login. We send only
 *   ``tenantId`` + ``userId`` + ``role`` — never ``email`` or ``phone``.
 */

import * as Sentry from '@sentry/react'

// Sensitive query/body fragments scrubbed from breadcrumbs and event data.
// Anything matching is replaced with `[scrubbed]`.
const SENSITIVE_KEYS = [
  'password',
  'access_token',
  'refresh_token',
  'token',
  'authorization',
  'cookie',
  'set-cookie',
  'x-nahla-key',
  'x-hub-signature',
  'x-hub-signature-256',
]

function scrubObject<T>(input: T): T {
  if (!input || typeof input !== 'object') return input
  // Don't mutate the original; clone shallowly and replace sensitive
  // keys. Sentry already deep-clones before send, but we add another
  // pass so a future SDK upgrade can't regress this.
  if (Array.isArray(input)) {
    return input.map((item) => scrubObject(item)) as unknown as T
  }
  const out: Record<string, unknown> = { ...(input as Record<string, unknown>) }
  for (const key of Object.keys(out)) {
    const lower = key.toLowerCase()
    if (SENSITIVE_KEYS.some(s => lower.includes(s))) {
      out[key] = '[scrubbed]'
    } else if (out[key] && typeof out[key] === 'object') {
      out[key] = scrubObject(out[key])
    }
  }
  return out as unknown as T
}

let initialised = false

export function initSentry(): void {
  if (initialised) return

  const dsn = (import.meta.env.VITE_SENTRY_DSN as string | undefined)?.trim()
  if (!dsn) {
    // Quiet by design — dev builds and preview deploys without a DSN
    // never need to know.
    return
  }

  const env =
    (import.meta.env.VITE_SENTRY_ENV as string | undefined)?.trim() ||
    (import.meta.env.MODE as string | undefined) ||
    'production'
  const release = (import.meta.env.VITE_SENTRY_RELEASE as string | undefined)?.trim()
  const sampleRateRaw = (import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE as string | undefined)?.trim()
  const tracesSampleRate = sampleRateRaw ? Number(sampleRateRaw) : 0.1

  Sentry.init({
    dsn,
    environment: env,
    release,
    tracesSampleRate: Number.isFinite(tracesSampleRate) ? tracesSampleRate : 0.1,
    sendDefaultPii: false,
    integrations: [
      Sentry.browserTracingIntegration(),
      // No replay integration on the dashboard — it would record the
      // merchant's own customer conversations including PII. Re-evaluate
      // in Phase 3 with strict masking + opt-in.
    ],
    beforeSend(event) {
      try {
        if (event.request) {
          if (event.request.headers) {
            event.request.headers = scrubObject(event.request.headers)
          }
          if (event.request.cookies) {
            // Sentry types `cookies` as `{ [k: string]: string }`; replace
            // every value with the scrub marker so the keys disappear.
            event.request.cookies = { _scrubbed: '[scrubbed]' }
          }
          if (event.request.data) {
            event.request.data = scrubObject(event.request.data)
          }
          if (event.request.query_string) {
            // Query strings on the dashboard rarely carry secrets, but
            // password reset flows use `?token=` — drop the whole
            // query for affected paths.
            const url = (event.request.url ?? '') as string
            if (url.includes('/reset-password') || url.includes('/verify-email')) {
              event.request.query_string = '[scrubbed]'
            }
          }
        }
        if (event.contexts) {
          event.contexts = scrubObject(event.contexts) as typeof event.contexts
        }
        if (event.extra) {
          event.extra = scrubObject(event.extra) as typeof event.extra
        }
      } catch {
        // Never break event delivery on a scrubber bug.
      }
      return event
    },
  })

  initialised = true
  // eslint-disable-next-line no-console
  console.info('[sentry] initialised env=%s release=%s', env, release ?? '(unset)')
}

/**
 * Attach a minimal, PII-free user context to the current scope. Call
 * after a successful login or impersonation start. Never pass the
 * email / phone — only the opaque identifiers.
 */
export function setSentryUser(opts: {
  userId: number | string | null
  tenantId: number | string | null
  role: string | null
}): void {
  if (!initialised) return
  Sentry.setUser({
    id:        opts.userId !== null && opts.userId !== undefined ? String(opts.userId) : undefined,
    tenant_id: opts.tenantId !== null && opts.tenantId !== undefined ? String(opts.tenantId) : undefined,
    role:      opts.role ?? 'unknown',
  })
}

/** Clear the Sentry user context — called on logout. */
export function clearSentryUser(): void {
  if (!initialised) return
  Sentry.setUser(null)
}
