/**
 * AdminTenantIntegrity — Tenant Consistency & Identity Isolation Audit
 *
 * Provides:
 *  • Per-tenant health overview (Healthy / Warning / Critical)
 *  • Duplicate detection: store_id, phone_number_id, waba_id across tenants
 *  • Orphan detection: WA without store / store without WA
 *  • Safe reconciliation workflow (dry-run first, then live merge)
 *  • Integrity event log (identity resolution, blocked writes, conflicts)
 */
import { useEffect, useState, useCallback } from 'react'
import {
  ShieldCheck, ShieldAlert, AlertTriangle, CheckCircle2,
  RefreshCw, Loader2, Copy, ChevronDown, ChevronUp,
  GitMerge, Eye, Play, Wifi, WifiOff, Store,
} from 'lucide-react'
import { apiCall } from '../api/client'

// ── Types ──────────────────────────────────────────────────────────────────────

type TenantHealth = 'healthy' | 'warning' | 'critical'

interface TenantRow {
  tenant_id:   number
  tenant_name: string
  health:      TenantHealth
  issues:      string[]
  store: {
    count:    number
    store_id: string | null
    provider: string | null
    enabled:  boolean | null
  }
  whatsapp: {
    status:           string
    phone_number_id:  string | null
    waba_id:          string | null
    webhook_verified: boolean
    sending_enabled:  boolean
    connection_type:  string | null
    provider:         string | null
  }
}

interface AuditSummary {
  total:                number
  healthy:              number
  warning:              number
  critical:             number
  duplicate_store_ids:  number
  duplicate_phone_ids:  number
  duplicate_waba_ids:   number
  orphaned_wa:          number
  orphaned_stores:      number
}

interface AuditResponse {
  summary:             AuditSummary
  tenants:             TenantRow[]
  duplicate_store_ids: Array<{ store_id: string; provider: string; tenant_ids: number[] }>
  duplicate_phone_ids: Array<{ phone_number_id: string; tenant_ids: number[] }>
  duplicate_waba_ids:  Array<{ waba_id: string; tenant_ids: number[] }>
  orphaned_wa:         Array<{ tenant_id: number; phone_number_id: string | null; waba_id: string | null }>
  orphaned_stores:     Array<{ tenant_id: number; store_id: string | null; provider: string | null }>
}

interface ReconcileAction {
  resource:    string
  action:      string
  from_tenant?: number
  to_tenant?:   number
  detail?:      Record<string, unknown>
}

interface ReconcileResult {
  dry_run:          boolean
  source_tenant_id: number
  source_name:      string
  target_tenant_id: number
  target_name:      string
  actions:          ReconcileAction[]
  warnings:         string[]
  status?:          string
  error?:           string
}

interface IntegrityEvent {
  id:              number
  event:           string
  tenant_id:       number | null
  other_tenant_id: number | null
  phone_number_id: string | null
  waba_id:         string | null
  store_id:        string | null
  provider:        string | null
  action:          string | null
  result:          string | null
  detail:          string | null
  actor:           string | null
  dry_run:         boolean | null
  created_at:      string | null
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function copyText(t: string) {
  navigator.clipboard.writeText(t).catch(() => {})
}

const HEALTH_CFG: Record<TenantHealth, { label: string; cls: string }> = {
  healthy:  { label: 'سليم',   cls: 'bg-emerald-100 text-emerald-700 border-emerald-200' },
  warning:  { label: 'تحذير',  cls: 'bg-amber-100 text-amber-700 border-amber-200' },
  critical: { label: 'حرج',    cls: 'bg-red-100 text-red-700 border-red-200' },
}

function HealthBadge({ health }: { health: TenantHealth }) {
  const cfg = HEALTH_CFG[health] ?? HEALTH_CFG.warning
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold border ${cfg.cls}`}>
      {cfg.label}
    </span>
  )
}

function EventBadge({ result }: { result: string | null }) {
  if (result === 'ok')       return <span className="text-[10px] px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded-full">ok</span>
  if (result === 'blocked')  return <span className="text-[10px] px-1.5 py-0.5 bg-red-100 text-red-700 rounded-full">blocked</span>
  if (result === 'conflict') return <span className="text-[10px] px-1.5 py-0.5 bg-orange-100 text-orange-700 rounded-full">conflict</span>
  if (result === 'fixed')    return <span className="text-[10px] px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded-full">fixed</span>
  return <span className="text-[10px] px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded-full">{result ?? '—'}</span>
}

// ── Tenant Row ─────────────────────────────────────────────────────────────────

function TenantIntegrityRow({
  row,
  onReconcileTarget,
}: {
  row:               TenantRow
  onReconcileTarget: (id: number, name: string) => void
}) {
  const [open, setOpen] = useState(false)
  const isCritical = row.health === 'critical'

  return (
    <div className={`border rounded-2xl overflow-hidden ${isCritical ? 'border-red-200' : 'border-slate-100'}`}>
      <button
        className={`w-full flex items-center gap-3 px-4 py-3 text-right hover:bg-slate-50/60 transition ${isCritical ? 'bg-red-50/30' : 'bg-white'}`}
        onClick={() => setOpen(v => !v)}
      >
        <HealthBadge health={row.health} />
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-slate-800 text-sm truncate">{row.tenant_name}</p>
          <p className="text-[10px] text-slate-400 font-mono">T#{row.tenant_id}</p>
        </div>

        {/* Store indicator */}
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium hidden sm:flex items-center gap-0.5 ${row.store.count > 0 ? 'bg-blue-50 border-blue-200 text-blue-600' : 'bg-slate-50 border-slate-200 text-slate-400'}`}>
          <Store className="w-2.5 h-2.5" /> {row.store.store_id ? row.store.store_id.substring(0, 8) : '—'}
        </span>

        {/* WA indicator */}
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full border font-medium hidden sm:flex items-center gap-0.5 ${row.whatsapp.status === 'connected' ? 'bg-emerald-50 border-emerald-200 text-emerald-600' : 'bg-slate-50 border-slate-200 text-slate-400'}`}>
          {row.whatsapp.status === 'connected' ? <Wifi className="w-2.5 h-2.5" /> : <WifiOff className="w-2.5 h-2.5" />}
          {row.whatsapp.status}
        </span>

        {/* Issues */}
        {row.issues.length > 0 && (
          <span className="text-[10px] bg-orange-50 border border-orange-200 text-orange-700 px-1.5 py-0.5 rounded-full font-medium hidden md:block">
            {row.issues.length} مشكلة
          </span>
        )}

        {open ? <ChevronUp className="w-3.5 h-3.5 text-slate-400 shrink-0" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />}
      </button>

      {open && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-3 space-y-3">
          {row.issues.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {row.issues.map(i => (
                <span key={i} className="text-xs bg-orange-100 text-orange-700 border border-orange-200 px-2 py-0.5 rounded-lg font-medium">
                  {i}
                </span>
              ))}
            </div>
          )}

          <div className="grid sm:grid-cols-2 gap-2 text-xs text-slate-600">
            {/* Store */}
            <div className="bg-white rounded-xl border border-slate-100 p-3 space-y-1">
              <p className="font-bold text-slate-700 mb-2 flex items-center gap-1"><Store className="w-3 h-3" /> المتجر</p>
              <Row label="Count"    val={String(row.store.count)} />
              <Row label="Store ID" val={row.store.store_id} copy />
              <Row label="Provider" val={row.store.provider} />
              <Row label="Enabled"  val={row.store.enabled ? 'نعم' : 'لا'} />
            </div>

            {/* WA */}
            <div className="bg-white rounded-xl border border-slate-100 p-3 space-y-1">
              <p className="font-bold text-slate-700 mb-2 flex items-center gap-1"><Wifi className="w-3 h-3" /> واتساب</p>
              <Row label="Status"           val={row.whatsapp.status} />
              <Row label="Phone Number ID"  val={row.whatsapp.phone_number_id} copy />
              <Row label="WABA ID"          val={row.whatsapp.waba_id} copy />
              <Row label="Webhook Verified" val={row.whatsapp.webhook_verified ? 'نعم ✓' : 'لا ✗'} />
              <Row label="Sending Enabled"  val={row.whatsapp.sending_enabled ? 'نعم' : 'لا'} />
            </div>
          </div>

          {/* Merge target button */}
          <div className="flex justify-end">
            <button
              onClick={() => onReconcileTarget(row.tenant_id, row.tenant_name)}
              className="flex items-center gap-1.5 text-xs text-violet-600 border border-violet-200 bg-violet-50 hover:bg-violet-100 px-3 py-1.5 rounded-xl transition font-medium"
            >
              <GitMerge className="w-3.5 h-3.5" /> دمج إلى هذا المتجر
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function Row({ label, val, copy: doCopy }: { label: string; val: string | null | undefined; copy?: boolean }) {
  if (!val) return null
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-slate-400 shrink-0">{label}:</span>
      <span className="font-mono text-slate-700 flex items-center gap-1 min-w-0 truncate">
        <span className="truncate">{val}</span>
        {doCopy && (
          <button onClick={() => copyText(val)} className="text-slate-300 hover:text-slate-500 shrink-0">
            <Copy className="w-2.5 h-2.5" />
          </button>
        )}
      </span>
    </div>
  )
}

// ── Duplicate Section ──────────────────────────────────────────────────────────

function DupList({ title, items, keyLabel, keyField }: {
  title:    string
  items:    Array<{ tenant_ids: number[] } & Record<string, unknown>>
  keyLabel: string
  keyField: string
}) {
  if (items.length === 0) return null
  return (
    <div className="bg-orange-50 border border-orange-200 rounded-2xl p-4 space-y-3">
      <h3 className="font-bold text-orange-800 text-sm flex items-center gap-2">
        <AlertTriangle className="w-4 h-4" /> {title} ({items.length})
      </h3>
      <div className="space-y-2">
        {items.map((item, i) => (
          <div key={i} className="bg-white rounded-xl border border-orange-100 px-3 py-2">
            <p className="text-xs text-slate-700 font-mono">
              {keyLabel}: <strong>{String(item[keyField] ?? '—')}</strong>
            </p>
            <p className="text-[10px] text-slate-400 mt-0.5">
              Tenants: {item.tenant_ids.join(', ')}
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Reconcile Panel ────────────────────────────────────────────────────────────

function ReconcilePanel({ targetId, targetName, onClose }: {
  targetId:   number
  targetName: string
  onClose:    () => void
}) {
  const [sourceId, setSourceId] = useState('')
  const [result, setResult]     = useState<ReconcileResult | null>(null)
  const [busy, setBusy]         = useState(false)
  const [error, setError]       = useState<string | null>(null)

  const run = async (dryRun: boolean) => {
    const sid = parseInt(sourceId.trim())
    if (!sid || isNaN(sid)) { setError('أدخل Source Tenant ID صحيح'); return }
    if (!dryRun && !window.confirm(
      `تنفيذ دمج حقيقي: Tenant #${sid} → #${targetId}؟\nلا يمكن التراجع!`
    )) return
    setBusy(true); setError(null); setResult(null)
    try {
      const data = await apiCall<ReconcileResult>('/admin/tenant-integrity/reconcile', {
        method: 'POST',
        body: JSON.stringify({ source_tenant_id: sid, target_tenant_id: targetId, dry_run: dryRun }),
      })
      setResult(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'خطأ')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/30 z-50 flex items-center justify-center p-4" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto p-6 space-y-4" dir="rtl">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="font-black text-slate-800 text-base flex items-center gap-2">
              <GitMerge className="w-5 h-5 text-violet-600" /> دمج المتاجر
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">الدمج إلى: <strong>{targetName}</strong> (#{targetId})</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 text-lg">✕</button>
        </div>

        <div className="space-y-2">
          <label className="text-xs font-medium text-slate-600">Source Tenant ID (المتجر الذي سيُحذف)</label>
          <input
            type="number"
            value={sourceId}
            onChange={e => setSourceId(e.target.value)}
            placeholder="Tenant ID"
            className="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
            dir="ltr"
          />
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => run(true)}
            disabled={busy}
            className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 border border-slate-200 rounded-xl text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-60 font-medium"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Eye className="w-4 h-4" />} معاينة فقط
          </button>
          <button
            onClick={() => run(false)}
            disabled={busy}
            className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 bg-red-500 hover:bg-red-600 text-white font-bold rounded-xl text-sm disabled:opacity-60"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />} تنفيذ الدمج
          </button>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 rounded-xl px-3 py-2 text-sm text-red-700">{error}</div>
        )}

        {result && (
          <div className="space-y-3">
            <div className={`flex items-center gap-2 px-3 py-2 rounded-xl border text-sm font-medium ${result.error ? 'bg-red-50 border-red-200 text-red-700' : result.dry_run ? 'bg-slate-50 border-slate-200 text-slate-700' : 'bg-emerald-50 border-emerald-200 text-emerald-700'}`}>
              {result.error ? '❌' : result.dry_run ? '👁 معاينة' : '✅ تم الدمج'}
              {result.status && <span className="font-mono text-xs">{result.status}</span>}
              {result.error && <span>{result.error}</span>}
            </div>

            {result.warnings.length > 0 && (
              <div className="space-y-1">
                {result.warnings.map((w, i) => (
                  <div key={i} className="text-xs bg-amber-50 border border-amber-200 text-amber-700 px-3 py-1.5 rounded-lg">
                    ⚠ {w}
                  </div>
                ))}
              </div>
            )}

            <div className="bg-slate-50 rounded-xl border border-slate-100 p-3 space-y-1 max-h-60 overflow-y-auto">
              <p className="text-xs font-bold text-slate-600 mb-2">الإجراءات ({result.actions.length})</p>
              {result.actions.map((a, i) => (
                <div key={i} className="text-[10px] text-slate-600 font-mono bg-white border border-slate-100 rounded-lg px-2 py-1">
                  {a.action.toUpperCase()} {a.resource}
                  {a.from_tenant && a.to_tenant ? ` (${a.from_tenant}→${a.to_tenant})` : ''}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Integrity Event Log ────────────────────────────────────────────────────────

function IntegrityEventLog() {
  const [entries, setEntries] = useState<IntegrityEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [eventFilter, setEventFilter] = useState('')

  const load = useCallback(() => {
    setLoading(true)
    const qs = eventFilter ? `?event=${encodeURIComponent(eventFilter)}&limit=60` : '?limit=60'
    apiCall<{ entries: IntegrityEvent[] }>(`/admin/tenant-integrity/events${qs}`)
      .then(d => setEntries(d.entries))
      .catch(() => setEntries([]))
      .finally(() => setLoading(false))
  }, [eventFilter])

  useEffect(() => { load() }, [load])

  const EVENT_TYPES = [
    'tenant_resolved', 'duplicate_identity', 'cross_tenant_conflict',
    'write_blocked', 'reconciliation_started', 'reconciliation_done',
    'orphaned_wa_connection', 'orphaned_store',
  ]

  return (
    <div className="bg-white border border-slate-100 rounded-2xl overflow-hidden shadow-sm">
      <div className="px-5 py-4 border-b border-slate-50 flex items-center justify-between flex-wrap gap-2">
        <h2 className="font-bold text-slate-800 text-sm">سجل أحداث Integrity</h2>
        <div className="flex items-center gap-2">
          <select
            value={eventFilter}
            onChange={e => setEventFilter(e.target.value)}
            className="text-xs border border-slate-200 rounded-lg px-2 py-1 focus:outline-none focus:ring-1 focus:ring-violet-400"
          >
            <option value="">كل الأحداث</option>
            {EVENT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <button onClick={load} className="text-xs text-sky-600 hover:underline flex items-center gap-1">
            <RefreshCw className="w-3 h-3" /> تحديث
          </button>
        </div>
      </div>

      {loading && (
        <div className="flex items-center justify-center h-16 text-slate-400 text-sm gap-2">
          <Loader2 className="w-4 h-4 animate-spin" /> جارٍ التحميل…
        </div>
      )}

      {!loading && entries.length === 0 && (
        <p className="px-5 py-4 text-sm text-slate-400">لا توجد أحداث بعد</p>
      )}

      {!loading && entries.length > 0 && (
        <div className="divide-y divide-slate-50 max-h-72 overflow-y-auto">
          {entries.map(e => (
            <div key={e.id} className="flex items-start gap-2 px-4 py-2">
              <EventBadge result={e.result} />
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium text-slate-700 truncate">
                  <span className="font-mono text-violet-600">{e.event}</span>
                  {e.action && <span className="text-slate-400 ml-1">· {e.action}</span>}
                </p>
                <p className="text-[10px] text-slate-400 truncate mt-0.5">
                  {[
                    e.tenant_id    && `T#${e.tenant_id}`,
                    e.phone_number_id && `phone=${e.phone_number_id}`,
                    e.waba_id      && `waba=${e.waba_id}`,
                    e.store_id     && `store=${e.store_id}`,
                  ].filter(Boolean).join(' · ')}
                  {e.detail ? ` — ${e.detail}` : ''}
                </p>
              </div>
              <span className="text-[10px] text-slate-300 shrink-0 hidden sm:block">
                {e.created_at ? new Date(e.created_at).toLocaleString('ar-SA') : ''}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────

export default function AdminTenantIntegrity() {
  const [data, setData]           = useState<AuditResponse | null>(null)
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState<string | null>(null)
  const [filter, setFilter]       = useState<TenantHealth | 'all'>('all')
  const [reconcileTarget, setReconcileTarget] = useState<{ id: number; name: string } | null>(null)

  const load = useCallback(() => {
    setLoading(true); setError(null)
    apiCall<AuditResponse>('/admin/tenant-integrity')
      .then(d => setData(d))
      .catch(e => setError(e instanceof Error ? e.message : 'خطأ في التحميل'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const s = data?.summary
  const filtered = data?.tenants.filter(t =>
    filter === 'all' ? true : t.health === filter
  ) ?? []

  const hasProblems = s && (
    s.critical > 0 || s.warning > 0 ||
    s.duplicate_phone_ids > 0 || s.duplicate_waba_ids > 0 ||
    s.duplicate_store_ids > 0
  )

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto" dir="rtl">

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-violet-700 flex items-center justify-center shadow-lg shadow-violet-600/30">
            <ShieldCheck className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">سلامة المستأجرين (Tenant Integrity)</h1>
            <p className="text-slate-400 text-xs">كل متجر يجب أن يملك هويات واتساب وسلة في نفس المستأجر</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex items-center gap-1.5 px-4 py-2 border border-slate-200 rounded-xl text-sm text-slate-600 hover:bg-slate-50 transition disabled:opacity-60"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          فحص الآن
        </button>
      </div>

      {/* Critical alert */}
      {!loading && hasProblems && (
        <div className="flex items-start gap-3 bg-red-50 border border-red-200 rounded-2xl px-4 py-3">
          <ShieldAlert className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
          <div className="text-sm text-red-700">
            <p className="font-bold">تحذير: تم اكتشاف مشاكل في تعزل المستأجرين</p>
            <p className="text-xs mt-0.5 text-red-600">
              {s?.duplicate_phone_ids ? `${s.duplicate_phone_ids} phone_number_id مكرر · ` : ''}
              {s?.duplicate_waba_ids  ? `${s.duplicate_waba_ids} WABA ID مكرر · ` : ''}
              {s?.duplicate_store_ids ? `${s.duplicate_store_ids} store_id مكرر · ` : ''}
              {s?.critical ? `${s.critical} متجر في حالة حرجة` : ''}
            </p>
          </div>
        </div>
      )}

      {/* Summary cards */}
      {s && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          {[
            { key: 'all' as const,        label: 'الكل',     value: s.total,               color: 'slate'   },
            { key: 'healthy' as const,    label: 'سليم',     value: s.healthy,             color: 'emerald' },
            { key: 'warning' as const,    label: 'تحذير',    value: s.warning,             color: 'amber'   },
            { key: 'critical' as const,   label: 'حرج',      value: s.critical,            color: 'red'     },
          ].map(({ key, label, value, color }) => (
            <button
              key={key}
              onClick={() => setFilter(f => f === key ? 'all' : key as typeof f)}
              className={`rounded-2xl p-3 text-right transition border-2 shadow-sm ${
                filter === key ? `border-${color}-400 bg-${color}-50` : 'border-transparent bg-white hover:bg-slate-50'
              }`}
            >
              <p className={`text-2xl font-black text-${color}-600`}>{value}</p>
              <p className="text-xs text-slate-400 mt-0.5">{label}</p>
            </button>
          ))}
          <div className="rounded-2xl bg-white border border-slate-100 shadow-sm p-3 text-right">
            <p className="text-sm font-black text-orange-600">
              {(s.duplicate_store_ids + s.duplicate_phone_ids + s.duplicate_waba_ids)}
            </p>
            <p className="text-[10px] text-slate-400 mt-0.5">تكرارات</p>
          </div>
        </div>
      )}

      {/* Duplicate reports */}
      {data && (
        <div className="grid sm:grid-cols-3 gap-4">
          <DupList title="Phone Number ID مكرر" items={data.duplicate_phone_ids} keyLabel="phone_number_id" keyField="phone_number_id" />
          <DupList title="WABA ID مكرر"         items={data.duplicate_waba_ids}  keyLabel="waba_id"         keyField="waba_id" />
          <DupList title="Store ID مكرر"         items={data.duplicate_store_ids} keyLabel="store_id"        keyField="store_id" />
        </div>
      )}

      {/* Orphans */}
      {data && (data.orphaned_wa.length > 0 || data.orphaned_stores.length > 0) && (
        <div className="grid sm:grid-cols-2 gap-4">
          {data.orphaned_wa.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4">
              <h3 className="font-bold text-amber-800 text-sm mb-2 flex items-center gap-1">
                <Wifi className="w-3.5 h-3.5" /> واتساب بلا متجر ({data.orphaned_wa.length})
              </h3>
              {data.orphaned_wa.map((o, i) => (
                <p key={i} className="text-xs text-slate-600 font-mono">T#{o.tenant_id} · {o.phone_number_id ?? '—'}</p>
              ))}
            </div>
          )}
          {data.orphaned_stores.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4">
              <h3 className="font-bold text-amber-800 text-sm mb-2 flex items-center gap-1">
                <Store className="w-3.5 h-3.5" /> متجر بلا واتساب ({data.orphaned_stores.length})
              </h3>
              {data.orphaned_stores.map((o, i) => (
                <p key={i} className="text-xs text-slate-600 font-mono">T#{o.tenant_id} · {o.store_id ?? '—'} ({o.provider})</p>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Tenant list */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-black text-slate-800 text-base">
            المتاجر
            {filter !== 'all' && (
              <button onClick={() => setFilter('all')} className="mr-2 text-xs text-sky-500 font-normal hover:underline">
                {HEALTH_CFG[filter as TenantHealth]?.label} ✕
              </button>
            )}
          </h2>
          {data && <span className="text-xs text-slate-400">{filtered.length} / {data.tenants.length}</span>}
        </div>

        {loading && (
          <div className="flex items-center justify-center h-32 bg-white border border-slate-100 rounded-2xl text-slate-400 gap-2">
            <Loader2 className="w-5 h-5 animate-spin text-violet-500" />
            <span className="text-sm">جارٍ الفحص…</span>
          </div>
        )}

        {!loading && error && (
          <div className="bg-red-50 border border-red-200 rounded-2xl px-4 py-4">
            <p className="text-sm font-bold text-red-700">تعذّر التحميل</p>
            <p className="text-xs text-red-600 font-mono mt-1">{error}</p>
            <button onClick={load} className="mt-2 text-xs text-red-700 border border-red-300 rounded-lg px-3 py-1.5 hover:bg-red-100">إعادة المحاولة</button>
          </div>
        )}

        {!loading && !error && filtered.length === 0 && (
          <div className="bg-emerald-50 border border-emerald-200 rounded-2xl px-5 py-6 text-center">
            <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto mb-2" />
            <p className="text-sm font-bold text-emerald-800">
              {filter === 'all' ? 'لا يوجد مستأجرون' : 'لا توجد مستأجرون في هذه الحالة'}
            </p>
          </div>
        )}

        {!loading && !error && filtered.length > 0 && (
          <div className="space-y-2">
            {filtered.map(row => (
              <TenantIntegrityRow
                key={row.tenant_id}
                row={row}
                onReconcileTarget={(id, name) => setReconcileTarget({ id, name })}
              />
            ))}
          </div>
        )}
      </div>

      {/* Integrity event log */}
      <IntegrityEventLog />

      {/* Reconcile modal */}
      {reconcileTarget && (
        <ReconcilePanel
          targetId={reconcileTarget.id}
          targetName={reconcileTarget.name}
          onClose={() => setReconcileTarget(null)}
        />
      )}
    </div>
  )
}
