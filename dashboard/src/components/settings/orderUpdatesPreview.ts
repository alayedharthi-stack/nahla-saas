import type { OrderUpdateServiceDetail } from '../../api/orderUpdates'

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

/** Footer only when the selected template revision includes a FOOTER component. */
export function resolvePreviewFooter(
  detail: Pick<OrderUpdateServiceDetail, 'preview_footer'> | null,
): string | undefined {
  const footer = detail?.preview_footer
  if (typeof footer === 'string' && footer.trim()) return footer.trim()
  return undefined
}

/** IMAGE header preview only when the selected revision has header_type=image. */
export function resolvePreviewHeaderImageUrl(
  detail: Pick<OrderUpdateServiceDetail, 'header_type' | 'preview_header_image_url'> | null,
): string | null {
  if (String(detail?.header_type ?? 'none').toLowerCase() !== 'image') return null
  return detail?.preview_header_image_url ?? null
}

export function resolveOrderUpdatePreview(
  detail: OrderUpdateServiceDetail | null,
  variableKeys: string[],
  samples: Record<string, string>,
) {
  const rawBody = (detail?.body_text ?? detail?.message_text ?? '').trim()
  return {
    body: buildOrderUpdatePreviewBody(rawBody, variableKeys, samples),
    footer: resolvePreviewFooter(detail),
    headerImageUrl: resolvePreviewHeaderImageUrl(detail),
  }
}
