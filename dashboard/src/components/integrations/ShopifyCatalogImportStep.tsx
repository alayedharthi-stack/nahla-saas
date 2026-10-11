import { AlertTriangle, CheckCircle2, Circle, Loader2, RefreshCw } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { shopifyCatalogImportApi } from '../../api/shopifyCatalogImport'
import type { ShopifyConnectionLabels } from '../../i18n/shopifyConnectionLabels'
import type { Lang } from '../../i18n/types'
import {
  type ImportEntry,
  type ImportReasonKey,
  displayState,
  parseImportStatus,
  projectImportFailure,
  startAllowed,
} from '../../lib/shopifyConnection/catalogImport'
import type { ShopifyStatus } from '../../lib/shopifyConnection/model'

/**
 * Catalog import step of the Shopify card (behind VITE_SHOPIFY_CATALOG_IMPORT_UI
 * and the backend's own default-off flags). Shows only server-committed
 * state: "imported" requires the server's committed evidence for the current
 * installation; inventory is reported separately and never implied by a
 * products-only import. When the backend route is off it renders the
 * card's original static step (``fallback``).
 *
 * Bound to the parent card's authoritative connection reads: every new
 * parent status (refresh, pageshow / back-forward restore, after a
 * disconnect or a session change) invalidates what is shown — the step
 * fails closed (no badge, no start) until its own re-read answers — and a
 * shop the parent no longer reports as active never shows "complete" or a
 * start action.
 */

const POLL_MS = 5000

type Load =
  | { phase: 'loading' } | { phase: 'off' } | { phase: 'failed' }
  | { phase: 'ready'; available: boolean; entries: ImportEntry[] }

interface Props {
  copy: ShopifyConnectionLabels
  lang: Lang
  readOnly: boolean
  sessionKey: () => string
  /** The parent's latest authoritative status (a new object per successful read). */
  parentStatus: ShopifyStatus | null
  /** A parent mutation (connect / disconnect) is in flight. */
  parentBusy: boolean
  fallback: ReactNode
}

function fmt(iso: string, lang: Lang): string {
  try {
    return new Date(iso).toLocaleString(lang === 'en' ? 'en-US' : 'ar-SA')
  } catch {
    return ''
  }
}

function fill(template: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce((text, [k, v]) => text.split(`{${k}}`).join(String(v)), template)
}

export default function ShopifyCatalogImportStep({ copy, lang, readOnly, sessionKey, parentStatus, parentBusy, fallback }: Props) {
  const c = copy.catalogImport
  const [load, setLoad] = useState<Load>({ phase: 'loading' })
  const [starting, setStarting] = useState<string | null>(null)
  const [startError, setStartError] = useState<ImportReasonKey | null>(null)
  const ticket = useRef(0)

  const refresh = useCallback(() => {
    const mine = ++ticket.current
    const key = sessionKey()
    const ctrl = new AbortController()
    shopifyCatalogImportApi.status(ctrl.signal).then(
      (raw) => {
        if (mine !== ticket.current || key !== sessionKey()) return
        const parsed = parseImportStatus(raw)
        setLoad(parsed ? { phase: 'ready', available: parsed.available, entries: parsed.entries } : { phase: 'failed' })
      },
      (err) => {
        if (mine !== ticket.current || key !== sessionKey()) return
        setLoad(projectImportFailure(err).kind === 'feature_off' ? { phase: 'off' } : { phase: 'failed' })
      },
    )
    return () => ctrl.abort()
  }, [sessionKey])

  // Every authoritative parent read invalidates the shown state: fail closed
  // until this step's own read for that epoch answers.
  useEffect(() => {
    setLoad({ phase: 'loading' })
    const abort = refresh()
    return () => {
      ticket.current++
      abort()
    }
  }, [refresh, parentStatus])

  const activeShops = useMemo(
    () => new Set((parentStatus?.connections ?? []).filter((c) => c.status === 'active').map((c) => c.shopDomain)),
    [parentStatus],
  )

  const importing = load.phase === 'ready' && load.entries.some((e) => e.state === 'importing')
  useEffect(() => {
    if (!importing) return undefined
    const timer = window.setTimeout(() => refresh(), POLL_MS)
    return () => window.clearTimeout(timer)
  }, [importing, load, refresh])

  const start = (shop: string) => {
    if (readOnly || starting || parentBusy) return
    const key = sessionKey()
    setStarting(shop)
    setStartError(null)
    const ctrl = new AbortController()
    shopifyCatalogImportApi.start(shop, ctrl.signal).then(
      () => {
        setStarting(null)
        if (key === sessionKey()) refresh()
      },
      (err) => {
        setStarting(null)
        if (key !== sessionKey()) return
        const failure = projectImportFailure(err)
        setStartError(failure.kind === 'refused' ? failure.key : 'generic')
        refresh()
      },
    )
  }

  if (load.phase === 'off') return <>{fallback}</>
  if (load.phase === 'loading') {
    return (
      <li className="flex items-center gap-2" data-testid="shopify-step-import" data-step="loading">
        <Loader2 className="w-4 h-4 text-slate-400 animate-spin" />
        <span className="text-slate-800">{copy.steps.productImport}</span>
      </li>
    )
  }
  if (load.phase === 'failed') {
    return (
      <li className="flex items-center gap-2" data-testid="shopify-step-import" data-step="unknown">
        <AlertTriangle className="w-4 h-4 text-amber-500" />
        <span className="text-slate-800">{copy.steps.productImport}</span>
        <span className="text-slate-500">— {c.unknown}</span>
        <button type="button" className="underline text-slate-600" onClick={() => refresh()}>{c.refresh}</button>
      </li>
    )
  }
  const entries = load.entries.filter((e) => e.connectionStatus === 'active' || e.lastSuccess !== null)
  if (entries.length === 0) return <>{fallback}</>
  const available = load.available
  return (
    <>
      {entries.map((entry) => {
        const parentActive = activeShops.has(entry.shop)
        const shown = displayState(entry, parentActive)
        const label = { not_started: c.notStarted, importing: c.importing, imported: c.complete, error: c.error,
          unknown: c.unknown }[shown]
        const evidence = entry.lastSuccess
        const canStart = startAllowed(entry, { available, parentActive, readOnly, busy: parentBusy })
        return (
          <li key={entry.shop} className="space-y-1" data-testid="shopify-step-import" data-step={shown}
              data-shop={entry.shop}>
            <div className="flex items-center gap-2">
              {shown === 'imported'
                ? <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                : shown === 'importing'
                  ? <Loader2 className="w-4 h-4 text-slate-500 animate-spin" />
                  : shown === 'error' || shown === 'unknown'
                    ? <AlertTriangle className="w-4 h-4 text-amber-500" />
                    : <Circle className="w-4 h-4 text-slate-300" />}
              <span className="text-slate-800">{copy.steps.productImport}</span>
              <span className="text-slate-500" dir="ltr">{entry.shop}</span>
              <span className="text-slate-500">— {label}</span>
            </div>
            {shown === 'error' && entry.reasonKey && (
              <p className="text-amber-800 ps-6" data-testid="shopify-import-reason">{c.reasons[entry.reasonKey]}</p>
            )}
            {evidence && (
              <div className="text-slate-600 ps-6 space-y-0.5" data-testid="shopify-import-evidence"
                   data-inventory={evidence.inventory.mode}>
                <p>{fill(c.lastSuccess, { time: fmt(evidence.completedAt, lang), products: evidence.products,
                  variants: evidence.variants })}</p>
                <p data-testid="shopify-import-api-version" dir="auto">{fill(c.apiVersion, { version: evidence.apiVersion })}</p>
                <p>{evidence.inventory.mode === 'per_location'
                  ? fill(c.inventoryRead, { locations: evidence.inventory.locations, levels: evidence.inventory.levels })
                  : c.inventoryNotRead}</p>
                {!evidence.currentInstallation && <p>{c.previousInstallation}</p>}
              </div>
            )}
            {canStart && (
              <button
                type="button"
                className="btn-secondary text-xs py-1 ms-6"
                disabled={starting !== null}
                onClick={() => start(entry.shop)}
                data-testid="shopify-import-start"
              >
                {starting === entry.shop ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                {starting === entry.shop ? c.starting : c.start}
              </button>
            )}
          </li>
        )
      })}
      {startError && (
        <li className="text-amber-800" role="alert" data-testid="shopify-import-start-error">{c.reasons[startError]}</li>
      )}
      <li className="text-[11px] text-slate-500" data-testid="shopify-import-mirror-note">{c.mirrorNote}</li>
    </>
  )
}
