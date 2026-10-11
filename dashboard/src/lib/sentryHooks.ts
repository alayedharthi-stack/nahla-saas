/**
 * dashboard/src/lib/sentryHooks.ts
 * ────────────────────────────────
 * The scrubbing hooks passed to ``Sentry.init`` (see ``sentry.ts``), kept in
 * a module without runtime package imports so the exact configured callbacks
 * are exercised by ``scripts/check-shopify-connection-ui.mts`` in CI.
 */
import type { BrowserOptions } from '@sentry/react'
import { redactShopifyDeep, sanitizeSentryEvent } from './shopifyConnection/model'

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
  'shopify_handle',
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

type ScrubHooks = Required<Pick<BrowserOptions, 'beforeBreadcrumb' | 'beforeSendTransaction' | 'beforeSendSpan' | 'beforeSend'>>

export const SENTRY_SCRUB_HOOKS: ScrubHooks = {
  // Shopify connection return values: the URL is cleaned before Sentry
  // initialises, but the browser keeps the original navigation URL (with
  // its fragment) in the performance timeline, which browser tracing turns
  // into span descriptions. Every event, transaction, span and breadcrumb is
  // therefore sanitized here (see sanitizeSentryEvent).
  beforeBreadcrumb(breadcrumb) {
    try {
      redactShopifyDeep(breadcrumb)
    } catch {
      // Never break breadcrumb capture on a scrubber bug.
    }
    return breadcrumb
  },
  beforeSendTransaction(event) {
    try {
      sanitizeSentryEvent(event as unknown as Record<string, unknown>)
    } catch {
      // Never break event delivery on a scrubber bug.
    }
    return event
  },
  beforeSendSpan(span) {
    try {
      redactShopifyDeep(span)
    } catch {
      // Never break span delivery on a scrubber bug.
    }
    return span
  },
  beforeSend(event) {
    try {
      sanitizeSentryEvent(event as unknown as Record<string, unknown>)
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
}
