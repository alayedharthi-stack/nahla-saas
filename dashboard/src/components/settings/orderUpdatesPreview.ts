import type { OrderUpdateServiceDetail, OrderUpdateServiceKey } from '../../api/orderUpdates'

export const ORDER_CONFIRMATION_PREVIEW_SAMPLES: Record<string, string> = {
  customer_name: 'أحمد',
  order_number: '12345',
  order_id: '12345',
  order_total: '350',
  store_name: 'متجر تجريبي عام',
}

export function buildOrderUpdatePreviewBody(
  text: string,
  variableKeys: string[],
  samples: Record<string, string> = ORDER_CONFIRMATION_PREVIEW_SAMPLES,
): string {
  let out = text
  variableKeys.forEach((key, idx) => {
    const sample = samples[key] ?? `[${key}]`
    out = out.split(`{{${key}}}`).join(sample)
    out = out.split(`{{${idx + 1}}}`).join(sample)
  })
  return out
}

/** Meta utility templates have no Nahla assistant footer in merchant preview. */
export function resolvePreviewFooter(
  serviceKey: OrderUpdateServiceKey | string,
  detail: Pick<OrderUpdateServiceDetail, 'preview_footer'> | null,
  isAr: boolean,
): string | undefined {
  if (serviceKey === 'order_confirmation') {
    return detail?.preview_footer ?? undefined
  }
  return detail?.preview_footer ?? (isAr ? 'نحلة — مساعد متجرك' : 'Nahla — your store assistant')
}

/** IMAGE header preview only when the active/pending template has an IMAGE header. */
export function resolvePreviewHeaderImageUrl(
  serviceKey: OrderUpdateServiceKey | string,
  detail: Pick<OrderUpdateServiceDetail, 'header_type' | 'preview_header_image_url'> | null,
): string | null {
  if (serviceKey !== 'order_confirmation') return null
  if (String(detail?.header_type ?? 'none').toLowerCase() !== 'image') return null
  return detail?.preview_header_image_url ?? null
}
