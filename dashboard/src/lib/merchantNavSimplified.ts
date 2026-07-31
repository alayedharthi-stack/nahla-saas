import type { Translations } from '../i18n/types'

/** Stable telemetry keys for the simplified 8-destination shell (P3.1). */
export type SimplifiedNavGroupKey =
  | 'dest_overview'
  | 'dest_inbox'
  | 'dest_products'
  | 'dest_orders'
  | 'dest_customers'
  | 'dest_marketing'
  | 'dest_channels'
  | 'dest_settings'
  | 'dest_settings_advanced'

/** Icon keys resolved to Lucide components in Sidebar render only. */
export type MerchantNavIconKey =
  | 'layout-dashboard'
  | 'message-square'
  | 'shopping-cart'
  | 'bot'
  | 'tag'
  | 'gift'
  | 'megaphone'
  | 'file-text'
  | 'brain'
  | 'plug'
  | 'bar-chart-2'
  | 'settings'
  | 'brain-circuit'
  | 'folder-tree'
  | 'store'
  | 'user-check'
  | 'users'
  | 'activity'
  | 'credit-card'
  | 'message-circle'
  | 'help-circle'
  | 'book-open'
  | 'gauge'
  | 'package'
  | 'shield-check'
  | 'trending-up'

export interface SimplifiedNavLink {
  to: string
  icon: MerchantNavIconKey
  label: (tr: Translations) => string
  isAI?: boolean
}

export interface SimplifiedNavSection {
  sectionKey: 'nahla_smart' | 'advanced'
  sectionLabel: (tr: Translations) => string
  navGroupKey: SimplifiedNavGroupKey
  items: SimplifiedNavLink[]
  defaultCollapsed?: boolean
}

export interface SimplifiedNavDestination {
  destKey: SimplifiedNavGroupKey
  destLabel: (tr: Translations) => string
  destIcon: MerchantNavIconKey
  /** Single-route destinations render as one top-level link. */
  directLink?: SimplifiedNavLink
  children?: SimplifiedNavLink[]
  sections?: SimplifiedNavSection[]
}

/** All merchant sidebar paths in the legacy 3-group layout (27 items). */
export const LEGACY_MERCHANT_NAV_PATHS: readonly string[] = [
  '/overview',
  '/conversations',
  '/catalog',
  '/catalog-intelligence',
  '/orders',
  '/customers',
  '/smart-automations',
  '/promotions',
  '/coupons',
  '/campaigns',
  '/templates',
  '/intelligence',
  '/knowledge-base',
  '/sales-channels',
  '/analytics',
  '/ai-sales-logs',
  '/handoff-queue',
  '/integrations',
  '/store-integration',
  '/whatsapp-connect',
  '/help/whatsapp-manual-setup',
  '/widgets',
  '/system-status',
  '/delivery-quality',
  '/billing',
  '/settings',
  '/settings/security',
] as const

/** P3.1 — eight top-level destinations; every legacy path remains reachable. */
export const SIMPLIFIED_NAV_DESTINATIONS: SimplifiedNavDestination[] = [
  {
    destKey: 'dest_overview',
    destLabel: tr => tr.nav.destinations.overview,
    destIcon: 'layout-dashboard',
    directLink: {
      to: '/overview',
      icon: 'layout-dashboard',
      label: tr => tr.nav.items.overview,
    },
  },
  {
    destKey: 'dest_inbox',
    destLabel: tr => tr.nav.destinations.inbox,
    destIcon: 'message-square',
    directLink: {
      to: '/inbox',
      icon: 'message-square',
      label: tr => tr.nav.destinations.inbox,
    },
    children: [
      { to: '/conversations', icon: 'message-square', label: tr => tr.nav.items.conversations },
      { to: '/handoff-queue', icon: 'user-check', label: tr => tr.nav.items.handoffQueue },
    ],
  },
  {
    destKey: 'dest_products',
    destLabel: tr => tr.nav.destinations.products,
    destIcon: 'package',
    directLink: {
      to: '/products',
      icon: 'package',
      label: tr => tr.nav.destinations.products,
    },
    children: [
      { to: '/catalog', icon: 'package', label: tr => tr.nav.items.whatsappCatalog },
      { to: '/catalog-intelligence', icon: 'folder-tree', label: tr => tr.nav.items.catalogIntelligence },
    ],
  },
  {
    destKey: 'dest_orders',
    destLabel: tr => tr.nav.destinations.orders,
    destIcon: 'shopping-cart',
    directLink: {
      to: '/orders',
      icon: 'shopping-cart',
      label: tr => tr.nav.items.orders,
    },
  },
  {
    destKey: 'dest_customers',
    destLabel: tr => tr.nav.destinations.customers,
    destIcon: 'users',
    directLink: {
      to: '/customers',
      icon: 'users',
      label: tr => tr.nav.items.customers,
    },
  },
  {
    destKey: 'dest_marketing',
    destLabel: tr => tr.nav.destinations.marketing,
    destIcon: 'megaphone',
    directLink: {
      to: '/marketing',
      icon: 'megaphone',
      label: tr => tr.nav.destinations.marketing,
    },
    children: [
      { to: '/campaigns', icon: 'megaphone', label: tr => tr.nav.items.campaigns },
      { to: '/promotions', icon: 'gift', label: tr => tr.nav.items.promotions, isAI: true },
      { to: '/coupons', icon: 'tag', label: tr => tr.nav.items.coupons, isAI: true },
      { to: '/widgets', icon: 'trending-up', label: tr => tr.nav.items.widgets },
      { to: '/smart-automations', icon: 'bot', label: tr => tr.nav.items.autopilot, isAI: true },
      { to: '/marketing/templates', icon: 'book-open', label: tr => tr.nav.items.nahlaTemplateLibrary },
      { to: '/templates', icon: 'file-text', label: tr => tr.nav.items.templates },
    ],
  },
  {
    destKey: 'dest_channels',
    destLabel: tr => tr.nav.destinations.channels,
    destIcon: 'plug',
    directLink: {
      to: '/channels',
      icon: 'plug',
      label: tr => tr.nav.destinations.channels,
    },
    children: [
      { to: '/integrations', icon: 'plug', label: tr => tr.nav.items.integrations },
      { to: '/store-integration', icon: 'store', label: tr => tr.nav.items.storeIntegration },
      { to: '/whatsapp-connect', icon: 'message-circle', label: tr => tr.nav.items.whatsappConnect },
      { to: '/help/whatsapp-manual-setup', icon: 'help-circle', label: tr => tr.nav.items.manualSetup },
      { to: '/sales-channels', icon: 'store', label: tr => tr.nav.items.salesChannels },
    ],
  },
  {
    destKey: 'dest_settings',
    destLabel: tr => tr.nav.destinations.settings,
    destIcon: 'settings',
    directLink: {
      to: '/settings-hub',
      icon: 'settings',
      label: tr => tr.nav.destinations.settings,
    },
    children: [
      { to: '/settings', icon: 'settings', label: tr => tr.nav.items.settings },
      { to: '/settings/security', icon: 'shield-check', label: tr => tr.nav.items.security },
      { to: '/billing', icon: 'credit-card', label: tr => tr.nav.items.billing },
    ],
    sections: [
      {
        sectionKey: 'nahla_smart',
        sectionLabel: tr => tr.nav.sections.nahlaSmart,
        navGroupKey: 'dest_settings',
        items: [
          { to: '/intelligence', icon: 'brain', label: tr => tr.nav.items.intelligence, isAI: true },
          { to: '/knowledge-base', icon: 'book-open', label: tr => tr.nav.items.knowledgeBase, isAI: true },
        ],
      },
      {
        sectionKey: 'advanced',
        sectionLabel: tr => tr.nav.sections.advanced,
        navGroupKey: 'dest_settings_advanced',
        defaultCollapsed: true,
        items: [
          { to: '/system-status', icon: 'activity', label: tr => tr.nav.items.systemStatus },
          { to: '/delivery-quality', icon: 'gauge', label: tr => tr.nav.items.deliveryQuality },
          { to: '/ai-sales-logs', icon: 'brain-circuit', label: tr => tr.nav.items.salesAgent, isAI: true },
          { to: '/analytics', icon: 'bar-chart-2', label: tr => tr.nav.items.analyticsAI, isAI: true },
        ],
      },
    ],
  },
]

/** Flatten every route reachable from the simplified merchant nav tree. */
export function collectSimplifiedNavPaths(
  destinations: readonly SimplifiedNavDestination[] = SIMPLIFIED_NAV_DESTINATIONS,
): string[] {
  const paths: string[] = []
  for (const dest of destinations) {
    if (dest.directLink) paths.push(dest.directLink.to)
    dest.children?.forEach(item => paths.push(item.to))
    dest.sections?.forEach(section => section.items.forEach(item => paths.push(item.to)))
  }
  return paths
}

/** Paths belonging to one top-level destination (hub + children + sections). */
export function collectDestinationPaths(
  dest: SimplifiedNavDestination,
): string[] {
  const paths: string[] = []
  if (dest.directLink) paths.push(dest.directLink.to)
  dest.children?.forEach(item => paths.push(item.to))
  dest.sections?.forEach(section => section.items.forEach(item => paths.push(item.to)))
  return paths
}

/** Whether the current pathname belongs to a simplified destination (parent active state). */
export function isPathInSimplifiedDestination(
  pathname: string,
  dest: SimplifiedNavDestination,
): boolean {
  const paths = collectDestinationPaths(dest)
  return paths.some(
    path => pathname === path || pathname.startsWith(`${path}/`),
  )
}

/** Sidebar shows exactly eight top-level destination links (directLink only). */
export const SIMPLIFIED_SIDEBAR_LINK_COUNT = 8
