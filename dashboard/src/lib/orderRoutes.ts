/**
 * Order list rows expose `id` as the human-visible number (often "#NHL-…").
 * React Router paths must use the DB pk (`internal_id`) — a leading "#"
 * in the path is interpreted as a URL fragment, not a route segment.
 */

export function formatOrderNumberLabel(order: {
  order_number?: string
  id?: string
}): string {
  const raw = String(order.order_number || order.id || '').trim()
  if (!raw) return '—'
  return raw.startsWith('#') ? raw : `#${raw}`
}

/** Path for OrderDetail — always avoids "#" in the pathname. */
export function orderDetailPath(order: {
  internal_id?: string
  order_number?: string
  id?: string
}): string {
  const internal = String(order.internal_id || '').trim()
  if (internal) {
    return `/orders/${internal}`
  }
  const raw = String(order.order_number || order.id || '').trim().replace(/^#/, '')
  return raw ? `/orders/${encodeURIComponent(raw)}` : '/orders'
}

/** Id passed to GET /orders/{id} and mutation endpoints. */
export function orderApiId(order: {
  internal_id?: string
  order_number?: string
  id?: string
}): string {
  const internal = String(order.internal_id || '').trim()
  if (internal) return internal
  return String(order.order_number || order.id || '').trim().replace(/^#/, '')
}
