import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

// Maps URL path → translation selector for page metadata
type MetaSelector = (tr: Translations) => { title: string; subtitle: string }

const PAGE_META: Record<string, MetaSelector> = {
  '/overview':      tr => tr.pages.overview,
  '/conversations': tr => tr.pages.conversations,
  '/orders':        tr => tr.pages.orders,
  '/coupons':       tr => tr.pages.coupons,
  '/campaigns':     tr => tr.pages.campaigns,
  '/integrations':  tr => tr.pages.integrations,
  '/analytics':     tr => tr.pages.analytics,
  '/settings':      tr => tr.pages.settings,
}

export default function Layout() {
  const { pathname } = useLocation()
  const { t } = useLanguage()

  const metaSelector = PAGE_META[pathname] ?? (() => ({ title: 'نهلة', subtitle: '' }))
  const meta = t(metaSelector)

  return (
    <div className="min-h-screen flex">
      <Sidebar />
      {/*
       * ms-60 = margin-inline-start: 240px
       * In LTR: pushes content right of the sidebar (left margin).
       * In RTL: pushes content left of the sidebar (right margin).
       */}
      <div className="flex-1 ms-60 flex flex-col min-h-screen">
        <Header title={meta.title} subtitle={meta.subtitle} />
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
