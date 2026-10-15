/**
 * Local-only layout harness for scripts/check-conversation-bubble-layout.mts.
 *
 * Renders the real MessagePresentationCard (and, through it, the real
 * InboundMediaPreview) with the real Tailwind stylesheet inside a copy of the
 * Conversations.tsx message-list / bubble-column markup, so image frames,
 * bubble colours and contrast can be measured in Chromium. The driver checks
 * that every copied class string still exists in Conversations.tsx.
 * Synthetic content only; served by the Vite dev server, not bundled.
 */
import React from 'react'
import ReactDOM from 'react-dom/client'

import '../../../src/index.css'
import { setApiBaseOverride } from '../../../src/auth'
import MessagePresentationCard, {
  type MessagePresentationLabels,
} from '../../../src/components/conversations/MessagePresentationCard'
import type { DashboardMessageMedia, MessagePresentation } from '../../../src/api/featureReality'

const params = new URLSearchParams(window.location.search)
const api = params.get('api') || ''
setApiBaseOverride(api)
localStorage.setItem('nahla_token', params.get('token') || '')
localStorage.setItem('nahla_tenant_id', params.get('tenant') || '')
if (params.get('theme') === 'dark') document.documentElement.classList.add('dark')
if (params.get('dir') === 'ltr') {
  document.documentElement.dir = 'ltr'
  document.documentElement.lang = 'en'
}

const labels: MessagePresentationLabels = {
  template: 'قالب واتساب',
  lifecycle: 'تحديث طلب',
  product: 'منتج',
  available: 'متوفر',
  unavailable: 'غير متوفر',
  mediaUnavailable: 'تعذر عرض الوسائط',
  openProduct: 'عرض المنتج',
  openCatalog: 'فتح الكتالوج',
  openMedia: 'فتح المرفق',
}

const LONG_DESCRIPTION =
  'صورة اصطناعية للاختبار: قميص قطني أزرق معلّق على شمّاعة خشبية أمام خلفية فاتحة، مع ملصق مقاس واضح '
  + 'وسعر مكتوب بخط صغير أسفل الصورة، والإضاءة طبيعية من الجهة اليمنى دون ظلال قوية.'
const LONG_CAPTION =
  'مرحباً، هل يتوفر هذا القميص القطني الأزرق بمقاس كبير؟ وهل يمكن التوصيل إلى الرياض خلال يومين، '
  + 'وما هي طريقة الدفع المتاحة عند الاستلام؟ أرفقت صورة المنتج كما ظهرت في المتجر.'

interface Row {
  id: string
  isOut: boolean
  presentation: MessagePresentation
  legacyMedia?: DashboardMessageMedia | null
  failed?: boolean
  eventBadge?: { cls: string; label: string }
}

function inboundImage(id: string, file: string, opts: { caption?: string; description?: string; legacy?: boolean } = {}): Row {
  const url = `/media/inbound/77/${file}`
  const caption = opts.caption ?? null
  return {
    id,
    isOut: false,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'media',
      body: caption || '',
      text_direction: 'auto',
      media: { kind: 'image', url, mime_type: 'image/png', caption, load_state: 'ok' },
      actions: [],
    },
    legacyMedia: opts.legacy === false ? null : {
      kind: 'image',
      message_event_id: 1000 + id.length,
      storage_url: url,
      mime_type: 'image/png',
      description: opts.description ?? null,
      image_kind: null,
      vision_status: opts.description ? 'ok' : 'empty',
      download_status: 'ok',
      ai_used: false,
      caption,
      error: null,
    },
  }
}

function text(id: string, isOut: boolean, body: string): Row {
  return { id, isOut, presentation: { version: 'message_presentation_v1', kind: 'text', body, text_direction: 'auto', actions: [] } }
}

const publicImage = `${api}/public/summer-offer-1600x1200.png`

function template(id: string, extra: Partial<Row> = {}): Row {
  return {
    id,
    isOut: true,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'template',
      body: 'أهلاً نورة، عرض الصيف متاح الآن على عطر ورد 100ml بخصم 15٪ حتى نهاية الأسبوع. يسعدنا خدمتك في أي وقت.',
      text_direction: 'rtl',
      media: { kind: 'image', url: publicImage, caption: null },
      template: { name: 'summer_offer_ar', category: 'MARKETING', language: 'ar', footer: 'متجر تجريبي عام' },
      actions: [
        { kind: 'url', label: 'تسوق الآن', url: 'https://shop.example.test/offer' },
        { kind: 'quick_reply', label: 'إيقاف العروض', payload: 'STOP' },
      ],
    },
    ...extra,
  }
}

const rows: Row[] = [
  // The observed defect: portrait inbound image with long description + caption.
  inboundImage('in-portrait-long', 'portrait-1200x1600.png', { caption: LONG_CAPTION, description: LONG_DESCRIPTION }),
  inboundImage('in-landscape', 'landscape-1600x1200.png', { caption: 'صورة الحذاء الرياضي الأبيض' }),
  inboundImage('in-square', 'square-1200x1200.png'),
  inboundImage('in-wide', 'wide-1600x300.png', { description: LONG_DESCRIPTION }),
  inboundImage('in-tall', 'tall-300x1600.png'),
  // Protected path without a legacy media block (ProtectedInboundMedia).
  { ...inboundImage('in-portrait-protected', 'portrait-1200x1600.png', { legacy: false }),
    presentation: {
      version: 'message_presentation_v1', kind: 'media', body: LONG_CAPTION, text_direction: 'auto',
      media: { kind: 'image', url: '/media/inbound/77/portrait-1200x1600.png', load_state: 'ok' }, actions: [],
    } },
  text('in-text', false, 'مرحباً، أبغى أعرف حالة طلبي RRRD1234 لو سمحت.'),
  text('out-plain', true, 'أهلاً أحمد، طلبك RRRD1234 قيد التجهيز وسيتم شحنه اليوم.'),
  template('out-template', { eventBadge: { cls: 'bg-blue-50 text-blue-600 border-blue-200', label: 'حملة' } }),
  {
    id: 'out-lifecycle',
    isOut: true,
    eventBadge: { cls: 'bg-amber-50 text-amber-600 border-amber-200', label: 'أتمتة' },
    presentation: {
      version: 'message_presentation_v1',
      kind: 'order_lifecycle',
      body: 'تم شحن طلبك RRRD1234 مع شركة الشحن، ويمكنك تتبع الشحنة من الزر أدناه.',
      text_direction: 'rtl',
      template: { name: 'order_shipped', category: 'UTILITY', language: 'ar', footer: 'متجر تجريبي عام' },
      order: { id: '501', reference: 'RRRD1234', status: 'shipped', lifecycle: 'order_shipped' },
      actions: [
        { kind: 'tracking', label: 'تتبع الشحنة', url: 'https://track.example.test/RRRD1234' },
        { kind: 'quick_reply', label: 'شكراً', payload: 'THANKS' },
      ],
    },
  },
  template('out-template-failed', { failed: true }),
  {
    id: 'out-interactive',
    isOut: true,
    presentation: {
      version: 'message_presentation_v1', kind: 'interactive', body: 'هل ترغب بتأكيد الطلب؟', text_direction: 'rtl',
      actions: [{ kind: 'quick_reply', label: 'تأكيد', payload: 'YES' }, { kind: 'quick_reply', label: 'إلغاء', payload: 'NO' }],
    },
  },
  {
    id: 'out-media',
    isOut: true,
    presentation: {
      version: 'message_presentation_v1', kind: 'media', body: 'صورة المنتج المطلوب', text_direction: 'auto',
      media: { kind: 'image', url: publicImage, caption: null }, actions: [],
    },
  },
]

// Markup copied from Conversations.tsx (message list -> bubble column -> presentation stack).
function Bubble({ row }: { row: Row }) {
  const isOut = row.isOut
  return (
    <div className={`flex ${isOut ? 'justify-end' : 'justify-start'} mb-1`} data-case={row.id} data-direction={isOut ? 'out' : 'in'}>
      <div className={`flex flex-col ${isOut ? 'items-end' : 'items-start'} max-w-[78%] md:max-w-md`} data-bubble-column>
        {isOut && row.eventBadge && (
          <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border mb-0.5 ${row.eventBadge.cls}`} data-event-badge>
            {row.eventBadge.label}
          </span>
        )}
        <div
          className={`w-full space-y-1 ${isOut && row.failed ? 'ring-1 ring-red-200 rounded-2xl' : ''}`}
          data-presentation-stack
        >
          <MessagePresentationCard
            presentation={row.presentation}
            isOutbound={isOut}
            labels={labels}
            legacyMedia={row.legacyMedia ?? null}
          />
          {isOut && row.failed && (
            <span className="inline-flex px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider bg-red-500 text-white shadow-sm" data-not-sent-badge>
              لم تُرسل
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <div style={{ maxWidth: 760, margin: '0 auto' }} data-chat-pane>
    <div
      className="flex-1 overflow-y-auto py-4 px-3 md:px-5 space-y-1"
      style={{ background: 'linear-gradient(180deg, #f8f9fb 0%, #f1f3f6 100%)' }}
    >
      {rows.map(row => <Bubble key={row.id} row={row} />)}
    </div>
  </div>,
)
document.documentElement.dataset.harnessReady = '1'
