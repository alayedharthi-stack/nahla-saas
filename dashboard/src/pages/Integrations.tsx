import { useState } from 'react'
import { CheckCircle, XCircle, ExternalLink, RefreshCw, AlertCircle, Plug, Smartphone } from 'lucide-react'
import Badge from '../components/ui/Badge'

interface Integration {
  id: string
  name: string
  description: string
  logo: string
  connected: boolean
  storeId?: string
  storeName?: string
  lastSync?: string
  syncStatus?: 'ok' | 'error' | 'syncing'
}

const integrations: Integration[] = [
  {
    id: 'salla',
    name: 'Salla',
    description: 'Connect your Salla store to sync products, orders and customers in real time.',
    logo: '🛒',
    connected: true,
    storeId: 'salla-89231',
    storeName: 'متجر أحمد للملابس',
    lastSync: '2 min ago',
    syncStatus: 'ok',
  },
  {
    id: 'zid',
    name: 'Zid',
    description: 'Connect your Zid store to enable WhatsApp commerce and AI assistance.',
    logo: '🏪',
    connected: false,
  },
  {
    id: 'whatsapp',
    name: 'WhatsApp Business API',
    description: 'Connect your WhatsApp Business number to receive and send messages.',
    logo: '💬',
    connected: true,
    storeId: '+966 50 123 4567',
    storeName: 'WhatsApp Business Cloud API',
    lastSync: '1 min ago',
    syncStatus: 'ok',
  },
]

const syncStatusIcon = (s?: Integration['syncStatus']) => {
  if (s === 'ok')      return <CheckCircle  className="w-4 h-4 text-emerald-500" />
  if (s === 'error')   return <XCircle      className="w-4 h-4 text-red-500" />
  if (s === 'syncing') return <RefreshCw    className="w-4 h-4 text-brand-500 animate-spin" />
  return null
}

function IntegrationCard({ integration }: { integration: Integration }) {
  const [syncing, setSyncing] = useState(false)

  const handleSync = () => {
    setSyncing(true)
    setTimeout(() => setSyncing(false), 2000)
  }

  return (
    <div className="card p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 bg-slate-50 border border-slate-200 rounded-xl flex items-center justify-center text-2xl shrink-0">
          {integration.logo}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-slate-900">{integration.name}</h3>
            {integration.connected
              ? <Badge label="Connected" variant="green" dot />
              : <Badge label="Not connected" variant="slate" />}
          </div>
          <p className="text-xs text-slate-500 mt-1">{integration.description}</p>

          {integration.connected && (
            <div className="mt-3 grid sm:grid-cols-2 gap-3">
              <div className="bg-slate-50 rounded-lg px-3 py-2.5">
                <p className="text-xs text-slate-400">Store / Account</p>
                <p className="text-xs font-medium text-slate-800 mt-0.5 truncate">{integration.storeName}</p>
              </div>
              <div className="bg-slate-50 rounded-lg px-3 py-2.5">
                <div className="flex items-center gap-1.5">
                  {syncing
                    ? <RefreshCw className="w-3.5 h-3.5 text-brand-500 animate-spin" />
                    : syncStatusIcon(integration.syncStatus)}
                  <p className="text-xs text-slate-400">Last sync</p>
                </div>
                <p className="text-xs font-medium text-slate-800 mt-0.5">{integration.lastSync}</p>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 mt-4 pt-4 border-t border-slate-100">
        {integration.connected ? (
          <>
            <button onClick={handleSync} className="btn-secondary text-xs py-1.5" disabled={syncing}>
              <RefreshCw className={`w-3.5 h-3.5 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? 'Syncing…' : 'Sync Now'}
            </button>
            <button className="btn-ghost text-xs py-1.5 text-red-500 hover:bg-red-50">
              Disconnect
            </button>
          </>
        ) : (
          <button className="btn-primary text-xs py-1.5">
            <Plug className="w-3.5 h-3.5" /> Connect {integration.name}
          </button>
        )}

        {integration.id !== 'whatsapp' && (
          <a
            href="#"
            className="btn-ghost text-xs py-1.5 ml-auto text-slate-400"
          >
            <ExternalLink className="w-3.5 h-3.5" /> View in {integration.name}
          </a>
        )}
      </div>
    </div>
  )
}

export default function Integrations() {
  return (
    <div className="space-y-5">
      {/* Summary bar */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card px-5 py-4 flex items-center gap-3">
          <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">Connected</p>
            <p className="text-sm font-bold text-slate-900">2 / 3</p>
          </div>
        </div>
        <div className="card px-5 py-4 flex items-center gap-3">
          <RefreshCw className="w-5 h-5 text-brand-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">Last full sync</p>
            <p className="text-sm font-bold text-slate-900">2 min ago</p>
          </div>
        </div>
        <div className="card px-5 py-4 flex items-center gap-3">
          <Smartphone className="w-5 h-5 text-blue-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">WhatsApp number</p>
            <p className="text-sm font-bold text-slate-900">+966 50 123 4567</p>
          </div>
        </div>
      </div>

      {/* Integration cards */}
      <div className="grid lg:grid-cols-2 gap-4">
        {integrations.map((i) => (
          <IntegrationCard key={i.id} integration={i} />
        ))}
      </div>

      {/* Webhook info */}
      <div className="card p-5">
        <div className="flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-brand-500 shrink-0 mt-0.5" />
          <div>
            <h3 className="text-sm font-semibold text-slate-900">Webhook Endpoints</h3>
            <p className="text-xs text-slate-500 mt-0.5 mb-3">
              Configure these webhook URLs in your Salla / Zid developer console to receive real-time events.
            </p>
            <div className="space-y-2">
              {[
                { label: 'Salla Webhooks',      url: 'https://api.nahla.co/integrations/salla/webhooks/{products|orders|customers}' },
                { label: 'Zid Webhooks',        url: 'https://api.nahla.co/integrations/zid/webhooks/{products|orders|customers}' },
                { label: 'WhatsApp Webhook',    url: 'https://api.nahla.co/whatsapp/webhook' },
              ].map(({ label, url }) => (
                <div key={label} className="flex items-center gap-3 bg-slate-50 rounded-lg px-3 py-2">
                  <span className="text-xs font-medium text-slate-500 w-36 shrink-0">{label}</span>
                  <code className="text-xs text-brand-700 font-mono truncate flex-1">{url}</code>
                  <button className="text-xs text-slate-400 hover:text-slate-600 shrink-0">Copy</button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
