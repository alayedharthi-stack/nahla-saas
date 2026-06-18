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

export type PricingBackRouteOptions = {
  /** Merchant just completed payment (URL param or activation). */
  paymentSuccess?: boolean
  /** Subscription is active (paid_active lifecycle). */
  subscriptionActive?: boolean
  /** Override embedded detection (tests). */
  sallaEmbedded?: boolean
}

/**
 * Back link for /app/pricing.
 *
 * Paid/active merchants always return to the full dashboard — never the Salla
 * mini-app. Unsubscribed Salla onboarding may still use /app/entry.
 */
export function pricingPageBackRoute(opts: PricingBackRouteOptions = {}): string {
  if (opts.paymentSuccess || opts.subscriptionActive) {
    return '/overview'
  }
  const embedded = opts.sallaEmbedded ?? isSallaEmbedded()
  if (embedded) {
    return '/app/entry'
  }
  return '/billing'
}

/** True when the pricing back target must open the full dashboard (top window). */
export function pricingBackOpensFullDashboard(route: string): boolean {
  return route === '/overview'
}

/** Arabic label for the /app/pricing back link target. */
export function pricingPageBackLabel(route: string): string {
  if (route === '/overview') return 'العودة للوحة التحكم'
  if (route === '/app/entry') return 'العودة للوحة التطبيق'
  return 'العودة للاشتراك والفوترة'
}

/**
 * Navigate to the full dashboard after payment — breaks out of the Salla
 * iframe when checkout started inside the embedded app.
 */
export function goToPostPaymentDashboard(
  navigate: (to: string, opts?: { replace?: boolean }) => void,
): void {
  const route = postPaymentDashboardRoute()
  try {
    if (typeof window !== 'undefined' && window.top && window.top !== window.self) {
      window.top.location.href = `${window.location.origin}${route}`
      return
    }
  } catch { /* cross-origin top access blocked */ }
  navigate(route, { replace: true })
}
