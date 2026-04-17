import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Bot, Link2, Search, Filter, Download, Store, MessageCircle, ShoppingBag } from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { ShoppingCart, Clock, CheckCircle, MessageSquare } from 'lucide-react'
import { featureRealityApi, type DashboardOrder, type OrderSourceKey, type OrdersDashboard } from '../api/featureReality'

type OrderStatus = 'paid' | 'pending' | 'failed' | 'cancelled'

const emptyData: OrdersDashboard = {
  summary: {
    total_orders: 0,
    today_revenue_sar: 0,
    pending_orders: 0,
    completed_today: 0,
    whatsapp_orders_today: 0,
    whatsapp_revenue_today: 0,
  },
  orders: [],
}

const TABS = [
  { key: 'all',        label: 'الكل' },
  { key: 'store',      label: 'من المتجر' },
  { key: 'whatsapp',   label: 'من واتساب' },
  { key: 'pending',    label: 'بانتظار الدفع' },
  { key: 'paid',       label: 'مدفوع' },
  { key: 'cancelled',  label: 'ملغي' },
] as const
type TabKey = typeof TABS[number]['key']

const statusVariant = (s: OrderStatus) =>
  s === 'paid' ? 'green' : s === 'pending' ? 'amber' : s === 'failed' ? 'red' : 'slate'

const statusLabel = (s: OrderStatus) =>
  s === 'paid'      ? 'مدفوع'         :
  s === 'pending'   ? 'قيد الانتظار'  :
  s === 'failed'    ? 'فشل'           : 'ملغي'

const SOURCE_LABEL_FALLBACK: Record<OrderSourceKey, string> = {
  salla:    'سلة',
  zid:      'زد',
  shopify:  'Shopify',
  whatsapp: 'واتساب',
  manual:   'يدوي',
}

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

// New ordering: الطلب · العميل · المبلغ · الحالة · المصدر · المنتجات · التاريخ
const TABLE_HEADERS = ['الطلب', 'العميل', 'المبلغ', 'الحالة', 'المصدر', 'المنتجات', 'التاريخ', '']

function formatDate(iso: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return new Intl.DateTimeFormat('ar-SA', {
      dateStyle: 'short',
      timeStyle: 'short',
    }).format(d)
  } catch {
    return iso
  }
}

export default function Orders() {
  const [tab, setTab] = useState<TabKey>('all')
  const [search, setSearch] = useState('')
  const [data, setData] = useState<OrdersDashboard>(emptyData)
  const { t } = useLanguage()

  useEffect(() => {
    featureRealityApi.orders()
      .then(setData)
      .catch(() => setData(emptyData))
  }, [])

  const filtered = data.orders.filter((o: DashboardOrder) => {
    if (tab === 'whatsapp'  && o.source !== 'whatsapp')                  return false
    if (tab === 'store'     && (o.source === 'whatsapp' || o.source === 'manual')) return false
    if (tab === 'pending'   && o.status !== 'pending')                   return false
    if (tab === 'paid'      && o.status !== 'paid')                      return false
    if (tab === 'cancelled' && o.status !== 'cancelled')                 return false
    const needle = search.toLowerCase()
    if (needle) {
      const haystack = [
        o.id,
        o.order_number,
        o.customer,
        o.customer_name,
        o.phone,
        o.external_id ?? '',
      ].join(' ').toLowerCase()
      if (!haystack.includes(needle)) return false
    }
    return true
  })

  return (
    <div className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.orders.title)}
        subtitle={t(tr => tr.pages.orders.subtitle)}
        action={
          <button className="btn-secondary text-sm">
            <Download className="w-4 h-4" /> {t(tr => tr.actions.export)}
          </button>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="إجمالي الطلبات"    value={String(data.summary.total_orders)}         icon={ShoppingCart} iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="بانتظار الدفع"     value={String(data.summary.pending_orders)}        icon={Clock}        iconColor="text-amber-600"   iconBg="bg-amber-50" />
        <StatCard label="مكتملة اليوم"      value={String(data.summary.completed_today)}        icon={CheckCircle}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label="إيرادات اليوم"     value={`${data.summary.today_revenue_sar.toLocaleString('ar-SA')} ر.س`}   icon={ShoppingCart} iconColor="text-slate-600"   iconBg="bg-slate-50" />
      </div>

      {/* Nahla-specific KPIs */}
      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="طلبات من واتساب اليوم"
          value={String(data.summary.whatsapp_orders_today)}
          icon={MessageSquare}
          iconColor="text-green-700"
          iconBg="bg-green-50"
        />
        <StatCard
          label="إيرادات من واتساب اليوم"
          value={`${data.summary.whatsapp_revenue_today.toLocaleString('ar-SA')} ر.س`}
          icon={Bot}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
      </div>

      {/* Table card */}
      <div className="card">
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-3 px-5 py-4 border-b border-slate-100">
          {/* Tabs */}
          <div className="flex flex-wrap gap-1">
            {TABS.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  tab === key ? 'bg-brand-500 text-white' : 'text-slate-500 hover:bg-slate-100'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="flex-1" />

          {/* Search */}
          <div className="relative">
            <Search className="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              className="input ps-8 text-xs py-1.5 w-48"
              placeholder={t(tr => tr.actions.search) + '...'}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <button className="btn-secondary text-xs py-1.5">
            <Filter className="w-3.5 h-3.5" /> تصفية
          </button>
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {TABLE_HEADERS.map((h, i) => (
                  <th key={`${h}-${i}`} className="text-start px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.map((o) => {
                const detailHref = `/orders/${encodeURIComponent(o.internal_id || o.order_number || o.id)}`
                const Icon = sourceIcon(o.source)
                const sourceLabel = o.source_label || SOURCE_LABEL_FALLBACK[o.source] || o.source
                const sourceCls   = SOURCE_BADGE_CLASS[o.source] || SOURCE_BADGE_CLASS.manual
                return (
                  <tr key={`${o.id}-${o.internal_id ?? ''}`} className="hover:bg-slate-50 transition-colors">
                    <td className="px-5 py-3.5 whitespace-nowrap">
                      <Link
                        to={detailHref}
                        className="text-xs font-mono font-medium text-brand-600 hover:text-brand-700 hover:underline"
                        dir="ltr"
                      >
                        {o.order_number || o.id}
                      </Link>
                      {o.is_ai_created && (
                        <span className="ms-1 inline-flex items-center gap-0.5 text-[10px] text-brand-600">
                          <Bot className="w-2.5 h-2.5" />
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-3.5">
                      <Link to={detailHref} className="block hover:underline">
                        <p className="text-xs font-medium text-slate-900">{o.customer_name || o.customer || o.phone || '—'}</p>
                        <p className="text-xs text-slate-400" dir="ltr">{o.phone}</p>
                      </Link>
                    </td>
                    <td className="px-5 py-3.5 text-xs font-semibold text-slate-900 whitespace-nowrap">{o.amount}</td>
                    <td className="px-5 py-3.5">
                      <Badge label={statusLabel(o.status)} variant={statusVariant(o.status)} dot />
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-1 flex-wrap">
                        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-[11px] font-medium ${sourceCls}`}>
                          <Icon className="w-3 h-3" /> {sourceLabel}
                        </span>
                        {o.is_ai_created
                          ? <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-brand-200 bg-brand-50 text-brand-700 text-[10px] font-medium"><Bot className="w-2.5 h-2.5" /> أنشأه الذكاء</span>
                          : <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-slate-200 bg-slate-50 text-slate-600 text-[10px] font-medium"><Store className="w-2.5 h-2.5" /> من المتجر</span>}
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-600 max-w-[18rem] truncate" title={o.items}>{o.items}</td>
                    <td className="px-5 py-3.5 text-xs text-slate-400 whitespace-nowrap">{formatDate(o.createdAt)}</td>
                    <td className="px-5 py-3.5 whitespace-nowrap">
                      {o.paymentLink ? (
                        <a
                          href={o.paymentLink.startsWith('http') ? o.paymentLink : `https://${o.paymentLink}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 font-medium"
                          dir="ltr"
                          title="رابط الدفع"
                        >
                          <Link2 className="w-3 h-3" />
                        </a>
                      ) : null}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

          {filtered.length === 0 && (
            <div className="py-12 text-center text-sm text-slate-400">لا توجد طلبات.</div>
          )}
        </div>

        {/* Pagination */}
        <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
          <p className="text-xs text-slate-400">عرض {filtered.length} من {data.orders.length} طلب</p>
        </div>
      </div>
    </div>
  )
}
