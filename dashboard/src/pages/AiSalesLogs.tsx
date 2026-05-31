import { useState, useEffect, useCallback } from 'react'
import {
  BrainCircuit, RefreshCw, Loader2, ShoppingCart, CreditCard,
  UserCheck, Package, AlertCircle, CheckCircle, Search,
} from 'lucide-react'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import { aiSalesApi, type AiSalesLogEntry, AI_SALES_INTENT_META } from '../api/aiSalesAgent'
import type { Translations } from '../i18n/types'

type SalesAgentLabels = Translations['salesAgentPage']

function formatTime(iso: string | null, locale: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
}

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100)
  const color =
    pct >= 80 ? 'bg-emerald-100 text-emerald-700' :
    pct >= 55 ? 'bg-amber-100  text-amber-700'    :
                'bg-slate-100  text-slate-500'
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${color}`}>
      {pct}%
    </span>
  )
}

function BoolBadge({ value, trueLabel, falseLabel }: { value: boolean; trueLabel: string; falseLabel: string }) {
  return value
    ? <span className="flex items-center gap-1 text-xs text-emerald-600"><CheckCircle className="w-3 h-3" />{trueLabel}</span>
    : <span className="text-xs text-slate-400">{falseLabel}</span>
}

function LogRow({ entry, sp, locale }: { entry: AiSalesLogEntry; sp: SalesAgentLabels; locale: string }) {
  const [expanded, setExpanded] = useState(false)
  const meta = AI_SALES_INTENT_META[entry.intent] ?? AI_SALES_INTENT_META['general']

  return (
    <>
      <tr
        className="border-b border-slate-50 hover:bg-slate-50/60 cursor-pointer transition-colors"
        onClick={() => setExpanded(e => !e)}
      >
        <td className="px-4 py-3 text-xs text-slate-400 whitespace-nowrap">
          {formatTime(entry.timestamp, locale)}
        </td>
        <td className="px-4 py-3">
          <p className="text-sm font-medium text-slate-800">{entry.customer_name}</p>
          <p className="text-xs text-slate-400 font-mono">{entry.customer_phone}</p>
        </td>
        <td className="px-4 py-3">
          <span className="flex items-center gap-1.5 text-xs font-medium text-slate-700">
            <span>{meta.emoji}</span>
            {meta.label}
          </span>
        </td>
        <td className="px-4 py-3">
          <ConfidenceBadge value={entry.confidence} />
        </td>
        <td className="px-4 py-3 text-center">
          {entry.product_used
            ? <Package className="w-4 h-4 text-brand-400 mx-auto" />
            : <span className="text-slate-200">—</span>}
        </td>
        <td className="px-4 py-3">
          {entry.order_created
            ? <span className="flex items-center gap-1 text-xs font-medium text-emerald-600">
                <ShoppingCart className="w-3 h-3" />
                {entry.order_id ? `#${entry.order_id}` : sp.detail.yes}
              </span>
            : <span className="text-xs text-slate-300">—</span>}
        </td>
        <td className="px-4 py-3 text-center">
          {entry.payment_link_sent
            ? <CreditCard className="w-4 h-4 text-violet-400 mx-auto" />
            : <span className="text-slate-200">—</span>}
        </td>
        <td className="px-4 py-3 text-center">
          {entry.handoff_triggered
            ? <UserCheck className="w-4 h-4 text-amber-400 mx-auto" />
            : <span className="text-slate-200">—</span>}
        </td>
      </tr>

      {expanded && (
        <tr className="bg-slate-50">
          <td colSpan={8} className="px-4 pb-4 pt-1">
            <div className="grid sm:grid-cols-2 gap-4 mt-2">
              <div>
                <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide mb-1">{sp.detail.customerMessage}</p>
                <p className="text-sm text-slate-700 bg-white rounded-lg px-3 py-2 border border-slate-100">
                  {entry.message || '—'}
                </p>
              </div>
              <div>
                <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide mb-1">{sp.detail.aiReply}</p>
                <p className="text-sm text-slate-700 bg-white rounded-lg px-3 py-2 border border-slate-100 whitespace-pre-line">
                  {entry.response_text || '—'}
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-4 mt-3 text-xs">
              <BoolBadge value={entry.product_used}      trueLabel={sp.flags.catalogUsed}        falseLabel={sp.flags.catalogNotUsed} />
              <BoolBadge value={entry.order_created}     trueLabel={sp.flags.orderCreated}       falseLabel={sp.flags.orderNotCreated} />
              <BoolBadge value={entry.payment_link_sent} trueLabel={sp.flags.paymentLinkSent}   falseLabel={sp.flags.paymentLinkNotSent} />
              <BoolBadge value={entry.handoff_triggered} trueLabel={sp.flags.handoff}           falseLabel={sp.flags.noHandoff} />
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function StatsStrip({ logs, sp }: { logs: AiSalesLogEntry[]; sp: SalesAgentLabels }) {
  const total           = logs.length
  const ordersCreated   = logs.filter(l => l.order_created).length
  const paymentsSent    = logs.filter(l => l.payment_link_sent).length
  const handoffs        = logs.filter(l => l.handoff_triggered).length
  const avgConfidence   = total > 0
    ? Math.round(logs.reduce((s, l) => s + l.confidence, 0) / total * 100)
    : 0

  const stats = [
    { label: sp.stats.total,         value: total,               color: 'text-slate-800' },
    { label: sp.stats.ordersCreated, value: ordersCreated,       color: 'text-emerald-600' },
    { label: sp.stats.paymentsSent,  value: paymentsSent,        color: 'text-violet-600' },
    { label: sp.stats.handoffs,      value: handoffs,            color: 'text-amber-600' },
    { label: sp.stats.avgConfidence, value: `${avgConfidence}%`, color: 'text-brand-600' },
  ]

  return (
    <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
      {stats.map(({ label, value, color }) => (
        <div key={label} className="card px-4 py-3 text-center">
          <p className={`text-xl font-bold ${color}`}>{value}</p>
          <p className="text-xs text-slate-400 mt-0.5">{label}</p>
        </div>
      ))}
    </div>
  )
}

export default function AiSalesLogs() {
  const [logs,     setLogs]    = useState<AiSalesLogEntry[]>([])
  const [total,    setTotal]   = useState(0)
  const [loading,  setLoading] = useState(true)
  const [error,    setError]   = useState<string | null>(null)
  const [search,   setSearch]  = useState('')
  const [filterIntent, setFilterIntent] = useState('all')
  const { t, lang } = useLanguage()
  const sp = t(tr => tr.salesAgentPage)
  void UI_ONLY_GUARD
  const locale = lang === 'ar' ? 'ar-SA' : 'en-US'

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await aiSalesApi.getLogs({ limit: 100, offset: 0 })
      setLogs(res.logs)
      setTotal(res.total)
    } catch {
      setError(sp.loadError)
    } finally { setLoading(false) }
  }, [sp.loadError])

  useEffect(() => { load() }, [load])

  const visible = logs.filter(l => {
    const matchSearch = !search ||
      l.customer_name.includes(search) ||
      l.customer_phone.includes(search) ||
      l.message.includes(search)
    const matchIntent = filterIntent === 'all' || l.intent === filterIntent
    return matchSearch && matchIntent
  })

  const intentOptions = [
    { value: 'all', label: sp.allIntents },
    ...Object.entries(AI_SALES_INTENT_META).map(([key, { label, emoji }]) => ({
      value: key, label: `${emoji} ${label}`,
    })),
  ]

  return (
    <div className="p-6 space-y-5 max-w-6xl mx-auto">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-brand-50 rounded-xl flex items-center justify-center">
            <BrainCircuit className="w-5 h-5 text-brand-500" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-slate-900">{sp.title}</h1>
            <p className="text-xs text-slate-400">{sp.interactionsRecorded.replace('{count}', String(total))}</p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="btn-secondary text-sm flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          {sp.refresh}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 bg-red-50 border border-red-200 text-red-700 text-sm px-4 py-3 rounded-xl">
          <AlertCircle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      {!loading && <StatsStrip logs={logs} sp={sp} />}

      <div className="flex gap-3 flex-wrap">
        <div className="relative flex-1 min-w-48">
          <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            className="input ps-9 text-sm"
            placeholder={sp.searchPlaceholder}
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <select
          className="input text-sm min-w-44"
          value={filterIntent}
          onChange={e => setFilterIntent(e.target.value)}
        >
          {intentOptions.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
          <span className="ms-3 text-sm text-slate-500">{sp.loading}</span>
        </div>
      ) : visible.length === 0 ? (
        <div className="card py-16 text-center">
          <BrainCircuit className="w-10 h-10 text-slate-200 mx-auto mb-3" />
          <p className="text-sm text-slate-400">{sp.noResults}</p>
        </div>
      ) : (
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-start">
              <thead>
                <tr className="border-b border-slate-100">
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-start">{sp.table.time}</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-start">{sp.table.customer}</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-start">{sp.table.intent}</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-start">{sp.table.confidence}</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-center">📦</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-start">{sp.table.order}</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-center">💳</th>
                  <th className="px-4 py-3 text-xs font-semibold text-slate-500 text-center">👤</th>
                </tr>
              </thead>
              <tbody>
                {visible.map(entry => (
                  <LogRow key={entry.id} entry={entry} sp={sp} locale={locale} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="px-4 py-3 border-t border-slate-50 text-xs text-slate-400">
            {sp.showing.replace('{shown}', String(visible.length)).replace('{total}', String(total))}
          </div>
        </div>
      )}

      <div className="flex flex-wrap gap-4 text-xs text-slate-500">
        <span className="flex items-center gap-1.5"><Package  className="w-3.5 h-3.5 text-brand-400" /> {sp.flags.catalogUsed}</span>
        <span className="flex items-center gap-1.5"><ShoppingCart className="w-3.5 h-3.5 text-emerald-500" /> {sp.flags.orderCreated}</span>
        <span className="flex items-center gap-1.5"><CreditCard   className="w-3.5 h-3.5 text-violet-400" /> {sp.flags.paymentLinkSent}</span>
        <span className="flex items-center gap-1.5"><UserCheck    className="w-3.5 h-3.5 text-amber-400"  /> {sp.flags.handoff}</span>
      </div>
    </div>
  )
}
