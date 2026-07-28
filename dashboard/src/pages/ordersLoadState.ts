import type { OrdersDashboard } from '../api/featureReality'

export const EMPTY_ORDERS_DASHBOARD: OrdersDashboard = {
  summary: {
    total_orders: 0,
    today_revenue_sar: 0,
    pending_orders: 0,
    completed_today: 0,
    whatsapp_orders_today: 0,
    whatsapp_revenue_today: 0,
    orders_needing_action: 0,
  },
  orders: [],
}

/** Only apply in-flight responses that still match the latest tab/reload request. */
export function shouldApplyOrdersRequest(
  requestId: number,
  latestRequestId: number,
  cancelled: boolean,
): boolean {
  return !cancelled && requestId === latestRequestId
}

export function ordersStatDisplay(
  value: number,
  loadError: string | null,
): string {
  if (loadError) return '—'
  return String(value)
}

export function ordersRevenueDisplay(
  value: number,
  loadError: string | null,
  locale: string,
  currency: string,
): string {
  if (loadError) return '—'
  return `${value.toLocaleString(locale)} ${currency}`
}
