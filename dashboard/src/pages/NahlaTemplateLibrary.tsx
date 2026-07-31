import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ExternalLink, Package, RefreshCw, Store } from 'lucide-react'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  ORDER_UPDATE_SERVICE_KEYS,
  orderUpdatesApi,
  serviceBodyText,
  type OrderUpdateServiceDetail,
  type OrderUpdateServiceKey,
} from '../api/orderUpdates'

type StoreFilter = 'all' | 'order_updates' | 'marketing'

type OrderUpdateCard = {
  serviceKey: OrderUpdateServiceKey
  body: string
  detail: OrderUpdateServiceDetail | null
}

/**
 * E-commerce store templates surface (organizational).
 *
 * Honest data contract (no invented store-template library):
 * - Order updates: only services in ORDER_UPDATE_SERVICE_KEYS via /order-updates/*
 * - Marketing / store-UI / custom store templates: not backed by a distinct
 *   store-template API today — show an explicit empty state, never nahlaLibrary
 *   WhatsApp MARKETING rows re-labeled as store templates.
 *
 * Ops (enablement, timing, channel) remain at /settings?tab=order_updates.
 *
 * Open-window / Meta / Lifecycle send orchestration are out of scope here.
 */
export default function NahlaTemplateLibrary() {
  const { t, dir } = useLanguage()
  const { hash } = useLocation()
  const page = t(tr => tr.pages.ecommerceTemplates)

  const [filter, setFilter] = useState<StoreFilter>('all')
  const [loading, setLoading] = useState(true)
  const [orderCards, setOrderCards] = useState<OrderUpdateCard[]>([])

  // Only expose filters with a real contract today:
  // - all / order_updates → ORDER_UPDATE_SERVICE_KEYS
  // - marketing → unsupported store category (empty only; no WhatsApp MARKETING reuse)
  const filters = useMemo(
    () =>
      [
        { key: 'all' as const, label: page.filters.all },
        { key: 'order_updates' as const, label: page.filters.orderUpdates },
        { key: 'marketing' as const, label: page.filters.marketing },
      ] satisfies { key: StoreFilter; label: string }[],
    [page.filters],
  )

  const serviceLabel = (key: OrderUpdateServiceKey): string => {
    if (key === 'order_confirmation') return page.orderUpdates.services.order_confirmation
    return page.orderUpdates.services.shipping_tracking
  }

  const serviceDescription = (key: OrderUpdateServiceKey): string => {
    if (key === 'order_confirmation') return page.orderUpdates.serviceDescriptions.order_confirmation
    return page.orderUpdates.serviceDescriptions.shipping_tracking
  }

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const details = await Promise.all(
        ORDER_UPDATE_SERVICE_KEYS.map(async key => {
          try {
            const detail = await orderUpdatesApi.getService(key)
            return { serviceKey: key, body: serviceBodyText(detail), detail } satisfies OrderUpdateCard
          } catch {
            return { serviceKey: key, body: '', detail: null } satisfies OrderUpdateCard
          }
        }),
      )
      setOrderCards(details)
    } catch {
      setOrderCards(
        ORDER_UPDATE_SERVICE_KEYS.map(key => ({
          serviceKey: key,
          body: '',
          detail: null,
        })),
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (hash === '#ecommerce' || hash === '#order-updates') {
      setFilter('order_updates')
      requestAnimationFrame(() => {
        document.getElementById('order-updates')?.scrollIntoView({
          behavior: 'smooth',
          block: 'start',
        })
      })
    }
  }, [hash])

  const showOrderUpdates =
    filter === 'all' || filter === 'order_updates'
  const showMarketingEmpty = filter === 'marketing'
  const showAllUnsupportedNote = filter === 'all'

  return (
    <div className="space-y-6" dir={dir}>
      <PageHeader title={page.title} subtitle={page.subtitle} />

      <div className="flex flex-wrap gap-2">
        {filters.map(item => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              filter === item.key
                ? 'bg-brand-500 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {showOrderUpdates && (
        <section id="order-updates" className="card p-5 scroll-mt-24">
          <div className="flex items-start gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center shrink-0">
              <Package className="w-5 h-5 text-slate-600" />
            </div>
            <div className="flex-1 min-w-0">
              <h2 className="text-sm font-semibold text-slate-900">{page.orderUpdates.title}</h2>
              <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                {page.orderUpdates.description}
              </p>
              <p className="text-[11px] text-slate-400 mt-2">{page.orderUpdates.scopeNote}</p>
              <Link
                to="/settings?tab=order_updates"
                className="inline-flex items-center gap-1.5 mt-4 text-xs font-medium text-brand-600 hover:text-brand-700"
              >
                {page.orderUpdates.opsLink}
                <ExternalLink className="w-3.5 h-3.5" />
              </Link>
            </div>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-12">
              <RefreshCw className="w-6 h-6 text-amber-500 animate-spin" />
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {orderCards.map(card => (
                <div
                  key={card.serviceKey}
                  className="border border-slate-200 rounded-xl p-4 bg-white"
                >
                  <p className="text-[10px] font-semibold text-slate-500 mb-1">
                    {card.serviceKey}
                  </p>
                  <p className="text-sm font-semibold text-slate-900">
                    {serviceLabel(card.serviceKey)}
                  </p>
                  <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                    {serviceDescription(card.serviceKey)}
                  </p>
                  {card.body ? (
                    <p className="text-[11px] text-slate-400 mt-2 leading-relaxed line-clamp-3 whitespace-pre-line">
                      {card.body}
                    </p>
                  ) : (
                    <p className="text-[11px] text-slate-400 mt-2">
                      {page.orderUpdates.noPreview}
                    </p>
                  )}
                  <Link
                    to="/settings?tab=order_updates"
                    className="inline-flex items-center gap-1 mt-3 text-xs font-medium text-brand-600 hover:text-brand-700"
                  >
                    {page.orderUpdates.opsLinkShort}
                    <ExternalLink className="w-3.5 h-3.5" />
                  </Link>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      <section id="ecommerce" className="card p-5 scroll-mt-24">
        <div className="flex items-start gap-3 mb-3">
          <div className="w-10 h-10 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center shrink-0">
            <Store className="w-5 h-5 text-amber-600" />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900">{page.libraryTitle}</h2>
            <p className="text-xs text-slate-500 mt-1">{page.librarySubtitle}</p>
          </div>
        </div>

        {showMarketingEmpty && (
          <p className="text-center text-slate-400 text-sm py-10">{page.empty.marketing}</p>
        )}
        {showAllUnsupportedNote && !showMarketingEmpty && (
          <p className="text-xs text-slate-500 leading-relaxed bg-slate-50 border border-slate-100 rounded-lg px-3 py-2">
            {page.empty.unsupportedStoreLibrary}
          </p>
        )}
        {!showMarketingEmpty && !showAllUnsupportedNote && filter === 'order_updates' && (
          <p className="text-xs text-slate-400 leading-relaxed">
            {page.empty.orderUpdatesOnlyHint}
          </p>
        )}
      </section>
    </div>
  )
}
