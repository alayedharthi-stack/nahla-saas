/**
 * Canonical dashboard navigation hierarchy.
 *
 * Top-level destinations use the primary sidebar / hub rail and do not show
 * an in-header Back control. Child and deep-child routes show Up navigation
 * to a stable in-app parent (never window.history.back as the only contract).
 *
 * Modals/sheets are not routes and must keep Close — they are omitted here.
 */
export type RouteKind = 'top_level' | 'child' | 'deep_child' | 'special'

export interface HierarchyMatch {
  kind: RouteKind
  pathname: string
  parentPath: string | null
  showBack: boolean
  showBreadcrumb: boolean
}

const TOP_LEVEL = new Set<string>([
  '/overview',
  '/conversations',
  '/catalog',
  '/products',
  '/catalog-intelligence',
  '/orders',
  '/customers',
  '/marketing',
  '/automation',
  '/templates-hub',
  '/channels',
  '/settings-hub',
  '/admin',
])

/** Exact child path → canonical parent. */
const EXACT_CHILD: Record<string, string> = {
  '/handoff-queue': '/conversations',
  '/products': '/catalog',
  '/whatsapp-catalog': '/catalog',
  '/catalog-intelligence': '/catalog',
  '/customers/import': '/customers',
  '/campaigns': '/marketing',
  '/promotions': '/marketing',
  '/coupons': '/marketing',
  '/widgets': '/marketing',
  '/campaigns/manual-coupon': '/campaigns',
  '/smart-automations': '/automation',
  '/templates': '/templates-hub',
  '/templates/manual-coupon': '/templates',
  '/marketing/templates': '/templates-hub',
  '/integrations': '/channels',
  '/store-integration': '/channels',
  '/whatsapp-connect': '/channels',
  '/help/whatsapp-manual-setup': '/channels',
  '/sales-channels': '/channels',
  '/sales-channels/branches': '/sales-channels',
  '/sales-channels/contacts': '/sales-channels',
  '/sales-channels/routing': '/sales-channels',
  '/operations-center': '/channels',
  '/settings': '/settings-hub',
  '/settings/security': '/settings-hub',
  '/billing': '/settings-hub',
  '/intelligence': '/settings-hub',
  '/knowledge-base': '/settings-hub',
  '/system-status': '/settings-hub',
  '/delivery-quality': '/settings-hub',
  '/ai-sales-logs': '/settings-hub',
  '/analytics': '/settings-hub',
  '/wa-usage': '/overview',
  '/admin/tenants': '/admin',
  '/admin/merchants': '/admin',
  '/admin/revenue': '/admin',
  '/admin/ai-usage': '/admin',
  '/admin/features': '/admin',
  '/admin/troubleshooting': '/admin',
  '/admin/coexistence': '/admin',
  '/admin/team': '/admin',
  '/admin/system': '/admin',
  '/admin/tools': '/admin',
  '/admin/webhook-health': '/admin',
  '/admin/ai-quality': '/admin',
  '/admin/tenant-integrity': '/admin',
  '/admin/catalog': '/admin',
  '/admin/salla-activations': '/admin',
  '/admin/salla/integrations/token-status': '/admin',
}

const DEEP_CHILD = new Set<string>([
  '/customers/import',
  '/campaigns/manual-coupon',
  '/templates/manual-coupon',
  '/sales-channels/branches',
  '/sales-channels/contacts',
  '/sales-channels/routing',
])

function normalizePath(pathname: string): string {
  if (!pathname) return '/'
  if (pathname.length > 1 && pathname.endsWith('/')) return pathname.slice(0, -1)
  return pathname
}

function prefixMatch(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`)
}

export function resolveRouteHierarchy(pathname: string): HierarchyMatch {
  const path = normalizePath(pathname)

  if (TOP_LEVEL.has(path)) {
    return {
      kind: 'top_level',
      pathname: path,
      parentPath: null,
      showBack: false,
      showBreadcrumb: false,
    }
  }

  if (prefixMatch(path, '/orders') && path !== '/orders') {
    return {
      kind: 'deep_child',
      pathname: path,
      parentPath: '/orders',
      showBack: true,
      showBreadcrumb: true,
    }
  }

  if (prefixMatch(path, '/sales-channels/branches') && path !== '/sales-channels/branches') {
    return {
      kind: 'deep_child',
      pathname: path,
      parentPath: '/sales-channels/branches',
      showBack: true,
      showBreadcrumb: true,
    }
  }

  if (prefixMatch(path, '/operations-center/branches')) {
    return {
      kind: 'deep_child',
      pathname: path,
      parentPath: '/operations-center',
      showBack: true,
      showBreadcrumb: true,
    }
  }

  if (prefixMatch(path, '/admin/salla/diagnose')) {
    return {
      kind: 'deep_child',
      pathname: path,
      parentPath: '/admin/salla/integrations/token-status',
      showBack: true,
      showBreadcrumb: true,
    }
  }

  const exactParent = EXACT_CHILD[path]
  if (exactParent) {
    const kind: RouteKind = DEEP_CHILD.has(path) ? 'deep_child' : 'child'
    return {
      kind,
      pathname: path,
      parentPath: exactParent,
      showBack: true,
      showBreadcrumb: true,
    }
  }

  if (prefixMatch(path, '/admin/') && path !== '/admin') {
    return {
      kind: 'child',
      pathname: path,
      parentPath: '/admin',
      showBack: true,
      showBreadcrumb: true,
    }
  }

  return {
    kind: 'special',
    pathname: path,
    parentPath: null,
    showBack: false,
    showBreadcrumb: false,
  }
}

export function listTopLevelPaths(): string[] {
  return [...TOP_LEVEL].sort()
}

export function listExactChildPaths(): string[] {
  return Object.keys(EXACT_CHILD).sort()
}

export function listDeepChildPaths(): string[] {
  return [...DEEP_CHILD].sort()
}
