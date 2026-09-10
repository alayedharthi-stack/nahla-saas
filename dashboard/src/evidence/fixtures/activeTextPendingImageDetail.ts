import type { OrderUpdateServiceDetail } from '../../api/orderUpdates'

/**
 * Mirrors backend test_preview_follows_pending_image_over_active_text.
 * Active = text-only; pending draft = IMAGE r3 — preview must follow pending.
 */
export const R2_ORDER_CONFIRMATION_HEADER =
  'https://pub-6c51fa068bbe49fa98f4444e88aeb093.r2.dev/platform/order-updates/order-confirmation-header-v1.jpg'

export const ACTIVE_TEXT_PENDING_IMAGE_DETAIL: OrderUpdateServiceDetail = {
  service_key: 'order_confirmation',
  enabled: true,
  body_text:
    'مسودة IMAGE {{1}} 📦\n\nمن {{4}}\nرقم الطلب: #{{2}}\nالمبلغ: {{3}} ريال',
  message_text:
    'مسودة IMAGE {{1}} 📦\n\nمن {{4}}\nرقم الطلب: #{{2}}\nالمبلغ: {{3}} ريال',
  header_type: 'image',
  preview_header_image_url: R2_ORDER_CONFIRMATION_HEADER,
  preview_footer: null,
  meta_status: 'DRAFT',
  variables: ['customer_name', 'order_number', 'order_total', 'store_name'],
  approved_revision: {
    id: 1,
    body_text: 'نسخة نشطة نصية {{1}} رقم {{2}}',
    status: 'APPROVED',
    label: 'r1',
  },
  pending_revision: {
    id: 2,
    body_text:
      'مسودة IMAGE {{1}} 📦\n\nمن {{4}}\nرقم الطلب: #{{2}}\nالمبلغ: {{3}} ريال',
    status: 'DRAFT',
    label: 'r2',
  },
}
