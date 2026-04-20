import { apiCall } from './client'

// ── Types ─────────────────────────────────────────────────────────────────────

export interface SyncStatus {
  has_snapshot: boolean
  // Live counts — always reflect what's actually in the DB right now,
  // matching what /orders, /coupons and /products return. Use these in UI.
  product_count: number
  category_count: number
  order_count: number
  coupon_count: number
  coupon_total?: number
  customer_count: number
  // Last-sync deltas (created+updated during the most recent run). Useful
  // for diagnostics / "your last sync upserted N rows", but NEVER for
  // headline counters — they go to 0 whenever a sync happens to fetch
  // nothing (token expired, empty incremental window, rate-limit, ...).
  snapshot_product_count?: number
  snapshot_order_count?: number
  snapshot_coupon_count?: number
  snapshot_category_count?: number
  last_full_sync_at: string | null
  last_incremental_sync_at: string | null
  sync_version: number
  sync_running: boolean
  last_job_status: string | null
  last_job_id: number | null
  last_job_error: string | null
}

export interface KnowledgeOverview {
  ready: boolean
  message?: string
  store_name?: string
  store_url?: string
  product_count: number
  category_count: number
  categories?: string[]
  order_count: number
  coupon_count: number
  coupon_total?: number
  active_coupons?: Array<{ code: string; description: string; discount_value: string }>
  last_full_sync: string | null
  last_inc_sync: string | null
  sync_version?: number
}

// ── API ───────────────────────────────────────────────────────────────────────

export const storeSyncApi = {
  trigger: () =>
    apiCall<{ status: string; job_id: number; message: string }>('/store-sync/trigger', {
      method: 'POST',
    }),

  getStatus: () =>
    apiCall<SyncStatus>('/store-sync/status'),

  getKnowledge: () =>
    apiCall<KnowledgeOverview>('/store-sync/knowledge'),
}
