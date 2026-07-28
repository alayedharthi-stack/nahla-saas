import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Bot, Crown, Link2, Search, Filter, Download, Store, MessageCircle, ShoppingBag } from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import { ShoppingCart, Clock, CheckCircle, MessageSquare } from 'lucide-react'
import { featureRealityApi, type DashboardOrder, type NeedsActionLevel, type OrderSourceKey, type OrdersDashboard } from '../api/featureReality'
import { formatOrderNumberLabel, orderDetailPath } from '../lib/orderRoutes'
import { formatRiyadh } from '../lib/datetime'
import {
  EMPTY_ORDERS_DASHBOARD,
  ordersRevenueDisplay,
  ordersStatDisplay,
  shouldApplyOrdersRequest,
} from './ordersLoadState'

// UI_ONLY_GUARD: only static labels below use t(); customer/product names stay as API data.

type OrderStatus = 'paid' | 'pending' | 'failed' | 'cancelled'

const TAB_KEYS = [
  'all',
  'needs_action',
  'missing_location',
  'pending_payment',
  'payment_submitted',
  'paid',
  'abandoned',
  'completed',
  'cancelled',
  'store',
  'whatsapp',
] as const
type TabKey = typeof TAB_KEYS[number]

const TAB_TO_FILTER: Partial<Record<TabKey, string>> = {
  needs_action: 'needs_action',
  missing_location: 'missing_location',
  pending_payment: 'pending_payment',
  payment_submitted: 'payment_submitted',
  paid: 'paid',
  abandoned: 'abandoned',
  completed: 'completed',
  cancelled: 'cancelled',
}

const NEEDS_ACTION_CHIP: Record<NeedsActionLevel, string> = {
  amber:  'bg-amber-50  text-amber-700  border-amber-200',
  red:    'bg-red-50    text-red-700    border-red-200',
  blue:   'bg-blue-50   text-blue-700   border-blue-200',
  purple: 'bg-purple-50 text-purple-700 border-purple-200',
}

const statusVariant = (s: OrderStatus) =>
  s === 'paid' ? 'green' : s === 'pending' ? 'amber' : s === 'failed' ? 'red' : 'slate'

const SOURCE_BADGE_CLASS: Record<OrderSourceKey, string> = {
  salla:    'bg-orange-50 text-orange-700 border-orange-200',
  zid:      'bg-purple-50 text-purple-700 border-purple-200',
  shopify:  'bg-emerald-50 text-emerald-700 border-emerald-200',
  whatsapp: 'bg-green-50 text-green-700 border-green-200',
  manual:   'bg-slate-50 text-slate-600 border-slate-200',
}

const sourceIcon = (s: OrderSourceKey) =>
  s === 'whatsapp' ? MessageCircle :
  s === 'manual'   ? ShoppingBag   : Store

const formatDate = (iso: string): string => formatRiyadh(iso)

export default function Orders() {
  const [tab, setTab] = useState<TabKey>('all')
  const [search, setSearch] = useState('')
  const [data, setData] = useState<OrdersDashboard>(EMPTY_ORDERS_DASHBOARD)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadNonce, setReloadNonce] = useState(0)
  const latestRequestRef = useRef(0)
  const { t, lang, dir } = useLanguage()
  const op = t(tr => tr.ordersPage)

  const tabs = useMemo(() => ([
    { key: 'all' as TabKey,               label: op.tabs.all },
    { key: 'needs_action' as TabKey,      label: op.tabs.needsAction },
    { key: 'missing_location' as TabKey,  label: op.tabs.missingLocation },
    { key: 'pending_payment' as TabKey,   label: op.tabs.pendingPayment },
    { key: 'payment_submitted' as TabKey,  label: op.tabs.paymentSubmitted },
    { key: 'paid' as TabKey,              label: op.tabs.paid },
    { key: 'abandoned' as TabKey,         label: op.tabs.abandoned },
    { key: 'completed' as TabKey,          label: op.tabs.completed },
    { key: 'cancelled' as TabKey,         label: op.tabs.cancelled },
    { key: 'store' as TabKey,             label: op.tabs.store },
    { key: 'whatsapp' as TabKey,          label: op.tabs.whatsapp },
  ]), [op])

  const tableHeaders = useMemo(() => [
    op.table.order,
    op.table.customer,
    op.table.amount,
    op.table.status,
    op.table.source,
    op.table.products,
    op.table.date,
    '',
  ], [op])

  const statusLabel = (s: OrderStatus) => op.status[s]

  const sourceLabel = (s: OrderSourceKey) => op.source[s] ?? s

  const locale = lang === 'ar' ? 'ar-SA' : 'en-US'

  useEffect(() => {
    let cancelled = false
    const requestId = latestRequestRef.current + 1
    latestRequestRef.current = requestId

    setLoading(true)
    setLoadError(null)
    setData(EMPTY_ORDERS_DASHBOARD)

    const lifecycle = TAB_TO_FILTER[tab]
    const source = tab === 'whatsapp' ? 'whatsapp' : undefined
    featureRealityApi.orders({ lifecycle_filter: lifecycle, source })
      .then((dashboard) => {
        if (!shouldApplyOrdersRequest(requestId, latestRequestRef.current, cancelled)) return
        setData(dashboard)
      })
      .catch((err: unknown) => {
        if (!shouldApplyOrdersRequest(requestId, latestRequestRef.current, cancelled)) return
        console.error('[Orders] failed to load orders', err)
        setLoadError(op.loadError)
        setData(EMPTY_ORDERS_DASHBOARD)
      })
      .finally(() => {
        if (!shouldApplyOrdersRequest(requestId, latestRequestRef.current, cancelled)) return
        setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [tab, reloadNonce, op.loadError])

  const statValue = (value: number) => ordersStatDisplay(value, loadError)

  const filtered = data.orders.filter((o: DashboardOrder) => {
    if (tab === 'store' && (o.source === 'whatsapp' || o.source === 'manual')) return false
    const needle = search.toLowerCase()
    if (needle) {
      const haystack = [
        o.id,
        o.order_number,
        o.customer,
        o.customer_name,
        o.phone,
        o.list_summary ?? '',
        o.city_line ?? '',
        o.external_id ?? '',
      ].join(' ').toLowerCase()
      if (!haystack.includes(needle)) return false
    }
    return true
  })

  void UI_ONLY_GUARD

  return (
    <div dir={dir} className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.orders.title)}
        subtitle={t(tr => tr.pages.orders.subtitle)}
        action={
          <button className="btn-secondary text-sm">
            <Download className="w-4 h-4" /> {t(tr => tr.actions.export)}
          </button>
        }
      />

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label={op.cards.totalOrders}      value={statValue(data.summary.total_orders)}         icon={ShoppingCart} iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label={op.cards.needsFollowUpNow} value={statValue(data.summary.orders_needing_action)} icon={AlertTriangle} iconColor="text-red-600"     iconBg="bg-red-50" />
        <StatCard label={op.cards.pendingPayment}   value={statValue(data.summary.pending_orders)}       icon={Clock}        iconColor="text-amber-600"   iconBg="bg-amber-50" />
        <StatCard label={op.cards.completedToday}   value={statValue(data.summary.completed_today)}      icon={CheckCircle}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <StatCard
          label={op.cards.whatsappOrdersToday}
          value={statValue(data.summary.whatsapp_orders_today)}
          icon={MessageSquare}
          iconColor="text-green-600"
          iconBg="bg-green-50"
        />
        <StatCard
          label={op.cards.whatsappRevenueToday}
          value={ordersRevenueDisplay(
            data.summary.whatsapp_revenue_today,
            loadError,
            locale,
            op.currency,
          )}
          icon={Crown}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label={op.cards.todayRevenue}
          value={ordersRevenueDisplay(
            data.summary.today_revenue_sar,
            loadError,
            locale,
            op.currency,
          )}
          icon={ShoppingCart}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
      </div>

      <div className="card p-0 overflow-hidden">
        {loadError && (
          <div className="px-5 py-4 border-b border-red-100 bg-red-50 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-red-700">{loadError}</p>
            <button
              type="button"
              className="btn-secondary text-xs"
              onClick={() => setReloadNonce((n) => n + 1)}
            >
              {op.retry}
            </button>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 p-4 border-b border-slate-100">
          {tabs.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                tab === key
                  ? 'bg-brand-600 text-white'
                  : 'bg-slate-50 text-slate-600 hover:bg-slate-100'
              }`}
            >
              {label}
            </button>
          ))}
          <div className="flex-1" />
          <div className="relative">
            <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder={t(tr => tr.actions.search)}
              dir={dir}
              className="ps-9 pe-3 py-1.5 text-sm border border-slate-200 rounded-lg w-48 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
            />
          </div>
          <button className="btn-secondary text-xs py-1.5">
            <Filter className="w-3.5 h-3.5" /> {t(tr => tr.actions.filter)}
          </button>
        </div>

        <div dir={dir} className="overflow-x-auto">
          {loading ? (
            <div className="py-12 text-center text-sm text-slate-400">جاري التحميل…</div>
          ) : (
          <table className="w-full text-sm" dir={dir}>
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/60">
                {tableHeaders.map((h, i) => (
                  <th key={i} className="px-5 py-3 text-xs font-semibold text-slate-500 text-start whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((o: DashboardOrder) => {
                const SourceIcon = sourceIcon(o.source)
                const chips = o.action_chips?.length ? o.action_chips : o.needs_action
                const displayStatus = o.status_label_ar || o.raw_status_label || o.status_label || statusLabel(o.status as OrderStatus)
                return (
                  <tr key={o.internal_id || o.id} className="border-b border-slate-50 hover:bg-slate-50/50 transition-colors">
                    <td className="px-5 py-3.5">
                      <Link to={orderDetailPath(o)} className="text-xs font-semibold text-brand-600 hover:underline">
                        {formatOrderNumberLabel(o)}
                      </Link>
                      {o.is_ai_created && (
                        <span className="inline-flex items-center text-brand-600 ms-1" title={op.badges.createdByAI}>
                          <Bot className="w-3 h-3" />
                        </span>
                      )}
                      {(chips?.length ?? 0) > 0 && (
                        <span
                          className={`inline-flex items-center gap-0.5 ms-1 px-1.5 py-0.5 rounded border text-[10px] font-medium ${NEEDS_ACTION_CHIP[chips![0].level]}`}
                          title={chips![0].label}
                        >
                          <AlertTriangle className="w-2.5 h-2.5" />
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-3.5">
                      <p className="text-xs font-medium text-slate-800">{o.customer_name || o.customer}</p>
                      {o.phone && (
                        <p dir="ltr" className="text-[10px] text-slate-400 mt-0.5 text-start">{o.phone}</p>
                      )}
                    </td>
                    <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">
                      {o.amount_sar != null ? `${Number(o.amount_sar).toLocaleString(locale)} ${op.currency}` : '—'}
                    </td>
                    <td className="px-5 py-3.5">
                      <Badge variant={statusVariant(o.status as OrderStatus)} label={displayStatus} />
                      {o.payment_status_label_ar && (
                        <p className="text-[10px] text-slate-500 mt-1">{o.payment_status_label_ar}</p>
                      )}
                      {o.address_status_label_ar && o.address_status_label_ar !== '—' && (
                        <p className="text-[10px] text-slate-500">{o.address_status_label_ar}</p>
                      )}
                    </td>
                    <td className="px-5 py-3.5">
                      <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] font-medium ${SOURCE_BADGE_CLASS[o.source]}`}>
                        <SourceIcon className="w-2.5 h-2.5" />
                        {sourceLabel(o.source)}
                      </span>
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-600 max-w-[220px]">
                      <p className="truncate">{o.items || '—'}</p>
                      {o.city_line && o.city_line !== '—' && (
                        <p className="text-[10px] text-slate-400 truncate mt-0.5">{o.city_line}</p>
                      )}
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-500 whitespace-nowrap">
                      {o.display_created_at
                        ? formatDate(o.display_created_at)
                        : o.createdAt
                          ? formatDate(o.createdAt)
                          : o.updated_at
                            ? formatDate(o.updated_at)
                            : '—'}
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2">
                        {o.paymentLink && (
                          <a
                            href={o.paymentLink}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-slate-400 hover:text-brand-600"
                            title={op.badges.paymentLink}
                          >
                            <Link2 className="w-3.5 h-3.5" />
                          </a>
                        )}
                        {o.source === 'whatsapp'
                          ? <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-brand-200 bg-brand-50 text-brand-700 text-[10px] font-medium"><Bot className="w-2.5 h-2.5" /> {op.badges.createdByAI}</span>
                          : <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-slate-200 bg-slate-50 text-slate-600 text-[10px] font-medium"><Store className="w-2.5 h-2.5" /> {op.badges.fromStore}</span>}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          )}
          {!loading && !loadError && filtered.length === 0 && (
            <div className="py-12 text-center text-sm text-slate-400">{op.empty}</div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100">
          <p className="text-xs text-slate-400">
            {op.showing.replace('{shown}', String(filtered.length)).replace('{total}', String(data.orders.length))}
          </p>
        </div>
      </div>
    </div>
  )
}
