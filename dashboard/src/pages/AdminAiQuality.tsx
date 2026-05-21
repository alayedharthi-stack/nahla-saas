/**
 * AdminAiQuality — مراقبة جودة الذكاء (AI Quality Monitor)
 *
 * Surfaces the ``ai_quality_events`` rows the brain pipeline writes
 * whenever ``answer_alignment.check_alignment`` flags a reply that
 * does not actually answer the customer's last message.
 *
 * Three panels:
 *   1. Hero — counts by mismatch type + total open + in-window total.
 *   2. Top conversations — most-flagged ``conversation_id`` values.
 *   3. Events table — latest 50 with filter chips and triage actions.
 *
 * Privacy: phone numbers come back already masked from the API.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  EyeOff,
  Flag,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from 'lucide-react'
import {
  AiQualityEvent,
  AiQualityEventListResponse,
  AiQualitySummaryResponse,
  ResolvedStatus,
  getAiQualitySummary,
  listAiQualityEvents,
  resolveAiQualityEvent,
} from '../api/adminAiQuality'

// ── Constants ─────────────────────────────────────────────────────────────────

const MISMATCH_TYPES: Array<{ key: string; label: string }> = [
  { key: 'all',                  label: 'الكل' },
  { key: 'question_to_social',   label: 'سؤال → مجاملة' },
  { key: 'closing_to_reopen',    label: 'إغلاق → إعادة فتح' },
  { key: 'religious_to_oos',     label: 'دعاء → خارج النطاق' },
  { key: 'delivery_to_receipt',  label: 'استلام → إيصال دفع' },
]

const STATUS_FILTERS: Array<{ key: 'all' | ResolvedStatus; label: string; tone: string }> = [
  { key: 'all',      label: 'الكل',       tone: 'bg-slate-100 text-slate-700 border-slate-200' },
  { key: 'open',     label: 'مفتوحة',      tone: 'bg-red-50 text-red-700 border-red-200' },
  { key: 'reviewed', label: 'تم المراجعة', tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' },
  { key: 'ignored',  label: 'مُتجاهلة',    tone: 'bg-slate-50 text-slate-600 border-slate-200' },
  { key: 'fixed',    label: 'مُصححة',      tone: 'bg-blue-50 text-blue-700 border-blue-200' },
]

const STATUS_TONE: Record<ResolvedStatus, string> = {
  open:     'bg-red-50 text-red-700 border-red-200',
  reviewed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  ignored:  'bg-slate-50 text-slate-600 border-slate-200',
  fixed:    'bg-blue-50 text-blue-700 border-blue-200',
}

const STATUS_LABEL: Record<ResolvedStatus, string> = {
  open:     'مفتوحة',
  reviewed: 'تم المراجعة',
  ignored:  'مُتجاهلة',
  fixed:    'مُصححة',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function mismatchLabel(key: string): string {
  return MISMATCH_TYPES.find(m => m.key === key)?.label ?? key
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('ar', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

// ── Panels ────────────────────────────────────────────────────────────────────

function CountsByTypePanel({ summary }: { summary: AiQualitySummaryResponse | null }) {
  if (!summary) {
    return (
      <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
        <div className="h-4 w-32 bg-slate-100 rounded animate-pulse mb-3" />
        <div className="grid grid-cols-2 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-16 bg-slate-50 rounded-xl animate-pulse" />
          ))}
        </div>
      </div>
    )
  }
  const counts = new Map(summary.counts_by_type.map(c => [c.mismatch_type, c.count]))
  const tracked = ['question_to_social', 'closing_to_reopen', 'religious_to_oos', 'delivery_to_receipt']
  return (
    <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-bold text-slate-800">العدّ حسب نوع الانحراف</h3>
        <span className="text-xs text-slate-500">آخر {summary.window_hours} ساعة</span>
      </div>
      <div className="grid grid-cols-2 gap-3">
        {tracked.map(key => {
          const n = counts.get(key) ?? 0
          const heat = n === 0 ? 'text-slate-400' : n >= 10 ? 'text-red-600' : 'text-amber-600'
          return (
            <div key={key} className="rounded-xl border border-slate-200 p-3 bg-slate-50/50">
              <div className="text-[11px] text-slate-500">{mismatchLabel(key)}</div>
              <div className={`text-2xl font-extrabold mt-1 ${heat}`}>{n}</div>
            </div>
          )
        })}
      </div>
      <div className="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-slate-100">
        <div>
          <div className="text-[11px] text-slate-500">المجموع في النافذة</div>
          <div className="text-lg font-bold text-slate-800">{summary.total_in_window}</div>
        </div>
        <div>
          <div className="text-[11px] text-slate-500">حالات مفتوحة (كل الوقت)</div>
          <div className="text-lg font-bold text-red-600">{summary.total_open}</div>
        </div>
      </div>
    </div>
  )
}

function TopConversationsPanel({ summary }: { summary: AiQualitySummaryResponse | null }) {
  if (!summary) {
    return (
      <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
        <div className="h-4 w-40 bg-slate-100 rounded animate-pulse mb-3" />
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-10 bg-slate-50 rounded animate-pulse" />
          ))}
        </div>
      </div>
    )
  }
  return (
    <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <h3 className="text-sm font-bold text-slate-800 mb-3">المحادثات الأكثر مشكلات</h3>
      {summary.top_conversations.length === 0 ? (
        <p className="text-sm text-slate-500 py-6 text-center">لا توجد محادثات في النافذة الزمنية.</p>
      ) : (
        <ul className="space-y-2">
          {summary.top_conversations.map(tc => (
            <li
              key={tc.conversation_id}
              className="flex items-center justify-between rounded-lg border border-slate-100 px-3 py-2 hover:bg-slate-50"
            >
              <div className="flex items-center gap-2">
                <span className="inline-flex w-7 h-7 items-center justify-center rounded-full bg-red-50 text-red-700 text-xs font-bold border border-red-200">
                  {tc.count}
                </span>
                <div className="text-sm text-slate-700">
                  محادثة <span className="font-mono font-semibold">#{tc.conversation_id}</span>
                </div>
              </div>
              <span className="text-[11px] text-slate-500">{formatTimestamp(tc.last_seen)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ── Event row ────────────────────────────────────────────────────────────────

function EventRow({
  ev, onResolve, busy,
}: {
  ev:        AiQualityEvent
  onResolve: (id: number, status: ResolvedStatus) => Promise<void>
  busy:      boolean
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border border-slate-200 rounded-xl p-3 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1.5">
            <span className="text-[11px] font-bold px-2 py-0.5 rounded-full border border-amber-200 bg-amber-50 text-amber-800">
              {mismatchLabel(ev.mismatch_type)}
            </span>
            <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full border ${STATUS_TONE[ev.resolved_status]}`}>
              {STATUS_LABEL[ev.resolved_status]}
            </span>
            <span className="text-[11px] text-slate-500 font-mono">{ev.customer_phone_masked}</span>
            {ev.conversation_id && (
              <span className="text-[11px] text-slate-500 font-mono">#{ev.conversation_id}</span>
            )}
            <span className="text-[11px] text-slate-400 mr-auto">{formatTimestamp(ev.created_at)}</span>
          </div>
          <div className="text-sm text-slate-800 mb-1">
            <span className="text-slate-400">العميل: </span>
            <span dir="auto">{ev.inbound_preview ?? '—'}</span>
          </div>
          <div className="text-sm text-slate-700">
            <span className="text-slate-400">الذكاء: </span>
            <span dir="auto">{ev.reply_preview ?? '—'}</span>
          </div>
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            className="mt-2 text-[11px] text-slate-500 hover:text-slate-700 underline-offset-4 hover:underline"
          >
            {open ? 'إخفاء التفاصيل' : 'تفاصيل التشخيص'}
          </button>
          {open && (
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-slate-600">
              <div><dt className="inline text-slate-400">السبب: </dt><dd className="inline">{ev.mismatch_reason ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">النية: </dt><dd className="inline">{ev.detected_intent ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">فئة اجتماعية: </dt><dd className="inline">{ev.social_category ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">الفعل: </dt><dd className="inline">{ev.action_taken ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">المسار: </dt><dd className="inline">{ev.chosen_path ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">حالة الطلب: </dt><dd className="inline">{ev.order_status ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">Fallback: </dt><dd className="inline">{ev.fallback_used ? 'نعم' : 'لا'}</dd></div>
              <div><dt className="inline text-slate-400">النموذج: </dt><dd className="inline">{ev.model_used ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">Turn: </dt><dd className="inline">{ev.turn ?? '—'}</dd></div>
              <div><dt className="inline text-slate-400">Regen: </dt><dd className="inline">{ev.regen_fired ? 'نعم' : 'لا'}</dd></div>
              {ev.resolved_status !== 'open' && (
                <div className="col-span-2">
                  <dt className="inline text-slate-400">آخر مراجعة: </dt>
                  <dd className="inline">{ev.resolved_by ?? '—'} — {formatTimestamp(ev.resolved_at)}</dd>
                </div>
              )}
            </dl>
          )}
        </div>
        <div className="flex flex-col gap-1.5 shrink-0">
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(ev.id, 'reviewed')}
            className="text-[11px] inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50"
            title="تم المراجعة"
          >
            <CheckCircle2 className="w-3 h-3" /> مُراجَعة
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(ev.id, 'ignored')}
            className="text-[11px] inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100 disabled:opacity-50"
            title="تجاهل"
          >
            <EyeOff className="w-3 h-3" /> تجاهل
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve(ev.id, 'fixed')}
            className="text-[11px] inline-flex items-center gap-1 px-2.5 py-1 rounded-md border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-50"
            title="بحاجة إصلاح"
          >
            <Wrench className="w-3 h-3" /> بحاجة إصلاح
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminAiQuality() {
  const [summary,    setSummary]    = useState<AiQualitySummaryResponse | null>(null)
  const [events,     setEvents]     = useState<AiQualityEventListResponse | null>(null)
  const [loading,    setLoading]    = useState(true)
  const [error,      setError]      = useState<string | null>(null)
  const [busyIds,    setBusyIds]    = useState<Set<number>>(new Set())
  const [typeFilter, setTypeFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<'all' | ResolvedStatus>('open')

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const [s, e] = await Promise.all([
        getAiQualitySummary({ window_hours: 24 }),
        listAiQualityEvents({
          mismatch_type:   typeFilter   === 'all' ? undefined : typeFilter,
          resolved_status: statusFilter === 'all' ? undefined : statusFilter,
          limit:           50,
        }),
      ])
      setSummary(s)
      setEvents(e)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [typeFilter, statusFilter])

  useEffect(() => { void load() }, [load])

  const onResolve = useCallback(async (id: number, status: ResolvedStatus) => {
    setBusyIds(prev => new Set(prev).add(id))
    try {
      await resolveAiQualityEvent(id, { resolved_status: status })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyIds(prev => {
        const next = new Set(prev); next.delete(id); return next
      })
    }
  }, [load])

  const visibleEvents = useMemo(() => events?.items ?? [], [events])

  return (
    <div className="space-y-5">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-extrabold text-slate-900 flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-emerald-600" />
            مراقبة جودة الذكاء
          </h2>
          <p className="text-sm text-slate-500 mt-1">
            الحالات التي اكتشف فيها مدقّق المحاذاة أن الرد لا يطابق آخر رسالة من العميل.
            تُحفَظ كأحداث ``ai_quality_events`` ولا تُعدِّل سلوك الذكاء حتى الآن.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm bg-white border border-slate-200 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          {loading
            ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
            : <RefreshCw className="w-3.5 h-3.5" />} تحديث
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 text-red-800 px-3 py-2 text-sm flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      )}

      {/* ── Hero panels ────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CountsByTypePanel summary={summary} />
        <TopConversationsPanel summary={summary} />
      </div>

      {/* ── Filter chips ──────────────────────────────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-2xl p-3 shadow-sm space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <Flag className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-[11px] text-slate-500">نوع الانحراف:</span>
          {MISMATCH_TYPES.map(t => (
            <button
              key={t.key}
              type="button"
              onClick={() => setTypeFilter(t.key)}
              className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
                typeFilter === t.key
                  ? 'bg-slate-900 border-slate-900 text-white'
                  : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[11px] text-slate-500">الحالة:</span>
          {STATUS_FILTERS.map(s => (
            <button
              key={s.key}
              type="button"
              onClick={() => setStatusFilter(s.key)}
              className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
                statusFilter === s.key
                  ? 'bg-slate-900 border-slate-900 text-white'
                  : `${s.tone} hover:opacity-80`
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {/* ── Events list ──────────────────────────────────────────────────── */}
      <div className="space-y-2">
        {loading && !events && (
          <div className="flex justify-center py-8 text-slate-500">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}
        {!loading && visibleEvents.length === 0 && (
          <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center text-slate-500 text-sm">
            لا توجد أحداث مطابقة للفلتر الحالي. حاول تغيير الحالة أو نوع الانحراف.
          </div>
        )}
        {visibleEvents.map(ev => (
          <EventRow
            key={ev.id}
            ev={ev}
            busy={busyIds.has(ev.id)}
            onResolve={onResolve}
          />
        ))}
        {events && events.total > visibleEvents.length && (
          <div className="text-center text-[11px] text-slate-500 py-2">
            تعرض أحدث {visibleEvents.length} من أصل {events.total} حدث.
          </div>
        )}
      </div>
    </div>
  )
}
