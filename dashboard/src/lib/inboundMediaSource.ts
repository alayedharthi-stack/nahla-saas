/**
 * Decide how a presentation media block may be loaded.
 *
 * Inbound WhatsApp media is persisted by the backend under the
 * auth-protected route ``GET /media/inbound/<tenant_id>/<slug>`` and
 * reaches the dashboard as a *relative* path (``storage_url``, mirrored
 * into ``presentation.media.url``). Binding that path to ``<img src>``
 * makes the browser request it from the dashboard origin without the
 * Authorization / X-Tenant-ID headers, where the SPA fallback answers
 * 200 text/html. Such paths must instead go through the authenticated
 * blob fetch in ``components/inbound/InboundMediaPreview``.
 *
 * Public media (template headers, product images, CDN links) keeps its
 * absolute URL and is rendered directly, exactly as before.
 */

const PROTECTED_INBOUND_MEDIA_PATH = /^\/media\/inbound\//

/** True for the backend's relative, auth-protected inbound media paths. */
export function isProtectedInboundMediaPath(url: string | null | undefined): url is string {
  return typeof url === 'string' && PROTECTED_INBOUND_MEDIA_PATH.test(url)
}

export type PresentationMediaSource =
  /** Reuse the legacy inbound media block (authenticated preview + AI details). */
  | 'inbound_preview'
  /** Protected path with no matching legacy block: authenticated fetch of the URL itself. */
  | 'protected_fetch'
  /** Public/external URL (or nothing to load): render as before. */
  | 'direct'

export function resolvePresentationMediaSource(
  media: { kind: string; url?: string | null },
  legacyMedia?: { kind: string; storage_url: string | null } | null,
): PresentationMediaSource {
  const sameKindLegacy = legacyMedia && legacyMedia.kind === media.kind ? legacyMedia : null
  if (isProtectedInboundMediaPath(media.url)) {
    return sameKindLegacy && sameKindLegacy.storage_url === media.url ? 'inbound_preview' : 'protected_fetch'
  }
  if (!media.url && sameKindLegacy) return 'inbound_preview'
  return 'direct'
}
