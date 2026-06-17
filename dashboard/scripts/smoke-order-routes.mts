import { formatOrderNumberLabel, orderApiId, orderDetailPath } from '../src/lib/orderRoutes.ts'

function assert(cond: boolean, msg: string): void {
  if (!cond) throw new Error(msg)
}

const sample = {
  id: '#NHL-33-000003',
  order_number: '#NHL-33-000003',
  internal_id: '847',
}

assert(orderDetailPath(sample) === '/orders/847', 'detail path must use internal_id')
assert(!orderDetailPath(sample).includes('#'), 'detail path must not contain #')
assert(formatOrderNumberLabel(sample) === '#NHL-33-000003', 'label keeps display hash once')
assert(orderApiId(sample) === '847', 'api id prefers internal_id')

const fallback = {
  id: '#NHL-33-000004',
  order_number: '#NHL-33-000004',
}
assert(orderDetailPath(fallback) === '/orders/NHL-33-000004', 'fallback strips hash for path')
assert(orderApiId(fallback) === 'NHL-33-000004', 'fallback strips hash for api')

console.log('smoke-order-routes: OK')
