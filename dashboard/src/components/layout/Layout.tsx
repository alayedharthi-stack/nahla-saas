import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import TrialBanner from '../ui/TrialBanner'
import ImpersonationBanner from '../ui/ImpersonationBanner'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'

type MetaSelector = (tr: Translations) => { title: string; subtitle: string }

const PAGE_META: Record<string, MetaSelector> = {
  '/overview':                  tr => tr.pages.overview,
  '/conversations':             tr => tr.pages.conversations,
  '/orders':                    tr => tr.pages.orders,
  '/customers':                 tr => tr.pages.customers,
  '/coupons':                   tr => tr.pages.coupons,
  '/promotions':                tr => tr.pages.promotions,
  '/campaigns':                 tr => tr.pages.campaigns,
  '/templates':                 tr => tr.pages.templates,
  '/integrations':              tr => tr.pages.integrations,
  '/analytics':                 tr => tr.pages.analytics,
  '/settings':                  tr => tr.pages.settings,
  '/smart-automations':         tr => tr.pages.smartAutomations,
  '/billing':                   tr => tr.pages.billing,
  '/widgets':                   tr => tr.pages.widgets,
  '/system-status':             tr => tr.pages.systemStatus,
  '/store-integration':         tr => tr.pages.storeIntegration,
  '/whatsapp-connect':          tr => tr.pages.whatsappConnect,
  '/help/whatsapp-manual-setup': tr => ({ title: tr.nav.items.manualSetup, subtitle: '' }),
  '/ai-sales-logs':             tr => ({ title: tr.nav.items.salesAgent,   subtitle: '' }),
  '/handoff-queue':             tr => ({ title: tr.nav.items.handoffQueue, subtitle: '' }),
  '/admin':                     tr => tr.adminPages.dashboard,
  '/admin/tenants':             tr => tr.adminPages.tenants,
  '/admin/merchants':           tr => tr.adminPages.merchants,
  '/admin/revenue':             tr => tr.adminPages.revenue,
  '/admin/ai-usage':            tr => tr.adminPages.aiUsage,
  '/admin/features':            tr => tr.adminPages.features,
  '/admin/troubleshooting':     tr => tr.adminPages.troubleshooting,
  '/admin/team':                tr => tr.adminPages.team,
  '/admin/system':              tr => tr.adminPages.system,
  '/admin/coexistence':         tr => tr.adminPages.coexistence,
  '/admin/tools':               tr => tr.adminPages.tools,
}

export default function Layout() {
  const { pathname } = useLocation()
  const { t } = useLanguage()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const metaSelector = PAGE_META[pathname] ?? ((_tr: Translations) => ({ title: 'Nahla', subtitle: '' }))
  const meta = t(metaSelector)

  return (
    <div className="min-h-dvh flex bg-slate-50 overflow-x-hidden">
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/*
       * ms-0 on mobile (sidebar overlays as a drawer).
       * ms-60 on lg+ (sidebar is always visible and takes up 240 px).
       */}
      <div className="flex-1 ms-0 lg:ms-60 flex flex-col min-h-dvh overflow-x-hidden">
        <Header
          title={meta.title}
          subtitle={meta.subtitle}
          onMenuClick={() => setSidebarOpen(o => !o)}
        />
        <ImpersonationBanner />
        <TrialBanner />
        <main className="flex-1 p-3 md:p-6 overflow-x-auto">
          <Outlet />
        </main>
        {/* iOS home-bar safe area */}
        <div className="pb-safe-bottom" />
      </div>
    </div>
  )
}
