// ── Promotions API client ─────────────────────────────────────────────────────
// Promotions are *automatic* discount rules (no code required) — the
// merchant-facing equivalent of Shopify's "Automatic discounts" or
// Magento's "Cart Price Rules". A Promotion stores the *terms* of an
// offer; when an automation fires, the backend issues a personal coupon
// from the promotion so the same flow works across all store backends.
//
// This module is intentionally separate from `coupons.ts` (which manages
// discrete coupon codes) and `automations.ts` (which manages campaigns
// that *use* promotions or coupons).

import { apiCall } from './client'

export type PromotionType =
  | 'percentage'
  | 'fixed'
  | 'free_shipping'
  | 'threshold_discount'
  | 'buy_x_get_y'

export type PromotionStatus =
  | 'draft'
  | 'scheduled'
  | 'active'
  | 'paused'
  | 'expired'

export interface PromotionConditions {
  min_order_amount?: number | null
  customer_segments?: string[]
  applicable_products?: string[]
  applicable_categories?: number[]
  x_quantity?: number
  y_quantity?: number
  x_product_ids?: string[]
  y_product_ids?: string[]
  // Allow forward-compat extra keys without breaking the typing
  [key: string]: unknown
}

export interface Promotion {
  id: number
  name: string
  description: string | null
  promotion_type: PromotionType
  discount_value: number | null
  conditions: PromotionConditions
  starts_at: string | null
  ends_at: string | null
  status: PromotionStatus
  effective_status: PromotionStatus
  is_live: boolean
  usage_count: number
  usage_limit: number | null
  extra_metadata: Record<string, unknown>
  created_at: string | null
  updated_at: string | null
}

export interface PromotionsList {
  promotions: Promotion[]
}

export interface PromotionsSummary {
  total: number
  active: number
  scheduled: number
  paused: number
  draft: number
  expired: number
  by_type: Record<string, number>
  codes_materialised: number
}

export interface PromotionCreatePayload {
  name: string
  description?: string | null
  promotion_type: PromotionType
  discount_value?: number | null
  conditions?: PromotionConditions
  starts_at?: string | null
  ends_at?: string | null
  status?: PromotionStatus
  usage_limit?: number | null
  extra_metadata?: Record<string, unknown>
}

export type PromotionPatchPayload = Partial<PromotionCreatePayload>

export const promotionsApi = {
  list: (filter?: { status?: PromotionStatus; promotion_type?: PromotionType }) => {
    const qs = new URLSearchParams()
    if (filter?.status) qs.set('status', filter.status)
    if (filter?.promotion_type) qs.set('promotion_type', filter.promotion_type)
    const tail = qs.toString() ? `?${qs.toString()}` : ''
    return apiCall<PromotionsList>(`/promotions${tail}`)
  },

  summary: () => apiCall<PromotionsSummary>('/promotions/summary'),

  get: (id: number) => apiCall<Promotion>(`/promotions/${id}`),

  create: (body: PromotionCreatePayload) =>
    apiCall<Promotion>('/promotions', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  update: (id: number, patch: PromotionPatchPayload) =>
    apiCall<Promotion>(`/promotions/${id}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  remove: (id: number) =>
    apiCall<void>(`/promotions/${id}`, { method: 'DELETE' }),

  activate: (id: number) =>
    apiCall<Promotion>(`/promotions/${id}/activate`, { method: 'POST' }),

  pause: (id: number) =>
    apiCall<Promotion>(`/promotions/${id}/pause`, { method: 'POST' }),
}

// ── Display metadata (Arabic-first) ──────────────────────────────────────────

export const PROMOTION_TYPE_META: Record<
  PromotionType,
  { label: string; icon: string; description: string; needsValue: boolean }
> = {
  percentage: {
    label:       'خصم نسبة مئوية',
    icon:        '%',
    description: 'خصم بنسبة مئوية على إجمالي السلة.',
    needsValue:  true,
  },
  fixed: {
    label:       'خصم مبلغ ثابت',
    icon:        'ر.س',
    description: 'خصم بمبلغ محدد بالريال على إجمالي السلة.',
    needsValue:  true,
  },
  free_shipping: {
    label:       'شحن مجاني',
    icon:        '🚚',
    description: 'إلغاء رسوم الشحن عند تحقّق الشروط.',
    needsValue:  false,
  },
  threshold_discount: {
    label:       'خصم عند تجاوز مبلغ',
    icon:        '🎯',
    description: 'يُطبَّق الخصم تلقائياً عند تجاوز السلة مبلغاً معيّناً.',
    needsValue:  true,
  },
  buy_x_get_y: {
    label:       'اشترِ X واحصل على Y',
    icon:        '🎁',
    description: 'اشترِ كمية معيّنة من منتج وتحصل على آخر مجاناً.',
    needsValue:  false,
  },
}

export const PROMOTION_STATUS_META: Record<
  PromotionStatus,
  { label: string; variant: 'green' | 'amber' | 'slate' | 'red' | 'blue' }
> = {
  active:    { label: 'نشط',     variant: 'green' },
  scheduled: { label: 'مجدول',   variant: 'blue'  },
  paused:    { label: 'متوقف',   variant: 'amber' },
  draft:     { label: 'مسودة',   variant: 'slate' },
  expired:   { label: 'منتهي',   variant: 'red'   },
}
