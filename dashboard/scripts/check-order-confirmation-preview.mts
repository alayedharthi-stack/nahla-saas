/**
 * Order updates preview helpers — unified pending-first source, no auto Nahla footer.
 *
 * Run: npm run check:order-confirmation-preview   (from dashboard/)
 */
import {
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
  buildOrderUpdatePreviewBody,
  resolveOrderUpdatePreview,
  resolvePreviewFooter,
  resolvePreviewHeaderImageUrl,
} from '../src/components/settings/orderUpdatesPreview.ts'
import { ACTIVE_TEXT_PENDING_IMAGE_DETAIL } from '../src/evidence/fixtures/activeTextPendingImageDetail.ts'

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

const R2_HEADER =
  'https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/platform/order-updates/order-confirmation-header-v1.jpg'

assert(
  resolvePreviewHeaderImageUrl({
    header_type: 'none',
    preview_header_image_url: R2_HEADER,
  }) === null,
  'text-only template must not show header image in preview',
)

assert(
  resolvePreviewHeaderImageUrl({
    header_type: 'image',
    preview_header_image_url: R2_HEADER,
  }) === R2_HEADER,
  'IMAGE revision exposes preview header URL',
)

assert(
  resolvePreviewFooter({ preview_footer: null }) === undefined,
  'missing FOOTER component yields no preview footer',
)

assert(
  resolvePreviewFooter({ preview_footer: 'متجر تجريبي عام' }) === 'متجر تجريبي عام',
  'FOOTER component text is shown when present',
)

const unified = resolveOrderUpdatePreview(
  ACTIVE_TEXT_PENDING_IMAGE_DETAIL,
  ['customer_name', 'order_number', 'order_total', 'store_name'],
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
)
assert(unified.headerImageUrl === R2_HEADER, 'fixture pending IMAGE shows header')
assert(unified.body.includes('مسودة IMAGE'), 'fixture preview body follows pending draft')
assert(unified.body.includes('أحمد'), 'fixture substitutes variables')
assert(unified.footer === undefined, 'fixture pending r3 has no FOOTER')

const activeTextBody = buildOrderUpdatePreviewBody(
  'نسخة نشطة نصية {{1}} رقم {{2}}',
  ['customer_name', 'order_number'],
  ORDER_CONFIRMATION_PREVIEW_SAMPLES,
)
assert(activeTextBody.includes('أحمد'), 'active text body still renders for comparison')
assert(!activeTextBody.includes('مسودة IMAGE'), 'active body is distinct from pending draft')

console.log('check-order-confirmation-preview: OK')
