// ── Shopify connection (dormant foundation) ───────────────────────────────────
// Thin wrappers over the authenticated apiCall. Responses are returned raw and
// normalized by lib/shopifyConnection/model.ts; errors are projected to fixed
// codes there — never rendered from Error.message or provider detail.
import { apiCall } from './client'

const BASE = '/merchant/integrations/shopify'
// Completion makes up to two bounded Shopify calls (2 x 20 s) behind a 60 s lease.
const COMPLETE_TIMEOUT_MS = 60_000

export const shopifyConnectionApi = {
  status: (signal: AbortSignal) => apiCall<unknown>(`${BASE}/status`, { signal }),

  start: (shop: string, signal: AbortSignal) =>
    apiCall<unknown>(`${BASE}/start`, { method: 'POST', body: JSON.stringify({ shop }), signal }),

  complete: (handle: string, signal: AbortSignal) =>
    apiCall<unknown>(`${BASE}/complete`, {
      method: 'POST',
      body: JSON.stringify({ handle }),
      signal,
      timeoutMs: COMPLETE_TIMEOUT_MS,
    }),

  disconnect: (shop: string, signal: AbortSignal) =>
    apiCall<unknown>(`${BASE}/disconnect`, { method: 'POST', body: JSON.stringify({ shop }), signal }),
}
