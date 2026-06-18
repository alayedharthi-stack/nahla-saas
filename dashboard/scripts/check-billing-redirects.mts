/**
 * Route-level checks for post-payment redirect targets.
 *
 * Run:  npm run check:billing-redirects   (from dashboard/)
 */
import {
  postPaymentDashboardRoute,
  postPaymentBillingRoute,
  checkoutRedirectBases,
  pricingPageBackRoute,
} from '../src/lib/billingPostPayment.ts'

let failed = 0

function assert(name: string, ok: boolean, detail = '') {
  if (!ok) {
    failed++
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    console.log(`OK   ${name}`)
  }
}

assert(
  'checkout success_url targets payment-result',
  checkoutRedirectBases('https://app.nahlah.ai').success_url === 'https://app.nahlah.ai/billing/payment-result',
)
assert(
  'post-payment dashboard route is overview with success param',
  postPaymentDashboardRoute() === '/overview?payment=success',
)
assert(
  'post-payment billing route carries success param',
  postPaymentBillingRoute() === '/billing?payment=success',
)
assert(
  'post-payment billing retry route carries failed param',
  postPaymentBillingRoute(true) === '/billing?payment=failed',
)
assert(
  'post-payment dashboard route never uses /app/pricing',
  !postPaymentDashboardRoute().includes('/app/pricing'),
)
assert(
  'post-payment dashboard route never uses /app/entry',
  !postPaymentDashboardRoute().includes('/app/entry'),
)
assert(
  'post-payment billing route never uses /app/pricing',
  !postPaymentBillingRoute().includes('/app/pricing'),
)
assert(
  'post-payment billing route never uses /app/entry',
  !postPaymentBillingRoute().includes('/app/entry'),
)
assert(
  'pricing back route defaults to billing when not embedded',
  pricingPageBackRoute() === '/billing',
)
assert(
  'unsubscribed salla onboarding back uses entry',
  pricingPageBackRoute({ sallaEmbedded: true }) === '/app/entry',
)
assert(
  'active merchant back from pricing uses overview even when embedded',
  pricingPageBackRoute({ sallaEmbedded: true, subscriptionActive: true }) === '/overview',
)
assert(
  'payment success back from pricing uses overview even when embedded',
  pricingPageBackRoute({ sallaEmbedded: true, paymentSuccess: true }) === '/overview',
)
assert(
  'active merchant back never uses /app/entry',
  pricingPageBackRoute({ sallaEmbedded: true, subscriptionActive: true }) !== '/app/entry',
)

if (failed > 0) {
  console.error(`\n${failed} billing redirect check(s) failed`)
  process.exit(1)
}
console.log('\nAll billing redirect checks passed.')
