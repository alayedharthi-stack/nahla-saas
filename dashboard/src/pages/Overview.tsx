import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import {
  DollarSign, MessageSquare, ShoppingCart, TrendingUp, Bot, User, ExternalLink,
  Sparkles, Clock,
} from 'lucide-react'
import StatCard from '../components/ui/StatCard'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

const revenueData = [
  { day: 'الاثنين', revenue: 4200 },
  { day: 'الثلاثاء', revenue: 5800 },
  { day: 'الأربعاء', revenue: 3900 },
  { day: 'الخميس', revenue: 7200 },
  { day: 'الجمعة', revenue: 6100 },
  { day: 'السبت', revenue: 9400 },
  { day: 'الأحد', revenue: 8700 },
]

const recentConversations = [
  { id: 'c1', customer: 'Ahmed Al-Rashid',  phone: '+966 50 123 4567', lastMsg: 'هل الهودي الأحمر متوفر بمقاس XL؟', time: 'منذ دقيقتين',   isAI: true,  status: 'active' },
  { id: 'c2', customer: 'Sara Al-Zahrani',  phone: '+966 55 987 6543', lastMsg: 'متى يصل طلبي؟',                        time: 'منذ 11 دقيقة', isAI: false, status: 'human'  },
  { id: 'c3', customer: 'Mohammed Khalid',  phone: '+966 56 222 3344', lastMsg: 'هل عندكم خصم على الكميات؟',            time: 'منذ 34 دقيقة', isAI: true,  status: 'active' },
  { id: 'c4', customer: 'Fatima Al-Hassan', phone: '+966 54 551 2200', lastMsg: 'شكراً، قدّمت الطلب ✅',               time: 'منذ ساعة',     isAI: true,  status: 'closed' },
]

const recentOrders = [
  { id: '#3812', customer: 'Ahmed Al-Rashid', amount: '342 ر.س', status: 'paid',    source: 'AI',     time: 'منذ 5 دقائق' },
  { id: '#3811', customer: 'Nora Al-Mutairi', amount: '180 ر.س', status: 'pending', source: 'AI',     time: 'منذ 22 دقيقة' },
  { id: '#3810', customer: 'Khalid Ibrahim',  amount: '510 ر.س', status: 'paid',    source: 'manual', time: 'منذ ساعة' },
  { id: '#3809', customer: 'Lina Al-Saud',    amount: '95 ر.س',  status: 'failed',  source: 'AI',     time: 'منذ ساعتين' },
]

const statusVariant = (s: string) =>
  s === 'paid'    ? 'green'  :
  s === 'pending' ? 'amber'  :
  s === 'failed'  ? 'red'    : 'slate'

const statusLabel = (s: string) =>
  s === 'paid'    ? 'مدفوع'         :
  s === 'pending' ? 'قيد الانتظار'  :
  s === 'failed'  ? 'فشل'           : 'ملغي'

export default function Overview() {
  const { t } = useLanguage()

  return (
    <div className="space-y-6">
      <PageHeader
        title={t(tr => tr.pages.overview.title)}
        subtitle={t(tr => tr.pages.overview.subtitle)}
      />

      {/* Nahla Impact Banner — "موظف مبيعات يعمل 24/7" */}
      <div className="rounded-2xl overflow-hidden bg-gradient-to-l from-brand-600 to-amber-500 p-px">
        <div className="bg-gradient-to-l from-brand-600/10 to-amber-500/10 rounded-2xl px-5 py-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-brand-500/20 flex items-center justify-center shrink-0">
              <Sparkles className="w-5 h-5 text-brand-600" />
            </div>
            <div>
              <p className="text-xs text-slate-900 font-semibold">مبيعات ولّدتها نهلة هذا الشهر</p>
              <p className="text-2xl font-black text-red-600 leading-none mt-0.5">
                4,320 <span className="text-sm font-semibold text-red-400">ر.س</span>
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-center hidden sm:block">
              <p className="text-xs text-slate-400">طلبات أنجزها الذكاء</p>
              <p className="text-lg font-bold text-slate-700">28 طلب</p>
            </div>
            <div className="h-8 w-px bg-slate-200 hidden sm:block" />
            <div className="flex items-center gap-1.5 text-xs text-slate-500 bg-white rounded-xl px-3 py-2 border border-slate-200">
              <Clock className="w-3.5 h-3.5 text-brand-500" />
              <span>موظف مبيعات يعمل <strong className="text-slate-700">24/7</strong></span>
            </div>
          </div>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="المبيعات اليوم"
          value="8,740 ر.س"
          change={12.4}
          icon={DollarSign}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label="المحادثات"
          value="124"
          change={7.1}
          icon={MessageSquare}
          iconColor="text-blue-600"
          iconBg="bg-blue-50"
        />
        <StatCard
          label="طلبات اليوم"
          value="37"
          change={-3.2}
          icon={ShoppingCart}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label="معدل التحويل"
          value="29.8%"
          change={4.5}
          icon={TrendingUp}
          iconColor="text-purple-600"
          iconBg="bg-purple-50"
        />
      </div>

      {/* Revenue Chart */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">الإيرادات — آخر 7 أيام</h2>
            <p className="text-xs text-slate-400 mt-0.5">المجموع: 45,300 ر.س</p>
          </div>
          <select className="text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white text-slate-600 focus:outline-none">
            <option>آخر 7 أيام</option>
            <option>آخر 30 يوم</option>
            <option>هذا الشهر</option>
          </select>
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={revenueData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="colorRevenue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#f59e0b" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="day" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <Tooltip
              contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8, boxShadow: '0 4px 6px -1px rgb(0 0 0 / .1)' }}
              formatter={(v: number) => [`${v.toLocaleString('ar-SA')} ر.س`, 'الإيرادات']}
            />
            <Area type="monotone" dataKey="revenue" stroke="#f59e0b" strokeWidth={2} fill="url(#colorRevenue)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Two-column: Conversations + Orders */}
      <div className="grid lg:grid-cols-2 gap-4">
        {/* Recent Conversations */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">أحدث المحادثات</h2>
            <a href="/conversations" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          <ul className="divide-y divide-slate-100">
            {recentConversations.map((c) => (
              <li key={c.id} className="flex items-start gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                <div className="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center shrink-0 mt-0.5">
                  <span className="text-slate-600 text-xs font-semibold">
                    {c.customer.split(' ').map(n => n[0]).join('').slice(0, 2)}
                  </span>
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <p className="text-xs font-medium text-slate-900 truncate">{c.customer}</p>
                    {c.isAI
                      ? <Bot  className="w-3 h-3 text-brand-500 shrink-0" />
                      : <User className="w-3 h-3 text-slate-400 shrink-0" />}
                  </div>
                  <p className="text-xs text-slate-500 truncate mt-0.5">{c.lastMsg}</p>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <span className="text-xs text-slate-400">{c.time}</span>
                  <Badge
                    label={c.status === 'active' ? 'نشطة' : c.status === 'human' ? 'بشري' : 'مغلقة'}
                    variant={c.status === 'active' ? 'green' : c.status === 'human' ? 'amber' : 'slate'}
                    dot
                  />
                </div>
              </li>
            ))}
          </ul>
        </div>

        {/* Recent Orders */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">أحدث الطلبات</h2>
            <a href="/orders" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          <ul className="divide-y divide-slate-100">
            {recentOrders.map((o) => (
              <li key={o.id} className="flex items-center gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono font-medium text-slate-700">{o.id}</span>
                    <Badge label={o.source === 'AI' ? 'ذكاء اصطناعي' : 'يدوي'} variant={o.source === 'AI' ? 'purple' : 'slate'} />
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{o.customer}</p>
                </div>
                <div className="text-end shrink-0">
                  <p className="text-xs font-semibold text-slate-900">{o.amount}</p>
                  <div className="mt-0.5">
                    <Badge label={statusLabel(o.status)} variant={statusVariant(o.status) as 'green' | 'amber' | 'red' | 'slate'} />
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
