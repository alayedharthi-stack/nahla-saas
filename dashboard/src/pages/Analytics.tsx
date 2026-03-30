import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts'
import { DollarSign, TrendingUp, ShoppingCart, MessageSquare } from 'lucide-react'
import StatCard from '../components/ui/StatCard'

const revenueData = [
  { month: 'أكت', revenue: 42000 },
  { month: 'نوف', revenue: 58000 },
  { month: 'ديس', revenue: 75000 },
  { month: 'يناير', revenue: 63000 },
  { month: 'فبراير', revenue: 81000 },
  { month: 'مارس', revenue: 94000 },
]

const conversionData = [
  { day: 'إثن', conversations: 98,  conversions: 28 },
  { day: 'ثلا', conversations: 124, conversions: 37 },
  { day: 'أرب', conversations: 87,  conversions: 22 },
  { day: 'خمس', conversations: 143, conversions: 51 },
  { day: 'جمع', conversations: 132, conversions: 44 },
  { day: 'سبت', conversations: 188, conversions: 72 },
  { day: 'أحد', conversations: 164, conversions: 63 },
]

const topProducts = [
  { name: 'هودي Classic الأحمر',        revenue: 18400, orders: 92,  trend: '+14%' },
  { name: 'قميص Slim-Fit الأزرق',       revenue: 12600, orders: 140, trend: '+8%'  },
  { name: 'حزام جلد Premium',           revenue: 8900,  orders: 60,  trend: '+22%' },
  { name: 'شورت Running Pro',           revenue: 7200,  orders: 96,  trend: '-3%'  },
  { name: 'حزام ساعة ذكية أسود',       revenue: 6600,  orders: 30,  trend: '+31%' },
]

const sourceData = [
  { name: 'محادثات الذكاء الاصطناعي', value: 68, color: '#f59e0b' },
  { name: 'الحملات',                   value: 19, color: '#3b82f6' },
  { name: 'مباشر / يدوي',             value: 13, color: '#94a3b8' },
]

const TABLE_HEADERS = ['#', 'المنتج', 'الطلبات', 'الإيرادات', 'الاتجاه']

export default function Analytics() {
  return (
    <div className="space-y-5">
      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="الإيرادات (الشهر الحالي)"    value="94,320 ر.س" change={15.8} icon={DollarSign}    iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="معدل التحويل"                 value="31.4%"      change={4.2}  icon={TrendingUp}    iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="الطلبات (الشهر الحالي)"       value="1,248"      change={9.6}  icon={ShoppingCart}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label="المحادثات (الشهر الحالي)"     value="4,890"      change={22.1} icon={MessageSquare} iconColor="text-purple-600"  iconBg="bg-purple-50" />
      </div>

      {/* Revenue trend */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">اتجاه الإيرادات</h2>
            <p className="text-xs text-slate-400 mt-0.5">آخر 6 أشهر</p>
          </div>
          <div className="text-end">
            <p className="text-sm font-bold text-emerald-600">+15.8%</p>
            <p className="text-xs text-slate-400">مقارنة بالفترة السابقة</p>
          </div>
        </div>
        <ResponsiveContainer width="100%" height={240}>
          <AreaChart data={revenueData} margin={{ top: 4, right: 4, left: -10, bottom: 0 }}>
            <defs>
              <linearGradient id="revGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#10b981" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="month" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false}
                   tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`} />
            <Tooltip
              contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8 }}
              formatter={(v: number) => [`${v.toLocaleString('ar-SA')} ر.س`, 'الإيرادات']}
            />
            <Area type="monotone" dataKey="revenue" stroke="#10b981" strokeWidth={2.5} fill="url(#revGrad)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Conversion chart + pie */}
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">المحادثات مقابل التحويلات</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={conversionData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="day" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8 }} />
              <Bar dataKey="conversations" name="المحادثات"  fill="#e2e8f0" radius={[4, 4, 0, 0]} />
              <Bar dataKey="conversions"   name="التحويلات"  fill="#f59e0b" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">مصدر الطلبات</h2>
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie
                data={sourceData}
                cx="50%"
                cy="50%"
                innerRadius={55}
                outerRadius={85}
                paddingAngle={3}
                dataKey="value"
              >
                {sourceData.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Pie>
              <Legend
                formatter={(value) => <span className="text-xs text-slate-600">{value}</span>}
                iconType="circle"
                iconSize={8}
              />
              <Tooltip
                contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8 }}
                formatter={(v: number) => [`${v}%`, '']}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Top products */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">أفضل المنتجات حسب الإيرادات</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {TABLE_HEADERS.map((h) => (
                  <th key={h} className="text-start px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {topProducts.map((p, i) => (
                <tr key={p.name} className="hover:bg-slate-50 transition-colors">
                  <td className="px-5 py-3.5 text-xs font-medium text-slate-400">{i + 1}</td>
                  <td className="px-5 py-3.5 text-xs font-medium text-slate-900">{p.name}</td>
                  <td className="px-5 py-3.5 text-xs text-slate-600">{p.orders}</td>
                  <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">{p.revenue.toLocaleString('ar-SA')} ر.س</td>
                  <td className="px-5 py-3.5">
                    <span className={`text-xs font-medium ${p.trend.startsWith('+') ? 'text-emerald-600' : 'text-red-500'}`}>
                      {p.trend}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
