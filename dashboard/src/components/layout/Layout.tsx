import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

type MetaSelector = (tr: Translations) => { title: string; subtitle: string }

const PAGE_META: Record<string, MetaSelector> = {
  '/overview':           tr => tr.pages.overview,
  '/conversations':      tr => tr.pages.conversations,
  '/orders':             tr => tr.pages.orders,
  '/coupons':            tr => tr.pages.coupons,
  '/campaigns':          tr => tr.pages.campaigns,
  '/integrations':       tr => tr.pages.integrations,
  '/analytics':          tr => tr.pages.analytics,
  '/settings':           tr => tr.pages.settings,
  '/smart-automations':  _tr => ({ title: 'الطيار الآلي', subtitle: 'إدارة الأتمتة الذكية' }),
}

export default function Layout() {
  const { pathname } = useLocation()
  const { t } = useLanguage()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const metaSelector = PAGE_META[pathname] ?? (() => ({ title: 'نهلة', subtitle: '' }))
  const meta = t(metaSelector)

  return (
    <div className="min-h-screen flex bg-slate-50">
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/*
       * ms-0 on mobile (sidebar overlays as a drawer).
       * ms-60 on lg+ (sidebar is always visible and takes up 240 px).
       */}
      <div className="flex-1 ms-0 lg:ms-60 flex flex-col min-h-screen">
        <Header
          title={meta.title}
          subtitle={meta.subtitle}
          onMenuClick={() => setSidebarOpen(o => !o)}
        />
        <main className="flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
