import { AlertTriangle, CheckCircle2, Circle, Loader2, ShieldCheck, Store } from 'lucide-react'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { shopifyConnectionApi } from '../../api/shopifyConnection'
import { getSessionBindingKey, isImpersonating, isImpersonatingSupport } from '../../auth'
import { useLanguage } from '../../i18n/context'
import type { Lang } from '../../i18n/types'
import { isShopifyConnectionUiEnabled } from '../../lib/platformFeatureFlags'
import { createConnectionController } from '../../lib/shopifyConnection/connectionController'
import {
  type ConnectionView,
  type ShopifyConnectionSummary,
  REQUESTED_SCOPE,
  canDisconnect,
  canReconnect,
  connectionView,
  normalizeShopInput,
  onboardingSteps,
} from '../../lib/shopifyConnection/model'
import Badge from '../ui/Badge'
import ConfirmModal from '../ui/ConfirmModal'
import { watchSessionSignals } from './shopifySessionSignals'

/**
 * Shopify connection card on /integrations (dormant: hidden unless the
 * dashboard flag is on AND the backend status route answers).
 *
 * Shows the store *authorization* only. Product import does not exist in
 * this release and is always shown as not started — never inferred.
 * All mutating actions go through the authenticated API; the authorize URL
 * is validated against the requested shop before the browser leaves.
 */

function fmtAt(iso: string | null, lang: Lang): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(lang === 'en' ? 'en-US' : 'ar-SA')
  } catch {
    return ''
  }
}

const VIEW_BADGE: Record<ConnectionView, 'green' | 'amber' | 'red' | 'slate'> = {
  authorized: 'green',
  verifying: 'amber',
  reconnect_required: 'red',
  uninstalled: 'slate',
  disconnected: 'slate',
  unknown: 'amber',
}

function readOnlySession(): boolean {
  return isImpersonatingSupport() || isImpersonating()
}

export default function ShopifyConnectionCard() {
  const { lang, tStatic } = useLanguage()
  const copy = tStatic(tr => tr.shopifyConnection)
  const [controller] = useState(() =>
    createConnectionController({
      enabled: isShopifyConnectionUiEnabled,
      readOnly: readOnlySession,
      sessionKey: getSessionBindingKey,
      status: shopifyConnectionApi.status,
      start: shopifyConnectionApi.start,
      disconnect: shopifyConnectionApi.disconnect,
      navigate: (url) => window.location.assign(url),
    }),
  )
  // Typed shop and the open confirmation live in the controller so a session
  // change clears them with the rest of the tenant's data.
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const shopInput = state.shopInput
  const confirmShop = state.confirmShop

  useEffect(() => {
    controller.refresh()
    const stopSignals = watchSessionSignals(controller.onSessionMaybeChanged)
    const onPageShow = (e: PageTransitionEvent) => controller.onPageShow(e.persisted)
    window.addEventListener('pageshow', onPageShow)
    return () => {
      stopSignals()
      window.removeEventListener('pageshow', onPageShow)
      controller.dispose()
    }
  }, [controller])

  if (state.load === 'idle' || state.load === 'hidden') return null

  const status = state.status
  const steps = onboardingSteps(status)
  const busyStart = state.start.phase === 'starting' || state.start.phase === 'redirecting'
  const preview = normalizeShopInput(shopInput)
  const scopes = (status?.requestedScopes.length ? status.requestedScopes : [REQUESTED_SCOPE]).join(', ')
  const canStart = state.load === 'ready' && !state.readOnly
  const viewLabel: Record<ConnectionView, string> = {
    authorized: copy.connection.authorized,
    verifying: copy.connection.verifying,
    reconnect_required: copy.connection.reconnectRequired,
    uninstalled: copy.connection.uninstalled,
    disconnected: copy.connection.disconnected,
    unknown: copy.connection.unknown,
  }
  const authLabel = steps.authorization === 'done'
    ? copy.steps.authorizationDone
    : steps.authorization === 'attention' ? copy.steps.authorizationAttention : copy.steps.authorizationTodo

  const renderConnection = (c: ShopifyConnectionSummary) => {
    const view = connectionView(c)
    const working = state.disconnect.phase === 'working' && state.disconnect.shop === c.shopDomain
    return (
      <li key={c.shopDomain} className="border border-slate-200 rounded-xl p-3 space-y-2" data-testid="shopify-connection-row">
        <div className="flex items-center gap-2 flex-wrap">
          <Store className="w-4 h-4 text-slate-500" />
          <span className="text-sm font-medium text-slate-900" dir="ltr">{c.shopDomain}</span>
          <Badge label={viewLabel[view]} variant={VIEW_BADGE[view]} dot={view === 'authorized'} />
        </div>
        <ul className="text-xs text-slate-600 space-y-0.5">
          {c.connectedAt && view !== 'disconnected' && view !== 'uninstalled' && (
            <li>{copy.connection.connectedAt.replace('{at}', fmtAt(c.connectedAt, lang))}</li>
          )}
          {c.disconnectedAt && (view === 'disconnected' || view === 'uninstalled' || view === 'reconnect_required') && (
            <li>{copy.connection.disconnectedAt.replace('{at}', fmtAt(c.disconnectedAt, lang))}</li>
          )}
          {view === 'authorized' && c.scopes.length > 0 && (
            <li>{copy.connection.scopes.replace('{scopes}', c.scopes.join(', '))}</li>
          )}
          {view === 'authorized' && <li>{copy.connection.importNote}</li>}
        </ul>
        {!state.readOnly && (
          <div className="flex gap-2 flex-wrap">
            {canReconnect(c) && state.load === 'ready' && (
              <button
                type="button"
                className="btn-secondary text-xs py-1.5"
                disabled={busyStart || state.disconnect.phase === 'working'}
                onClick={() => controller.start(c.shopDomain)}
                data-testid="shopify-reconnect-button"
              >
                {copy.connection.reconnect}
              </button>
            )}
            {canDisconnect(c) && (
              <button
                type="button"
                className="btn-secondary text-xs py-1.5 text-red-600 border-red-200 hover:bg-red-50"
                disabled={working || state.disconnect.phase === 'working' || busyStart}
                onClick={() => controller.openConfirm(c.shopDomain)}
                data-testid="shopify-disconnect-button"
              >
                {working ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                {working ? copy.connection.disconnecting : copy.connection.disconnect}
              </button>
            )}
          </div>
        )}
      </li>
    )
  }

  return (
    <section className="card p-5 space-y-4" data-testid="shopify-connection-card">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 bg-slate-50 border border-slate-200 rounded-xl flex items-center justify-center shrink-0">
          <ShieldCheck className="w-6 h-6 text-emerald-600" />
        </div>
        <div className="flex-1 min-w-0 space-y-1">
          <h3 className="text-sm font-semibold text-slate-900">{copy.card.title}</h3>
          <p className="text-xs text-slate-500">{copy.card.description}</p>
          <p className="text-xs text-slate-500">{copy.card.accessLine.replace('{scopes}', scopes)}</p>
        </div>
      </div>

      {state.load === 'loading' && (
        <p className="text-xs text-slate-500 flex items-center gap-2" role="status">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> {copy.card.loading}
        </p>
      )}
      {state.load === 'failed' && (
        <div className="flex items-center gap-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2" role="alert">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span className="flex-1">{copy.card.loadFailed}</span>
          <button type="button" className="underline" onClick={() => controller.refresh()}>{copy.card.retryLoad}</button>
        </div>
      )}
      {state.load === 'unavailable' && (
        <p className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2" data-testid="shopify-unavailable">
          {copy.card.unavailable}
        </p>
      )}
      {state.readOnly && (state.load === 'ready' || state.load === 'unavailable') && (
        <p className="text-xs text-slate-600 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2" data-testid="shopify-read-only">
          {copy.card.readOnlyNote}
        </p>
      )}

      {(state.load === 'ready' || state.load === 'unavailable') && (
        <>
          <div className="space-y-1.5" data-testid="shopify-onboarding-steps">
            <p className="text-xs font-semibold text-slate-700">{copy.steps.title}</p>
            <ol className="text-xs space-y-1">
              <li className="flex items-center gap-2">
                {steps.authorization === 'done'
                  ? <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                  : steps.authorization === 'attention'
                    ? <AlertTriangle className="w-4 h-4 text-amber-500" />
                    : <Circle className="w-4 h-4 text-slate-300" />}
                <span className="text-slate-800">{copy.steps.authorization}</span>
                <span className="text-slate-500" data-testid="shopify-step-authorization" data-step={steps.authorization}>— {authLabel}</span>
              </li>
              <li className="flex items-center gap-2">
                <Circle className="w-4 h-4 text-slate-300" />
                <span className="text-slate-800">{copy.steps.productImport}</span>
                <span className="text-slate-500" data-testid="shopify-step-import" data-step={steps.productImport}>— {copy.steps.productImportNotStarted}</span>
              </li>
            </ol>
          </div>

          {status && status.connections.length > 0 && (
            <ul className="space-y-2">{status.connections.map(renderConnection)}</ul>
          )}

          {canStart && (
            <form
              className="space-y-2"
              onSubmit={(e) => {
                e.preventDefault()
                controller.start(shopInput)
              }}
              data-testid="shopify-start-form"
            >
              <label htmlFor="shopify-shop-input" className="text-xs font-medium text-slate-700">{copy.form.label}</label>
              <input
                id="shopify-shop-input"
                type="text"
                dir="ltr"
                inputMode="url"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={120}
                className="input w-full text-sm"
                placeholder={copy.form.placeholder}
                value={shopInput}
                disabled={busyStart}
                onChange={(e) => controller.setShopInput(e.target.value)}
              />
              <p className="text-[11px] text-slate-500">{copy.form.hint}</p>
              {preview && (
                <p className="text-xs text-slate-700" data-testid="shopify-shop-preview">
                  {copy.form.willConnect.replace('{shop}', preview)}
                </p>
              )}
              <button
                type="submit"
                className="btn-primary text-xs py-1.5"
                disabled={busyStart || state.disconnect.phase === 'working' || !shopInput.trim()}
                data-testid="shopify-connect-button"
              >
                {busyStart ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ShieldCheck className="w-3.5 h-3.5" />}
                {state.start.phase === 'redirecting'
                  ? copy.form.redirecting
                  : state.start.phase === 'starting' ? copy.form.starting : copy.form.connect}
              </button>
            </form>
          )}
        </>
      )}

      {state.start.phase === 'error' && (
        <div className="flex items-start gap-2 text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2" role="alert" data-testid="shopify-start-error">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <p>{copy.messages[state.start.message]}</p>
        </div>
      )}
      {state.disconnect.phase === 'error' && (
        <div className="flex items-start gap-2 text-xs text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2" role="alert" data-testid="shopify-disconnect-error">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <p>{copy.messages[state.disconnect.message]}</p>
        </div>
      )}
      {state.disconnect.phase === 'done' && (
        <p className="text-xs text-slate-700" role="status" data-testid="shopify-disconnect-done">{copy.connection.disconnectedDone}</p>
      )}

      <ConfirmModal
        open={confirmShop !== null}
        title={copy.disconnectConfirm.title}
        message={copy.disconnectConfirm.message.replace('{shop}', confirmShop ?? '')}
        confirmLabel={copy.disconnectConfirm.confirm}
        cancelLabel={copy.disconnectConfirm.cancel}
        destructive
        onCancel={() => controller.closeConfirm()}
        onConfirm={() => controller.confirmDisconnect()}
      />
    </section>
  )
}
