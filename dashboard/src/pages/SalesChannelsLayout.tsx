import { NavLink, Outlet } from 'react-router-dom'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'

export default function SalesChannelsLayout() {
  const { t } = useLanguage()
  const sc = t(tr => tr.pages.salesChannels)

  const tabClass = ({ isActive }: { isActive: boolean }) =>
    [
      'px-4 py-2 text-sm font-medium rounded-lg transition-colors',
      isActive
        ? 'bg-brand-50 text-brand-700 ring-1 ring-brand-200'
        : 'text-slate-600 hover:bg-slate-100',
    ].join(' ')

  return (
    <div className="max-w-5xl mx-auto space-y-6 pb-10">
      <PageHeader title={sc.title} subtitle={sc.subtitle} />

      <nav className="flex flex-wrap gap-2 border-b border-slate-200 pb-3">
        <NavLink to="/sales-channels" end className={tabClass}>
          {sc.tabs.sales}
        </NavLink>
        <NavLink to="/sales-channels/branches" className={tabClass}>
          {sc.tabs.branches}
        </NavLink>
        <NavLink to="/sales-channels/contacts" className={tabClass}>
          {sc.tabs.contacts}
        </NavLink>
        <NavLink to="/sales-channels/routing" className={tabClass}>
          {sc.tabs.routing}
        </NavLink>
      </nav>

      <Outlet />
    </div>
  )
}
