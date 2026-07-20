/**
 * Focused regression checks for merchant dashboard header metadata.
 *
 * Run: npm run check:page-metadata (from dashboard/)
 */
import { readFileSync } from 'node:fs'
import { resolvePageMetaSelector } from '../src/lib/pageMetadata.ts'

const MERCHANT_PATHS = [
  '/overview',
  '/conversations',
  '/orders',
  '/orders/123',
  '/customers',
  '/customers/import',
  '/catalog',
  '/whatsapp-catalog',
  '/catalog-intelligence',
  '/coupons',
  '/promotions',
  '/campaigns',
  '/campaigns/manual-coupon',
  '/templates',
  '/templates/manual-coupon',
  '/smart-automations',
  '/automations',
  '/intelligence',
  '/knowledge-base',
  '/sales-channels',
  '/sales-channels/branches',
  '/sales-channels/branches/123',
  '/sales-channels/contacts',
  '/sales-channels/routing',
  '/operations-center',
  '/operations-center/branches/123',
  '/integrations',
  '/analytics',
  '/settings',
  '/settings/security',
  '/ai-sales-logs',
  '/store-integration',
  '/whatsapp-connect',
  '/wa-usage',
  '/delivery-quality',
  '/handoff-queue',
  '/system-status',
  '/billing',
  '/widgets',
  '/help/whatsapp-manual-setup',
] as const

const missing = MERCHANT_PATHS.filter(path => !resolvePageMetaSelector(path))
if (missing.length > 0) {
  console.error(`Missing page metadata: ${missing.join(', ')}`)
  process.exit(1)
}

if (resolvePageMetaSelector('/not-a-dashboard-route')) {
  console.error('Unknown routes must not resolve to unrelated page metadata')
  process.exit(1)
}

const layoutSource = readFileSync(
  new URL('../src/components/layout/Layout.tsx', import.meta.url),
  'utf8',
)
if (!layoutSource.includes('resolvePageMetaSelector(pathname)')) {
  console.error('Layout must delegate page headers to the metadata resolver')
  process.exit(1)
}

console.log(`[page-metadata] OK — ${MERCHANT_PATHS.length} merchant paths covered.`)
