import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { AlertOctagon, FlaskConical, Loader2, RefreshCw, RotateCcw, Search, Send } from 'lucide-react'
import {
  INTERNAL_E2E_ALIASES,
  internalE2EApi,
  type InternalE2EAlias,
  type InternalE2EResult,
  type InternalE2EStatus,
} from '../api/internalE2E'
import { internalE2EStopReasons } from '../lib/internalE2ESafety'

type Busy = 'status' | 'provision' | 'reset' | 'turn' | 'lookup' | null
type LookupKind = 'trace' | 'message'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function Metric({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
      <div className="text-[11px] font-medium text-slate-500">{label}</div>
      <div className="mt-1 break-all font-mono text-sm font-bold text-slate-800">
        {value === null || value === undefined ? '—' : String(value)}
      </div>
    </div>
  )
}

function ResultEvidence({ result }: { result: InternalE2EResult }) {
  const stopReasons = internalE2EStopReasons(result)
  return (
    <section className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" data-internal-e2e-result>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-black text-slate-800">Result and safety evidence</h2>
          <p className="mt-1 text-xs text-slate-500">Read-only artifact returned by the existing operator API.</p>
        </div>
        <span className={`rounded-full px-3 py-1 text-xs font-black ${stopReasons.length ? 'bg-red-100 text-red-700' : 'bg-emerald-100 text-emerald-700'}`}>
          {stopReasons.length ? 'STOP' : 'SAFETY PASS'}
        </span>
      </div>

      {stopReasons.length > 0 && (
        <div role="alert" data-safety-stop className="rounded-xl border-2 border-red-300 bg-red-50 p-4">
          <div className="flex items-center gap-2 font-black text-red-800">
            <AlertOctagon className="h-5 w-5" />
            Safety invariant failed — do not submit another turn
          </div>
          <p className="mt-2 break-words font-mono text-xs text-red-700">{stopReasons.join(', ')}</p>
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Metric label="Status" value={result.status} />
        <Metric label="Alias" value={result.account_alias} />
        <Metric label="Latency (ms)" value={result.total_runner_latency_ms} />
        <Metric label="Fallback" value={result.fallback_type} />
        <Metric label="Trace ID" value={result.trace_id} />
        <Metric label="Case ID" value={result.case_id} />
        <Metric label="Internal inbound ID" value={result.internal_inbound_message_id} />
        <Metric label="Internal outbound ID" value={result.internal_outbound_message_id} />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric label="External egress" value={result.external_egress_count} />
        <Metric label="Cross-tenant leakage" value={result.cross_tenant_leakage} />
        <Metric label="Cross-customer leakage" value={result.cross_customer_leakage} />
        <Metric label="Write mutations" value={result.write_mutations} />
        <Metric label="Salla mutations" value={result.salla_mutations} />
        <Metric label="Unsupported claims" value={result.unsupported_commercial_claims} />
        <Metric label="Duplicate replies" value={result.duplicate_replies} />
        <Metric label="Silent V1 fallback" value={result.silent_v1_fallback} />
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200 p-4">
          <h3 className="text-xs font-bold text-slate-600">Tools</h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {(result.tool_calls ?? []).length
              ? result.tool_calls?.map(tool => <code key={tool} className="rounded bg-indigo-50 px-2 py-1 text-xs text-indigo-700">{tool}</code>)
              : <span className="text-xs text-slate-400">None</span>}
          </div>
        </div>
        <div className="rounded-xl border border-slate-200 p-4">
          <h3 className="text-xs font-bold text-slate-600">Guardrail result</h3>
          <p className="mt-2 text-sm font-bold">{String(result.guardrail_passed ?? '—')}</p>
          <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
            {JSON.stringify(result.guardrail_result ?? {}, null, 2)}
          </pre>
        </div>
      </div>

      <div className="rounded-xl border border-slate-200 p-4">
        <h3 className="text-xs font-bold text-slate-600">Safety proofs</h3>
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
          {JSON.stringify(result.safety_proofs ?? {}, null, 2)}
        </pre>
      </div>
    </section>
  )
}

export default function AdminInternalE2E() {
  const [status, setStatus] = useState<InternalE2EStatus | null>(null)
  const [result, setResult] = useState<InternalE2EResult | null>(null)
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [alias, setAlias] = useState<InternalE2EAlias>('A')
  const [turnText, setTurnText] = useState('')
  const [caseId, setCaseId] = useState('')
  const [lookupKind, setLookupKind] = useState<LookupKind>('trace')
  const [lookupValue, setLookupValue] = useState('')

  const stopReasons = useMemo(() => internalE2EStopReasons(result), [result])
  const enabled = status?.enabled === true
  const controlsLocked = !enabled || stopReasons.length > 0 || busy !== null
  const fixtureCount = Object.values(status?.fixtures ?? {}).filter(item => item?.provisioned).length

  const refreshStatus = useCallback(async () => {
    setBusy('status')
    setError('')
    try {
      setStatus(await internalE2EApi.status())
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(null)
    }
  }, [])

  useEffect(() => { void refreshStatus() }, [refreshStatus])

  const provision = async () => {
    setBusy('provision')
    setError('')
    try {
      await internalE2EApi.provision()
      setStatus(await internalE2EApi.status())
      setNotice('A/B/C fixtures provisioned.')
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(null)
    }
  }

  const reset = async (target: InternalE2EAlias) => {
    setBusy('reset')
    setError('')
    try {
      await internalE2EApi.reset(target)
      setStatus(await internalE2EApi.status())
      setNotice(`Reset complete: ${target}`)
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(null)
    }
  }

  const submitTurn = async (event: FormEvent) => {
    event.preventDefault()
    if (controlsLocked || !turnText.trim() || !caseId.trim()) return
    setBusy('turn')
    setError('')
    try {
      const artifact = await internalE2EApi.submitTurn({
        alias,
        text: turnText.trim(),
        caseId: caseId.trim(),
      })
      setResult(artifact)
      setNotice('Turn complete. Review all safety evidence before continuing.')
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(null)
    }
  }

  const lookup = async (event: FormEvent) => {
    event.preventDefault()
    if (!lookupValue.trim()) return
    setBusy('lookup')
    setError('')
    try {
      setResult(await internalE2EApi.result(
        lookupKind === 'trace'
          ? { traceId: lookupValue.trim() }
          : { internalMessageId: lookupValue.trim() },
      ))
    } catch (error) {
      setError(message(error))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-6 p-4 md:p-6" dir="auto" data-internal-e2e-admin-page>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-slate-900 text-white">
            <FlaskConical className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-black text-slate-900">INTERNAL_E2E Operator</h1>
            <p className="mt-1 text-xs text-slate-500">Tenant 1 controlled A/B/C smoke — Platform Admin only</p>
          </div>
        </div>
        <button type="button" onClick={() => void refreshStatus()} disabled={busy !== null} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-bold disabled:opacity-40">
          {busy === 'status' ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          Refresh Status
        </button>
      </header>

      {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm font-semibold text-red-700">{error}</div>}
      {notice && <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm font-semibold text-emerald-700">{notice}</div>}

      {!enabled && status && (
        <div role="status" data-internal-e2e-disabled className="rounded-2xl border-2 border-amber-300 bg-amber-50 p-5">
          <div className="flex items-center gap-2 font-black text-amber-900"><AlertOctagon className="h-5 w-5" /> INTERNAL_E2E DISABLED</div>
          <p className="mt-2 text-sm text-amber-800">Provision, reset, and turn controls are locked.</p>
        </div>
      )}

      {status && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-black text-slate-800">Runtime status</h2>
          <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Metric label="Enabled" value={status.enabled} />
            <Metric label="Effective allowlist" value={status.configured_allowlist || '—'} />
            <Metric label="Hard-scoped tenant" value={status.operator_tenant_id} />
            <Metric label="Fixture count" value={fixtureCount} />
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            {status.approved_aliases.map(item => (
              <span key={item} className={`rounded-full px-3 py-1 text-xs font-bold ${status.fixtures[item]?.provisioned ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                {item}: {status.fixtures[item]?.provisioned ? 'present' : 'absent'}
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <div className="space-y-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div>
            <h2 className="font-black text-slate-800">Synthetic fixtures</h2>
            <p className="mt-1 text-xs text-slate-500">Server-fixed to Tenant 1 and aliases A/B/C.</p>
          </div>
          <button type="button" onClick={() => void provision()} disabled={controlsLocked} className="w-full rounded-xl bg-slate-900 px-4 py-3 text-sm font-bold text-white disabled:opacity-40">
            Provision A/B/C
          </button>
          <div className="grid grid-cols-3 gap-2">
            {INTERNAL_E2E_ALIASES.map(item => (
              <button key={item} type="button" onClick={() => void reset(item)} disabled={controlsLocked || !status?.fixtures[item]?.provisioned} className="inline-flex items-center justify-center gap-1 rounded-xl border border-slate-200 px-3 py-2 text-xs font-bold disabled:opacity-40">
                <RotateCcw className="h-3.5 w-3.5" /> Reset {item}
              </button>
            ))}
          </div>
        </div>

        <form onSubmit={submitTurn} data-auto-turn-form className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h2 className="font-black text-slate-800">Single controlled turn</h2>
          <p className="text-xs text-slate-500">AUTO is the only service tier exposed for this smoke.</p>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="space-y-1 text-xs font-bold">Alias
              <select value={alias} onChange={event => setAlias(event.target.value as InternalE2EAlias)} disabled={controlsLocked} className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm">
                {INTERNAL_E2E_ALIASES.map(item => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="space-y-1 text-xs font-bold">Service tier
              <select value="auto" disabled className="w-full rounded-xl border border-slate-200 bg-slate-100 px-3 py-2 text-sm">
                <option value="auto">AUTO</option>
              </select>
            </label>
          </div>
          <label className="block space-y-1 text-xs font-bold">Case ID
            <input value={caseId} onChange={event => setCaseId(event.target.value)} maxLength={96} disabled={controlsLocked} className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm" />
          </label>
          <label className="block space-y-1 text-xs font-bold">Text
            <textarea value={turnText} onChange={event => setTurnText(event.target.value)} maxLength={6000} rows={4} disabled={controlsLocked} className="w-full rounded-xl border border-slate-200 px-3 py-2 text-sm" />
          </label>
          <button type="submit" disabled={controlsLocked || !turnText.trim() || !caseId.trim()} className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 py-3 text-sm font-bold text-white disabled:opacity-40">
            <Send className="h-4 w-4" /> Submit AUTO Turn
          </button>
        </form>
      </section>

      <form onSubmit={lookup} data-result-lookup className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="font-black text-slate-800">Result lookup</h2>
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-[180px_1fr_auto]">
          <select value={lookupKind} onChange={event => setLookupKind(event.target.value as LookupKind)} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
            <option value="trace">Trace ID</option>
            <option value="message">Internal message ID</option>
          </select>
          <input value={lookupValue} onChange={event => setLookupValue(event.target.value)} className="rounded-xl border border-slate-200 px-3 py-2 font-mono text-sm" />
          <button type="submit" disabled={!enabled || busy !== null || !lookupValue.trim()} className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 px-4 py-2 text-sm font-bold disabled:opacity-40">
            <Search className="h-4 w-4" /> Lookup
          </button>
        </div>
      </form>

      {result && <ResultEvidence result={result} />}
    </div>
  )
}
