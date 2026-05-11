/**
 * InboundMediaPreview
 * ───────────────────
 * Render an inbound WhatsApp media attachment (voice note, audio file,
 * or image) inside the conversation drawer, plus the AI-extracted
 * transcript / description rendered immediately underneath.
 *
 * Why a dedicated component instead of inlining in Conversations.tsx:
 *   * The media URL is served by an auth-protected endpoint
 *     (``GET /media/inbound/<tenant_id>/<slug>``) so we cannot just
 *     drop the path into ``<audio src=...>`` — the browser strips
 *     our Authorization + X-Tenant-ID headers on subresource loads.
 *     We fetch via the existing authenticated fetch wrapper, build a
 *     blob URL, and bind THAT to the player.
 *   * The transcript / vision text needs careful empty / failure
 *     handling so we never display a dangling "النص المستخرج من
 *     الصوت:" with nothing under it.
 *   * Blob URLs MUST be revoked on unmount to avoid leaking memory
 *     on long conversation views (1000+ messages).
 */
import { useEffect, useState } from 'react'

import { getApiBase, getToken, getTenantId } from '../../auth'

import type {
  DashboardMessageMedia,
  DashboardMessageMediaAudio,
  DashboardMessageMediaImage,
} from '../../api/featureReality'

interface BlobUrlState {
  url: string | null
  loading: boolean
  error: string | null
}

/** Fetch an authenticated media URL and surface it as a blob URL. */
function useAuthedMediaBlob(storage_url: string | null | undefined): BlobUrlState {
  const [state, setState] = useState<BlobUrlState>({
    url: null, loading: !!storage_url, error: null,
  })

  useEffect(() => {
    if (!storage_url) {
      setState({ url: null, loading: false, error: null })
      return
    }
    let cancelled = false
    let createdUrl: string | null = null

    const load = async () => {
      try {
        const token    = getToken()
        const tenantId = getTenantId()
        const base     = getApiBase()
        const res = await fetch(`${base}${storage_url}`, {
          mode: 'cors',
          headers: {
            ...(token    ? { Authorization: `Bearer ${token}` } : {}),
            ...(tenantId ? { 'X-Tenant-ID': String(tenantId) } : {}),
          },
        })
        if (!res.ok) {
          throw new Error(`http_${res.status}`)
        }
        const blob = await res.blob()
        createdUrl = URL.createObjectURL(blob)
        if (!cancelled) {
          setState({ url: createdUrl, loading: false, error: null })
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            url: null,
            loading: false,
            error: e instanceof Error ? e.message : 'fetch_failed',
          })
        }
      }
    }
    void load()

    return () => {
      cancelled = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [storage_url])

  return state
}

function AudioPreview({ media }: { media: DashboardMessageMediaAudio }) {
  const { url, loading, error } = useAuthedMediaBlob(media.storage_url)
  const transcriptOk = media.transcript_status === 'ok' && !!media.transcript

  return (
    <div className="flex flex-col gap-1.5 max-w-full">
      <div className="flex items-center gap-2 rounded-xl bg-emerald-50/70 border border-emerald-100 px-3 py-2">
        <span className="text-emerald-700 text-base">🎙️</span>
        {url ? (
          <audio
            controls
            preload="metadata"
            src={url}
            className="h-9 max-w-[260px]"
          />
        ) : loading ? (
          <span className="text-[12px] text-emerald-700/80">جاري تحميل التسجيل…</span>
        ) : (
          <span className="text-[12px] text-rose-600">
            تعذر تشغيل التسجيل{error ? ` (${error})` : ''}
          </span>
        )}
      </div>

      {/* AI-extracted transcript */}
      {transcriptOk && (
        <div className="text-[12.5px] leading-relaxed text-slate-700 bg-white/60 border border-slate-100 rounded-lg px-2.5 py-1.5">
          <span className="text-[11px] font-medium text-slate-500 block mb-0.5">
            النص المستخرج من الصوت:
          </span>
          <span className="whitespace-pre-wrap break-words">{media.transcript}</span>
          {media.ai_used && (
            <span className="inline-flex items-center gap-1 ml-2 text-[10px] text-emerald-700 align-middle">
              · استخدمته نحلة في الرد
            </span>
          )}
        </div>
      )}

      {/* Transcription failed / skipped */}
      {!transcriptOk && (
        <div className="text-[11.5px] text-amber-700 bg-amber-50/60 border border-amber-100 rounded-lg px-2.5 py-1">
          {media.transcript_status === 'failed' && 'تعذر تفريغ التسجيل تلقائياً.'}
          {media.transcript_status === 'skipped' && 'لم يتم تفريغ التسجيل (الميزة غير مفعّلة).'}
          {media.transcript_status === 'empty'   && 'لم نتمكن من سماع كلمات واضحة في التسجيل.'}
          {!['failed','skipped','empty'].includes(media.transcript_status || '') && 'التسجيل بدون نص مستخرج.'}
        </div>
      )}

      {media.caption && (
        <div className="text-[12.5px] text-slate-600 whitespace-pre-wrap break-words">
          {media.caption}
        </div>
      )}
    </div>
  )
}

function ImagePreview({ media }: { media: DashboardMessageMediaImage }) {
  const { url, loading, error } = useAuthedMediaBlob(media.storage_url)
  const visionOk = media.vision_status === 'ok' && !!media.description

  return (
    <div className="flex flex-col gap-1.5 max-w-full">
      <div className="rounded-xl overflow-hidden bg-slate-50 border border-slate-100">
        {url ? (
          <img
            src={url}
            alt="صورة من العميل"
            className="block max-w-[280px] max-h-[280px] object-contain bg-white"
            loading="lazy"
          />
        ) : loading ? (
          <div className="px-3 py-6 text-[12px] text-slate-500 text-center">
            جاري تحميل الصورة…
          </div>
        ) : (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر عرض الصورة{error ? ` (${error})` : ''}
          </div>
        )}
      </div>

      {visionOk && (
        <div className="text-[12.5px] leading-relaxed text-slate-700 bg-white/60 border border-slate-100 rounded-lg px-2.5 py-1.5">
          <span className="text-[11px] font-medium text-slate-500 block mb-0.5">
            وصف الصورة:
          </span>
          <span className="whitespace-pre-wrap break-words">{media.description}</span>
          {media.ai_used && (
            <span className="inline-flex items-center gap-1 ml-2 text-[10px] text-emerald-700 align-middle">
              · استخدمته نحلة في الرد
            </span>
          )}
        </div>
      )}

      {!visionOk && (
        <div className="text-[11.5px] text-amber-700 bg-amber-50/60 border border-amber-100 rounded-lg px-2.5 py-1">
          {media.vision_status === 'failed' && 'تعذر استخراج وصف للصورة تلقائياً.'}
          {media.vision_status === 'skipped' && 'لم يتم استخراج وصف للصورة (الميزة غير مفعّلة).'}
          {media.vision_status === 'empty'   && 'الصورة لم تحتوِ على نص أو معالم يمكن وصفها.'}
          {!['failed','skipped','empty'].includes(media.vision_status || '') && 'صورة بدون وصف مستخرج.'}
        </div>
      )}

      {media.caption && (
        <div className="text-[12.5px] text-slate-600 whitespace-pre-wrap break-words">
          {media.caption}
        </div>
      )}
    </div>
  )
}

export default function InboundMediaPreview({ media }: { media: DashboardMessageMedia }) {
  if (media.kind === 'audio') return <AudioPreview media={media} />
  if (media.kind === 'image') return <ImagePreview media={media} />
  return null
}
