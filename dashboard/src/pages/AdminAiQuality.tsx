/**
 * AdminAiQuality — مراقبة جودة الذكاء (AI Quality Monitor)
 *
 * Surfaces the ``ai_quality_events`` rows the system writes whenever
 * something goes wrong with an inbound message. May 2026 #22 widened
 * this to three orthogonal failure families, each behind its own tab:
 *
 *   1. ``ai_mismatch``    — answer-alignment check fired (brain reply
 *      did not match the customer's intent).
 *   2. ``inbound_drop``   — message vanished BEFORE the brain ran
 *      (unsupported type, empty text, handoff ack failure, dispatcher
 *      exception).
 *   3. ``webhook_routing`` — 360dialog / Meta webhook arrived but could
 *      not be routed to a tenant.
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
  AiQualityCategory,
  AiQualityEvent,
  AiQualityEventListResponse,
  AiQualitySummaryResponse,
  ResolvedStatus,
  getAiQualitySummary,
  listAiQualityEvents,
  resolveAiQualityEvent,
} from '../api/adminAiQuality'

// ── Tab configuration ────────────────────────────────────────────────────────
//
// One row per category. ``trackedTypes`` is the canonical vocabulary the
// hero panel highlights — anything else still appears in the events list
// but is not pre-allocated a KPI tile (it would have a 0-count tile
// otherwise, which is noise during early rollout).

interface TabSpec {
  key:           AiQualityCategory
  label:         string
  description:   string
  trackedTypes:  Array<{ key: string; label: string }>
}

const TABS: TabSpec[] = [
  {
    key:         'ai_mismatch',
    label:       'اختلافات الذكاء',
    description:
      'الحالات التي اكتشف فيها مدقّق المحاذاة أن الرد لا يطابق آخر رسالة من العميل.',
    trackedTypes: [
      { key: 'question_to_social',  label: 'سؤال → مجاملة' },
      { key: 'closing_to_reopen',   label: 'إغلاق → إعادة فتح' },
      { key: 'religious_to_oos',    label: 'دعاء → خارج النطاق' },
      { key: 'delivery_to_receipt', label: 'استلام → إيصال دفع' },
    ],
  },
  {
    key:         'inbound_drop',
    label:       'إسقاطات الإدخال',
    description:
      'رسائل وصلت إلى نحلة لكن سقطت قبل أن يبدأ الذكاء — أنواع غير مدعومة، نصّ فارغ بعد التحويل، أو استثناء في dispatcher.',
    trackedTypes: [
      { key: 'unsupported_type',        label: 'نوع غير مدعوم' },
      { key: 'empty_text',              label: 'نص فارغ' },
      { key: 'pre_brain_handoff_drop',  label: 'إخفاق تسليم لموظف' },
      { key: 'dispatcher_exception',    label: 'استثناء في الذكاء' },
    ],
  },
  {
    key:         'webhook_routing',
    label:       'مشاكل الـ Webhook',
    description:
      'إشعارات Meta / 360dialog وصلت ولم نتمكن من ربطها بأي تاجر — phone_number_id مفقود/غير معروف/مكرر، أو سرّ التكامل لم يطابق.',
    trackedTypes: [
      { key: 'unrouted_missing_phone_id', label: 'بدون phone_number_id' },
      { key: 'unrouted_unknown_phone_id', label: 'phone_number_id غير معروف' },
      { key: 'unrouted_ambiguous',        label: 'تطابق ملتبس' },
      { key: 'unrouted_wrong_provider',   label: 'مزوّد خاطئ' },
      { key: 'unrouted_bad_secret',       label: 'سرّ تكامل غير صحيح' },
    ],
  },
]

// ── Constants ─────────────────────────────────────────────────────────────────

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

function findTabSpec(category: AiQualityCategory): TabSpec {
  return TABS.find(t => t.key === category) ?? TABS[0]
}

function mismatchLabel(tab: TabSpec, key: string): string {
  return tab.trackedTypes.find(m => m.key === key)?.label ?? key
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

function CountsByTypePanel({
  summary, tab,
}: {
  summary: AiQualitySummaryResponse | null
  tab:     TabSpec
}) {
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
  return (
    <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-sm">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-bold text-slate-800">العدّ حسب النوع</h3>
        <span className="text-xs text-slate-500">آخر {summary.window_hours} ساعة</span>
      </div>
      <div className="grid grid-cols-2 gap-3">
        {tab.trackedTypes.map(({ key, label }) => {
          const n = counts.get(key) ?? 0
          const heat = n === 0 ? 'text-slate-400' : n >= 10 ? 'text-red-600' : 'text-amber-600'
          return (
            <div key={key} className="rounded-xl border border-slate-200 p-3 bg-slate-50/50">
              <div className="text-[11px] text-slate-500">{label}</div>
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
  ev, tab, onResolve, busy,
}: {
  ev:        AiQualityEvent
  tab:       TabSpec
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
              {mismatchLabel(tab, ev.mismatch_type)}
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
          {/*
            For inbound_drop / webhook_routing the reply_preview is
            empty by design (the AI never ran). We still render the
            inbound preview (or a dash) so the column layout stays
            consistent across tabs.
          */}
          <div className="text-sm text-slate-800 mb-1">
            <span className="text-slate-400">العميل: </span>
            <span dir="auto">{ev.inbound_preview ?? '—'}</span>
          </div>
          {tab.key === 'ai_mismatch' && (
            <div className="text-sm text-slate-700">
              <span className="text-slate-400">الذكاء: </span>
              <span dir="auto">{ev.reply_preview ?? '—'}</span>
            </div>
          )}
          {tab.key !== 'ai_mismatch' && ev.mismatch_reason && (
            <div className="text-sm text-slate-700">
              <span className="text-slate-400">التفاصيل: </span>
              <span dir="auto">{ev.mismatch_reason}</span>
            </div>
          )}
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
              <div><dt className="inline text-slate-400">المسار: </dt><dd className="inline">{ev.chosen_path ?? '—'}</dd></div>
              {tab.key === 'ai_mismatch' && (
                <>
                  <div><dt className="inline text-slate-400">النية: </dt><dd className="inline">{ev.detected_intent ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">فئة اجتماعية: </dt><dd className="inline">{ev.social_category ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">الفعل: </dt><dd className="inline">{ev.action_taken ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">حالة الطلب: </dt><dd className="inline">{ev.order_status ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">Fallback: </dt><dd className="inline">{ev.fallback_used ? 'نعم' : 'لا'}</dd></div>
                  <div><dt className="inline text-slate-400">النموذج: </dt><dd className="inline">{ev.model_used ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">Turn: </dt><dd className="inline">{ev.turn ?? '—'}</dd></div>
                  <div><dt className="inline text-slate-400">Regen: </dt><dd className="inline">{ev.regen_fired ? 'نعم' : 'لا'}</dd></div>
                </>
              )}
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
  const [activeTab,    setActiveTab]    = useState<AiQualityCategory>('ai_mismatch')
  const [summary,      setSummary]      = useState<AiQualitySummaryResponse | null>(null)
  const [events,       setEvents]       = useState<AiQualityEventListResponse | null>(null)
  const [loading,      setLoading]      = useState(true)
  const [error,        setError]        = useState<string | null>(null)
  const [busyIds,      setBusyIds]      = useState<Set<number>>(new Set())
  const [typeFilter,   setTypeFilter]   = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<'all' | ResolvedStatus>('open')

  const currentTab = useMemo(() => findTabSpec(activeTab), [activeTab])

  // Reset the type filter when switching tabs — the vocabulary changes.
  useEffect(() => {
    setTypeFilter('all')
  }, [activeTab])

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const [s, e] = await Promise.all([
        getAiQualitySummary({ window_hours: 24, category: activeTab }),
        listAiQualityEvents({
          category:        activeTab,
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
  }, [activeTab, typeFilter, statusFilter])

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

  // The tab badge count comes from ``counts_by_category`` on whichever
  // summary call we just made. It's an unfiltered roll-up, so a tab
  // shows its true total even when the active tab is a different one.
  const categoryCounts = useMemo(() => {
    const map = new Map<string, number>()
    for (const row of summary?.counts_by_category ?? []) {
      map.set(String(row.category), row.count)
    }
    return map
  }, [summary])

  return (
    <div className="space-y-5">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-extrabold text-slate-900 flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-emerald-600" />
            مراقبة جودة الذكاء
          </h2>
          <p className="text-sm text-slate-500 mt-1">{currentTab.description}</p>
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

      {/* ── Tabs ──────────────────────────────────────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-2xl p-2 shadow-sm">
        <div className="flex items-center gap-1 flex-wrap">
          {TABS.map(t => {
            const isActive = t.key === activeTab
            const count = categoryCounts.get(t.key) ?? 0
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => setActiveTab(t.key)}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors border ${
                  isActive
                    ? 'bg-slate-900 text-white border-slate-900'
                    : 'bg-white text-slate-700 border-slate-200 hover:bg-slate-50'
                }`}
              >
                {t.label}
                <span className={`inline-flex min-w-5 px-1.5 justify-center items-center rounded-full text-[10px] font-bold ${
                  isActive
                    ? 'bg-white text-slate-900'
                    : count > 0
                      ? 'bg-red-100 text-red-700'
                      : 'bg-slate-100 text-slate-500'
                }`}>
                  {count}
                </span>
              </button>
            )
          })}
        </div>
      </div>

      {/* ── Hero panels ────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CountsByTypePanel summary={summary} tab={currentTab} />
        <TopConversationsPanel summary={summary} />
      </div>

      {/* ── Filter chips ──────────────────────────────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-2xl p-3 shadow-sm space-y-3">
        <div className="flex items-center gap-2 flex-wrap">
          <Flag className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-[11px] text-slate-500">النوع:</span>
          <button
            type="button"
            onClick={() => setTypeFilter('all')}
            className={`text-[11px] px-2.5 py-1 rounded-full border transition-colors ${
              typeFilter === 'all'
                ? 'bg-slate-900 border-slate-900 text-white'
                : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
          >
            الكل
          </button>
          {currentTab.trackedTypes.map(t => (
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
            لا توجد أحداث مطابقة للفلتر الحالي في هذا التبويب.
          </div>
        )}
        {visibleEvents.map(ev => (
          <EventRow
            key={ev.id}
            ev={ev}
            tab={currentTab}
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
