import { useState, useEffect, useCallback } from 'react'
import {
  Activity, Database, Cpu, CreditCard, Store,
  RefreshCw, Loader2, CheckCircle2, XCircle,
  AlertTriangle, Info, Clock, Zap,
} from 'lucide-react'
import { systemApi, type SystemHealth, type SystemEventEntry } from '../api/system'

// ── helpers ──────────────────────────────────────────────────────────────────

const CATEGORY_LABELS: Record<string, string> = {
  '':           'الكل',
  payment:      'دفع',
  ai_sales:     'مبيعات',
  handoff:      'تحويل',
  order:        'طلبات',
  orchestrator: 'المنسق',
  system:       'نظام',
}

const CATEGORY_COLORS: Record<string, string> = {
  payment:      'bg-violet-100 text-violet-700',
  ai_sales:     'bg-blue-100 text-blue-700',
  handoff:      'bg-amber-100 text-amber-700',
  order:        'bg-emerald-100 text-emerald-700',
  orchestrator: 'bg-purple-100 text-purple-700',
  system:       'bg-slate-100 text-slate-600',
}

const COMPONENT_LABELS: Record<string, string> = {
  database:     'قاعدة البيانات',
  orchestrator: 'محرك الذكاء',
  moyasar:      'بوابة موياسر',
  salla:        'متجر سلة',
}

function statusColor(s: string) {
  if (s === 'ok' || s === 'configured') return 'text-emerald-600 bg-emerald-50 border-emerald-200'
  if (s === 'not_configured')           return 'text-slate-500 bg-slate-50 border-slate-200'
  if (s === 'degraded')                 return 'text-amber-600 bg-amber-50 border-amber-200'
  return 'text-red-600 bg-red-50 border-red-200'
}

function statusLabel(s: string) {
  const m: Record<string, string> = {
    ok: 'يعمل', configured: 'مُعَدّ', not_configured: 'غير مُعَدّ',
    degraded: 'ضعيف', error: 'خطأ', unreachable: 'غير متاح',
  }
  return m[s] ?? s
}

function timeAgo(iso: string | null) {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const s = Math.floor(diff / 1000)
  if (s < 60)  return `${s}ث`
  const m = Math.floor(s / 60)
  if (m < 60)  return `${m}د`
  return `${Math.floor(m / 60)}س`
}

const COMPONENT_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  database:     Database,
  orchestrator: Cpu,
  moyasar:      CreditCard,
  salla:        Store,
}

// ── Component ────────────────────────────────────────────────────────────────

export default function SystemStatus() {
  const [health,    setHealth]    = useState<SystemHealth | null>(null)
  const [events,    setEvents]    = useState<SystemEventEntry[]>([])
  const [total,     setTotal]     = useState(0)
  const [loading,   setLoading]   = useState(true)
  const [refreshed, setRefreshed] = useState<Date | null>(null)

  const [catFilter, setCatFilter] = useState('')
  const [sevFilter, setSevFilter] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [h, e] = await Promise.all([
        systemApi.health(),
        systemApi.events({ category: catFilter, severity: sevFilter, limit: 100 }),
      ])
      setHealth(h)
      setEvents(e.events)
      setTotal(e.total)
      setRefreshed(new Date())
    } catch {
      // keep stale data
    } finally {
      setLoading(false)
    }
  }, [catFilter, sevFilter])

  useEffect(() => { load() }, [load])

  // Auto-refresh every 30s
  useEffect(() => {
    const id = setInterval(() => load(), 30_000)
    return () => clearInterval(id)
  }, [load])

  const overallColor =
    health?.status === 'ok'      ? 'bg-emerald-500' :
    health?.status === 'degraded' ? 'bg-amber-500'   : 'bg-red-500'

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold text-slate-900">حالة النظام</h1>
          {health && (
            <span className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold text-white ${overallColor}`}>
              <span className="w-1.5 h-1.5 rounded-full bg-white/70" />
              {health.status === 'ok' ? 'يعمل بشكل طبيعي' : health.status === 'degraded' ? 'أداء منخفض' : 'خطأ'}
            </span>
          )}
          <span className={`px-2 py-0.5 rounded text-xs font-mono font-medium ${
            health?.production ? 'bg-red-100 text-red-700' : 'bg-yellow-100 text-yellow-700'
          }`}>
            {health?.environment ?? '…'}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {refreshed && (
            <span className="text-slate-400 text-xs flex items-center gap-1">
              <Clock className="w-3 h-3" />
              آخر تحديث: {refreshed.toLocaleTimeString('ar-SA')}
            </span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-lg text-sm text-slate-700 transition-colors disabled:opacity-50"
          >
            {loading
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <RefreshCw className="w-3.5 h-3.5" />
            }
            تحديث
          </button>
        </div>
      </div>

      {/* Health Cards */}
      <div className="grid grid-cols-4 gap-4">
        {health
          ? Object.entries(health.components).map(([key, comp]) => {
              const Icon = COMPONENT_ICONS[key] ?? Activity
              return (
                <div key={key} className={`bg-white rounded-xl border p-4 shadow-sm ${statusColor(comp.status)}`}>
                  <div className="flex items-center justify-between mb-2">
                    <Icon className="w-5 h-5 opacity-70" />
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${statusColor(comp.status)}`}>
                      {statusLabel(comp.status)}
                    </span>
                  </div>
                  <p className="text-sm font-semibold">{COMPONENT_LABELS[key] ?? key}</p>
                  {comp.model    && <p className="text-xs mt-0.5 opacity-60">{comp.model}</p>}
                  {comp.platform && <p className="text-xs mt-0.5 opacity-60">{comp.platform}</p>}
                  {comp.error    && <p className="text-xs mt-0.5 opacity-60 truncate">{comp.error}</p>}
                </div>
              )
            })
          : Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="bg-white rounded-xl border border-slate-200 p-4 h-20 animate-pulse" />
            ))
        }
      </div>

      {/* Event Timeline */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <Zap className="w-4 h-4 text-brand-500" />
            <h2 className="font-semibold text-slate-800">سجل الأحداث</h2>
            <span className="text-xs text-slate-400">({total} حدث)</span>
          </div>
          {/* Severity filter */}
          <div className="flex gap-1">
            {(['', 'warning', 'error'] as const).map(sev => (
              <button
                key={sev}
                onClick={() => setSevFilter(sev)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  sevFilter === sev
                    ? 'bg-slate-800 text-white'
                    : 'text-slate-500 hover:bg-slate-100'
                }`}
              >
                {sev === '' ? 'الكل' : sev === 'warning' ? '⚠ تحذير' : '✕ خطأ'}
              </button>
            ))}
          </div>
        </div>

        {/* Category tabs */}
        <div className="flex gap-0.5 px-4 pt-3 pb-0 border-b border-slate-100 overflow-x-auto">
          {Object.entries(CATEGORY_LABELS).map(([cat, label]) => (
            <button
              key={cat}
              onClick={() => setCatFilter(cat)}
              className={`px-3 py-1.5 text-xs font-medium rounded-t whitespace-nowrap transition-colors ${
                catFilter === cat
                  ? 'bg-white border border-b-white border-slate-200 -mb-px text-brand-600'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {loading && events.length === 0 ? (
          <div className="flex items-center justify-center py-10">
            <Loader2 className="w-5 h-5 text-brand-500 animate-spin" />
          </div>
        ) : events.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-10 text-slate-400">
            <Activity className="w-8 h-8 mb-2 text-slate-300" />
            <p className="text-sm">لا توجد أحداث بعد</p>
          </div>
        ) : (
          <div className="divide-y divide-slate-50">
            {events.map(ev => (
              <div key={ev.id} className="flex items-start gap-3 px-5 py-3 hover:bg-slate-50/50 transition-colors">
                {/* Severity icon */}
                <div className="mt-0.5 shrink-0">
                  {ev.severity === 'error'   ? <XCircle       className="w-4 h-4 text-red-500"   /> :
                   ev.severity === 'warning' ? <AlertTriangle className="w-4 h-4 text-amber-500" /> :
                                               <Info          className="w-4 h-4 text-blue-400"  />}
                </div>
                {/* Category badge */}
                <span className={`mt-0.5 shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded ${CATEGORY_COLORS[ev.category] ?? 'bg-slate-100 text-slate-500'}`}>
                  {CATEGORY_LABELS[ev.category] ?? ev.category}
                </span>
                {/* Content */}
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-mono text-slate-400">{ev.event_type}</p>
                  <p className="text-sm text-slate-700 truncate">{ev.summary}</p>
                </div>
                {/* Reference + time */}
                <div className="shrink-0 text-right">
                  {ev.reference_id && (
                    <p className="text-xs text-slate-400 font-mono">#{ev.reference_id}</p>
                  )}
                  <p className="text-xs text-slate-400 flex items-center justify-end gap-0.5 mt-0.5">
                    <Clock className="w-3 h-3" />
                    {timeAgo(ev.created_at)}
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
