import { useState } from 'react'
import { ImageIcon, Package } from 'lucide-react'
import { resolveCatalogProductImage, type CatalogImageSource } from '../../lib/catalogProductImage'

type ProductThumbnailProps = {
  row: CatalogImageSource
  className?: string
  iconClassName?: string
  fallbackIcon?: 'image' | 'package'
  alt?: string
}

export function ProductThumbnail({
  row,
  className = 'w-full h-full object-cover',
  iconClassName = 'w-5 h-5 text-slate-300',
  fallbackIcon = 'image',
  alt = '',
}: ProductThumbnailProps) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const src = resolveCatalogProductImage(row)
  const showImage = Boolean(src) && src !== failedSrc
  const Icon = fallbackIcon === 'package' ? Package : ImageIcon

  if (!showImage) {
    return <Icon className={iconClassName} aria-hidden />
  }

  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      decoding="async"
      className={className}
      onError={() => setFailedSrc(src)}
    />
  )
}
