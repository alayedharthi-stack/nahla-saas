/**
 * InboundMediaPreview
 * ───────────────────
 * Render an inbound WhatsApp media attachment (voice note, audio file,
 * or image) inside the conversation drawer, plus the AI-extracted
 * transcript / description rendered immediately underneath.
 *
 * Hardened from v1 (commit c064e038):
 *
 *   1. The blob fetcher now surfaces the **real** HTTP status code
 *      (401/403/404/500) instead of a generic "تعذر التحميل".
 *      Different status codes mean different operator actions:
 *        401 → session expired, re-login
 *        403 → cross-tenant URL, file belongs to a different tenant
 *        404 → file genuinely missing on disk (volume not mounted,
 *              swept on redeploy, or never persisted)
 *        500 → storage layer broke server-side
 *   2. Distinct copy for audio vs image when AI is disabled —
 *      the v1 component reused the same label "(الميزة غير مفعّلة)"
 *      for both, which read as if image status applied to audio.
 *      Now: "ميزة التفريغ الصوتي غير مفعّلة على الخادم" vs
 *           "ميزة وصف الصور غير مفعّلة على الخادم".
 *   3. "إعادة معالجة" button — calls the new
 *      `POST /conversations/media/{id}/reprocess` endpoint to
 *      redownload from Meta + rerun Whisper/Vision. Useful when
 *      OPENAI_API_KEY was added after the message arrived.
 *
 * Why a dedicated component instead of inlining in Conversations.tsx:
 *   * The media URL is served by an auth-protected endpoint
 *     (``GET /media/inbound/<tenant_id>/<slug>``) so we cannot just
 *     drop the path into ``<audio src=...>`` — the browser strips
 *     our Authorization + X-Tenant-ID headers on subresource loads.
 *     We fetch via the existing authenticated fetch wrapper, build a
 *     blob URL, and bind THAT to the player.
 *   * Blob URLs MUST be revoked on unmount to avoid leaking memory
 *     on long conversation views (1000+ messages).
 */
import { useCallback, useEffect, useState } from 'react'

import { getApiBase, getToken, getTenantId } from '../../auth'
import { featureRealityApi } from '../../api/featureReality'

import type {
  DashboardMessageMedia,
  DashboardMessageMediaAudio,
  DashboardMessageMediaImage,
  DashboardMessageMediaVideo,
} from '../../api/featureReality'

interface BlobUrlState {
  url: string | null
  loading: boolean
  /** HTTP status when the fetch returned a non-2xx, or null on success. */
  httpStatus: number | null
  /** Network-level error (timeout, CORS, etc.) when the fetch never
   * completed. Mutually exclusive with ``httpStatus``. */
  networkError: string | null
}

const _STATUS_LABEL_AR: Record<number, string> = {
  401: 'الجلسة منتهية — أعد تسجيل الدخول.',
  403: 'الملف يخصّ مستأجراً آخر — تواصل مع الدعم.',
  404: 'الملف غير موجود على القرص. قد لا يكون الـ volume مربوطاً، أو حُذف بعد آخر deploy.',
  500: 'خطأ في طبقة التخزين على الخادم.',
  502: 'تعذّر وصول الخادم لمزود التخزين.',
  503: 'خدمة التخزين غير متاحة مؤقتاً.',
}

function statusLabelAr(status: number): string {
  return _STATUS_LABEL_AR[status] || `تعذر التحميل (HTTP ${status})`
}

/** Fetch an authenticated media URL and surface it as a blob URL. */
function useAuthedMediaBlob(storage_url: string | null | undefined): BlobUrlState & {
  reload: () => void
} {
  const [reloadKey, setReloadKey] = useState(0)
  const [state, setState] = useState<BlobUrlState>({
    url: null, loading: !!storage_url, httpStatus: null, networkError: null,
  })

  useEffect(() => {
    if (!storage_url) {
      setState({ url: null, loading: false, httpStatus: null, networkError: null })
      return
    }
    let cancelled = false
    let createdUrl: string | null = null

    const load = async () => {
      setState({ url: null, loading: true, httpStatus: null, networkError: null })
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
          if (!cancelled) {
            setState({
              url: null,
              loading: false,
              httpStatus: res.status,
              networkError: null,
            })
          }
          return
        }
        // Defensive: a 200 with a JSON error body (e.g. proxy hijack)
        // would otherwise be bound to <audio src=...> and render a
        // silent broken player. Reject anything that isn't audio/image/video.
        const contentType = res.headers.get('content-type') || ''
        if (!/^(audio|image|video)\//.test(contentType)) {
          if (!cancelled) {
            setState({
              url: null,
              loading: false,
              httpStatus: null,
              networkError: `unexpected_content_type:${contentType || 'unknown'}`,
            })
          }
          return
        }
        const blob = await res.blob()
        createdUrl = URL.createObjectURL(blob)
        if (!cancelled) {
          setState({ url: createdUrl, loading: false, httpStatus: null, networkError: null })
        }
      } catch (e) {
        if (!cancelled) {
          setState({
            url: null,
            loading: false,
            httpStatus: null,
            networkError: e instanceof Error ? e.message : 'fetch_failed',
          })
        }
      }
    }
    void load()

    return () => {
      cancelled = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [storage_url, reloadKey])

  return { ...state, reload: () => setReloadKey(k => k + 1) }
}

/** Tiny helper button shared by audio + image. */
function ReprocessButton({
  messageEventId,
  onAfter,
}: {
  messageEventId: number
  onAfter: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async () => {
    setBusy(true); setError(null)
    try {
      await featureRealityApi.reprocessInboundMedia(messageEventId)
      onAfter()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'فشل')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="inline-flex items-center gap-1.5 mt-0.5">
      <button
        onClick={run}
        disabled={busy}
        className="text-[10.5px] text-slate-500 hover:text-slate-800 underline underline-offset-2 disabled:opacity-50"
      >
        {busy ? 'جاري المعالجة…' : 'إعادة معالجة'}
      </button>
      {error && <span className="text-[10.5px] text-rose-600">— {error}</span>}
    </div>
  )
}


function AudioPreview({ media }: { media: DashboardMessageMediaAudio }) {
  const { url, loading, httpStatus, networkError, reload } = useAuthedMediaBlob(media.storage_url)
  const transcriptOk = media.transcript_status === 'ok' && !!media.transcript
  const downloadFailed = (media.download_status || '').toLowerCase() === 'failed'

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
        ) : downloadFailed && !media.storage_url ? (
          <span className="text-[12px] text-rose-600">
            لم يصل الملف من واتساب أثناء الاستقبال.
          </span>
        ) : httpStatus ? (
          <span className="text-[12px] text-rose-600">
            تعذر تشغيل التسجيل — {statusLabelAr(httpStatus)}
          </span>
        ) : networkError ? (
          <span className="text-[12px] text-rose-600">
            تعذر تشغيل التسجيل — {networkError}
          </span>
        ) : (
          <span className="text-[12px] text-rose-600">تعذر تشغيل التسجيل.</span>
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
            <span className="inline-flex items-center gap-1 ms-2 text-[10px] text-emerald-700 align-middle">
              · استخدمته نحلة في الرد
            </span>
          )}
        </div>
      )}

      {/* Transcription failed / skipped — DIFFERENT copy from image. */}
      {!transcriptOk && (
        <div className="text-[11.5px] text-amber-700 bg-amber-50/60 border border-amber-100 rounded-lg px-2.5 py-1">
          {media.transcript_status === 'failed' && 'تعذر تفريغ التسجيل تلقائياً.'}
          {media.transcript_status === 'skipped' && 'ميزة التفريغ الصوتي غير مفعّلة على الخادم (OPENAI_API_KEY مفقود).'}
          {/* `stale_skipped` is set by the backend at READ TIME when the
              historical row was skipped due to a missing key but the
              current server now has it. We don't promise the bytes
              are still downloadable (Meta media URLs expire after
              ~5 minutes), only that the snapshot is from a different
              configuration era. */}
          {media.transcript_status === 'stale_skipped' && 'لم يُستخرج التفريغ وقت الاستقبال (لقطة قديمة قبل تفعيل المفتاح). جرّب إعادة المعالجة — قد تنجح إن لم تنته صلاحية الملف.'}
          {media.transcript_status === 'empty'   && 'لم نتمكن من سماع كلمات واضحة في التسجيل.'}
          {!['failed','skipped','stale_skipped','empty'].includes(media.transcript_status || '') && 'التسجيل بدون نص مستخرج.'}
          {' '}
          <ReprocessButton
            messageEventId={media.message_event_id}
            onAfter={reload}
          />
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
  const { url, loading, httpStatus, networkError, reload } = useAuthedMediaBlob(media.storage_url)
  const visionOk = media.vision_status === 'ok' && !!media.description
  const downloadFailed = (media.download_status || '').toLowerCase() === 'failed'

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
        ) : downloadFailed && !media.storage_url ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            لم تصل الصورة من واتساب أثناء الاستقبال.
          </div>
        ) : httpStatus ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر عرض الصورة — {statusLabelAr(httpStatus)}
          </div>
        ) : networkError ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر عرض الصورة — {networkError}
          </div>
        ) : (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر عرض الصورة.
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
            <span className="inline-flex items-center gap-1 ms-2 text-[10px] text-emerald-700 align-middle">
              · استخدمته نحلة في الرد
            </span>
          )}
        </div>
      )}

      {!visionOk && (
        <div className="text-[11.5px] text-amber-700 bg-amber-50/60 border border-amber-100 rounded-lg px-2.5 py-1">
          {media.vision_status === 'failed' && 'تعذر استخراج وصف للصورة تلقائياً.'}
          {media.vision_status === 'skipped' && 'ميزة وصف الصور غير مفعّلة على الخادم (OPENAI_API_KEY مفقود).'}
          {/* See transcript comment above — same logic for vision. */}
          {media.vision_status === 'stale_skipped' && 'لم يُستخرج الوصف وقت الاستقبال (لقطة قديمة قبل تفعيل المفتاح). جرّب إعادة المعالجة — قد تنجح إن لم تنته صلاحية الملف.'}
          {media.vision_status === 'empty'   && 'الصورة لم تحتوِ على نص أو معالم يمكن وصفها.'}
          {!['failed','skipped','stale_skipped','empty'].includes(media.vision_status || '') && 'صورة بدون وصف مستخرج.'}
          {' '}
          <ReprocessButton
            messageEventId={media.message_event_id}
            onAfter={reload}
          />
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

function VideoPreview({ media }: { media: DashboardMessageMediaVideo }) {
  const { url, loading, httpStatus, networkError } = useAuthedMediaBlob(media.storage_url)
  const downloadFailed = (media.download_status || '').toLowerCase() === 'failed'

  return (
    <div className="flex flex-col gap-1.5 max-w-full">
      <div className="rounded-xl overflow-hidden bg-slate-50 border border-slate-100">
        {url ? (
          <video
            controls
            preload="metadata"
            src={url}
            className="block max-w-[280px] max-h-[280px] bg-black"
          />
        ) : loading ? (
          <div className="px-3 py-6 text-[12px] text-slate-500 text-center">
            جاري تحميل الفيديو…
          </div>
        ) : downloadFailed && !media.storage_url ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            لم يصل الفيديو من واتساب أثناء الاستقبال.
          </div>
        ) : httpStatus ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر تشغيل الفيديو — {statusLabelAr(httpStatus)}
          </div>
        ) : networkError ? (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر تشغيل الفيديو — {networkError}
          </div>
        ) : (
          <div className="px-3 py-6 text-[12px] text-rose-600 text-center">
            تعذر تشغيل الفيديو.
          </div>
        )}
      </div>

      {(media.filename || media.duration != null) && (
        <div className="text-[11px] text-slate-500 flex flex-wrap gap-2">
          {media.filename && <span>📎 {media.filename}</span>}
          {media.duration != null && <span>⏱ {media.duration}s</span>}
          {media.frequently_forwarded && (
            <span className="text-amber-700">↪ معاد توجيهه كثيراً</span>
          )}
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
  if (media.kind === 'video') return <VideoPreview media={media} />
  return null
}
