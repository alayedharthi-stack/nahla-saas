import { useState } from 'react'
import { Bot, Link2, Search, Filter, Download } from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { ShoppingCart, DollarSign, Clock, CheckCircle } from 'lucide-react'

type OrderStatus = 'paid' | 'pending' | 'failed' | 'cancelled'
type OrderSource = 'AI' | 'manual'

interface Order {
  id: string
  customer: string
  phone: string
  items: string
  amount: string
  status: OrderStatus
  source: OrderSource
  paymentLink?: string
  createdAt: string
}

const orders: Order[] = [
  { id: '#3812', customer: 'Ahmed Al-Rashid',  phone: '+966 50 123 4567', items: 'هودي أحمر XL',              amount: '342 ر.س', status: 'paid',      source: 'AI',     createdAt: 'منذ 5 دقائق' },
  { id: '#3811', customer: 'Nora Al-Mutairi',  phone: '+966 52 654 3210', items: 'قميص أزرق × 2',             amount: '180 ر.س', status: 'pending',   source: 'AI',     paymentLink: 'pay.nahla.co/nk3x', createdAt: 'منذ 22 دقيقة' },
  { id: '#3810', customer: 'Khalid Ibrahim',   phone: '+966 57 888 7766', items: 'حذاء أبيض 42',              amount: '510 ر.س', status: 'paid',      source: 'manual', createdAt: 'منذ ساعة' },
  { id: '#3809', customer: 'Lina Al-Saud',     phone: '+966 54 551 2200', items: 'فستان صيفي M',              amount: '95 ر.س',  status: 'failed',    source: 'AI',     paymentLink: 'pay.nahla.co/lx7f', createdAt: 'منذ ساعتين' },
  { id: '#3808', customer: 'Omar Al-Ghamdi',   phone: '+966 56 321 9900', items: 'شورت رياضي L',              amount: '75 ر.س',  status: 'paid',      source: 'AI',     createdAt: 'منذ 3 ساعات' },
  { id: '#3807', customer: 'Reem Al-Harbi',    phone: '+966 55 410 0033', items: 'حزام جلد',                  amount: '149 ر.س', status: 'cancelled', source: 'manual', createdAt: 'منذ 4 ساعات' },
  { id: '#3806', customer: 'Yousef Al-Shehri', phone: '+966 50 775 5522', items: 'حزام ساعة ذكية أسود',      amount: '220 ر.س', status: 'pending',   source: 'AI',     paymentLink: 'pay.nahla.co/yw5v', createdAt: 'منذ 5 ساعات' },
]

const TABS = [
  { key: 'all',        label: 'الكل' },
  { key: 'ai',         label: 'أنشأها الذكاء' },
  { key: 'pending',    label: 'بانتظار الدفع' },
  { key: 'completed',  label: 'مكتملة' },
] as const
type TabKey = typeof TABS[number]['key']

const statusVariant = (s: OrderStatus) =>
  s === 'paid' ? 'green' : s === 'pending' ? 'amber' : s === 'failed' ? 'red' : 'slate'

const statusLabel = (s: OrderStatus) =>
  s === 'paid'      ? 'مدفوع'         :
  s === 'pending'   ? 'قيد الانتظار'  :
  s === 'failed'    ? 'فشل'           : 'ملغي'

const TABLE_HEADERS = ['الطلب', 'العميل', 'المنتجات', 'المبلغ', 'الحالة', 'المصدر', 'رابط الدفع', 'التاريخ']

export default function Orders() {
  const [tab, setTab] = useState<TabKey>('all')
  const [search, setSearch] = useState('')
  const { t } = useLanguage()

  const filtered = orders.filter((o) => {
    if (tab === 'ai'        && o.source !== 'AI')     return false
    if (tab === 'pending'   && o.status !== 'pending') return false
    if (tab === 'completed' && o.status !== 'paid')    return false
    if (search && !o.customer.toLowerCase().includes(search.toLowerCase()) &&
                  !o.id.toLowerCase().includes(search.toLowerCase())) return false
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
        <StatCard label="إجمالي الطلبات"    value="37"        change={8.2}  icon={ShoppingCart} iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="الإيرادات اليوم"   value="8,740 ر.س" change={12.4} icon={DollarSign}   iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="بانتظار الدفع"     value="7"         change={-2.1} icon={Clock}        iconColor="text-amber-600"   iconBg="bg-amber-50" />
        <StatCard label="مكتملة اليوم"      value="28"        change={5.3}  icon={CheckCircle}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
      </div>

      {/* Table card */}
      <div className="card">
        {/* Toolbar */}
        <div className="flex flex-wrap items-center gap-3 px-5 py-4 border-b border-slate-100">
          {/* Tabs */}
          <div className="flex gap-1">
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
                {TABLE_HEADERS.map((h) => (
                  <th key={h} className="text-start px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.map((o) => (
                <tr key={o.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-5 py-3.5 text-xs font-mono font-medium text-slate-700">{o.id}</td>
                  <td className="px-5 py-3.5">
                    <p className="text-xs font-medium text-slate-900">{o.customer}</p>
                    <p className="text-xs text-slate-400" dir="ltr">{o.phone}</p>
                  </td>
                  <td className="px-5 py-3.5 text-xs text-slate-600 whitespace-nowrap">{o.items}</td>
                  <td className="px-5 py-3.5 text-xs font-semibold text-slate-900 whitespace-nowrap">{o.amount}</td>
                  <td className="px-5 py-3.5">
                    <Badge label={statusLabel(o.status)} variant={statusVariant(o.status)} dot />
                  </td>
                  <td className="px-5 py-3.5">
                    {o.source === 'AI'
                      ? <span className="inline-flex items-center gap-1 text-xs text-brand-600 font-medium"><Bot className="w-3 h-3" /> ذكاء اصطناعي</span>
                      : <span className="text-xs text-slate-500">يدوي</span>}
                  </td>
                  <td className="px-5 py-3.5">
                    {o.paymentLink ? (
                      <a
                        href={`https://${o.paymentLink}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 font-medium"
                        dir="ltr"
                      >
                        <Link2 className="w-3 h-3" />
                        {o.paymentLink}
                      </a>
                    ) : (
                      <span className="text-xs text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-5 py-3.5 text-xs text-slate-400 whitespace-nowrap">{o.createdAt}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {filtered.length === 0 && (
            <div className="py-12 text-center text-sm text-slate-400">لا توجد طلبات.</div>
          )}
        </div>

        {/* Pagination */}
        <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
          <p className="text-xs text-slate-400">عرض {filtered.length} من {orders.length} طلب</p>
          <div className="flex items-center gap-1">
            <button className="btn-ghost text-xs py-1.5 px-2">السابق</button>
            <button className="px-3 py-1.5 rounded-lg bg-brand-500 text-white text-xs font-medium">1</button>
            <button className="btn-ghost text-xs py-1.5 px-2">2</button>
            <button className="btn-ghost text-xs py-1.5 px-2">التالي</button>
          </div>
        </div>
      </div>
    </div>
  )
}
