import { useState, useEffect, useCallback } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import TrialBanner from '../ui/TrialBanner'
import ImpersonationBanner from '../ui/ImpersonationBanner'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'
import { API_BASE } from '../../api/client'
import { Shield, X } from 'lucide-react'

// ── Active Support Access Warning Banner ─────────────────────────────────────
// Shows in ALL merchant pages when the admin has active access

function SupportAccessWarningBanner() {
  const navigate = useNavigate()
  const [access, setAccess] = useState<{
    enabled: boolean; expires_at: string | null; reason?: string
  } | null>(null)
  const [dismissed, setDismissed] = useState(false)

  const load = useCallback(async () => {
    try {
      const token = localStorage.getItem('nahla_token') ?? ''
      if (!token) return
      const res = await fetch(`${API_BASE}/merchant/support-access`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (res.ok) setAccess(await res.json())
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    load()
    // Poll every 30s to detect expiry
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [load])

  if (!access?.enabled || dismissed) return null

  const fmt = (iso: string | null) => {
    if (!iso) return ''
    try {
      return new Intl.DateTimeFormat('ar-SA', {
        timeStyle: 'short', dateStyle: 'short', timeZone: 'Asia/Riyadh',
      }).format(new Date(iso))
    } catch { return iso }
  }

  return (
    <div dir="rtl" className="w-full bg-red-600 text-white text-xs px-4 py-2.5 flex items-center justify-between gap-3 sticky top-0 z-40">
      <div className="flex items-center gap-2 min-w-0">
        <span className="relative flex h-2 w-2 shrink-0">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-60" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-white" />
        </span>
        <Shield className="w-3.5 h-3.5 shrink-0" />
        <span className="font-semibold">
          تنبيه: دعم نحلة لديه وصول مؤقت إلى لوحتك
          {access.expires_at && (
            <span className="font-normal opacity-90"> حتى {fmt(access.expires_at)}</span>
          )}
        </span>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={() => navigate('/settings?tab=security')}
          className="bg-white/20 hover:bg-white/30 px-2.5 py-1 rounded-lg font-semibold transition"
        >
          إدارة الوصول
        </button>
        <button onClick={() => setDismissed(true)} className="opacity-70 hover:opacity-100">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  )
}

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
        <SupportAccessWarningBanner />
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
