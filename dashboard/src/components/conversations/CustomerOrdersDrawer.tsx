import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp, ExternalLink, Loader2, Package, Truck, X } from 'lucide-react'

import {
  featureRealityApi,
  type ConversationCustomerOrder,
} from '../../api/featureReality'
import { formatRiyadh } from '../../lib/datetime'

export interface CustomerOrdersLabels {
  title: string
  readOnly: string
  close: string
  loading: string
  empty: string
  loadError: string
  retry: string
  order: string
  date: string
  status: string
  total: string
  items: string
  shipment: string
  carrier: string
  tracking: string
  openTracking: string
  details: string
}

export default function CustomerOrdersDrawer({
  open,
  onClose,
  phone,
  customerId,
  customerLabel,
  labels,
  dir,
}: {
  open: boolean
  onClose: () => void
  phone: string
  customerId?: number | null
  customerLabel: string
  labels: CustomerOrdersLabels
  dir: 'rtl' | 'ltr'
}) {
  const [orders, setOrders] = useState<ConversationCustomerOrder[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (!open || !phone) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setExpanded(null)
    featureRealityApi
      .conversationCustomerOrders(phone, { customerId, limit: 10 })
      .then((result) => {
        if (!cancelled) setOrders(result.orders || [])
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setOrders([])
          setError(reason instanceof Error ? reason.message : labels.loadError)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [open, phone, customerId, reloadKey, labels.loadError])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[80]" role="dialog" aria-modal="true" aria-label={labels.title} dir={dir}>
      <button
        type="button"
        className="absolute inset-0 bg-slate-950/35"
        onClick={onClose}
        aria-label={labels.close}
      />
      <aside
        className="absolute inset-y-0 end-0 flex w-full max-w-md flex-col bg-white shadow-2xl"
        data-customer-orders-drawer="read-only"
      >
        <header className="flex shrink-0 items-start gap-3 border-b border-slate-200 px-4 py-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-50 text-brand-600">
            <Package className="h-5 w-5" aria-hidden />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="font-semibold text-slate-900">{labels.title}</h2>
            <p className="truncate text-xs text-slate-500">{customerLabel}</p>
            <span className="mt-1 inline-flex rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">
              {labels.readOnly}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-2 text-slate-500 hover:bg-slate-100"
            aria-label={labels.close}
          >
            <X className="h-5 w-5" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto bg-slate-50 p-3">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-slate-500">
              <Loader2 className="h-4 w-4 animate-spin" />
              {labels.loading}
            </div>
          )}
          {!loading && error && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
              <p>{labels.loadError}</p>
              <button
                type="button"
                onClick={() => setReloadKey((key) => key + 1)}
                className="mt-3 rounded-lg bg-white px-3 py-1.5 font-medium shadow-sm"
              >
                {labels.retry}
              </button>
            </div>
          )}
          {!loading && !error && orders.length === 0 && (
            <div className="py-16 text-center text-sm text-slate-500">{labels.empty}</div>
          )}
          {!loading && !error && orders.length > 0 && (
            <div className="space-y-2">
              {orders.map((order) => {
                const isExpanded = expanded === order.id
                return (
                  <article key={order.id} className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                    <button
                      type="button"
                      className="w-full p-3 text-start"
                      onClick={() => setExpanded(isExpanded ? null : order.id)}
                      aria-expanded={isExpanded}
                    >
                      <div className="flex items-start gap-2">
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-semibold text-slate-900">{labels.order} {order.reference}</span>
                            {order.statusLabel && (
                              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                                {order.statusLabel}
                              </span>
                            )}
                          </div>
                          <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-slate-500">
                            <span>{labels.date}: {order.date ? formatRiyadh(order.date) : '—'}</span>
                            <span>{labels.total}: {order.formattedTotal || `${order.total ?? '—'} ${order.currency || ''}`}</span>
                            <span className="col-span-2 truncate">{labels.items}: {order.itemSummary || order.itemCount}</span>
                          </div>
                        </div>
                        {isExpanded ? <ChevronUp className="h-4 w-4 shrink-0 text-slate-400" /> : <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" />}
                      </div>
                    </button>

                    {isExpanded && (
                      <div className="space-y-3 border-t border-slate-100 bg-slate-50/60 p-3 text-xs text-slate-600">
                        {order.lineItems.length > 0 && (
                          <div>
                            <div className="mb-1 font-medium text-slate-700">{labels.details}</div>
                            <ul className="space-y-1">
                              {order.lineItems.map((item, index) => (
                                <li key={`${item.product_id}-${index}`} className="flex justify-between gap-3">
                                  <span>{item.name} ×{item.quantity}</span>
                                  {item.line_total != null && <span>{item.line_total} {order.currency || ''}</span>}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                        {order.shipment && (
                          <div className="rounded-lg border border-blue-100 bg-blue-50 p-2.5">
                            <div className="flex items-center gap-1.5 font-medium text-blue-800">
                              <Truck className="h-3.5 w-3.5" /> {labels.shipment}
                            </div>
                            <div className="mt-1 space-y-0.5 text-blue-700">
                              {order.shipment.status && <div>{labels.status}: {order.shipment.status}</div>}
                              {order.shipment.carrier && <div>{labels.carrier}: {order.shipment.carrier}</div>}
                              {order.shipment.trackingNumber && <div>{labels.tracking}: {order.shipment.trackingNumber}</div>}
                              {order.shipment.trackingUrl && (
                                <a href={order.shipment.trackingUrl} target="_blank" rel="noopener noreferrer" className="mt-1 inline-flex items-center gap-1 font-medium underline">
                                  <ExternalLink className="h-3 w-3" /> {labels.openTracking}
                                </a>
                              )}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </article>
                )
              })}
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}
