import { useState } from 'react'
import { ExternalLink, ImageOff, PackageCheck } from 'lucide-react'

import type {
  DashboardMessageMedia,
  MessagePresentation,
  MessagePresentationAction,
} from '../../api/featureReality'
import { resolvePresentationMediaSource } from '../../lib/inboundMediaSource'
import InboundMediaPreview, { ProtectedInboundMedia } from '../inbound/InboundMediaPreview'

export interface MessagePresentationLabels {
  template: string
  lifecycle: string
  product: string
  available: string
  unavailable: string
  mediaUnavailable: string
  openProduct: string
  openCatalog: string
  openMedia: string
}

function actionLabel(action: MessagePresentationAction, labels: MessagePresentationLabels): string {
  if (action.kind === 'open_product') return labels.openProduct
  if (action.kind === 'open_catalog') return labels.openCatalog
  return action.label
}

function PresentationAction({
  action,
  labels,
  onOutboundBubble,
}: {
  action: MessagePresentationAction
  labels: MessagePresentationLabels
  onOutboundBubble: boolean
}) {
  // White buttons on the brand bubble use a dark brand label (AA on white);
  // every other bubble keeps the existing WhatsApp-green label.
  const tone = onOutboundBubble
    ? 'text-brand-800 dark:text-brand-300 hover:bg-brand-50 dark:hover:bg-slate-700'
    : 'text-[#00a884] hover:bg-emerald-50'
  const content = (
    <>
      {(action.kind === 'url' || action.kind === 'open_product' || action.kind === 'tracking') && (
        <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
      )}
      <span className="truncate">{actionLabel(action, labels)}</span>
    </>
  )
  if (action.url) {
    return (
      <a
        href={action.url}
        target="_blank"
        rel="noopener noreferrer"
        className={`flex items-center justify-center gap-2 rounded-lg border border-slate-100 bg-white px-3 py-2 text-[13px] font-medium shadow-sm ${tone}`}
        data-presentation-action={action.kind}
      >
        {content}
      </a>
    )
  }
  return (
    <div
      className={`flex cursor-default select-none items-center justify-center gap-2 rounded-lg border border-slate-100 bg-white px-3 py-2 text-[13px] font-medium shadow-sm ${onOutboundBubble ? 'text-brand-800 dark:text-brand-300' : 'text-[#00a884]'}`}
      data-presentation-action={action.kind}
    >
      {content}
    </div>
  )
}

function SafeImage({
  src,
  alt,
  fallback,
  onLoad,
}: {
  src?: string | null
  alt: string
  fallback: string
  onLoad?: () => void
}) {
  if (!src) {
    return (
      <div className="flex min-h-24 items-center justify-center gap-2 rounded-xl bg-slate-100 px-4 text-xs text-slate-600">
        <ImageOff className="h-4 w-4" aria-hidden />
        {fallback}
      </div>
    )
  }
  return (
    <div className="aspect-[4/3] min-h-24 overflow-hidden rounded-xl bg-slate-100">
      <img
        src={src}
        alt={alt}
        className="block h-full max-h-72 w-full object-contain bg-white"
        loading="lazy"
        onLoad={onLoad}
        onError={(event) => {
          // `hidden` loses to the `block` utility, so hide via inline style.
          event.currentTarget.style.display = 'none'
          const fallbackElement = event.currentTarget.nextElementSibling as HTMLElement | null
          if (fallbackElement) fallbackElement.style.display = 'flex'
          onLoad?.()
        }}
      />
      <div style={{ display: 'none' }} className="h-full min-h-24 items-center justify-center gap-2 px-4 text-xs text-slate-600">
        <ImageOff className="h-4 w-4" aria-hidden />
        {fallback}
      </div>
    </div>
  )
}

/**
 * Template header image. SafeImage's fixed 4:3 frame + object-contain letterboxes
 * any other ratio (a 16:9 header at 426px wide renders 239.6px of image in a
 * 426x319.5 frame). Here the frame hugs the loaded image at its natural aspect
 * ratio, scaled down to the bubble width and a 288px height cap, never cropped
 * or trimmed — no corner rounding / overflow clip on the loaded image, since the
 * frame now meets the artwork's own edges. While loading, a 16:9 placeholder
 * (the common header ratio) holds the space; a failed source shows the
 * media-unavailable state, and a new source starts loading again.
 */
function TemplateHeaderImage({
  src,
  alt,
  fallback,
  onLoad,
}: {
  src?: string | null
  alt: string
  fallback: string
  onLoad?: () => void
}) {
  const [settled, setSettled] = useState<{ src: string; status: 'loaded' | 'error' } | null>(null)
  if (!src) return <SafeImage src={src} alt={alt} fallback={fallback} onLoad={onLoad} />
  const status = settled?.src === src ? settled.status : 'loading'
  if (status === 'error') {
    return (
      <div
        className="flex min-h-24 items-center justify-center gap-2 rounded-xl bg-slate-100 px-4 text-xs text-slate-600"
        data-template-header-image="error"
      >
        <ImageOff className="h-4 w-4" aria-hidden />
        {fallback}
      </div>
    )
  }
  return (
    <div
      className={status === 'loaded' ? 'w-fit max-w-full' : 'aspect-video w-full overflow-hidden rounded-xl bg-slate-100'}
      data-template-header-image={status}
    >
      <img
        key={src}
        src={src}
        alt={alt}
        className="block h-auto w-auto max-h-72 max-w-full bg-white"
        loading="lazy"
        onLoad={() => {
          setSettled({ src, status: 'loaded' })
          onLoad?.()
        }}
        onError={() => {
          setSettled({ src, status: 'error' })
          onLoad?.()
        }}
      />
    </div>
  )
}

function PresentationMedia({
  media,
  labels,
  onLoad,
  templateHeader = false,
}: {
  media: NonNullable<MessagePresentation['media']>
  labels: MessagePresentationLabels
  onLoad?: () => void
  templateHeader?: boolean
}) {
  if (media.kind === 'image') {
    const Image = templateHeader ? TemplateHeaderImage : SafeImage
    return (
      <Image
        src={media.url}
        alt={media.caption || labels.template}
        fallback={labels.mediaUnavailable}
        onLoad={onLoad}
      />
    )
  }
  if (!media.url) {
    return (
      <div className="flex min-h-20 items-center justify-center gap-2 rounded-xl bg-slate-100 px-4 text-xs text-slate-600">
        <ImageOff className="h-4 w-4" aria-hidden />
        {labels.mediaUnavailable}
      </div>
    )
  }
  if (media.kind === 'audio') {
    return <audio controls preload="none" className="w-full" src={media.url} onLoadedMetadata={onLoad} />
  }
  if (media.kind === 'video') {
    return (
      <video controls preload="metadata" className="aspect-video max-h-72 w-full rounded-xl bg-black" src={media.url} onLoadedMetadata={onLoad} />
    )
  }
  return (
    <a
      href={media.url}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-2 rounded-xl bg-slate-100 px-3 py-3 text-xs font-medium text-slate-700 hover:bg-slate-200"
    >
      <ExternalLink className="h-4 w-4" aria-hidden />
      <span className="truncate">{media.filename || media.caption || labels.openMedia}</span>
    </a>
  )
}

export default function MessagePresentationCard({
  presentation,
  isOutbound,
  labels,
  legacyMedia,
  onMediaLoad,
}: {
  presentation: MessagePresentation
  isOutbound: boolean
  labels: MessagePresentationLabels
  legacyMedia?: DashboardMessageMedia | null
  onMediaLoad?: () => void
}) {
  const product = presentation.product
  const media = presentation.media
  const lifecycle = presentation.kind === 'order_lifecycle'
  const isTemplate = lifecycle || presentation.kind === 'template'
  const plainText = presentation.kind === 'text' && !presentation.media && presentation.actions.length === 0
  // Outbound WhatsApp templates (order-lifecycle templates included) are
  // ordinary outbound messages, so they share the outbound text bubble.
  // Dark ink on the amber bubble: white on brand-500 is only ~2.1:1, while
  // slate-950 is ~9.5:1 and is not remapped by the dark-mode retrofit.
  const outboundBubble = isOutbound && (plainText || isTemplate)
  const cardTheme = outboundBubble
    ? 'border-brand-500 bg-brand-500 text-slate-950'
    : isOutbound
      ? 'border-amber-100 bg-amber-50 text-slate-800'
      : 'border-slate-100 bg-white text-slate-800'
  const radius = outboundBubble
    ? 'rounded-2xl rounded-ee-sm'
    : plainText ? 'rounded-2xl rounded-es-sm' : 'rounded-2xl'
  // Template label / footer / order meta and dividers on the outbound bubble.
  const metaText = outboundBubble ? 'text-slate-950' : 'text-slate-500'
  const divider = outboundBubble ? 'border-black/10' : 'border-black/5'
  const productImage = product?.image_url || media?.url
  // Protected inbound paths (/media/inbound/...) are only loadable through the
  // authenticated blob fetch; never bind them to a bare <img>/<audio>/<a> src.
  const mediaSource = media ? resolvePresentationMediaSource(media, legacyMedia) : null
  const inboundPreviewMedia = mediaSource === 'inbound_preview' ? legacyMedia ?? null : null
  // InboundMediaPreview already renders the caption under the media.
  const bodyRepeatsInboundCaption = Boolean(
    inboundPreviewMedia?.caption && inboundPreviewMedia.caption.trim() === presentation.body.trim(),
  )

  return (
    <div
      className={`overflow-hidden border shadow-sm ${radius} ${cardTheme}`}
      dir={presentation.text_direction || 'auto'}
      data-presentation-kind={presentation.kind}
    >
      {(isTemplate || presentation.kind === 'product') && (
        <div className={`flex items-center gap-1.5 border-b px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide ${divider} ${metaText}`}>
          {lifecycle ? <PackageCheck className="h-3 w-3" aria-hidden /> : null}
          <span>{lifecycle ? labels.lifecycle : presentation.kind === 'product' ? labels.product : labels.template}</span>
          {presentation.template?.name && (
            <span className={`truncate font-mono font-normal normal-case ${outboundBubble ? '' : 'opacity-70'}`}>· {presentation.template.name}</span>
          )}
        </div>
      )}

      <div className="space-y-2 p-2.5">
        {presentation.kind === 'product' ? (
          <SafeImage
            src={productImage}
            alt={product?.name || labels.product}
            fallback={labels.mediaUnavailable}
            onLoad={onMediaLoad}
          />
        ) : inboundPreviewMedia ? (
          <InboundMediaPreview media={inboundPreviewMedia} />
        ) : media && mediaSource === 'protected_fetch' && media.url ? (
          <ProtectedInboundMedia
            kind={media.kind}
            storageUrl={media.url}
            filename={media.filename}
            alt={media.caption || labels.template}
            unavailableLabel={labels.mediaUnavailable}
            openLabel={labels.openMedia}
            onLoad={onMediaLoad}
          />
        ) : media ? (
          <PresentationMedia media={media} labels={labels} onLoad={onMediaLoad} templateHeader={isTemplate} />
        ) : legacyMedia ? (
          <InboundMediaPreview media={legacyMedia} />
        ) : null}

        {product?.name && (
          <div className="px-1">
            <div className="font-semibold text-slate-900">{product.name}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-600">
              {product.price && (
                <span className="font-medium">
                  {product.price} {product.currency || ''}
                </span>
              )}
              {typeof product.availability === 'boolean' && (
                <span className={product.availability ? 'text-emerald-700' : 'text-rose-600'}>
                  {product.availability ? labels.available : labels.unavailable}
                </span>
              )}
            </div>
          </div>
        )}

        {presentation.body && !bodyRepeatsInboundCaption && (
          <div className="whitespace-pre-wrap break-words px-1 text-sm leading-relaxed">
            {presentation.body}
          </div>
        )}
        {presentation.template?.footer && (
          <div className={`border-t px-1 pt-2 text-[11px] ${divider} ${metaText}`}>
            {presentation.template.footer}
          </div>
        )}
        {presentation.order && (presentation.order.reference || presentation.order.status) && (
          <div className={`flex flex-wrap gap-2 border-t px-1 pt-2 text-[10px] font-medium ${divider} ${metaText}`}>
            {presentation.order.reference && (
              <span>{presentation.order.reference.startsWith('#') ? presentation.order.reference : `#${presentation.order.reference}`}</span>
            )}
            {presentation.order.status && <span>{presentation.order.status}</span>}
          </div>
        )}
      </div>

      {presentation.actions.length > 0 && (
        <div className={`space-y-[3px] border-t p-[3px] ${divider} ${outboundBubble ? 'bg-black/10' : 'bg-slate-50/70'}`}>
          {presentation.actions.map((action, index) => (
            <PresentationAction
              key={`${action.kind}-${action.label}-${index}`}
              action={action}
              labels={labels}
              onOutboundBubble={outboundBubble}
            />
          ))}
        </div>
      )}
    </div>
  )
}
