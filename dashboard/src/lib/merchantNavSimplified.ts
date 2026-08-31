import type { Translations } from '../i18n/types'

/** Stable telemetry keys for the simplified merchant nav shell. */
export type SimplifiedNavGroupKey =
  | 'dest_overview'
  | 'dest_inbox'
  | 'dest_products'
  | 'dest_orders'
  | 'dest_customers'
  | 'dest_marketing'
  | 'dest_automation'
  | 'dest_templates'
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
  /** Sidebar link only — children/sections live inside hub pages, not the rail. */
  directLink: SimplifiedNavLink
  children?: SimplifiedNavLink[]
  sections?: SimplifiedNavSection[]
}

/** All merchant sidebar paths in the legacy 3-group layout (26 items). */
export const LEGACY_MERCHANT_NAV_PATHS: readonly string[] = [
  '/overview',
  '/conversations',
  '/catalog',
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
  '/settings-hub',
  '/settings/security',
] as const

/**
 * Daily-use IA — ten top-level destinations in the sidebar.
 * Nested routes remain in children/sections for hub pages + active-state matching.
 */
export const SIMPLIFIED_NAV_DESTINATIONS: SimplifiedNavDestination[] = [
  {
    destKey: 'dest_overview',
    destLabel: tr => tr.nav.destinations.overview,
    destIcon: 'layout-dashboard',
    directLink: {
      to: '/overview',
      icon: 'layout-dashboard',
      label: tr => tr.nav.destinations.overview,
    },
  },
  {
    destKey: 'dest_inbox',
    destLabel: tr => tr.nav.destinations.inbox,
    destIcon: 'message-square',
    directLink: {
      to: '/conversations',
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
      to: '/catalog',
      icon: 'package',
      label: tr => tr.nav.destinations.products,
    },
    children: [
      { to: '/catalog', icon: 'package', label: tr => tr.nav.items.whatsappCatalog },
    ],
  },
  {
    destKey: 'dest_orders',
    destLabel: tr => tr.nav.destinations.orders,
    destIcon: 'shopping-cart',
    directLink: {
      to: '/orders',
      icon: 'shopping-cart',
      label: tr => tr.nav.destinations.orders,
    },
    children: [
      { to: '/orders', icon: 'shopping-cart', label: tr => tr.nav.items.orders },
    ],
  },
  {
    destKey: 'dest_customers',
    destLabel: tr => tr.nav.destinations.customers,
    destIcon: 'users',
    directLink: {
      to: '/customers',
      icon: 'users',
      label: tr => tr.nav.destinations.customers,
    },
    children: [
      { to: '/customers', icon: 'users', label: tr => tr.nav.items.customers },
    ],
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
      { to: '/promotions', icon: 'gift', label: tr => tr.nav.items.promotions },
      { to: '/coupons', icon: 'tag', label: tr => tr.nav.items.coupons },
      { to: '/widgets', icon: 'trending-up', label: tr => tr.nav.items.widgets },
    ],
  },
  {
    destKey: 'dest_automation',
    destLabel: tr => tr.nav.destinations.automation,
    destIcon: 'bot',
    directLink: {
      to: '/automation',
      icon: 'bot',
      label: tr => tr.nav.destinations.automation,
    },
    children: [
      { to: '/smart-automations', icon: 'bot', label: tr => tr.nav.items.automations },
      { to: '/smart-automations', icon: 'bot', label: tr => tr.nav.items.autopilot },
    ],
  },
  {
    destKey: 'dest_templates',
    destLabel: tr => tr.nav.destinations.templates,
    destIcon: 'file-text',
    directLink: {
      to: '/templates-hub',
      icon: 'file-text',
      label: tr => tr.nav.destinations.templates,
    },
    children: [
      { to: '/templates', icon: 'file-text', label: tr => tr.nav.items.templates },
      { to: '/marketing/templates', icon: 'store', label: tr => tr.nav.items.ecommerceTemplates },
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
      { to: '/settings-hub', icon: 'settings', label: tr => tr.nav.items.settings },
      { to: '/settings/security', icon: 'shield-check', label: tr => tr.nav.items.security },
      { to: '/billing', icon: 'credit-card', label: tr => tr.nav.items.billing },
      { to: '/settings?tab=order_updates', icon: 'package', label: tr => tr.nav.items.orderUpdates },
    ],
    sections: [
      {
        sectionKey: 'nahla_smart',
        sectionLabel: tr => tr.nav.sections.nahlaSmart,
        navGroupKey: 'dest_settings',
        items: [
          { to: '/intelligence', icon: 'brain', label: tr => tr.nav.items.intelligence },
          { to: '/knowledge-base', icon: 'book-open', label: tr => tr.nav.items.knowledgeBase },
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
          { to: '/ai-sales-logs', icon: 'brain-circuit', label: tr => tr.nav.items.salesAgent },
          { to: '/analytics', icon: 'bar-chart-2', label: tr => tr.nav.items.analyticsAI },
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
  return [...new Set(paths)]
}

/** Paths belonging to one top-level destination (hub + children + sections). */
export function collectDestinationPaths(
  dest: SimplifiedNavDestination,
): string[] {
  const paths: string[] = []
  if (dest.directLink) paths.push(dest.directLink.to)
  dest.children?.forEach(item => paths.push(item.to.split('?')[0]))
  dest.sections?.forEach(section =>
    section.items.forEach(item => paths.push(item.to.split('?')[0])),
  )
  return [...new Set(paths)]
}

function destinationMatchLength(pathname: string, dest: SimplifiedNavDestination): number {
  let best = -1
  for (const path of collectDestinationPaths(dest)) {
    if (pathname === path) best = Math.max(best, path.length)
    else if (path !== '/' && pathname.startsWith(`${path}/`)) best = Math.max(best, path.length)
  }
  return best
}

/**
 * Whether the current pathname belongs to a simplified destination (parent active state).
 * Exact path match first; prefix match only for nested segments (e.g. /orders/123).
 * When two destinations could claim the same path via prefix (e.g. /marketing vs
 * /marketing/templates), the longer / more specific destination wins.
 */
export function isPathInSimplifiedDestination(
  pathname: string,
  dest: SimplifiedNavDestination,
  allDestinations: readonly SimplifiedNavDestination[] = SIMPLIFIED_NAV_DESTINATIONS,
): boolean {
  const ownLen = destinationMatchLength(pathname, dest)
  if (ownLen < 0) return false
  for (const other of allDestinations) {
    if (other.destKey === dest.destKey) continue
    if (destinationMatchLength(pathname, other) > ownLen) return false
  }
  return true
}

/** Sidebar shows exactly ten top-level destination links (directLink only). */
export const SIMPLIFIED_SIDEBAR_LINK_COUNT = 10
