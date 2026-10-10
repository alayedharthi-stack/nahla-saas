import { AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react'
import { useEffect, useSyncExternalStore } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { shopifyConnectionApi } from '../api/shopifyConnection'
import { getSessionBindingKey } from '../auth'
import { watchSessionSignals } from '../components/integrations/shopifySessionSignals'
import { useLanguage } from '../i18n/context'
import { isShopifyConnectionUiEnabled } from '../lib/platformFeatureFlags'
import { createCompletionController } from '../lib/shopifyConnection/completionController'
import {
  SHOPIFY_COMPLETE_PATH,
  discardShopifyReturn,
  takeShopifyReturn,
} from '../lib/shopifyConnection/returnCapture'

/**
 * /integrations/shopify/complete — the fixed return target of the backend
 * callback. The fragment was already taken out of the URL at boot
 * (``bootCapture``); this page only consumes the in-memory value once.
 *
 * One controller per page load: React StrictMode's double mount and any
 * re-render join the same single-flight completion.
 */
const completion = createCompletionController({
  enabled: isShopifyConnectionUiEnabled,
  sessionKey: getSessionBindingKey,
  now: () => Date.now(),
  take: () => takeShopifyReturn(Date.now()),
  discard: discardShopifyReturn,
  complete: shopifyConnectionApi.complete,
  status: shopifyConnectionApi.status,
  schedule: (fn, ms) => {
    const id = window.setTimeout(fn, ms)
    return () => window.clearTimeout(id)
  },
})

export default function ShopifyConnectionComplete() {
  const { tStatic } = useLanguage()
  const copy = tStatic(tr => tr.shopifyConnection)
  const state = useSyncExternalStore(completion.subscribe, completion.getSnapshot)
  const location = useLocation()
  const navigate = useNavigate()

  // Only the boot capture reads return data. An in-app navigation that still
  // carries a query or fragment is cleaned here, never parsed.
  useEffect(() => {
    if (location.search || location.hash) navigate(SHOPIFY_COMPLETE_PATH, { replace: true })
  }, [location.search, location.hash, navigate])

  useEffect(() => {
    completion.attach()
    const stopSignals = watchSessionSignals(completion.onSessionMaybeChanged)
    return () => {
      stopSignals()
      completion.detach()
    }
  }, [])

  const view = state.view
  const check = state.check
  const ok = view.phase === 'connected'
  const working = view.phase === 'idle' || view.phase === 'completing'

  let message: string
  switch (view.phase) {
    case 'idle':
    case 'completing': message = copy.complete.completing; break
    case 'connected': message = copy.complete.connected; break
    case 'feature_off': message = copy.complete.featureOff; break
    case 'nothing': message = copy.complete.nothing; break
    case 'invalid': message = copy.complete.invalid; break
    case 'expired': message = copy.complete.expired; break
    case 'unconfirmed': message = copy.complete.unconfirmed; break
    case 'uncertain': message = copy.complete.uncertain; break
    case 'session_changed': message = copy.complete.sessionChanged; break
    case 'cancelled': message = copy.complete.cancelled; break
    case 'refused': message = copy.messages[view.message]; break
  }

  const canCheck = ['unconfirmed', 'uncertain', 'cancelled', 'refused', 'nothing', 'expired'].includes(view.phase)
  const activeShops = check.kind === 'done'
    ? check.status.connections.filter((c) => c.status === 'active').map((c) => c.shopDomain)
    : []

  return (
    <div className="max-w-xl mx-auto">
      <section className="card p-6 space-y-4" data-testid="shopify-complete" data-phase={view.phase}>
        <div
          className={`flex items-start gap-3 rounded-xl border p-4 text-sm ${
            ok
              ? 'bg-emerald-50 border-emerald-200 text-emerald-900'
              : working
                ? 'bg-slate-50 border-slate-200 text-slate-700'
                : 'bg-amber-50 border-amber-200 text-amber-900'
          }`}
          role={working ? 'status' : 'alert'}
        >
          {ok
            ? <CheckCircle2 className="w-5 h-5 shrink-0" />
            : working
              ? <Loader2 className="w-5 h-5 shrink-0 animate-spin" />
              : <AlertTriangle className="w-5 h-5 shrink-0" />}
          <div className="space-y-1">
            <p className="font-medium" data-testid="shopify-complete-message">{message}</p>
            {view.phase === 'connected' && (
              <>
                <p dir="auto">{copy.complete.connectedShop.replace('{shop}', view.connection.shopDomain)}</p>
                <p className="text-xs" data-testid="shopify-complete-next">{copy.complete.nextStep}</p>
              </>
            )}
          </div>
        </div>

        {check.kind === 'loading' && (
          <p className="text-xs text-slate-500 flex items-center gap-2" role="status">
            <Loader2 className="w-3.5 h-3.5 animate-spin" /> {copy.complete.checking}
          </p>
        )}
        {check.kind === 'failed' && <p className="text-xs text-amber-800" role="alert">{copy.complete.statusFailed}</p>}
        {check.kind === 'done' && (
          <div className="text-xs text-slate-700 space-y-0.5" data-testid="shopify-complete-status">
            {activeShops.length > 0
              ? activeShops.map((shop) => <p key={shop}>{copy.complete.statusActive.replace('{shop}', shop)}</p>)
              : <p>{copy.complete.statusNone}</p>}
          </div>
        )}

        <div className="flex flex-wrap gap-2">
          {view.phase === 'refused' && view.retryable && (
            <>
              <button type="button" className="btn-primary text-xs py-1.5" onClick={() => completion.retry()} data-testid="shopify-complete-retry">
                {copy.complete.retry}
              </button>
              <button type="button" className="btn-secondary text-xs py-1.5" onClick={() => completion.cancel()} data-testid="shopify-complete-cancel">
                {copy.complete.cancel}
              </button>
            </>
          )}
          {canCheck && !(view.phase === 'refused' && view.retryable) && (
            <button
              type="button"
              className="btn-secondary text-xs py-1.5"
              disabled={check.kind === 'loading'}
              onClick={() => completion.checkStatus()}
              data-testid="shopify-complete-check"
            >
              {copy.complete.checkStatus}
            </button>
          )}
          {!working && (
            <Link to="/integrations" className="btn-ghost text-xs py-1.5" data-testid="shopify-complete-back">
              {copy.complete.backToIntegrations}
            </Link>
          )}
        </div>
      </section>
    </div>
  )
}
