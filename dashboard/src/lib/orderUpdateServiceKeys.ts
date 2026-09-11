// Pure service classification contract: safe for UI helpers and dependency-free CI.
export type OrderUpdateServiceKey =
  | 'order_confirmation'
  | 'cod_confirmation'
  | 'payment_pending'
  | 'payment_confirmed'
  | 'order_preparing'
  | 'order_ready'
  | 'shipping_tracking'
  | 'out_for_delivery'
  | 'order_delivered'
  | 'order_cancelled'
  | 'order_refunded'

export const ORDER_UPDATE_SERVICE_KEYS: readonly OrderUpdateServiceKey[] = [
  'order_confirmation',
  'cod_confirmation',
  'payment_pending',
  'payment_confirmed',
  'order_preparing',
  'order_ready',
  'shipping_tracking',
  'out_for_delivery',
  'order_delivered',
  'order_cancelled',
  'order_refunded',
] as const

export function isOrderUpdateServiceKey(key: string): key is OrderUpdateServiceKey {
  return (ORDER_UPDATE_SERVICE_KEYS as readonly string[]).includes(key)
}
