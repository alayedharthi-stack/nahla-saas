/**
 * AdminSallaTokenStatus.tsx — /admin/salla/integrations/token-status
 *
 * Owner-side dashboard for Salla Easy Mode token health. Mounted at the same
 * URL referenced by the [SALLA TOKEN] reauth alert email so a click on
 * "Open Token Status Dashboard" lands on the correct page.
 *
 * Features
 * ────────
 * • Aggregate summary (ok / warning / critical / expired / needs_reauth).
 * • Filter by ?tenant_id=N (preselected from the alert email link).
 * • Per-row deep diagnose: lists every integration record for a tenant +
 *   stores grouped by store_id so duplicate / superseded rows are obvious.
 * • Manual "Force Refresh" button per integration that calls the new
 *   /admin/salla/integrations/{id}/refresh endpoint and shows the Salla
 *   response + DB-state-before / DB-state-after for ops verification.
 * • "Dry Run" toggle so ops can probe Salla without mutating DB state.
 *
 * The page is gated server-side by ENABLE_ADMIN_DEBUG=true. If the flag is
 * off the API returns 403 and we surface a clear error.
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, CheckCircle2, RefreshCw, Search, ShieldAlert,
  AlertCircle, Loader2, ChevronDown, ChevronRight, Copy, Clock,
  KeyRound, ExternalLink,
} from 'lucide-react'
import {
  adminApi,
  type SallaTokenRow,
  type SallaTokenStatusResponse,
  type SallaDiagnoseResponse,
  type SallaForceRefreshResponse,
} from '../api/admin'

function classNames(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('en-CA', { hour12: false })
  } catch {
    return iso
  }
}

function HealthPill({ row }: { row: SallaTokenRow }) {
  if (row.needs_reauth) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-100 text-rose-700 text-xs font-semibold">
        <ShieldAlert className="w-3 h-3" /> needs_reauth
      </span>
    )
  }
  if (row.superseded) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-200 text-slate-600 text-xs font-semibold">
        superseded
      </span>
    )
  }
  if (row.expiry_health === 'expired') {
    return <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-rose-100 text-rose-700 text-xs font-semibold">expired</span>
  }
  if (row.expiry_health === 'critical') {
    return <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-orange-100 text-orange-700 text-xs font-semibold">critical</span>
  }
  if (row.expiry_health === 'warning') {
    return <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 text-xs font-semibold">warning</span>
  }
  if (row.expiry_health === 'ok') {
    return <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-xs font-semibold">healthy</span>
  }
  return <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold">unknown</span>
}

function SummaryStat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-3 min-w-[110px]">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={classNames('text-2xl font-bold', color)}>{value}</div>
    </div>
  )
}

function CopyBtn({ value }: { value: string | null | undefined }) {
  const [done, setDone] = useState(false)
  if (!value) return null
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(value).then(() => {
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        })
      }}
      className="text-slate-400 hover:text-slate-600 inline-flex items-center"
      title="Copy"
      type="button"
    >
      {done ? <CheckCircle2 className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
    </button>
  )
}

function FieldRow({ label, value, mono = false }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-3 text-xs">
      <span className="text-slate-500 min-w-[170px]">{label}</span>
      <span className={classNames('text-slate-800', mono && 'font-mono')}>{value ?? '—'}</span>
    </div>
  )
}

function RefreshOutcomeCard({ result }: { result: SallaForceRefreshResponse }) {
  const ok = result.ok
  const outcome = result.outcome ?? result.reason ?? '—'
  const tone =
    outcome === 'refreshed'                     ? 'bg-emerald-50 border-emerald-200 text-emerald-800' :
    outcome === 'superseded_invalid_grant'      ? 'bg-slate-50 border-slate-200 text-slate-700'      :
    outcome === 'invalid_grant_needs_reauth'    ? 'bg-rose-50 border-rose-200 text-rose-800'         :
    outcome === 'transient_failure'             ? 'bg-amber-50 border-amber-200 text-amber-800'      :
                                                  'bg-slate-50 border-slate-200 text-slate-700'
  return (
    <div className={classNames('rounded-xl border p-3 text-xs space-y-2 mt-3', tone)}>
      <div className="flex items-center gap-2 font-semibold">
        {ok ? <CheckCircle2 className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
        outcome: {outcome}
        {result.dry_run && <span className="ml-2 px-1.5 py-0.5 rounded bg-slate-200 text-slate-700 text-[10px]">DRY RUN</span>}
      </div>
      {result.error && <div className="text-rose-700">error: {result.error}</div>}
      {result.salla_response && (
        <details>
          <summary className="cursor-pointer text-slate-600">Salla response (HTTP {result.salla_response.status ?? '—'})</summary>
          <pre className="mt-2 p-2 bg-white border border-slate-200 rounded overflow-auto max-h-48 text-[11px]">
            {JSON.stringify(result.salla_response.body, null, 2)}
          </pre>
        </details>
      )}
      {(result.before || result.after) && (
        <details>
          <summary className="cursor-pointer text-slate-600">DB state before / after</summary>
          <div className="grid md:grid-cols-2 gap-2 mt-2">
            {result.before && (
              <pre className="p-2 bg-white border border-slate-200 rounded overflow-auto max-h-64 text-[11px]">
                {JSON.stringify(result.before, null, 2)}
              </pre>
            )}
            {result.after && (
              <pre className="p-2 bg-white border border-slate-200 rounded overflow-auto max-h-64 text-[11px]">
                {JSON.stringify(result.after, null, 2)}
              </pre>
            )}
          </div>
        </details>
      )}
    </div>
  )
}

function IntegrationCard({ row, onChanged }: { row: SallaTokenRow; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [dryRun, setDryRun] = useState(false)
  const [result, setResult] = useState<SallaForceRefreshResponse | null>(null)
  const [err, setErr] = useState<string>('')

  const runRefresh = async () => {
    setErr('')
    setRefreshing(true)
    try {
      const r = await adminApi.sallaForceRefresh(row.integration_id, { dry_run: dryRun })
      setResult(r)
      if (!dryRun) onChanged()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  const counterAnomaly = (row.token_refresh_error || row.needs_reauth_reason) && row.refresh_attempts === 0
  const showShadow = row.shadow

  return (
    <div className={classNames(
      'border rounded-xl bg-white shadow-sm overflow-hidden',
      row.needs_reauth ? 'border-rose-300' : showShadow ? 'border-slate-300 bg-slate-50' : 'border-slate-200',
    )}>
      <button
        onClick={() => setOpen(o => !o)}
        type="button"
        className="w-full flex items-center justify-between gap-3 px-4 py-3 hover:bg-slate-50"
      >
        <div className="flex items-center gap-3 min-w-0">
          {open ? <ChevronDown className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
          <div className="text-left min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-sm text-slate-800">tenant #{row.tenant_id}</span>
              <span className="text-xs text-slate-500">integration #{row.integration_id}</span>
              {row.store_id && <span className="text-xs text-slate-400">store {row.store_id}</span>}
              {row.app_type && <span className="text-[10px] uppercase tracking-wide bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">{row.app_type}</span>}
            </div>
            <div className="text-xs text-slate-500 truncate">
              {row.store_name || '—'} · expires {fmtDate(row.expires_at)}
              {row.days_until_expiry !== null && (
                <span className="ml-1">({row.days_until_expiry.toFixed(1)} d)</span>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {showShadow && <span className="text-[10px] uppercase tracking-wide bg-slate-200 text-slate-600 px-1.5 py-0.5 rounded">shadow</span>}
          {row.alert_suppressed && <span className="text-[10px] uppercase tracking-wide bg-violet-100 text-violet-700 px-1.5 py-0.5 rounded">alert muted</span>}
          <HealthPill row={row} />
        </div>
      </button>

      {open && (
        <div className="px-4 pb-4 pt-1 space-y-3 border-t border-slate-100">
          {counterAnomaly && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <div>
                <div className="font-semibold">refresh_attempts = 0 with last_error set.</div>
                <div>
                  This indicates the alert was emitted before the counter could be stamped (legacy invalid_grant path).
                  The latest backend now stamps attempts ≥ 1 on every observed failure. Click "Force Refresh" to restamp this row.
                </div>
              </div>
            </div>
          )}

          {row.superseded && (
            <div className="rounded-lg border border-slate-300 bg-slate-50 p-3 text-xs text-slate-700 flex items-start gap-2">
              <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0 text-emerald-500" />
              <div>
                Superseded by integration #{row.superseded_by_integration_id ?? '—'} at {fmtDate(row.superseded_at)}.
                Owner alerts for this row are suppressed automatically.
              </div>
            </div>
          )}

          {showShadow && !row.superseded && (
            <div className="rounded-lg border border-slate-300 bg-slate-50 p-3 text-xs text-slate-700">
              A newer healthy integration (#{row.newest_healthy_sibling_id}) exists for the same store.
              This row is treated as a shadow record and won't drive merchant traffic.
            </div>
          )}

          <div className="grid md:grid-cols-2 gap-x-6 gap-y-1">
            <FieldRow label="store_id"             value={<><span>{row.store_id ?? '—'}</span> <CopyBtn value={row.store_id} /></>} mono />
            <FieldRow label="token_source"         value={row.token_source} />
            <FieldRow label="app_type"             value={row.app_type} />
            <FieldRow label="enabled"              value={String(row.enabled)} />
            <FieldRow label="created_at"           value={fmtDate(row.created_at)} />
            <FieldRow label="updated_at"           value={fmtDate(row.updated_at)} />
            <FieldRow label="token_expires_at"     value={fmtDate(row.token_expires_at)} />
            <FieldRow label="last_successful_refresh" value={fmtDate(row.last_successful_refresh)} />
            <FieldRow label="last_failed_refresh"  value={fmtDate(row.last_failed_refresh)} />
            <FieldRow label="first_failure_at"     value={fmtDate(row.first_failure_at)} />
            <FieldRow label="refresh_attempts"     value={<span className={row.refresh_attempts > 0 ? 'font-bold text-rose-600' : ''}>{row.refresh_attempts}</span>} />
            <FieldRow label="last_error"           value={<span className={row.token_refresh_error ? 'text-rose-700 font-mono' : ''}>{row.token_refresh_error}</span>} />
            <FieldRow label="needs_reauth"         value={String(row.needs_reauth)} />
            <FieldRow label="needs_reauth_reason"  value={row.needs_reauth_reason} />
            <FieldRow label="alert_sent_at"        value={fmtDate(row.token_reauth_alert_sent_at)} />
            <FieldRow label="has_refresh_token"    value={String(row.has_refresh_token)} />
            <FieldRow label="has_access_token"     value={String(row.has_access_token)} />
            <FieldRow label="connected_at"         value={fmtDate(row.connected_at)} />
          </div>

          <div className="border-t border-slate-100 pt-3 flex flex-wrap items-center gap-2">
            <label className="inline-flex items-center gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)} />
              Dry run (don't mutate DB)
            </label>
            <button
              onClick={runRefresh}
              disabled={refreshing || !row.has_refresh_token}
              type="button"
              className="ml-auto inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-900 disabled:bg-slate-300 text-white text-xs font-semibold"
            >
              {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
              Force Refresh
            </button>
            {!row.has_refresh_token && (
              <span className="text-[11px] text-slate-500">no refresh_token stored</span>
            )}
          </div>
          {err && <div className="text-xs text-rose-700">{err}</div>}
          {result && <RefreshOutcomeCard result={result} />}
        </div>
      )}
    </div>
  )
}

export default function AdminSallaTokenStatus() {
  const [search, setSearch] = useSearchParams()
  const tenantParam = search.get('tenant_id')
  const tenantId = tenantParam ? Number(tenantParam) : undefined

  const [data,      setData]      = useState<SallaTokenStatusResponse | null>(null)
  const [diagnose,  setDiagnose]  = useState<SallaDiagnoseResponse  | null>(null)
  const [loading,   setLoading]   = useState(true)
  const [err,       setErr]       = useState<string>('')
  const [filter,    setFilter]    = useState('')
  const [enabledOnly, setEnabledOnly] = useState(false)

  const load = async () => {
    setLoading(true); setErr('')
    try {
      const r = await adminApi.sallaTokenStatus({ tenant_id: tenantId, enabled_only: enabledOnly })
      setData(r)
      if (tenantId) {
        try {
          const d = await adminApi.sallaDiagnose(tenantId)
          setDiagnose(d)
        } catch {
          setDiagnose(null)
        }
      } else {
        setDiagnose(null)
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed to load')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, enabledOnly])

  const visible = useMemo(() => {
    if (!data) return []
    const q = filter.trim().toLowerCase()
    if (!q) return data.integrations
    return data.integrations.filter(r =>
      String(r.tenant_id).includes(q) ||
      String(r.integration_id).includes(q) ||
      (r.store_id ?? '').toLowerCase().includes(q) ||
      (r.store_name ?? '').toLowerCase().includes(q),
    )
  }, [data, filter])

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-orange-100 flex items-center justify-center">
          <KeyRound className="w-5 h-5 text-orange-600" />
        </div>
        <div>
          <h1 className="text-xl font-bold text-slate-800">Salla Token Status</h1>
          <p className="text-xs text-slate-500">Token health, refresh history, and force-refresh tooling for ops.</p>
        </div>
        <button
          onClick={load}
          type="button"
          className="ml-auto inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-white hover:bg-slate-50 border border-slate-200 text-xs font-semibold text-slate-700"
        >
          <RefreshCw className={classNames('w-3.5 h-3.5', loading && 'animate-spin')} />
          Reload
        </button>
      </div>

      {err && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">{err}</div>
            <div className="text-xs">
              Check that <code className="font-mono">ENABLE_ADMIN_DEBUG=true</code> is set on the backend and that you have access.
            </div>
          </div>
        </div>
      )}

      {data && (
        <div className="flex flex-wrap gap-3">
          <SummaryStat label="total"        value={data.summary.total}             color="text-slate-800" />
          <SummaryStat label="ok"           value={data.summary.expiry_ok}         color="text-emerald-600" />
          <SummaryStat label="warning"      value={data.summary.expiry_warning}    color="text-amber-600" />
          <SummaryStat label="critical"     value={data.summary.expiry_critical}   color="text-orange-600" />
          <SummaryStat label="expired"      value={data.summary.expiry_expired}    color="text-rose-600" />
          <SummaryStat label="needs_reauth" value={data.summary.needs_reauth}      color="text-rose-700" />
          <SummaryStat label="failed last"  value={data.summary.failed_last_refresh} color="text-rose-700" />
          <SummaryStat label="no refresh_token" value={data.summary.no_refresh_token} color="text-slate-600" />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="filter by tenant_id / store_id / store name"
            className="pl-9 pr-3 py-2 rounded-lg border border-slate-200 text-sm w-80 bg-white"
          />
        </div>
        <label className="inline-flex items-center gap-2 text-xs text-slate-600">
          <input type="checkbox" checked={enabledOnly} onChange={e => setEnabledOnly(e.target.checked)} />
          enabled only
        </label>
        {tenantId !== undefined && (
          <button
            onClick={() => setSearch({})}
            type="button"
            className="text-xs text-slate-600 hover:text-slate-900 underline"
          >
            clear tenant filter (#{tenantId})
          </button>
        )}
      </div>

      {diagnose && (
        <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-slate-500" />
            <h2 className="font-semibold text-sm text-slate-800">Tenant #{diagnose.tenant_id} — diagnose</h2>
            <span className="ml-auto text-xs text-slate-500">
              {diagnose.summary.total} rows · {diagnose.summary.stores} stores
              {diagnose.summary.duplicate_stores > 0 && ` · ${diagnose.summary.duplicate_stores} duplicate stores`}
            </span>
          </div>
          {diagnose.selected && (
            <div className="text-xs text-slate-700">
              <span className="text-slate-500">Selected (canonical):</span>{' '}
              <span className="font-mono">integration #{diagnose.selected.integration_id}</span>
              {' · '}
              store <span className="font-mono">{diagnose.selected.store_id ?? '—'}</span>
              {' · '}
              <HealthPill row={diagnose.selected} />
            </div>
          )}
          {diagnose.summary.duplicate_stores > 0 && (
            <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              This tenant has duplicate integration rows for the same store_id.
              Older rows that have a newer healthy sibling are tagged "shadow"
              and their reauth alerts are suppressed automatically by the backend.
            </div>
          )}
        </div>
      )}

      <div className="space-y-2">
        {loading && (
          <div className="text-center py-12 text-slate-400 text-sm">
            <Loader2 className="w-5 h-5 animate-spin inline-block mr-2" />
            loading…
          </div>
        )}
        {!loading && visible.length === 0 && (
          <div className="text-center py-12 text-slate-400 text-sm">
            no integrations match.
          </div>
        )}
        {visible.map(row => (
          <IntegrationCard key={row.integration_id} row={row} onChanged={load} />
        ))}
      </div>

      <div className="text-[11px] text-slate-400 pt-4 flex items-center gap-1">
        <ExternalLink className="w-3 h-3" />
        Endpoint: <code className="font-mono">/admin/salla/integrations/token-status</code>
        {' · '}
        diagnose: <code className="font-mono">/admin/salla/diagnose/&#123;tenant_id&#125;</code>
        {' · '}
        force refresh: <code className="font-mono">POST /admin/salla/integrations/&#123;id&#125;/refresh</code>
      </div>
    </div>
  )
}
