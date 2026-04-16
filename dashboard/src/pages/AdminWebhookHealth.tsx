/**
 * AdminWebhookHealth — WhatsApp Webhook Reliability Dashboard
 *
 * Shows real-time health status for every merchant's WhatsApp webhook
 * subscription, powered by the webhook_guardian backend worker.
 *
 * Health statuses:
 *   active       – verified + received event in last 15 min   (green)
 *   warning      – verified but no recent event               (yellow)
 *   critical     – webhook_verified=false while connected      (red — CRITICAL)
 *   disconnected – status != connected                         (grey)
 */
import { useEffect, useState, useCallback } from 'react'
import {
  Activity, AlertTriangle, CheckCircle2, WifiOff,
  RefreshCw, Loader2, RotateCcw, ShieldCheck, Clock,
  ChevronDown, ChevronUp, Copy,
} from 'lucide-react'
import { apiCall } from '../api/client'

// ── Types ──────────────────────────────────────────────────────────────────────

type HealthStatus = 'active' | 'warning' | 'critical' | 'disconnected'

interface ConnectionHealth {
  tenant_id:                number
  tenant_name:              string
  phone_number:             string | null
  phone_number_id:          string | null
  waba_id:                  string | null
  status:                   string
  webhook_verified:         boolean
  sending_enabled:          boolean
  health:                   HealthStatus
  last_webhook_received_at: string | null
  minutes_since_last_event: number | null
  last_error:               string | null
  connection_type:          string | null
  provider:                 string | null
}

interface HealthSummary {
  active:       number
  warning:      number
  critical:     number
  disconnected: number
  total:        number
}

interface HealthResponse {
  summary:     HealthSummary
  connections: ConnectionHealth[]
}

interface GuardianLogEntry {
  id:              number
  tenant_id:       number
  phone_number_id: string | null
  waba_id:         string | null
  event:           string
  success:         boolean
  detail:          string | null
  created_at:      string | null
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function copyText(t: string) {
  navigator.clipboard.writeText(t).catch(() => {})
}

function formatAgo(isoStr: string | null, minutes: number | null): string {
  if (!isoStr) return 'لا يوجد'
  if (minutes === null) return '—'
  if (minutes < 1)  return 'أقل من دقيقة'
  if (minutes < 60) return `${minutes} دقيقة`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `${h}س ${m}د` : `${h} ساعة`
}

// ── Status Badge ───────────────────────────────────────────────────────────────

const HEALTH_CONFIG: Record<HealthStatus, { label: string; cls: string; icon: React.ReactNode }> = {
  active:       { label: 'نشط',       cls: 'bg-emerald-100 text-emerald-700 border-emerald-200', icon: <CheckCircle2 className="w-3.5 h-3.5" /> },
  warning:      { label: 'تحذير',     cls: 'bg-amber-100 text-amber-700 border-amber-200',       icon: <Clock className="w-3.5 h-3.5" /> },
  critical:     { label: 'حرج',       cls: 'bg-red-100 text-red-700 border-red-200',             icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  disconnected: { label: 'مفصول',     cls: 'bg-slate-100 text-slate-500 border-slate-200',       icon: <WifiOff className="w-3.5 h-3.5" /> },
}

function HealthBadge({ status }: { status: HealthStatus }) {
  const cfg = HEALTH_CONFIG[status] ?? HEALTH_CONFIG.disconnected
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold border ${cfg.cls}`}>
      {cfg.icon} {cfg.label}
    </span>
  )
}

// ── Event badge in guardian log ────────────────────────────────────────────────

function EventBadge({ event, success }: { event: string; success: boolean }) {
  const base = 'text-[10px] px-1.5 py-0.5 rounded-full font-medium'
  if (!success) return <span className={`${base} bg-red-100 text-red-700`}>{event}</span>
  if (event.includes('stalled'))  return <span className={`${base} bg-amber-100 text-amber-700`}>{event}</span>
  if (event.includes('subscrib')) return <span className={`${base} bg-emerald-100 text-emerald-700`}>{event}</span>
  if (event.includes('critical')) return <span className={`${base} bg-red-100 text-red-700`}>{event}</span>
  return <span className={`${base} bg-slate-100 text-slate-600`}>{event}</span>
}

// ── Row ────────────────────────────────────────────────────────────────────────

function ConnectionRow({
  conn,
  onResubscribe,
}: {
  conn:          ConnectionHealth
  onResubscribe: (tenantId: number) => Promise<void>
}) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy]         = useState(false)

  const handleResubscribe = async () => {
    if (!window.confirm(`إعادة اشتراك webhook للـ Tenant #${conn.tenant_id}؟`)) return
    setBusy(true)
    try {
      await onResubscribe(conn.tenant_id)
    } finally {
      setBusy(false)
    }
  }

  const isCritical = conn.health === 'critical'

  return (
    <div className={`border rounded-2xl overflow-hidden ${isCritical ? 'border-red-200' : 'border-slate-100'}`}>
      {/* Main row */}
      <div
        className={`flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-slate-50/60 transition ${isCritical ? 'bg-red-50/40' : 'bg-white'}`}
        onClick={() => setExpanded(v => !v)}
      >
        {/* Health */}
        <HealthBadge status={conn.health} />

        {/* Merchant */}
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-slate-800 text-sm truncate">{conn.tenant_name}</p>
          <p className="text-xs text-slate-400 font-mono truncate">{conn.phone_number ?? '—'}</p>
        </div>

        {/* Last event */}
        <div className="text-right shrink-0 hidden sm:block">
          <p className="text-xs text-slate-600 font-medium">
            {formatAgo(conn.last_webhook_received_at, conn.minutes_since_last_event)}
          </p>
          <p className="text-[10px] text-slate-400">آخر حدث</p>
        </div>

        {/* Webhook verified pill */}
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium shrink-0 ${conn.webhook_verified ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-600'}`}>
          {conn.webhook_verified ? 'مُحقَّق' : 'غير محقق'}
        </span>

        {/* Re-subscribe button (only for non-disconnected) */}
        {conn.status === 'connected' && (
          <button
            onClick={e => { e.stopPropagation(); handleResubscribe() }}
            disabled={busy}
            title="إعادة الاشتراك"
            className="shrink-0 p-1.5 rounded-lg bg-violet-100 hover:bg-violet-200 text-violet-600 transition disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />}
          </button>
        )}

        {expanded ? <ChevronUp className="w-3.5 h-3.5 text-slate-400 shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />}
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-3 grid sm:grid-cols-2 gap-2 text-xs text-slate-600">
          {[
            ['Tenant ID',        String(conn.tenant_id)],
            ['Phone Number ID',  conn.phone_number_id ?? '—'],
            ['WABA ID',          conn.waba_id ?? '—'],
            ['Status',           conn.status],
            ['Connection type',  conn.connection_type ?? '—'],
            ['Provider',         conn.provider ?? '—'],
            ['Sending enabled',  conn.sending_enabled ? 'نعم' : 'لا'],
          ].map(([label, val]) => (
            <div key={label} className="flex items-center justify-between gap-2">
              <span className="text-slate-400">{label}:</span>
              <span className="font-mono font-medium text-slate-700 flex items-center gap-1">
                {val}
                {val && val !== '—' && (
                  <button onClick={() => copyText(val)} className="text-slate-300 hover:text-slate-500">
                    <Copy className="w-2.5 h-2.5" />
                  </button>
                )}
              </span>
            </div>
          ))}
          {conn.last_error && (
            <div className="sm:col-span-2 mt-1 bg-red-50 border border-red-100 rounded-lg px-3 py-2 text-red-700">
              {conn.last_error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Guardian Log Section ───────────────────────────────────────────────────────

function GuardianLog() {
  const [entries, setEntries] = useState<GuardianLogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    apiCall<{ entries: GuardianLogEntry[] }>('/admin/whatsapp/guardian-log?limit=50')
      .then(d => { setEntries(d.entries); setError(null) })
      .catch(e => setError(e instanceof Error ? e.message : 'خطأ'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="bg-white border border-slate-100 rounded-2xl overflow-hidden shadow-sm">
      <div className="px-5 py-4 border-b border-slate-50 flex items-center justify-between">
        <h2 className="font-bold text-slate-800 text-sm">سجل Guardian (آخر 50 حدث)</h2>
        <button onClick={load} className="text-xs text-sky-600 hover:underline flex items-center gap-1">
          <RefreshCw className="w-3 h-3" /> تحديث
        </button>
      </div>

      {loading && (
        <div className="flex items-center justify-center h-20 text-slate-400 text-sm gap-2">
          <Loader2 className="w-4 h-4 animate-spin" /> جارٍ التحميل…
        </div>
      )}
      {!loading && error && (
        <p className="px-5 py-4 text-sm text-red-600">{error}</p>
      )}
      {!loading && !error && entries.length === 0 && (
        <p className="px-5 py-4 text-sm text-slate-400">لا توجد أحداث بعد — Guardian سيبدأ بعد الدقيقة الأولى</p>
      )}
      {!loading && !error && entries.length > 0 && (
        <div className="divide-y divide-slate-50">
          {entries.map(e => (
            <div key={e.id} className="flex items-start gap-3 px-5 py-3">
              <EventBadge event={e.event} success={e.success} />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-700 truncate">{e.detail ?? '—'}</p>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">
                  T#{e.tenant_id} · {e.phone_number_id ?? '—'} ·{' '}
                  {e.created_at ? new Date(e.created_at).toLocaleString('ar-SA') : ''}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function AdminWebhookHealth() {
  const [data, setData]           = useState<HealthResponse | null>(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState<string | null>(null)
  const [resubAll, setResubAll]   = useState(false)
  const [filter, setFilter]       = useState<HealthStatus | 'all'>('all')

  const load = useCallback(() => {
    setLoading(true); setError(null)
    apiCall<HealthResponse>('/admin/whatsapp/health')
      .then(d => setData(d))
      .catch(e => setError(e instanceof Error ? e.message : 'خطأ'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const handleResubscribe = async (tenantId: number) => {
    await apiCall(`/admin/whatsapp/resubscribe-webhook/${tenantId}`, { method: 'POST' })
    load()
  }

  const handleResubscribeAll = async () => {
    if (!window.confirm('إعادة اشتراك webhook لجميع المتاجر المتصلة؟')) return
    setResubAll(true)
    try {
      await apiCall('/admin/whatsapp/resubscribe-all', { method: 'POST' })
      load()
    } finally {
      setResubAll(false)
    }
  }

  const filtered = data?.connections.filter(c =>
    filter === 'all' ? true : c.health === filter
  ) ?? []

  const summary = data?.summary

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto" dir="rtl">

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-600 flex items-center justify-center shadow-lg shadow-violet-500/30">
            <ShieldCheck className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">صحة Webhook — واتساب</h1>
            <p className="text-slate-400 text-xs">مراقبة تلقائية كل 5 دقائق · يحمي اشتراكات Meta</p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 px-4 py-2 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition disabled:opacity-60"
          >
            {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            تحديث
          </button>
          <button
            onClick={handleResubscribeAll}
            disabled={resubAll}
            className="flex items-center gap-1.5 px-4 py-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-60 text-white font-bold rounded-xl text-sm transition"
          >
            {resubAll ? <Loader2 className="w-4 h-4 animate-spin" /> : <RotateCcw className="w-4 h-4" />}
            إعادة اشتراك الكل
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {[
            { key: 'active' as HealthStatus,       label: 'نشط',       value: summary.active,       color: 'emerald' },
            { key: 'warning' as HealthStatus,      label: 'تحذير',     value: summary.warning,      color: 'amber'   },
            { key: 'critical' as HealthStatus,     label: 'حرج',       value: summary.critical,     color: 'red'     },
            { key: 'disconnected' as HealthStatus, label: 'مفصول',     value: summary.disconnected, color: 'slate'   },
          ].map(({ key, label, value, color }) => (
            <button
              key={key}
              onClick={() => setFilter(f => f === key ? 'all' : key)}
              className={`rounded-2xl p-4 text-right transition border-2 ${
                filter === key
                  ? `border-${color}-400 bg-${color}-50`
                  : 'border-transparent bg-white hover:bg-slate-50'
              } shadow-sm`}
            >
              <p className={`text-2xl font-black text-${color}-600`}>{value}</p>
              <p className="text-xs text-slate-500 mt-1">{label}</p>
            </button>
          ))}
        </div>
      )}

      {/* Critical alert banner */}
      {summary && summary.critical > 0 && (
        <div className="flex items-start gap-3 bg-red-50 border border-red-200 rounded-2xl px-4 py-3">
          <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
          <p className="text-sm text-red-700 font-medium">
            تحذير: {summary.critical} متجر في حالة حرجة (webhook_verified=false بينما الحالة connected).
            Guardian سيحاول إصلاحها تلقائيًا خلال 5 دقائق، أو اضغط "إعادة اشتراك الكل" الآن.
          </p>
        </div>
      )}

      {/* Connections list */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="font-black text-slate-800 text-base">
            المتاجر
            {filter !== 'all' && (
              <span className="mr-2 text-xs font-normal text-slate-400">
                فلتر: {HEALTH_CONFIG[filter]?.label}
                <button
                  onClick={() => setFilter('all')}
                  className="mr-1 text-sky-500 hover:underline"
                >✕</button>
              </span>
            )}
          </h2>
          {data && (
            <span className="text-xs text-slate-400">
              {filtered.length} / {data.connections.length} متجر
            </span>
          )}
        </div>

        {loading && (
          <div className="flex items-center justify-center h-32 bg-white border border-slate-100 rounded-2xl text-slate-400 gap-2">
            <Loader2 className="w-5 h-5 animate-spin text-violet-500" />
            <span className="text-sm">جارٍ التحميل…</span>
          </div>
        )}

        {!loading && error && (
          <div className="bg-red-50 border border-red-200 rounded-2xl px-4 py-4">
            <p className="text-sm font-bold text-red-700">تعذّر تحميل البيانات</p>
            <p className="text-xs text-red-600 font-mono mt-1">{error}</p>
            <button onClick={load} className="mt-2 text-xs text-red-700 border border-red-300 rounded-lg px-3 py-1.5 hover:bg-red-100">
              إعادة المحاولة
            </button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className="bg-emerald-50 border border-emerald-200 rounded-2xl px-5 py-6 text-center">
            <Activity className="w-8 h-8 text-emerald-500 mx-auto mb-2" />
            <p className="text-sm font-bold text-emerald-800">
              {filter === 'all' ? 'لا توجد اتصالات بعد' : 'لا توجد متاجر في هذه الحالة'}
            </p>
          </div>
        )}

        {!loading && !error && filtered.length > 0 && (
          <div className="space-y-2">
            {filtered.map(conn => (
              <ConnectionRow
                key={conn.tenant_id}
                conn={conn}
                onResubscribe={handleResubscribe}
              />
            ))}
          </div>
        )}
      </div>

      {/* Guardian audit log */}
      <GuardianLog />
    </div>
  )
}
