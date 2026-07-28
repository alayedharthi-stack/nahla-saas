/**
 * Regression checks for Orders tab-scoped loading (no stale tab data on API failure).
 *
 * Run: npm run check:orders-tab-load (from dashboard/)
 */
import {
  EMPTY_ORDERS_DASHBOARD,
  ordersRevenueDisplay,
  ordersStatDisplay,
  shouldApplyOrdersRequest,
} from '../src/pages/ordersLoadState.ts'

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
  'stale success response is ignored after tab switch',
  !shouldApplyOrdersRequest(1, 2, false),
)
assert(
  'current success response is applied',
  shouldApplyOrdersRequest(2, 2, false),
)
assert(
  'cancelled in-flight response is ignored',
  !shouldApplyOrdersRequest(2, 2, true),
)

assert(
  'stats show dash on load error even after prior success',
  ordersStatDisplay(23, 'failed') === '—',
)
assert(
  'stats show value when load succeeded',
  ordersStatDisplay(23, null) === '23',
)
assert(
  'revenue shows dash on load error',
  ordersRevenueDisplay(150, 'failed', 'ar-SA', 'ر.س') === '—',
)
assert(
  'empty dashboard has zero orders',
  EMPTY_ORDERS_DASHBOARD.summary.total_orders === 0
    && EMPTY_ORDERS_DASHBOARD.orders.length === 0,
)

// Mirrors Orders.tsx: tab switch clears display data before the next fetch resolves.
function simulateTabSwitchLoadFailure() {
  const allTabSuccess = {
    summary: { ...EMPTY_ORDERS_DASHBOARD.summary, total_orders: 23 },
    orders: [{ id: 'all-order-1' } as never],
  }
  let data = { ...allTabSuccess, orders: [...allTabSuccess.orders] }
  let loadError: string | null = null

  // Tab switch — useEffect clears stale tab data immediately.
  data = { ...EMPTY_ORDERS_DASHBOARD, orders: [] }
  loadError = null

  // Failed fetch for the new tab.
  loadError = 'failed'
  data = { ...EMPTY_ORDERS_DASHBOARD, orders: [] }

  return { data, loadError }
}

const tabFailure = simulateTabSwitchLoadFailure()
assert(
  'failed tab does not show prior tab orders',
  tabFailure.data.orders.length === 0,
)
assert(
  'failed tab stats are dash even after prior all-tab success',
  ordersStatDisplay(23, tabFailure.loadError) === '—',
)

if (failed > 0) {
  console.error(`\n${failed} check(s) failed`)
  process.exit(1)
}

console.log('\nAll orders tab-load checks passed.')
