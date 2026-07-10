import { Bot, Megaphone, MessageCircle, ShoppingBag } from 'lucide-react'
import type { CatalogDiagnostics } from '../../api/catalog'
import { useLanguage } from '../../i18n/context'

type ChannelStatus = 'ready' | 'needs_action' | 'not_connected' | 'coming_soon'

const STATUS_STYLE: Record<ChannelStatus, string> = {
  ready:          'bg-emerald-50 border-emerald-200 text-emerald-700',
  needs_action:   'bg-amber-50 border-amber-200 text-amber-700',
  not_connected:  'bg-slate-50 border-slate-200 text-slate-600',
  coming_soon:    'bg-slate-50 border-slate-200 text-slate-500',
}

function ChannelRow(props: {
  icon: React.ReactNode
  label: string
  status: ChannelStatus
  statusLabel: string
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-100 bg-slate-50/80 px-3 py-2.5">
      <div className="flex items-center gap-2 min-w-0">
        <span className="shrink-0">{props.icon}</span>
        <span className="text-sm font-semibold text-slate-800 truncate">{props.label}</span>
      </div>
      <span className={`shrink-0 text-[11px] font-bold px-2.5 py-1 rounded-full border ${STATUS_STYLE[props.status]}`}>
        {props.statusLabel}
      </span>
    </div>
  )
}

export default function CatalogChannelsCard(props: { diagnostics: CatalogDiagnostics }) {
  const { tStatic } = useLanguage()
  const ch = tStatic(tr => tr.catalogMgmt.channels)
  const d = props.diagnostics

  const statusLabel = (s: ChannelStatus) => {
    switch (s) {
      case 'ready': return ch.statusReady
      case 'needs_action': return ch.statusNeedsAction
      case 'not_connected': return ch.statusNotConnected
      case 'coming_soon': return ch.statusComingSoon
    }
  }

  const whatsappStatus: ChannelStatus =
    d.readiness.catalog_ready ? 'ready'
    : d.catalog.whatsapp_connected ? 'needs_action'
    : 'not_connected'

  const aiStatus: ChannelStatus = d.products.total > 0 ? 'ready' : 'needs_action'
  const campaignsStatus: ChannelStatus = d.products.total > 0 ? 'ready' : 'needs_action'

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-3">
      <h3 className="text-base font-bold text-slate-800">{ch.title}</h3>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <ChannelRow
          icon={<MessageCircle className="w-4 h-4 text-emerald-600" />}
          label={ch.whatsapp}
          status={whatsappStatus}
          statusLabel={statusLabel(whatsappStatus)}
        />
        <ChannelRow
          icon={<Bot className="w-4 h-4 text-violet-600" />}
          label={ch.ai}
          status={aiStatus}
          statusLabel={statusLabel(aiStatus)}
        />
        <ChannelRow
          icon={<Megaphone className="w-4 h-4 text-rose-600" />}
          label={ch.campaigns}
          status={campaignsStatus}
          statusLabel={statusLabel(campaignsStatus)}
        />
        <ChannelRow
          icon={<ShoppingBag className="w-4 h-4 text-amber-500" />}
          label={ch.google}
          status="coming_soon"
          statusLabel={statusLabel('coming_soon')}
        />
      </div>
    </div>
  )
}
