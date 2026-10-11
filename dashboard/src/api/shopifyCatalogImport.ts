// ── Shopify catalog import (dormant) ──────────────────────────────────────────
// Thin wrappers over the authenticated apiCall. Responses are returned raw and
// normalized by lib/shopifyConnection/catalogImport.ts; errors are projected to
// fixed codes there — never rendered from Error.message or provider detail.
import { apiCall } from './client'

const BASE = '/merchant/integrations/shopify/catalog-import'

export const shopifyCatalogImportApi = {
  status: (signal: AbortSignal) => apiCall<unknown>(`${BASE}/status`, { signal }),

  start: (shop: string, signal: AbortSignal) =>
    apiCall<unknown>(`${BASE}/start`, { method: 'POST', body: JSON.stringify({ shop }), signal }),
}
