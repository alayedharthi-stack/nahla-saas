/**
 * Local-only harness: mounts the real MessagePresentationCard with
 * synthetic message rows so scripts/check-inbound-media-presentation.mts
 * can observe, in Chromium, which origin each media request goes to and
 * whether the bytes decode. Served by the Vite dev server only; it is not
 * part of the production bundle (vite build only takes index.html).
 */
import React from 'react'
import ReactDOM from 'react-dom/client'

import MessagePresentationCard, {
  type MessagePresentationLabels,
} from '../../../src/components/conversations/MessagePresentationCard'
import type {
  DashboardMessageMedia,
  MessagePresentation,
} from '../../../src/api/featureReality'
import {
  MEDIA_PATHS,
  PUBLIC_PRODUCT_IMAGE,
  PUBLIC_TEMPLATE_IMAGE,
  PUBLIC_TRACKING_URL,
} from './cases'

const labels: MessagePresentationLabels = {
  template: 'WhatsApp template',
  lifecycle: 'Order update',
  product: 'Product',
  available: 'Available',
  unavailable: 'Unavailable',
  mediaUnavailable: 'Media unavailable',
  openProduct: 'View product',
  openCatalog: 'Open catalog',
  openMedia: 'Open attachment',
}

interface HarnessCase {
  id: string
  isOutbound: boolean
  presentation: MessagePresentation
  legacyMedia?: DashboardMessageMedia | null
}

let nextEventId = 9100

function inboundMediaPresentation(
  kind: 'image' | 'audio' | 'video' | 'document',
  url: string | null,
  body: string,
  extra: { mime_type?: string; filename?: string | null; caption?: string | null } = {},
): MessagePresentation {
  return {
    version: 'message_presentation_v1',
    kind: 'media',
    body,
    text_direction: 'auto',
    media: {
      kind,
      url,
      mime_type: extra.mime_type ?? null,
      caption: extra.caption ?? null,
      filename: extra.filename ?? null,
      load_state: 'ok',
      error: null,
    },
    actions: [],
  }
}

function legacyImage(storage_url: string, caption: string | null = null): DashboardMessageMedia {
  return {
    kind: 'image',
    message_event_id: nextEventId++,
    storage_url,
    mime_type: 'image/png',
    description: 'قميص قطني أزرق على خلفية بيضاء',
    image_kind: 'product_photo',
    vision_status: 'ok',
    download_status: 'ok',
    ai_used: false,
    caption,
    error: null,
  }
}

function legacyAudio(storage_url: string): DashboardMessageMedia {
  return {
    kind: 'audio',
    message_event_id: nextEventId++,
    storage_url,
    mime_type: 'audio/wav',
    duration: 1,
    voice: true,
    transcript: 'هل يتوفر الحذاء الرياضي الأبيض مقاس 42؟',
    transcript_status: 'ok',
    download_status: 'ok',
    ai_used: false,
    caption: null,
    error: null,
  }
}

function legacyVideo(storage_url: string): DashboardMessageMedia {
  return {
    kind: 'video',
    message_event_id: nextEventId++,
    storage_url,
    mime_type: 'video/webm',
    duration: 1,
    download_status: 'ok',
    caption: null,
    filename: 'unboxing.webm',
    forwarded: false,
    frequently_forwarded: false,
    error: null,
  }
}

function legacyDocument(storage_url: string): DashboardMessageMedia {
  return {
    kind: 'document',
    message_event_id: nextEventId++,
    storage_url,
    mime_type: 'application/pdf',
    filename: 'invoice-RRRD1234.pdf',
    byte_size: 512,
    download_status: 'ok',
    pdf_kind: 'invoice',
    pdf_text_status: 'ok',
    summary: 'فاتورة تجريبية لطلب عطر ورد 100ml',
    payment_evidence_hints: null,
    payment_evidence_status: null,
    caption: null,
    error: null,
  }
}

const IMAGE_CAPTION = 'هل هذا القميص القطني الأزرق متوفر؟'

const mainCases: HarnessCase[] = [
  // ── The production path: canonical presentation.media + legacy media block ──
  {
    id: 'image-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.imageWithLegacy, IMAGE_CAPTION, {
      mime_type: 'image/png',
      caption: IMAGE_CAPTION,
    }),
    legacyMedia: legacyImage(MEDIA_PATHS.imageWithLegacy, IMAGE_CAPTION),
  },
  {
    id: 'audio-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('audio', MEDIA_PATHS.audioWithLegacy, '', { mime_type: 'audio/wav' }),
    legacyMedia: legacyAudio(MEDIA_PATHS.audioWithLegacy),
  },
  {
    id: 'video-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('video', MEDIA_PATHS.videoWithLegacy, '', { mime_type: 'video/webm' }),
    legacyMedia: legacyVideo(MEDIA_PATHS.videoWithLegacy),
  },
  {
    id: 'document-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('document', MEDIA_PATHS.documentWithLegacy, '', {
      mime_type: 'application/pdf',
      filename: 'invoice-RRRD1234.pdf',
    }),
    legacyMedia: legacyDocument(MEDIA_PATHS.documentWithLegacy),
  },
  // ── Canonical presentation.media with no legacy block at all ──
  {
    id: 'image-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.imageWithoutLegacy, 'صورة حذاء رياضي أبيض', {
      mime_type: 'image/png',
    }),
    legacyMedia: null,
  },
  {
    id: 'audio-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('audio', MEDIA_PATHS.audioWithoutLegacy, ''),
  },
  {
    id: 'video-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('video', MEDIA_PATHS.videoWithoutLegacy, ''),
  },
  {
    id: 'document-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('document', MEDIA_PATHS.documentWithoutLegacy, '', {
      filename: 'catalog-sample.pdf',
    }),
  },
  // ── Legacy-only branch (no presentation.media) stays as before ──
  {
    id: 'legacy-only-image',
    isOutbound: false,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'text',
      body: '',
      text_direction: 'auto',
      actions: [],
    },
    legacyMedia: legacyImage(MEDIA_PATHS.legacyOnlyImage),
  },
  // ── Presentation media without a URL, legacy block carries the stored path ──
  {
    id: 'missing-url-audio',
    isOutbound: false,
    presentation: inboundMediaPresentation('audio', null, ''),
    legacyMedia: legacyAudio(MEDIA_PATHS.missingUrlAudio),
  },
  // ── Graceful failure states, on both the legacy-backed and the bare path ──
  {
    id: 'html200-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.htmlImage, ''),
    legacyMedia: legacyImage(MEDIA_PATHS.htmlImage),
  },
  {
    id: 'html200-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.htmlImage, ''),
  },
  {
    id: 'html200-document-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('document', MEDIA_PATHS.htmlDocument, '', { filename: 'statement.pdf' }),
  },
  {
    id: 'http401-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.expiredImage, ''),
    legacyMedia: legacyImage(MEDIA_PATHS.expiredImage),
  },
  {
    id: 'http401-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.expiredImage, ''),
  },
  {
    id: 'http403-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.crossTenantImage, ''),
    legacyMedia: legacyImage(MEDIA_PATHS.crossTenantImage),
  },
  {
    id: 'http403-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.crossTenantImage, ''),
  },
  {
    id: 'http404-with-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.missingImage, ''),
    legacyMedia: legacyImage(MEDIA_PATHS.missingImage),
  },
  {
    id: 'http404-without-legacy',
    isOutbound: false,
    presentation: inboundMediaPresentation('image', MEDIA_PATHS.missingImage, ''),
  },
  // ── Public/external media and non-media cards must be untouched ──
  {
    id: 'public-template-image',
    isOutbound: true,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'template',
      body: 'عرض الصيف على قميص قطني أزرق',
      text_direction: 'auto',
      media: { kind: 'image', url: PUBLIC_TEMPLATE_IMAGE, caption: null },
      template: { name: 'summer_offer', category: 'MARKETING', language: 'ar', footer: 'متجر تجريبي عام' },
      actions: [
        { kind: 'url', label: 'تسوق الآن', url: `${PUBLIC_TEMPLATE_IMAGE}?cta=1` },
        { kind: 'quick_reply', label: 'إيقاف العروض', payload: 'STOP_PROMOS' },
      ],
    },
  },
  {
    id: 'product-card',
    isOutbound: true,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'product',
      body: '',
      text_direction: 'auto',
      product: {
        id: 'p-1',
        retailer_id: 'SKU-WHITE-42',
        name: 'حذاء رياضي أبيض',
        image_url: PUBLIC_PRODUCT_IMAGE,
        price: '199',
        currency: 'SAR',
        availability: true,
        url: 'https://shop.public-media.example/p/white-sneaker',
      },
      actions: [{ kind: 'open_product', label: 'View', url: 'https://shop.public-media.example/p/white-sneaker' }],
    },
  },
  {
    id: 'salla-order-lifecycle',
    isOutbound: true,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'order_lifecycle',
      body: 'تم شحن طلبك RRRD1234',
      text_direction: 'rtl',
      template: { name: 'order_shipped', category: 'UTILITY', language: 'ar', service_key: 'order_shipped' },
      order: { id: '501', reference: 'RRRD1234', status: 'shipped', lifecycle: 'order_shipped' },
      actions: [
        { kind: 'tracking', label: 'تتبع الشحنة', url: PUBLIC_TRACKING_URL },
        { kind: 'quick_reply', label: 'شكراً', payload: 'THANKS' },
      ],
    },
  },
  {
    id: 'agent-text',
    isOutbound: true,
    presentation: {
      version: 'message_presentation_v1',
      kind: 'text',
      body: 'أهلاً نورة، طلب عطر ورد 100ml قيد التجهيز.',
      text_direction: 'rtl',
      actions: [],
    },
  },
]

const slowCase: HarnessCase = {
  id: 'slow-unmount',
  isOutbound: false,
  presentation: inboundMediaPresentation('image', MEDIA_PATHS.slowImage, ''),
  legacyMedia: legacyImage(MEDIA_PATHS.slowImage),
}

function CaseList({ cases }: { cases: HarnessCase[] }) {
  return (
    <div style={{ display: 'grid', gap: 12, padding: 12, maxWidth: 420 }}>
      {cases.map(c => (
        <section key={c.id} data-case={c.id} data-direction={c.isOutbound ? 'out' : 'in'}>
          <MessagePresentationCard
            presentation={c.presentation}
            isOutbound={c.isOutbound}
            labels={labels}
            legacyMedia={c.legacyMedia ?? null}
          />
        </section>
      ))}
    </div>
  )
}

const mainRoot = ReactDOM.createRoot(document.getElementById('main-root')!)
const slowRoot = ReactDOM.createRoot(document.getElementById('slow-root')!)

declare global {
  interface Window {
    __inboundMediaHarness?: {
      mountSlow: () => void
      unmountSlow: () => void
      unmountMain: () => void
    }
  }
}

window.__inboundMediaHarness = {
  mountSlow: () => slowRoot.render(
    <React.StrictMode>
      <CaseList cases={[slowCase]} />
    </React.StrictMode>,
  ),
  unmountSlow: () => slowRoot.unmount(),
  unmountMain: () => mainRoot.unmount(),
}

mainRoot.render(
  <React.StrictMode>
    <CaseList cases={mainCases} />
  </React.StrictMode>,
)
