import { useState } from 'react'
import { Plus, Send, Users, ShoppingCart, BarChart2, Clock, CheckCircle, XCircle, Megaphone } from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

type CampaignStatus = 'active' | 'scheduled' | 'completed' | 'draft'

interface Campaign {
  id: string
  name: string
  type: 'broadcast' | 'abandoned_cart' | 'vip'
  status: CampaignStatus
  audience: number
  sent: number
  opened: number
  converted: number
  scheduledAt?: string
}

const campaigns: Campaign[] = [
  { id: '1', name: 'Ramadan Flash Sale',     type: 'broadcast',      status: 'active',    audience: 1240, sent: 1240, opened: 894,  converted: 128, scheduledAt: undefined },
  { id: '2', name: 'Abandoned Cart Recovery',type: 'abandoned_cart', status: 'active',    audience: 87,   sent: 87,   opened: 64,   converted: 22  },
  { id: '3', name: 'VIP Summer Collection',  type: 'vip',            status: 'scheduled', audience: 310,  sent: 0,    opened: 0,    converted: 0,  scheduledAt: 'Apr 5, 10:00 AM' },
  { id: '4', name: 'Weekend Promo Blast',    type: 'broadcast',      status: 'completed', audience: 2100, sent: 2100, opened: 1530, converted: 240 },
  { id: '5', name: 'New Arrivals Alert',     type: 'broadcast',      status: 'draft',     audience: 0,    sent: 0,    opened: 0,    converted: 0 },
]

const statusVariant = (s: CampaignStatus) =>
  s === 'active'    ? 'green'  :
  s === 'scheduled' ? 'amber'  :
  s === 'completed' ? 'blue'   : 'slate'

const typeIcon = (t: Campaign['type']) =>
  t === 'broadcast'      ? <Megaphone   className="w-3.5 h-3.5 text-blue-500" />  :
  t === 'abandoned_cart' ? <ShoppingCart className="w-3.5 h-3.5 text-amber-500" /> :
                           <Users        className="w-3.5 h-3.5 text-purple-500" />

const typeLabel = (t: Campaign['type']) =>
  t === 'broadcast'      ? 'Broadcast'      :
  t === 'abandoned_cart' ? 'Abandoned Cart'  : 'VIP'

export default function Campaigns() {
  const [showCompose, setShowCompose] = useState(false)
  const [message, setMessage] = useState('')
  const [audience, setAudience] = useState('all')
  const { t } = useLanguage()

  return (
    <div className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.campaigns.title)}
        subtitle={t(tr => tr.pages.campaigns.subtitle)}
        action={
          <button
            onClick={() => setShowCompose(!showCompose)}
            className="btn-primary text-sm"
          >
            <Plus className="w-4 h-4" /> {t(tr => tr.actions.newCampaign)}
          </button>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Active Campaigns"     value="2"     change={0}   icon={Megaphone}   iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="Messages Sent"        value="3,427" change={18}  icon={Send}         iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label="Open Rate"            value="63.4%" change={4.2} icon={BarChart2}    iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="Conversions"          value="390"   change={9.1} icon={CheckCircle}  iconColor="text-purple-600"  iconBg="bg-purple-50" />
      </div>

      {/* Abandoned Cart Recovery */}
      <div className="card p-5">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-slate-900 flex items-center gap-2">
              <ShoppingCart className="w-4 h-4 text-amber-500" />
              Abandoned Cart Recovery
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Automatically message customers who added items but didn't complete checkout
            </p>
          </div>
          <label className="relative inline-flex items-center cursor-pointer">
            <input type="checkbox" defaultChecked className="sr-only peer" />
            <div className="w-9 h-5 bg-slate-200 peer-focus:ring-2 peer-focus:ring-brand-300 rounded-full peer
                            peer-checked:bg-brand-500 after:content-[''] after:absolute after:top-0.5 after:left-0.5
                            after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all
                            peer-checked:after:translate-x-4" />
          </label>
        </div>

        <div className="grid sm:grid-cols-3 gap-4 mt-5">
          <div>
            <label className="label">Delay before sending</label>
            <select className="input text-sm">
              <option>30 minutes</option>
              <option>1 hour</option>
              <option>2 hours</option>
              <option>6 hours</option>
            </select>
          </div>
          <div>
            <label className="label">Include coupon</label>
            <select className="input text-sm">
              <option>CART10AUTO — 10% off</option>
              <option>No coupon</option>
              <option>WELCOME20 — 20% off</option>
            </select>
          </div>
          <div>
            <label className="label">Max recoveries/day</label>
            <input type="number" className="input text-sm" defaultValue={200} />
          </div>
        </div>
      </div>

      {/* Broadcast Compose */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">Campaigns</h2>
        </div>

        {showCompose && (
          <div className="px-5 py-4 border-b border-slate-100 bg-slate-50">
            <h3 className="text-sm font-medium text-slate-800 mb-3">Compose Broadcast Message</h3>
            <div className="grid sm:grid-cols-2 gap-4 mb-4">
              <div>
                <label className="label">Campaign Name</label>
                <input className="input text-sm" placeholder="e.g. Spring Sale Announcement" />
              </div>
              <div>
                <label className="label">Audience</label>
                <select className="input text-sm" value={audience} onChange={e => setAudience(e.target.value)}>
                  <option value="all">All Customers ({1850})</option>
                  <option value="vip">VIP Only (310)</option>
                  <option value="abandoned">Abandoned Cart (87)</option>
                  <option value="new">New Customers (240)</option>
                </select>
              </div>
            </div>
            <div className="mb-4">
              <label className="label">Message</label>
              <textarea
                rows={4}
                className="input text-sm"
                placeholder="Type your WhatsApp message here… Use {{name}} for personalization."
                value={message}
                onChange={(e) => setMessage(e.target.value)}
              />
              <p className="text-xs text-slate-400 mt-1">{message.length}/1024 characters</p>
            </div>
            <div className="flex items-center gap-3">
              <button className="btn-primary text-xs">
                <Send className="w-3.5 h-3.5" /> Send Now
              </button>
              <button className="btn-secondary text-xs">
                <Clock className="w-3.5 h-3.5" /> Schedule
              </button>
              <button onClick={() => setShowCompose(false)} className="btn-ghost text-xs text-slate-400">
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Campaigns table */}
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {['Campaign', 'Type', 'Status', 'Audience', 'Sent', 'Opened', 'Converted', ''].map((h) => (
                  <th key={h} className="text-left px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {campaigns.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-5 py-3.5">
                    <p className="text-xs font-semibold text-slate-900">{c.name}</p>
                    {c.scheduledAt && (
                      <p className="text-xs text-amber-600 flex items-center gap-1 mt-0.5">
                        <Clock className="w-3 h-3" /> {c.scheduledAt}
                      </p>
                    )}
                  </td>
                  <td className="px-5 py-3.5">
                    <span className="flex items-center gap-1.5 text-xs text-slate-600">
                      {typeIcon(c.type)} {typeLabel(c.type)}
                    </span>
                  </td>
                  <td className="px-5 py-3.5">
                    <Badge label={c.status} variant={statusVariant(c.status)} dot />
                  </td>
                  <td className="px-5 py-3.5 text-xs text-slate-700">{c.audience.toLocaleString()}</td>
                  <td className="px-5 py-3.5 text-xs text-slate-700">{c.sent.toLocaleString()}</td>
                  <td className="px-5 py-3.5">
                    <span className="text-xs text-slate-700">
                      {c.sent > 0 ? `${c.opened} (${Math.round((c.opened / c.sent) * 100)}%)` : '—'}
                    </span>
                  </td>
                  <td className="px-5 py-3.5">
                    <span className={`text-xs font-medium ${c.converted > 0 ? 'text-emerald-600' : 'text-slate-400'}`}>
                      {c.sent > 0 ? `${c.converted} (${Math.round((c.converted / c.sent) * 100)}%)` : '—'}
                    </span>
                  </td>
                  <td className="px-5 py-3.5">
                    {c.status === 'active' ? (
                      <button className="text-xs text-red-400 hover:text-red-600 transition-colors flex items-center gap-1">
                        <XCircle className="w-3.5 h-3.5" /> Stop
                      </button>
                    ) : c.status === 'draft' ? (
                      <button className="text-xs text-brand-500 hover:text-brand-700 transition-colors flex items-center gap-1">
                        <Send className="w-3.5 h-3.5" /> Launch
                      </button>
                    ) : null}
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
