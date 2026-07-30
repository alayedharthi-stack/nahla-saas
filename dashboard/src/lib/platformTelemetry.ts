/**
 * Shared platform telemetry for the Nahla merchant dashboard.
 *
 * Forwards events to whichever analytics provider is already on the page
 * (PostHog, GA4 via gtag) and always logs to console for dev visibility.
 *
 * Payloads must stay flat (no nested objects) and must not include PII
 * (customer names, chat text, phone numbers, etc.).
 */

export type PlatformTelemetryPayload = Record<
  string,
  string | number | boolean | null | undefined
>

/** Closed registry of approved platform telemetry event names. */
export const PLATFORM_TELEMETRY_EVENTS = {
  // Billing (active)
  salla_redirect_clicked: 'salla_redirect_clicked',
  billing_payment_success_landed: 'billing_payment_success_landed',
  salla_returned_without_subscription: 'salla_returned_without_subscription',

  // Baseline navigation / overview (wired in Layout, Sidebar, Overview — P2)
  platform_page_view: 'platform_page_view',
  platform_nav_click: 'platform_nav_click',
  overview_loaded: 'overview_loaded',
  overview_period_changed: 'overview_period_changed',
  overview_cta_clicked: 'overview_cta_clicked',
} as const

export type PlatformTelemetryEventName =
  (typeof PLATFORM_TELEMETRY_EVENTS)[keyof typeof PLATFORM_TELEMETRY_EVENTS]

export function trackPlatformEvent(
  name: PlatformTelemetryEventName,
  payload: PlatformTelemetryPayload = {},
): void {
  try {
    console.info(`[track] ${name}`, payload)

    const ph = (
      window as unknown as {
        posthog?: { capture: (n: string, p: PlatformTelemetryPayload) => void }
      }
    ).posthog
    if (ph && typeof ph.capture === 'function') {
      ph.capture(name, payload)
    }

    const gtag = (
      window as unknown as {
        gtag?: (cmd: string, eventName: string, p: PlatformTelemetryPayload) => void
      }
    ).gtag
    if (typeof gtag === 'function') {
      gtag('event', name, payload)
    }
  } catch (e) {
    console.warn('[track] failed:', e)
  }
}
