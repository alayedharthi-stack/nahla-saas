import type { Translations } from '../i18n/types'

export type PageMetaSelector = (tr: Translations) => {
  title: string
  subtitle: string
}

const PAGE_META: Record<string, PageMetaSelector> = {
  '/overview':                   tr => tr.pages.overview,
  '/conversations':              tr => tr.pages.conversations,
  '/orders':                     tr => tr.pages.orders,
  '/customers':                  tr => tr.pages.customers,
  '/customers/import':           tr => tr.pages.customers,
  '/coupons':                    tr => tr.pages.coupons,
  '/promotions':                 tr => tr.pages.promotions,
  '/campaigns':                  tr => tr.pages.campaigns,
  '/campaigns/manual-coupon':    tr => tr.pages.campaigns,
  '/marketing':                  tr => tr.pages.marketingHub,
  '/marketing/templates':        tr => tr.pages.nahlaTemplateLibrary,
  '/inbox':                      tr => tr.pages.inboxHub,
  '/products':                   tr => tr.pages.productsHub,
  '/orders-hub':                 tr => tr.pages.ordersHub,
  '/automation':                 tr => tr.pages.automationHub,
  '/templates-hub':              tr => tr.pages.templatesHub,
  '/channels':                   tr => tr.pages.channelsHub,
  '/settings-hub':               tr => tr.pages.settingsHub,
  '/templates':                  tr => tr.pages.templates,
  '/templates/manual-coupon':    tr => tr.pages.campaigns,
  '/integrations':               tr => tr.pages.integrations,
  '/analytics':                  tr => tr.pages.analytics,
  '/settings':                   tr => tr.pages.settings,
  '/settings/security':          tr => ({
    title: tr.security.pageTitle,
    subtitle: tr.security.pageSubtitle,
  }),
  '/smart-automations':          tr => tr.pages.smartAutomations,
  '/automations':                tr => tr.pages.smartAutomations,
  '/intelligence':               tr => tr.pages.intelligence,
  '/billing':                    tr => tr.pages.billing,
  '/widgets':                    tr => tr.pages.widgets,
  '/system-status':              tr => tr.pages.systemStatus,
  '/store-integration':          tr => tr.pages.storeIntegration,
  '/whatsapp-connect':           tr => tr.pages.whatsappConnect,
  '/catalog':                    tr => ({ title: tr.nav.items.whatsappCatalog, subtitle: '' }),
  '/whatsapp-catalog':           tr => ({ title: tr.nav.items.whatsappCatalog, subtitle: '' }),
  '/catalog-intelligence':       tr => tr.pages.catalogIntelligence,
  '/wa-usage':                   tr => ({
    title: tr.overview.waUsage.title,
    subtitle: tr.overview.waUsage.periodUsageTitle,
  }),
  '/delivery-quality':           tr => ({ title: tr.nav.items.deliveryQuality, subtitle: '' }),
  '/help/whatsapp-manual-setup': tr => ({ title: tr.nav.items.manualSetup, subtitle: '' }),
  '/knowledge-base':             tr => tr.pages.knowledgeBase,
  '/sales-channels':             tr => tr.pages.salesChannels,
  '/operations-center':          tr => tr.pages.operationsCenter,
  '/ai-sales-logs':              tr => ({ title: tr.nav.items.salesAgent, subtitle: '' }),
  '/handoff-queue':              tr => ({ title: tr.nav.items.handoffQueue, subtitle: '' }),
  '/admin':                      tr => tr.adminPages.dashboard,
  '/admin/tenants':              tr => tr.adminPages.tenants,
  '/admin/merchants':            tr => tr.adminPages.merchants,
  '/admin/revenue':              tr => tr.adminPages.revenue,
  '/admin/ai-usage':             tr => tr.adminPages.aiUsage,
  '/admin/features':             tr => tr.adminPages.features,
  '/admin/troubleshooting':      tr => tr.adminPages.troubleshooting,
  '/admin/team':                 tr => tr.adminPages.team,
  '/admin/system':               tr => tr.adminPages.system,
  '/admin/coexistence':          tr => tr.adminPages.coexistence,
  '/admin/tools':                tr => tr.adminPages.tools,
  '/admin/ai-quality':           tr => tr.adminPages.aiQuality,
}

export function resolvePageMetaSelector(pathname: string): PageMetaSelector | undefined {
  if (PAGE_META[pathname]) return PAGE_META[pathname]
  if (pathname.startsWith('/orders/')) return tr => tr.pages.orders
  if (pathname.startsWith('/sales-channels')) return tr => tr.pages.salesChannels
  if (pathname.startsWith('/operations-center/branches/')) {
    return tr => tr.pages.operationsCenter
  }
  return undefined
}
