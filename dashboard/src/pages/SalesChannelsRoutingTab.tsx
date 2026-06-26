import { Compass } from 'lucide-react'
import { useLanguage } from '../i18n/context'

export default function SalesChannelsRoutingTab() {
  const { t } = useLanguage()
  const sc = t(tr => tr.pages.salesChannels)

  return (
    <div className="card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <Compass className="w-5 h-5 text-brand-500" />
        <h2 className="text-sm font-semibold text-slate-900">{sc.tabs.routing}</h2>
      </div>
      <ul className="text-sm text-slate-600 space-y-2 list-disc list-inside">
        {sc.routingTab.rules.map((rule: string) => (
          <li key={rule}>{rule}</li>
        ))}
      </ul>
      <p className="text-xs text-slate-500">{sc.routingTab.note}</p>
    </div>
  )
}
