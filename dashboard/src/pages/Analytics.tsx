import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend,
} from 'recharts'
import { DollarSign, TrendingUp, ShoppingCart, MessageSquare } from 'lucide-react'
import StatCard from '../components/ui/StatCard'

const revenueData = [
  { month: 'Oct', revenue: 42000 },
  { month: 'Nov', revenue: 58000 },
  { month: 'Dec', revenue: 75000 },
  { month: 'Jan', revenue: 63000 },
  { month: 'Feb', revenue: 81000 },
  { month: 'Mar', revenue: 94000 },
]

const conversionData = [
  { day: 'Mon', conversations: 98,  conversions: 28 },
  { day: 'Tue', conversations: 124, conversions: 37 },
  { day: 'Wed', conversations: 87,  conversions: 22 },
  { day: 'Thu', conversations: 143, conversions: 51 },
  { day: 'Fri', conversations: 132, conversions: 44 },
  { day: 'Sat', conversations: 188, conversions: 72 },
  { day: 'Sun', conversations: 164, conversions: 63 },
]

const topProducts = [
  { name: 'Red Classic Hoodie',       revenue: 18400, orders: 92,  trend: '+14%' },
  { name: 'Blue Slim-Fit Shirt',      revenue: 12600, orders: 140, trend: '+8%'  },
  { name: 'Leather Belt Premium',     revenue: 8900,  orders: 60,  trend: '+22%' },
  { name: 'Running Shorts Pro',       revenue: 7200,  orders: 96,  trend: '-3%'  },
  { name: 'Smart Watch Band Black',   revenue: 6600,  orders: 30,  trend: '+31%' },
]

const sourceData = [
  { name: 'AI Conversations', value: 68, color: '#f59e0b' },
  { name: 'Campaigns',        value: 19, color: '#3b82f6' },
  { name: 'Direct / Manual',  value: 13, color: '#94a3b8' },
]

export default function Analytics() {
  return (
    <div className="space-y-5">
      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Revenue (MTD)"       value="SAR 94,320" change={15.8} icon={DollarSign}    iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="Conversion Rate"     value="31.4%"      change={4.2}  icon={TrendingUp}    iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="Orders (MTD)"        value="1,248"      change={9.6}  icon={ShoppingCart}  iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label="Conversations (MTD)" value="4,890"      change={22.1} icon={MessageSquare} iconColor="text-purple-600"  iconBg="bg-purple-50" />
      </div>

      {/* Revenue trend */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Revenue Trend</h2>
            <p className="text-xs text-slate-400 mt-0.5">Last 6 months</p>
          </div>
          <div className="text-right">
            <p className="text-sm font-bold text-emerald-600">+15.8%</p>
            <p className="text-xs text-slate-400">vs previous period</p>
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
              formatter={(v: number) => [`SAR ${v.toLocaleString()}`, 'Revenue']}
            />
            <Area type="monotone" dataKey="revenue" stroke="#10b981" strokeWidth={2.5} fill="url(#revGrad)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Conversion chart + pie */}
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">Conversations vs Conversions</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={conversionData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="day" tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8 }} />
              <Bar dataKey="conversations" name="Conversations" fill="#e2e8f0" radius={[4, 4, 0, 0]} />
              <Bar dataKey="conversions"   name="Conversions"   fill="#f59e0b" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-5">Order Source</h2>
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
          <h2 className="text-sm font-semibold text-slate-900">Top Products by Revenue</h2>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {['#', 'Product', 'Orders', 'Revenue', 'Trend'].map((h) => (
                  <th key={h} className="text-left px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide">
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
                  <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">SAR {p.revenue.toLocaleString()}</td>
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
