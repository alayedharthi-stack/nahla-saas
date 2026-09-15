import { ExternalLink, ImageOff, PackageCheck } from 'lucide-react'

import type {
  DashboardMessageMedia,
  MessagePresentation,
  MessagePresentationAction,
} from '../../api/featureReality'
import InboundMediaPreview from '../inbound/InboundMediaPreview'

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
}: {
  action: MessagePresentationAction
  labels: MessagePresentationLabels
}) {
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
        className="flex items-center justify-center gap-2 rounded-lg border border-slate-100 bg-white px-3 py-2 text-[13px] font-medium text-[#00a884] shadow-sm hover:bg-emerald-50"
        data-presentation-action={action.kind}
      >
        {content}
      </a>
    )
  }
  return (
    <div
      className="flex cursor-default select-none items-center justify-center gap-2 rounded-lg border border-slate-100 bg-white px-3 py-2 text-[13px] font-medium text-[#00a884] shadow-sm"
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
      <div className="flex min-h-24 items-center justify-center gap-2 rounded-xl bg-slate-100 px-4 text-xs text-slate-500">
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
          event.currentTarget.hidden = true
          const fallbackElement = event.currentTarget.nextElementSibling as HTMLElement | null
          if (fallbackElement) fallbackElement.style.display = 'flex'
          onLoad?.()
        }}
      />
      <div style={{ display: 'none' }} className="h-full min-h-24 items-center justify-center gap-2 px-4 text-xs text-slate-500">
        <ImageOff className="h-4 w-4" aria-hidden />
        {fallback}
      </div>
    </div>
  )
}

function PresentationMedia({
  media,
  labels,
  onLoad,
}: {
  media: NonNullable<MessagePresentation['media']>
  labels: MessagePresentationLabels
  onLoad?: () => void
}) {
  if (media.kind === 'image') {
    return (
      <SafeImage
        src={media.url}
        alt={media.caption || labels.template}
        fallback={labels.mediaUnavailable}
        onLoad={onLoad}
      />
    )
  }
  if (!media.url) {
    return (
      <div className="flex min-h-20 items-center justify-center gap-2 rounded-xl bg-slate-100 px-4 text-xs text-slate-500">
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
  const cardTheme = isOutbound && plainText
    ? 'border-brand-500 bg-brand-500 text-white'
    : isOutbound
      ? 'border-amber-100 bg-amber-50 text-slate-800'
      : 'border-slate-100 bg-white text-slate-800'
  const radius = plainText
    ? (isOutbound ? 'rounded-2xl rounded-ee-sm' : 'rounded-2xl rounded-es-sm')
    : 'rounded-2xl'
  const productImage = product?.image_url || media?.url

  return (
    <div
      className={`overflow-hidden border shadow-sm ${radius} ${cardTheme}`}
      dir={presentation.text_direction || 'auto'}
      data-presentation-kind={presentation.kind}
    >
      {(isTemplate || presentation.kind === 'product') && (
        <div className="flex items-center gap-1.5 border-b border-black/5 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
          {lifecycle ? <PackageCheck className="h-3 w-3" aria-hidden /> : null}
          <span>{lifecycle ? labels.lifecycle : presentation.kind === 'product' ? labels.product : labels.template}</span>
          {presentation.template?.name && (
            <span className="truncate font-mono font-normal normal-case opacity-70">· {presentation.template.name}</span>
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
        ) : media ? (
          <PresentationMedia media={media} labels={labels} onLoad={onMediaLoad} />
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

        {presentation.body && (
          <div className="whitespace-pre-wrap break-words px-1 text-sm leading-relaxed">
            {presentation.body}
          </div>
        )}
        {presentation.template?.footer && (
          <div className="border-t border-black/5 px-1 pt-2 text-[11px] text-slate-500">
            {presentation.template.footer}
          </div>
        )}
        {presentation.order && (presentation.order.reference || presentation.order.status) && (
          <div className="flex flex-wrap gap-2 border-t border-black/5 px-1 pt-2 text-[10px] font-medium text-slate-500">
            {presentation.order.reference && (
              <span>{presentation.order.reference.startsWith('#') ? presentation.order.reference : `#${presentation.order.reference}`}</span>
            )}
            {presentation.order.status && <span>{presentation.order.status}</span>}
          </div>
        )}
      </div>

      {presentation.actions.length > 0 && (
        <div className="space-y-[3px] border-t border-black/5 bg-slate-50/70 p-[3px]">
          {presentation.actions.map((action, index) => (
            <PresentationAction key={`${action.kind}-${action.label}-${index}`} action={action} labels={labels} />
          ))}
        </div>
      )}
    </div>
  )
}
