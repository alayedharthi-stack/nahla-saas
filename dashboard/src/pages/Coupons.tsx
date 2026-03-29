import { useState } from 'react'
import { Plus, Tag, Copy, Crown, Zap, Trash2, ToggleLeft, ToggleRight } from 'lucide-react'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

interface Coupon {
  id: string
  code: string
  type: 'percentage' | 'fixed'
  value: number
  usages: number
  limit: number
  expires: string
  category: 'standard' | 'vip' | 'auto'
  active: boolean
}

const coupons: Coupon[] = [
  { id: 'c1', code: 'WELCOME20',  type: 'percentage', value: 20, usages: 142, limit: 500, expires: '2026-06-30', category: 'standard', active: true },
  { id: 'c2', code: 'VIP50',      type: 'fixed',       value: 50, usages: 38,  limit: 100, expires: '2026-04-15', category: 'vip',      active: true },
  { id: 'c3', code: 'CART10AUTO', type: 'percentage', value: 10, usages: 89,  limit: 999, expires: '2026-12-31', category: 'auto',     active: true },
  { id: 'c4', code: 'RAMADAN30',  type: 'percentage', value: 30, usages: 310, limit: 300, expires: '2026-04-01', category: 'standard', active: false },
  { id: 'c5', code: 'BULK100',    type: 'fixed',       value: 100,usages: 12,  limit: 50,  expires: '2026-09-01', category: 'vip',      active: true },
]

const rules = [
  { id: 'r1', label: 'Auto-send coupon after abandoned cart (> 30 min)',    enabled: true },
  { id: 'r2', label: 'VIP coupon for customers with > 5 orders',            enabled: true },
  { id: 'r3', label: 'Birthday discount (10% off on customer birthday)',    enabled: false },
  { id: 'r4', label: 'Bundle discount — buy 3 get 10% off',                 enabled: true },
  { id: 'r5', label: 'First purchase discount — 15% off',                   enabled: false },
]

const categoryIcon = (cat: Coupon['category']) => {
  if (cat === 'vip')  return <Crown className="w-3.5 h-3.5 text-amber-500" />
  if (cat === 'auto') return <Zap    className="w-3.5 h-3.5 text-purple-500" />
  return                      <Tag    className="w-3.5 h-3.5 text-slate-400" />
}

const categoryBadge = (cat: Coupon['category']) =>
  cat === 'vip'  ? <Badge label="VIP"  variant="amber"  /> :
  cat === 'auto' ? <Badge label="Auto" variant="purple" /> :
                   <Badge label="Standard" variant="slate" />

export default function Coupons() {
  const [rulesState, setRulesState] = useState(rules)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const { t } = useLanguage()

  const toggleRule = (id: string) =>
    setRulesState(rs => rs.map(r => r.id === id ? { ...r, enabled: !r.enabled } : r))

  const copyCode = (code: string, id: string) => {
    navigator.clipboard.writeText(code)
    setCopiedId(id)
    setTimeout(() => setCopiedId(null), 1500)
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.coupons.title)}
        subtitle={t(tr => tr.pages.coupons.subtitle)}
        action={
          <button className="btn-primary text-sm">
            <Plus className="w-4 h-4" /> {t(tr => tr.actions.newCoupon)}
          </button>
        }
      />

      {/* Coupon Rules */}
      <div className="card">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Coupon Rules</h2>
            <p className="text-xs text-slate-400 mt-0.5">Automatic rules trigger coupon generation and sending</p>
          </div>
        </div>
        <ul className="divide-y divide-slate-100">
          {rulesState.map((rule) => (
            <li key={rule.id} className="flex items-center justify-between px-5 py-3.5">
              <p className="text-sm text-slate-700">{rule.label}</p>
              <button onClick={() => toggleRule(rule.id)} className="shrink-0 ml-4">
                {rule.enabled
                  ? <ToggleRight className="w-6 h-6 text-brand-500" />
                  : <ToggleLeft  className="w-6 h-6 text-slate-300" />}
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* VIP Tiers */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900 flex items-center gap-2">
            <Crown className="w-4 h-4 text-amber-500" /> VIP Discount Tiers
          </h2>
        </div>
        <div className="grid sm:grid-cols-3 divide-y sm:divide-y-0 sm:divide-x divide-slate-100">
          {[
            { tier: 'Silver', threshold: '3+ orders', discount: '10%', color: 'text-slate-500 bg-slate-50' },
            { tier: 'Gold',   threshold: '7+ orders', discount: '20%', color: 'text-amber-600 bg-amber-50' },
            { tier: 'Platinum', threshold: '15+ orders', discount: '30%', color: 'text-purple-600 bg-purple-50' },
          ].map(({ tier, threshold, discount, color }) => (
            <div key={tier} className={`flex flex-col items-center py-6 ${color.split(' ')[1]}`}>
              <span className={`text-xs font-bold uppercase tracking-widest ${color.split(' ')[0]}`}>{tier}</span>
              <p className={`text-3xl font-bold mt-2 ${color.split(' ')[0]}`}>{discount}</p>
              <p className="text-xs text-slate-500 mt-1">{threshold}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Active Coupons */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">Active Coupons</h2>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {['Code', 'Type', 'Discount', 'Usages', 'Expires', 'Category', 'Status', ''].map((h) => (
                  <th key={h} className="text-left px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {coupons.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-5 py-3.5">
                    <div className="flex items-center gap-2">
                      {categoryIcon(c.category)}
                      <span className="text-xs font-mono font-semibold text-slate-800">{c.code}</span>
                      <button
                        onClick={() => copyCode(c.code, c.id)}
                        className="text-slate-300 hover:text-slate-500 transition-colors"
                      >
                        <Copy className="w-3 h-3" />
                      </button>
                      {copiedId === c.id && <span className="text-xs text-emerald-600">Copied!</span>}
                    </div>
                  </td>
                  <td className="px-5 py-3.5 text-xs text-slate-600 capitalize">{c.type}</td>
                  <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">
                    {c.type === 'percentage' ? `${c.value}%` : `SAR ${c.value}`}
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex items-center gap-2">
                      <div className="flex-1 bg-slate-100 rounded-full h-1.5 w-20">
                        <div
                          className="bg-brand-500 h-1.5 rounded-full"
                          style={{ width: `${Math.min((c.usages / c.limit) * 100, 100)}%` }}
                        />
                      </div>
                      <span className="text-xs text-slate-500">{c.usages}/{c.limit}</span>
                    </div>
                  </td>
                  <td className="px-5 py-3.5 text-xs text-slate-500">{c.expires}</td>
                  <td className="px-5 py-3.5">{categoryBadge(c.category)}</td>
                  <td className="px-5 py-3.5">
                    <Badge label={c.active ? 'Active' : 'Inactive'} variant={c.active ? 'green' : 'slate'} dot />
                  </td>
                  <td className="px-5 py-3.5">
                    <button className="text-slate-300 hover:text-red-500 transition-colors">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
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
