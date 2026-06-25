import { useState, useEffect, useCallback } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import Sidebar from './Sidebar'
import Header from './Header'
import TrialBanner from '../ui/TrialBanner'
import ImpersonationBanner from '../ui/ImpersonationBanner'
import { MobileChatFullscreenProvider, useMobileChatFullscreen } from '../../context/MobileChatFullscreenContext'
import { useLanguage } from '../../i18n/context'
import type { Translations } from '../../i18n/types'
import { getApiBase } from '../../auth'
import { useDashboardPoll } from '../../lib/dashboardPolling'
import { X } from 'lucide-react'

// ── Countdown hook ────────────────────────────────────────────────────────────
function useCountdown(expiresAt: string | null) {
  const [remaining, setRemaining] = useState<string>('')

  useEffect(() => {
    if (!expiresAt) { setRemaining(''); return }

    const update = () => {
      const now  = Date.now()
      const end  = new Date(expiresAt).getTime()
      const diff = end - now
      if (diff <= 0) { setRemaining('انتهى'); return }

      const h = Math.floor(diff / 3_600_000)
      const m = Math.floor((diff % 3_600_000) / 60_000)
      const s = Math.floor((diff % 60_000) / 1_000)

      if (h > 0) setRemaining(`${h}س ${m}د`)
      else if (m > 0) setRemaining(`${m} دقيقة ${s} ثانية`)
      else setRemaining(`${s} ثانية`)
    }
    update()
    const id = setInterval(update, 1_000)
    return () => clearInterval(id)
  }, [expiresAt])

  return remaining
}

// ── Active Support Access Warning Banner ─────────────────────────────────────
// Shows in ALL merchant pages when there is an active approved support access

function SupportAccessWarningBanner() {
  const navigate = useNavigate()
  const [access, setAccess] = useState<{
    enabled: boolean; expires_at: string | null; reason?: string
  } | null>(null)
  const [dismissed, setDismissed] = useState(false)
  const countdown = useCountdown(access?.enabled ? (access?.expires_at ?? null) : null)

  const loadBanner = useCallback(async (signal: AbortSignal) => {
    try {
      const token = localStorage.getItem('nahla_token') ?? ''
      if (!token) return
      let effectiveSignal: AbortSignal = signal
      if (typeof AbortSignal.timeout === 'function' && typeof AbortSignal.any === 'function') {
        effectiveSignal = AbortSignal.any([signal, AbortSignal.timeout(25_000)])
      }

      const base = getApiBase()
      const url = `${base}/merchant/support-access`
      // eslint-disable-next-line no-console
      console.info('[auth] tenant bootstrap (support-access)', { url })
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
        signal: effectiveSignal,
      })
      if (res.ok) {
        const data = await res.json()
        setAccess(data)
        // Auto-dismiss if expired
        if (!data.enabled) setDismissed(false)
      }
    } catch { /* ignore */ }
  }, [])

  const bootstrap = useCallback(async () => {
    const ac = new AbortController()
    const tid = window.setTimeout(() => ac.abort(), 25_000)
    try {
      await loadBanner(ac.signal)
    } finally {
      clearTimeout(tid)
    }
  }, [loadBanner])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  useDashboardPoll({
    pollKey: 'GET:/merchant/support-access',
    intervalMs: 45_000,
    leading: false,
    run: signal => loadBanner(signal),
  })

  // Re-show if a new access grant appears after dismissal
  useEffect(() => {
    if (access?.enabled) setDismissed(false)
  }, [access?.enabled])

  if (!access?.enabled || dismissed) return null

  return (
    <div dir="rtl" className="w-full bg-red-700 text-white shadow-lg">
      {/* Main row */}
      <div className="flex items-center justify-between px-4 py-2.5 gap-3">
        {/* Left: message + countdown */}
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <div className="flex items-center gap-1.5 shrink-0">
            <span className="relative flex h-2.5 w-2.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-white opacity-60" />
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-white" />
            </span>
            <span className="text-base">🛡️</span>
          </div>
          <div className="min-w-0">
            <p className="text-xs font-bold leading-snug">
              فريق نحلة يساعدك الآن في حل المشكلة
            </p>
            <p className="text-xs opacity-85 leading-snug">
              الوصول سينتهي خلال:{' '}
              <span className="font-bold">
                {countdown || (access.expires_at ? new Intl.DateTimeFormat('ar-SA', {
                  timeStyle: 'short', timeZone: 'Asia/Riyadh',
                }).format(new Date(access.expires_at)) : '—')}
              </span>
              {' '}· يمكنك إيقافه في أي وقت
            </p>
          </div>
        </div>

        {/* Right: actions */}
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => navigate('/settings?tab=support')}
            className="flex items-center gap-1.5 bg-white text-red-700 font-bold px-3 py-1.5 rounded-lg text-xs hover:bg-red-50 transition"
          >
            إدارة الوصول
          </button>
          <button
            onClick={() => setDismissed(true)}
            className="opacity-70 hover:opacity-100 p-0.5"
            title="إخفاء التنبيه (سيعود عند تحديث الصفحة)"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
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
  '/knowledge-base':            tr => tr.pages.knowledgeBase,
  '/sales-channels':            tr => tr.pages.salesChannels,
  '/operations-center':         tr => tr.pages.operationsCenter,
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
  '/admin/ai-quality':          tr => tr.adminPages.aiQuality,
}

export default function Layout() {
  return (
    <MobileChatFullscreenProvider>
      <LayoutShell />
    </MobileChatFullscreenProvider>
  )
}

function LayoutShell() {
  const { pathname } = useLocation()
  const { t, dir } = useLanguage()
  const { active: mobileChatFullscreen } = useMobileChatFullscreen()
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const metaSelector =
    PAGE_META[pathname]
    ?? (pathname.startsWith('/operations-center/branches/')
      ? (tr: Translations) => tr.pages.operationsCenter
      : (_tr: Translations) => ({ title: 'Nahlah AI', subtitle: '' }))
  const meta = t(metaSelector)

  return (
    <div className="min-h-dvh flex bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100 overflow-x-hidden transition-colors">
      <Sidebar isOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/*
       * ms-0 on mobile (sidebar overlays as a drawer).
       * ms-60 on lg+ (sidebar is always visible and takes up 240 px).
       */}
      <div className="flex-1 ms-0 lg:ms-60 flex flex-col min-h-0 h-dvh max-h-dvh overflow-hidden">
        {!mobileChatFullscreen && (
          <div className="sticky top-0 z-30 shrink-0 bg-white dark:bg-slate-900">
            <Header
              title={meta.title}
              subtitle={meta.subtitle}
              onMenuClick={() => setSidebarOpen(o => !o)}
            />
            <ImpersonationBanner />
            <SupportAccessWarningBanner />
            <TrialBanner />
          </div>
        )}
        <main
          dir={dir}
          className={`flex-1 min-h-0 overflow-x-hidden dashboard-main-scroll ${
            mobileChatFullscreen ? 'p-0 overflow-hidden' : 'p-3 md:p-6 overflow-y-auto'
          }`}
        >
          <Outlet />
        </main>
        {!mobileChatFullscreen && <div className="pb-safe-bottom" />}
      </div>
    </div>
  )
}
