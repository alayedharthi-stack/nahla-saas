/**
 * WABA ↔ Meta catalog link status with linked-catalog picker (GET + PATCH config).
 * Selecting a catalog updates meta_catalog_id only — no import, no Graph writes.
 */
import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, XCircle } from 'lucide-react'
import {
  catalogApi,
  type WabaCatalogLinkStatus,
  type WabaLinkedCatalog,
} from '../../api/catalog'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

function WhatsAppIcon({ className = 'w-5 h-5' }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} fill="currentColor" aria-hidden>
      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.435 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" />
    </svg>
  )
}

type WabaLinkCopy = Translations['catalogMgmt']['wabaLinkStatus']

type ViewCase =
  | 'loading'
  | 'fetch_error'
  | 'linked'
  | 'none'
  | 'mismatch'
  | 'missing'
  | 'waba_inaccessible'
  | 'waba_not_found'
  | 'meta_error'

export interface WabaCatalogLinkStatusCardProps {
  /** Called after PATCH succeeds — parent should refresh status/diagnostics. */
  onCatalogApplied?: (catalogId: string) => void | Promise<void>
}

function resolveCase(
  status: WabaCatalogLinkStatus | null,
  fetchError: string | null,
  loading: boolean,
): ViewCase {
  if (loading) return 'loading'
  if (fetchError) return 'fetch_error'
  if (!status) return 'fetch_error'
  if (status.missing.length > 0) return 'missing'
  if (!status.ok) {
    if (status.error === 'waba_inaccessible') return 'waba_inaccessible'
    if (status.error === 'waba_not_found') return 'waba_not_found'
    return 'meta_error'
  }
  if (status.expected_catalog_linked === true) return 'linked'
  if (status.connected) return 'mismatch'
  return 'none'
}

function missingLabel(key: string, copy: WabaLinkCopy): string {
  const map: Record<string, string> = {
    connection:      copy.missingConnection,
    waba_id:         copy.missingWaba,
    meta_catalog_id: copy.missingCatalogId,
    graph_token:     copy.missingToken,
  }
  return map[key] ?? key
}

function isCurrentCatalog(catalogId: string, expectedId: string | null | undefined): boolean {
  const current = (expectedId ?? '').trim()
  return Boolean(current) && current === catalogId
}

type LinkedCatalogListProps = {
  catalogs: WabaLinkedCatalog[]
  expectedCatalogId: string | null | undefined
  applyingId: string | null
  onUse: (catalogId: string) => void
  copy: WabaLinkCopy
  showSingleRecommendation?: boolean
}

function LinkedCatalogList({
  catalogs,
  expectedCatalogId,
  applyingId,
  onUse,
  copy,
  showSingleRecommendation = false,
}: LinkedCatalogListProps) {
  const singleLinked = catalogs.length === 1

  return (
    <div className="space-y-2">
      {showSingleRecommendation && singleLinked && !isCurrentCatalog(catalogs[0].id, expectedCatalogId) && (
        <p className="text-xs text-slate-600 leading-relaxed">{copy.singleCatalogRecommendation}</p>
      )}
      {catalogs.map(catalog => {
        const current = isCurrentCatalog(catalog.id, expectedCatalogId)
        const useLabel = singleLinked ? copy.useLinkedCatalog : copy.useThisCatalog
        return (
          <div
            key={catalog.id}
            className="flex flex-wrap items-center justify-between gap-2 bg-white border border-slate-200 rounded-lg p-3"
          >
            <div className="min-w-0 space-y-0.5">
              {catalog.name && (
                <p className="text-sm font-semibold text-slate-800">{catalog.name}</p>
              )}
              <p className="font-mono text-xs text-slate-600" dir="ltr">{catalog.id}</p>
            </div>
            {current ? (
              <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-800 bg-emerald-50 border border-emerald-200 px-2.5 py-1 rounded-full shrink-0">
                <CheckCircle2 className="w-3.5 h-3.5" />
                {copy.currentlyInUse}
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onUse(catalog.id)}
                disabled={applyingId !== null}
                className="inline-flex items-center gap-1.5 text-xs font-bold text-white bg-[#25D366] hover:bg-[#128C7E] disabled:opacity-50 px-3 py-1.5 rounded-lg transition shrink-0"
              >
                {applyingId === catalog.id
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <WhatsAppIcon className="w-3.5 h-3.5" />}
                {useLabel}
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function WabaCatalogLinkStatusCard({ onCatalogApplied }: WabaCatalogLinkStatusCardProps) {
  const { tStatic } = useLanguage()
  const copy = tStatic(tr => tr.catalogMgmt.wabaLinkStatus)

  const [status, setStatus] = useState<WabaCatalogLinkStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [showTechDetails, setShowTechDetails] = useState(false)
  const [applyingId, setApplyingId] = useState<string | null>(null)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [applySuccess, setApplySuccess] = useState<string | null>(null)

  const load = useCallback(async (isRefresh = false) => {
    if (isRefresh) setRefreshing(true)
    else setLoading(true)
    setFetchError(null)
    try {
      const data = await catalogApi.wabaLinkStatus()
      setStatus(data)
    } catch {
      setFetchError(copy.fetchFailed)
      setStatus(null)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [copy.fetchFailed])

  useEffect(() => { void load() }, [load])

  const applyCatalog = useCallback(async (catalogId: string) => {
    if (!status || applyingId) return
    if (isCurrentCatalog(catalogId, status.expected_catalog_id)) return

    const currentId = (status.expected_catalog_id ?? '').trim()
    if (currentId && currentId !== catalogId) {
      if (!window.confirm(copy.switchConfirm)) return
    }

    setApplyingId(catalogId)
    setApplyError(null)
    setApplySuccess(null)
    try {
      await catalogApi.patch({ meta_catalog_id: catalogId })
      await load(true)
      await onCatalogApplied?.(catalogId)
      setApplySuccess(copy.catalogAppliedSuccess)
    } catch {
      setApplyError(copy.catalogApplyFailed)
    } finally {
      setApplyingId(null)
    }
  }, [status, applyingId, copy.switchConfirm, copy.catalogAppliedSuccess, copy.catalogApplyFailed, load, onCatalogApplied])

  const view = resolveCase(status, fetchError, loading && !status)

  const refreshBtn = (
    <button
      type="button"
      onClick={() => void load(true)}
      disabled={loading || refreshing || applyingId !== null}
      className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-600 hover:text-slate-800 border border-slate-200 rounded-lg px-2.5 py-1.5 transition disabled:opacity-50"
    >
      {(loading || refreshing)
        ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
        : <RefreshCw className="w-3.5 h-3.5" />}
      {copy.refresh}
    </button>
  )

  const feedbackBanner = (applySuccess || applyError) && (
    <div className={`text-xs font-semibold rounded-lg px-3 py-2 ${
      applySuccess ? 'bg-emerald-50 text-emerald-800 border border-emerald-200' : 'bg-rose-50 text-rose-800 border border-rose-200'
    }`}>
      {applySuccess ?? applyError}
    </div>
  )

  if (view === 'loading') {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 flex items-center gap-2 text-sm text-slate-600">
        <Loader2 className="w-4 h-4 animate-spin" />
        {copy.loading}
      </div>
    )
  }

  if (view === 'fetch_error') {
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 space-y-2">
        <div className="flex items-start gap-2 text-rose-800">
          <XCircle className="w-5 h-5 shrink-0" />
          <p className="text-sm font-semibold">{fetchError ?? copy.fetchFailed}</p>
        </div>
        <div className="flex justify-end">{refreshBtn}</div>
      </div>
    )
  }

  if (view === 'linked' && status) {
    return (
      <div className="rounded-xl border border-[#25D366]/40 bg-[#25D366]/5 p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3 min-w-0">
            <span className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-[#25D366] text-white shrink-0">
              <WhatsAppIcon className="w-5 h-5" />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h4 className="text-sm font-bold text-slate-800">{copy.linkedTitle}</h4>
                <span className="inline-flex items-center gap-1 text-xs font-bold text-white bg-[#25D366] px-2.5 py-1 rounded-full">
                  <WhatsAppIcon className="w-3.5 h-3.5" />
                  {copy.linkedBadge}
                </span>
              </div>
              <p className="text-xs text-slate-600 mt-1 leading-relaxed">{copy.linkedDesc}</p>
              <p className="text-[11px] text-slate-500 mt-2 leading-relaxed">{copy.linkedDisclaimer}</p>
              <p className="text-[11px] text-slate-500 mt-1 leading-relaxed">{copy.linkedManualCheck}</p>
            </div>
          </div>
          {refreshBtn}
        </div>
        {feedbackBanner}
        <div>
          <p className="text-xs font-semibold text-slate-500 mb-2">{copy.linkedCatalogsLabel}</p>
          <LinkedCatalogList
            catalogs={status.linked_catalogs}
            expectedCatalogId={status.expected_catalog_id}
            applyingId={applyingId}
            onUse={catalogId => void applyCatalog(catalogId)}
            copy={copy}
          />
        </div>
      </div>
    )
  }

  if (view === 'none' && status) {
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0" />
            <div>
              <h4 className="text-sm font-bold text-slate-800">{copy.noneTitle}</h4>
              <p className="text-xs text-slate-600 mt-1 leading-relaxed">{copy.noneDesc}</p>
              <p className="text-xs text-slate-600 mt-2 leading-relaxed">{copy.noneGuidance}</p>
            </div>
          </div>
          {refreshBtn}
        </div>
      </div>
    )
  }

  if (view === 'mismatch' && status) {
    return (
      <div className="rounded-xl border border-amber-300 bg-amber-50 p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-5 h-5 text-amber-700 shrink-0" />
            <div>
              <h4 className="text-sm font-bold text-slate-800">{copy.mismatchTitle}</h4>
              <p className="text-xs text-slate-600 mt-1">{copy.mismatchDesc}</p>
            </div>
          </div>
          {refreshBtn}
        </div>
        {feedbackBanner}
        {status.expected_catalog_id && (
          <div className="bg-white border border-amber-200 rounded-lg p-3 text-xs">
            <p className="font-semibold text-slate-500 mb-1">{copy.expectedCatalogLabel}</p>
            <p className="font-mono" dir="ltr">{status.expected_catalog_id}</p>
          </div>
        )}
        <div>
          <p className="text-xs font-semibold text-slate-500 mb-2">{copy.linkedCatalogsLabel}</p>
          <LinkedCatalogList
            catalogs={status.linked_catalogs}
            expectedCatalogId={status.expected_catalog_id}
            applyingId={applyingId}
            onUse={catalogId => void applyCatalog(catalogId)}
            copy={copy}
            showSingleRecommendation
          />
        </div>
      </div>
    )
  }

  if (view === 'missing' && status) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-5 h-5 text-slate-500 shrink-0" />
            <div>
              <h4 className="text-sm font-bold text-slate-800">{copy.missingTitle}</h4>
              <ul className="mt-2 space-y-1 text-xs text-slate-600">
                {status.missing.map(key => (
                  <li key={key}>• {missingLabel(key, copy)}</li>
                ))}
              </ul>
            </div>
          </div>
          {refreshBtn}
        </div>
      </div>
    )
  }

  if ((view === 'waba_inaccessible' || view === 'waba_not_found') && status) {
    const title = view === 'waba_not_found' ? copy.wabaNotFoundTitle : copy.wabaInaccessibleTitle
    const desc = view === 'waba_not_found' ? copy.wabaNotFoundDesc : copy.wabaInaccessibleDesc
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-5 h-5 text-amber-700 shrink-0" />
            <div>
              <h4 className="text-sm font-bold text-slate-800">{title}</h4>
              <p className="text-xs text-slate-600 mt-1 leading-relaxed">{desc}</p>
              {status.catalog_exists === true && (
                <p className="text-xs text-emerald-800 mt-2 leading-relaxed">
                  {copy.wabaCatalogExistsNote}
                </p>
              )}
              {status.catalog_exists === true && status.expected_catalog_id && (
                <p className="text-[11px] text-emerald-800 mt-2">
                  {copy.catalogIdLabel}{' '}
                  <span className="font-mono" dir="ltr">{status.expected_catalog_id}</span>
                </p>
              )}
            </div>
          </div>
          {refreshBtn}
        </div>
        {status.error_message && (
          <div>
            <button
              type="button"
              onClick={() => setShowTechDetails(v => !v)}
              className="text-[11px] font-semibold text-slate-600 hover:text-slate-800 underline"
            >
              {showTechDetails ? copy.hideTechnicalDetails : copy.showTechnicalDetails}
            </button>
            {showTechDetails && (
              <p className="text-[11px] text-slate-500 mt-1 line-clamp-4" dir="ltr">
                {status.error_message}
              </p>
            )}
          </div>
        )}
      </div>
    )
  }

  if (view === 'meta_error' && status) {
    return (
      <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-2">
            <XCircle className="w-5 h-5 text-rose-600 shrink-0" />
            <div>
              <h4 className="text-sm font-bold text-rose-900">{copy.metaErrorTitle}</h4>
              {status.error_message && (
                <>
                  <button
                    type="button"
                    onClick={() => setShowTechDetails(v => !v)}
                    className="text-[11px] font-semibold text-rose-800 hover:text-rose-900 underline mt-1"
                  >
                    {showTechDetails ? copy.hideTechnicalDetails : copy.showTechnicalDetails}
                  </button>
                  {showTechDetails && (
                    <p className="text-[11px] text-rose-700 mt-1 line-clamp-3" dir="ltr">
                      {status.error_message}
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
          {refreshBtn}
        </div>
      </div>
    )
  }

  return null
}
