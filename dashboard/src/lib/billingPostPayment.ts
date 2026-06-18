/**
 * Post-payment navigation helpers — single source of truth for where
 * merchants land after Moyasar checkout (success / failure / retry).
 *
 * Authenticated merchants must never be sent back to /app/pricing after
 * payment; that route is a standalone Salla iframe pricing page without
 * dashboard shell/navigation.
 */

export function isSallaEmbedded(): boolean {
  try { return localStorage.getItem('nahla_salla_embedded') === '1' } catch { return false }
}

/** Dashboard landing after successful payment activation. */
export function postPaymentDashboardRoute(): string {
  return '/overview?payment=success'
}

/** Billing page for retry or subscription management after payment. */
export function postPaymentBillingRoute(failed = false): string {
  return failed ? '/billing?payment=failed' : '/billing?payment=success'
}

/** Moyasar checkout redirect bases sent to POST /billing/checkout. */
export function checkoutRedirectBases(origin: string): { success_url: string; error_url: string } {
  const base = `${origin.replace(/\/$/, '')}/billing/payment-result`
  return { success_url: base, error_url: base }
}

/** Back link for /app/pricing when merchant is browsing inside the app. */
export function pricingPageBackRoute(): string {
  return isSallaEmbedded() ? '/app/entry' : '/billing'
}
