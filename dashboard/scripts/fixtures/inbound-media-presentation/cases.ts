/**
 * Synthetic constants shared by the inbound-media presentation harness
 * (rendered by Vite in Chromium) and its Node driver
 * (scripts/check-inbound-media-presentation.mts).
 *
 * Test data only: a fake tenant, a fake token and synthetic sha-shaped
 * slugs. Nothing here identifies a real merchant, customer or file.
 */
export const SYNTHETIC_TENANT_ID = 77
export const SYNTHETIC_OTHER_TENANT_ID = 78
export const SYNTHETIC_TOKEN = 'synthetic-dashboard-test-token'

function slug(hexChar: string, ext: string): string {
  return `${hexChar.repeat(64)}${ext}`
}

function inboundPath(hexChar: string, ext: string, tenantId = SYNTHETIC_TENANT_ID): string {
  return `/media/inbound/${tenantId}/${slug(hexChar, ext)}`
}

/** Protected inbound paths, exactly the shape the backend persists as storage_url. */
export const MEDIA_PATHS = {
  imageWithLegacy: inboundPath('a', '.png'),
  audioWithLegacy: inboundPath('b', '.wav'),
  videoWithLegacy: inboundPath('c', '.webm'),
  documentWithLegacy: inboundPath('d', '.pdf'),
  imageWithoutLegacy: inboundPath('e', '.png'),
  audioWithoutLegacy: inboundPath('f', '.wav'),
  videoWithoutLegacy: inboundPath('1', '.webm'),
  documentWithoutLegacy: inboundPath('2', '.pdf'),
  legacyOnlyImage: inboundPath('3', '.png'),
  /** API answers 200 text/html (proxy/SPA hijack) — must be rejected. */
  htmlImage: inboundPath('4', '.png'),
  /** API answers 401 (expired session). */
  expiredImage: inboundPath('5', '.png'),
  /** Path belongs to another tenant — API answers 403 against the caller's X-Tenant-ID. */
  crossTenantImage: inboundPath('6', '.png', SYNTHETIC_OTHER_TENANT_ID),
  /** Unknown file — API answers 404. */
  missingImage: inboundPath('7', '.png'),
  /** API answers only after a delay; the card is unmounted before that. */
  slowImage: inboundPath('8', '.png'),
  /** API answers 200 text/html for a document — must be rejected. */
  htmlDocument: inboundPath('9', '.pdf'),
  /** Same file reached via a legacy row whose presentation has no URL. */
  missingUrlAudio: inboundPath('0', '.wav'),
} as const

export const PUBLIC_MEDIA_ORIGIN = 'https://cdn.public-media.example'
export const PUBLIC_TEMPLATE_IMAGE = `${PUBLIC_MEDIA_ORIGIN}/templates/summer-offer.png`
export const PUBLIC_PRODUCT_IMAGE = `${PUBLIC_MEDIA_ORIGIN}/products/white-sneaker.png`
export const PUBLIC_TRACKING_URL = 'https://track.public-media.example/RRRD1234'

export const PNG_WIDTH = 8
export const PNG_HEIGHT = 6
export const WAV_SECONDS = 0.25
export const VIDEO_SIZE = 16
