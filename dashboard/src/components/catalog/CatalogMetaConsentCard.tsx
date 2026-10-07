import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { catalogApi, type MetaCatalogConsentStatus } from '../../api/catalog'
import { useLanguage } from '../../i18n/context'
import type { Lang } from '../../i18n/types'
import { resolveConsentBanner } from '../../lib/metaCatalogConsentResult'

/**
 * Catalog-only Meta consent (catalog_management + business_management).
 *
 * Hidden unless the backend reports the path as available for this store.
 * The consent URL comes from the authenticated POST — the session token never
 * enters a URL — and the browser only ever navigates to Meta's own dialog.
 * After Meta, the backend returns to /catalog with a fixed result code in the
 * fragment; it is read once and removed. The fragment is only a hint: success
 * is claimed only when the fresh authenticated status shows an active
 * authorization for exactly the approved catalog and business
 * (``resolveConsentBanner``).
 */

const RESULT_PARAM = 'meta_catalog_consent'
const META_DIALOG_ORIGIN = 'https://www.facebook.com'

function localeTag(lang: Lang): string {
  return lang === 'en' ? 'en-US' : 'ar-SA'
}

function fmtAt(iso: string | null | undefined, lang: Lang): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(localeTag(lang))
  } catch {
    return iso
  }
}

/** Read and clear ``#meta_catalog_consent=<code>``; only [a-z_] codes are accepted. */
function takeResultFromHash(): string | null {
  if (typeof window === 'undefined') return null
  const raw = window.location.hash.replace(/^#/, '')
  if (!raw) return null
  const params = new URLSearchParams(raw)
  const code = params.get(RESULT_PARAM)
  if (code === null) return null
  try {
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  } catch {
    /* history API unavailable — the label is still shown once */
  }
  return /^[a-z_]{1,64}$/.test(code) ? code : 'error'
}

/** Accept only Meta's OAuth dialog on its own origin. */
function isMetaDialogUrl(url: string): boolean {
  try {
    const parsed = new URL(url)
    return parsed.origin === META_DIALOG_ORIGIN && /^\/v\d+\.\d+\/dialog\/oauth$/.test(parsed.pathname)
  } catch {
    return false
  }
}

export default function CatalogMetaConsentCard() {
  const { lang, tStatic } = useLanguage()
  const copy = tStatic(tr => tr.catalogMgmt.metaConsent)
  const [status, setStatus] = useState<MetaCatalogConsentStatus | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [busy, setBusy] = useState(false)
  const [startError, setStartError] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    setResult(takeResultFromHash())
    return () => {
      mounted.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const next = await catalogApi.metaConsentStatus()
      if (!mounted.current) return
      setStatus(next)
      setLoadError(false)
    } catch {
      if (mounted.current) setLoadError(true)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onConnect = useCallback(async () => {
    if (busy) return
    setBusy(true)
    setStartError(false)
    try {
      const started = await catalogApi.metaConsentStart()
      if (!isMetaDialogUrl(started.authorize_url)) throw new Error('unexpected_authorize_url')
      window.location.assign(started.authorize_url)
    } catch {
      if (mounted.current) {
        setStartError(true)
        setBusy(false)
      }
    }
  }, [busy])

  const banner = resolveConsentBanner(result, status, loadError, new Set(Object.keys(copy.results)))
  const resultLabel = banner
    ? (banner.key === 'notConfirmed' ? copy.notConfirmed : (copy.results[banner.key] ?? copy.results.error))
    : null
  const resultOk = banner?.tone === 'ok'

  // Nothing to show outside an enabled environment, unless a result came back.
  if (!status?.available && !resultLabel && !loadError) return null
  if (loadError && !resultLabel) return null

  const auth = status?.authorization
  const stateLine = !auth || auth.state === 'none'
    ? copy.stateNone
    : auth.state === 'active'
      ? copy.stateActive
      : auth.state === 'expired'
        ? copy.stateExpired
        : copy.stateInactive

  return (
    <section className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-4" data-testid="meta-catalog-consent-card">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
            <ShieldCheck className="w-5 h-5 text-emerald-600" />
            {copy.title}
          </h2>
          <p className="text-sm text-slate-600">{copy.description}</p>
          {status?.approved && (
            <p className="text-sm text-slate-700">
              {copy.approvedLine
                .replace('{catalog}', status.approved.catalog_id)
                .replace('{business}', status.approved.business_id)}
            </p>
          )}
          {status?.available && <p className="text-sm font-semibold text-slate-800">{stateLine}</p>}
          {auth?.state === 'active' && (
            <ul className="text-xs text-slate-600 space-y-0.5">
              {auth.granted_scopes && auth.granted_scopes.length > 0 && (
                <li>{copy.scopesLine.replace('{scopes}', auth.granted_scopes.join(', '))}</li>
              )}
              {auth.verified_at && <li>{copy.verifiedAt.replace('{at}', fmtAt(auth.verified_at, lang))}</li>}
              <li>
                {auth.token_expires_at
                  ? copy.expiresAt.replace('{at}', fmtAt(auth.token_expires_at, lang))
                  : copy.noExpiry}
              </li>
            </ul>
          )}
          <p className="text-xs text-slate-500">{copy.writeNote}</p>
        </div>
        {status?.available && (
          <button
            type="button"
            onClick={() => void onConnect()}
            disabled={busy}
            className="inline-flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-4 py-2.5 rounded-xl text-sm transition shadow-sm shrink-0 min-h-[44px]"
            data-testid="meta-catalog-consent-button"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
            {busy ? copy.connecting : auth && auth.state !== 'none' ? copy.reconnect : copy.button}
          </button>
        )}
      </div>

      {resultLabel && (
        <div
          className={`flex items-start gap-2 border rounded-xl p-3 text-sm ${
            resultOk ? 'bg-emerald-50 border-emerald-200 text-emerald-900' : 'bg-amber-50 border-amber-200 text-amber-900'
          }`}
          role="status"
        >
          {resultOk ? <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" /> : <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />}
          <p>{resultLabel}</p>
        </div>
      )}
      {startError && (
        <div className="flex items-start gap-2 border rounded-xl p-3 text-sm bg-amber-50 border-amber-200 text-amber-900" role="alert">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <p>{copy.startFailed}</p>
        </div>
      )}
    </section>
  )
}
