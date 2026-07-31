import {
  LayoutDashboard,
  MessageSquare,
  ShoppingCart,
  Bot,
  Tag,
  Gift,
  Megaphone,
  FileText,
  Brain,
  Plug,
  BarChart2,
  Settings,
  BrainCircuit,
  FolderTree,
  Store,
  UserCheck,
  Users,
  Activity,
  CreditCard,
  MessageCircle,
  HelpCircle,
  BookOpen,
  Gauge,
  Package,
  ShieldCheck,
  TrendingUp,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
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

export interface SimplifiedNavLink {
  to: string
  icon: LucideIcon
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
  destIcon: LucideIcon
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
    destIcon: LayoutDashboard,
    directLink: {
      to: '/overview',
      icon: LayoutDashboard,
      label: tr => tr.nav.items.overview,
    },
  },
  {
    destKey: 'dest_inbox',
    destLabel: tr => tr.nav.destinations.inbox,
    destIcon: MessageSquare,
    children: [
      { to: '/conversations', icon: MessageSquare, label: tr => tr.nav.items.conversations },
      { to: '/handoff-queue', icon: UserCheck, label: tr => tr.nav.items.handoffQueue },
    ],
  },
  {
    destKey: 'dest_products',
    destLabel: tr => tr.nav.destinations.products,
    destIcon: Package,
    children: [
      { to: '/catalog', icon: Package, label: tr => tr.nav.items.whatsappCatalog },
      { to: '/catalog-intelligence', icon: FolderTree, label: tr => tr.nav.items.catalogIntelligence },
    ],
  },
  {
    destKey: 'dest_orders',
    destLabel: tr => tr.nav.destinations.orders,
    destIcon: ShoppingCart,
    directLink: {
      to: '/orders',
      icon: ShoppingCart,
      label: tr => tr.nav.items.orders,
    },
  },
  {
    destKey: 'dest_customers',
    destLabel: tr => tr.nav.destinations.customers,
    destIcon: Users,
    directLink: {
      to: '/customers',
      icon: Users,
      label: tr => tr.nav.items.customers,
    },
  },
  {
    destKey: 'dest_marketing',
    destLabel: tr => tr.nav.destinations.marketing,
    destIcon: Megaphone,
    children: [
      { to: '/campaigns', icon: Megaphone, label: tr => tr.nav.items.campaigns },
      { to: '/promotions', icon: Gift, label: tr => tr.nav.items.promotions, isAI: true },
      { to: '/coupons', icon: Tag, label: tr => tr.nav.items.coupons, isAI: true },
      { to: '/widgets', icon: TrendingUp, label: tr => tr.nav.items.widgets },
      { to: '/smart-automations', icon: Bot, label: tr => tr.nav.items.autopilot, isAI: true },
      { to: '/templates', icon: FileText, label: tr => tr.nav.items.templates },
    ],
  },
  {
    destKey: 'dest_channels',
    destLabel: tr => tr.nav.destinations.channels,
    destIcon: Plug,
    children: [
      { to: '/integrations', icon: Plug, label: tr => tr.nav.items.integrations },
      { to: '/store-integration', icon: Store, label: tr => tr.nav.items.storeIntegration },
      { to: '/whatsapp-connect', icon: MessageCircle, label: tr => tr.nav.items.whatsappConnect },
      { to: '/help/whatsapp-manual-setup', icon: HelpCircle, label: tr => tr.nav.items.manualSetup },
      { to: '/sales-channels', icon: Store, label: tr => tr.nav.items.salesChannels },
    ],
  },
  {
    destKey: 'dest_settings',
    destLabel: tr => tr.nav.destinations.settings,
    destIcon: Settings,
    children: [
      { to: '/settings', icon: Settings, label: tr => tr.nav.items.settings },
      { to: '/settings/security', icon: ShieldCheck, label: tr => tr.nav.items.security },
      { to: '/billing', icon: CreditCard, label: tr => tr.nav.items.billing },
    ],
    sections: [
      {
        sectionKey: 'nahla_smart',
        sectionLabel: tr => tr.nav.sections.nahlaSmart,
        navGroupKey: 'dest_settings',
        items: [
          { to: '/intelligence', icon: Brain, label: tr => tr.nav.items.intelligence, isAI: true },
          { to: '/knowledge-base', icon: BookOpen, label: tr => tr.nav.items.knowledgeBase, isAI: true },
        ],
      },
      {
        sectionKey: 'advanced',
        sectionLabel: tr => tr.nav.sections.advanced,
        navGroupKey: 'dest_settings_advanced',
        defaultCollapsed: true,
        items: [
          { to: '/system-status', icon: Activity, label: tr => tr.nav.items.systemStatus },
          { to: '/delivery-quality', icon: Gauge, label: tr => tr.nav.items.deliveryQuality },
          { to: '/ai-sales-logs', icon: BrainCircuit, label: tr => tr.nav.items.salesAgent, isAI: true },
          { to: '/analytics', icon: BarChart2, label: tr => tr.nav.items.analyticsAI, isAI: true },
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
