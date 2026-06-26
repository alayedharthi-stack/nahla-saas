import { Link } from 'react-router-dom'
import { UserCheck } from 'lucide-react'
import { useLanguage } from '../i18n/context'

export default function SalesChannelsContactsTab() {
  const { t } = useLanguage()
  const sc = t(tr => tr.pages.salesChannels)

  return (
    <div className="card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <UserCheck className="w-5 h-5 text-brand-500" />
        <h2 className="text-sm font-semibold text-slate-900">{sc.tabs.contacts}</h2>
      </div>
      <p className="text-sm text-slate-600 leading-relaxed">{sc.contactsTab.description}</p>
      <Link to="/sales-channels/branches" className="text-sm text-brand-600 hover:text-brand-700 font-medium">
        {sc.contactsTab.openBranches}
      </Link>
    </div>
  )
}
