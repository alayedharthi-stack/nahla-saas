/**
 * Order confirmation preview helpers — text-only vs IMAGE r3, no Nahla footer.
 *
 * Run: npm run check:order-confirmation-preview   (from dashboard/)
 */
import {
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
  buildOrderUpdatePreviewBody,
  resolvePreviewFooter,
  resolvePreviewHeaderImageUrl,
} from '../src/components/settings/orderUpdatesPreview.ts'

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

const R2_HEADER =
  'https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/platform/order-updates/order-confirmation-header-v1.jpg'

const r3Body =
  'تم استلام طلبك يا {{1}} 📦\n\nمن {{4}}\nرقم الطلب: #{{2}}\nالمبلغ الإجمالي: {{3}} ريال\n\nسنبدأ تجهيز طلبك فوراً ونُعلمك بكل جديد.'

const rendered = buildOrderUpdatePreviewBody(
  r3Body,
  ['customer_name', 'order_number', 'order_total', 'store_name'],
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
)
assert(rendered.includes('أحمد'), 'r3 preview substitutes customer_name')
assert(rendered.includes('متجر تجريبي عام'), 'r3 preview substitutes store_name')
assert(rendered.includes('350'), 'r3 preview substitutes order_total')

assert(
  resolvePreviewHeaderImageUrl('order_confirmation', {
    header_type: 'none',
    preview_header_image_url: R2_HEADER,
  }) === null,
  'text-only template must not show header image in preview',
)

assert(
  resolvePreviewHeaderImageUrl('order_confirmation', {
    header_type: 'image',
    preview_header_image_url: R2_HEADER,
  }) === R2_HEADER,
  'IMAGE r3 exposes preview header URL',
)

assert(
  resolvePreviewFooter('order_confirmation', { preview_footer: null }, true) === undefined,
  'order_confirmation preview has no Nahla footer',
)

assert(
  resolvePreviewFooter('shipping_tracking', null, true) === 'نحلة — مساعد متجرك',
  'other lifecycle services keep Nahla footer default',
)

console.log('check-order-confirmation-preview: OK')
