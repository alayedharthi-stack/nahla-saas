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
import { SENTRY_SCRUB_HOOKS } from './sentryHooks'

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
    // Scrubbing and Shopify connection sanitization: see sentryHooks.ts.
    ...SENTRY_SCRUB_HOOKS,
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
