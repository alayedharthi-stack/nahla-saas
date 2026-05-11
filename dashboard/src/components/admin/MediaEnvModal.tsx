/**
 * MediaEnvModal
 * ─────────────
 * Admin-only diagnostic dialog that fetches
 * `GET /admin/debug/media-env` and renders the result as readable
 * pills + a copy-to-clipboard JSON block. Used by the "فحص الوسائط"
 * button on the Campaigns page when a merchant reports that voice
 * notes / images appear "غير مفعّلة" in the conversation drawer.
 *
 * Why a separate modal (not inline on the page):
 *   * The check is a one-shot read — we don't want it to refetch
 *     on every render of the page.
 *   * The OPENAI_API_KEY tail and the storage path are sensitive
 *     enough that we'd rather not have them visible all the time
 *     while the merchant is screen-sharing with us.
 *
 * Security: visibility gated by `isAdmin()` at the parent. This
 * modal itself does NOT re-check — the backend always re-checks
 * via `require_admin` regardless.
 */
import { useCallback, useEffect, useState } from 'react'
import { X, RefreshCw, Copy, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'

import {
  adminDebugApi,
  type AdminMediaEnvSnapshot,
} from '../../api/adminDebug'

interface Props {
  open:    boolean
  onClose: () => void
}

function StatusPill({
  label,
  ok,
  hint,
}: {
  label: string
  ok:    boolean
  hint?: string | null
}) {
  return (
    <div
      className={`flex items-center justify-between gap-2 rounded-lg border px-2.5 py-1.5 text-[12px] ${
        ok
          ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
          : 'bg-rose-50 border-rose-200 text-rose-800'
      }`}
    >
      <span className="font-medium">{label}</span>
      <span className="inline-flex items-center gap-1">
        {ok ? <CheckCircle2 className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />}
        <span className="text-[11px] font-mono">
          {ok ? 'مفعّل' : 'غير مفعّل'}
          {hint ? ` · ${hint}` : ''}
        </span>
      </span>
    </div>
  )
}

function KvLine({ k, v, mono = false }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between text-[12px] py-0.5">
      <span className="text-slate-500">{k}</span>
      <span className={`text-slate-800 ${mono ? 'font-mono text-[11px]' : ''}`} dir="ltr">
        {v ?? '—'}
      </span>
    </div>
  )
}

export default function MediaEnvModal({ open, onClose }: Props) {
  const [data,      setData]      = useState<AdminMediaEnvSnapshot | null>(null)
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)
  const [copied,    setCopied]    = useState(false)

  const fetchSnapshot = useCallback(async () => {
    setLoading(true); setError(null); setCopied(false)
    try {
      const snap = await adminDebugApi.mediaEnv()
      setData(snap)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'فشل الفحص — راجع كونسول المتصفح')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void fetchSnapshot()
  }, [open, fetchSnapshot])

  const copyJson = useCallback(async () => {
    if (!data) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(data, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 2200)
    } catch {
      setCopied(false)
    }
  }, [data])

  if (!open) return null

  // Top-line health summary so the merchant sees one verdict before
  // scanning the details. "all green" = audio + vision + storage all
  // ready; anything missing kicks us to "issues present".
  const allGreen = !!data && data.openai_key_present
    && data.vision_enabled && data.stt_enabled
    && data.media_dir_writable

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-slate-900/60 backdrop-blur-sm overflow-y-auto p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl my-8" dir="rtl">
        <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100">
          <div>
            <h3 className="text-base font-bold text-slate-900">
              فحص الوسائط (Admin)
            </h3>
            <p className="text-[11.5px] text-slate-500 mt-0.5">
              يفحص إعدادات OpenAI + مجلد التخزين + ffmpeg على الخادم.
              لا يكشف قيمة OPENAI_API_KEY — فقط آخر 4 أحرف.
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              onClick={fetchSnapshot}
              disabled={loading}
              className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500 disabled:opacity-50"
              title="إعادة الفحص"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500"
              aria-label="إغلاق"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="p-5 space-y-3.5">
          {loading && (
            <div className="text-[12.5px] text-slate-500 text-center py-8">
              جاري الفحص…
            </div>
          )}

          {!loading && error && (
            <div className="rounded-lg bg-rose-50 border border-rose-200 text-rose-800 text-[12.5px] px-3 py-2 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span className="whitespace-pre-wrap break-words">{error}</span>
            </div>
          )}

          {!loading && data && (
            <>
              {/* Top verdict banner */}
              <div className={`rounded-lg border px-3 py-2 flex items-center gap-2 text-[12.5px] ${
                allGreen
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                  : 'bg-amber-50 border-amber-200 text-amber-800'
              }`}>
                {allGreen ? <CheckCircle2 className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
                <span>
                  {allGreen
                    ? 'كل ميزات الوسائط مفعّلة وجاهزة.'
                    : `يوجد ${data.issues.length} مشكلة — راجع التفاصيل أدناه.`}
                </span>
              </div>

              {/* Status pills */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <StatusPill
                  label="OPENAI_API_KEY"
                  ok={data.openai_key_present}
                  hint={data.openai_key_tail || undefined}
                />
                <StatusPill
                  label="وصف الصور (Vision)"
                  ok={data.vision_enabled}
                />
                <StatusPill
                  label="تفريغ الصوت (STT)"
                  ok={data.stt_enabled}
                />
                <StatusPill
                  label="مجلد التخزين قابل للكتابة"
                  ok={data.media_dir_writable}
                />
                <StatusPill
                  label="ffmpeg"
                  ok={data.ffmpeg_found}
                  hint={data.ffmpeg_version ? data.ffmpeg_version.split(' ').slice(0, 3).join(' ') : undefined}
                />
              </div>

              {/* Config details */}
              {/* Process identity — surfaces WHICH service answered.
                  Crucial when web vs worker have env drift: this
                  block tells us which one we're looking at right
                  now, so support can grep [MEDIA_NORMALIZER_BOOT]
                  in Railway logs for the OTHER services. */}
              <div className={`rounded-lg border px-3 py-2 ${
                data.process.needs_restart_to_pick_up_env
                  ? 'bg-amber-50 border-amber-300'
                  : 'bg-slate-50 border-slate-200'
              }`}>
                <h4 className="text-[12px] font-bold text-slate-700 mb-1.5 flex items-center gap-1.5">
                  <span>الـ process المُجيب</span>
                  {data.process.needs_restart_to_pick_up_env && (
                    <span className="text-[10px] bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded">
                      يحتاج Restart
                    </span>
                  )}
                </h4>
                <KvLine k="service" v={data.process.service} mono />
                <KvLine k="pid"     v={String(data.process.pid)} mono />
                <KvLine
                  k="openai_key_present_now"
                  v={String(data.process.openai_key_present_now)}
                  mono
                />
                <KvLine
                  k="openai_key_present_at_boot"
                  v={data.process.openai_key_present_at_boot === null
                      ? '—'
                      : String(data.process.openai_key_present_at_boot)}
                  mono
                />
                {data.process.railway_service_name && (
                  <KvLine
                    k="railway_service_name"
                    v={data.process.railway_service_name}
                    mono
                  />
                )}
                {data.process.railway_replica_id && (
                  <KvLine
                    k="railway_replica_id"
                    v={data.process.railway_replica_id}
                    mono
                  />
                )}
                {data.process.needs_restart_to_pick_up_env && (
                  <p className="text-[11px] text-amber-900 mt-1.5 leading-relaxed">
                    هذا الـ process يرى المفتاح الآن، لكنه بدأ بدونه.
                    أعِد تشغيل خدمات <strong>worker</strong> و
                    <strong> scheduler</strong> أيضًا (ليس فقط web)،
                    وإلا ستظهر رسائل "OPENAI_API_KEY مفقود" في
                    الرسائل الفعلية. تحقق من Railway logs: ابحث عن
                    <code className="font-mono mx-1">[MEDIA_NORMALIZER_BOOT]</code>
                    وقارن <code className="font-mono">openai_key_present_at_boot</code>
                    لكل خدمة.
                  </p>
                )}
              </div>

              <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
                <h4 className="text-[12px] font-bold text-slate-700 mb-1.5">إعدادات OpenAI</h4>
                <KvLine k="api_base"     v={data.openai.api_base} mono />
                <KvLine k="chat_model"   v={data.openai.chat_model} mono />
                <KvLine k="audio_model"  v={data.openai.audio_model} mono />
                <KvLine k="vision_model" v={data.openai.vision_model} mono />
                <KvLine k="stt_language" v={data.openai.stt_language} mono />
                <KvLine k="key_tail"     v={data.openai_key_tail || '—'} mono />
              </div>

              <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
                <h4 className="text-[12px] font-bold text-slate-700 mb-1.5">التخزين</h4>
                <KvLine k="inbound_media_dir" v={data.inbound_media_dir} mono />
                <KvLine k="exists"            v={String(data.storage.exists)} mono />
                <KvLine k="writable"          v={String(data.media_dir_writable)} mono />
                <KvLine k="free_bytes"        v={data.storage.free_bytes ? `${(data.storage.free_bytes / 1024 / 1024).toFixed(1)} MB` : '—'} mono />
                <KvLine k="max_inbound_bytes" v={`${(data.storage.max_inbound_bytes / 1024 / 1024).toFixed(0)} MB`} mono />
                {data.storage.write_probe_error && (
                  <KvLine k="write_probe_error" v={data.storage.write_probe_error} mono />
                )}
              </div>

              <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2">
                <h4 className="text-[12px] font-bold text-slate-700 mb-1.5">ffmpeg</h4>
                <KvLine k="found"   v={String(data.ffmpeg_found)} mono />
                <KvLine k="path"    v={data.ffmpeg.path || '—'} mono />
                <KvLine k="version" v={data.ffmpeg_version || '—'} mono />
              </div>

              {data.issues.length > 0 && (
                <div className="rounded-lg bg-rose-50 border border-rose-200 px-3 py-2">
                  <h4 className="text-[12px] font-bold text-rose-800 mb-1.5">المشاكل</h4>
                  <ul className="text-[12px] text-rose-800 space-y-1 list-disc list-inside">
                    {data.issues.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}

              {data.hints.length > 0 && (
                <div className="rounded-lg bg-sky-50 border border-sky-200 px-3 py-2">
                  <h4 className="text-[12px] font-bold text-sky-800 mb-1.5">خطوات الإصلاح</h4>
                  <ul className="text-[12px] text-sky-800 space-y-1 list-disc list-inside">
                    {data.hints.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Copy JSON for support tickets */}
              <div className="flex justify-end">
                <button
                  onClick={copyJson}
                  className="inline-flex items-center gap-1.5 text-[11.5px] text-slate-600 hover:text-slate-900 border border-slate-200 rounded-lg px-2.5 py-1"
                >
                  <Copy className="w-3.5 h-3.5" />
                  {copied ? 'تم النسخ ✓' : 'نسخ JSON كامل'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
